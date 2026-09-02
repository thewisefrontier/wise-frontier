"""
frontier_markets_writer.py
----------------------------
주요국 증시(미국·일본·독일·프랑스·영국·유로존) + 프론티어 마켓 통화 12개·
증시 지수 7개(ETF 대리지표 2개 포함)의 일일 변동을 추적해 "글로벌 마켓 동향"
기사를 자동 생성합니다(2026-08-21 신설 — "결국 콘텐츠 다양화만이 PV를 올릴
수 있다" 사용자 판단). 처음엔 "프론티어 마켓 동향"으로 좁게 시작했으나,
이스라엘 등 포함국이 "프론티어"라는 이름과 안 맞는다는 지적 + 주요국 증시도
같이 다뤄달라는 요청으로 같은 날 "글로벌 마켓 동향"으로 범위를 넓혔다.
검색 그라운딩(`use_search=True`)으로 실제 뉴스를 찾아 반영 — 초기 버전은
수치만 나열해 내용이 빈약하다는 지적을 받고 추가함.

데이터 소스: 야후 파이낸스 비공식 차트 API (키 불필요, 봇 검증 없음,
oil_price_writer.py에서 2026-08-21 검증된 패턴 재사용)
  https://query1.finance.yahoo.com/v8/finance/chart/{symbol}

⚠️ 파키스탄·케냐·가나·사우디·카자흐스탄 "증시"는 야후에 없다(거래소 자체가
커버리지 밖). Stooq엔 있지만 robots.txt가 Disallow: /(구글봇·빙봇 제외 전체
차단)라 접근하지 않는다 — oil_price_writer.py 2026-08-21 조치와 동일 판단.
해당 5개국 통화는 야후에 다 있어 통화 목록엔 포함돼 있다.

실행: python scripts/frontier_markets_writer.py
권장: 1일 1회
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

# 저장 시점 raw JSON 본문 차단. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from json_body_guard import unwrap_json_body
except Exception:
    def unwrap_json_body(text, _depth=0):
        return None

# articles 테이블 삽입 공용 로직(2026-09-02, article_store.py로 공용화).
try:
    from article_store import insert_final_article
except Exception:
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")  # oil_price_writer.py와 동일 키 재사용

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

try:
    from gemini_client import GeminiClient
except Exception:
    class GeminiClient:  # import 실패해도 본 기능이 죽지 않도록 폴백을 둔다
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            return None

_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)

# Pixabay 이미지 URL은 임시라 R2에 영구 저장한 뒤 그 URL을 쓴다.
try:
    from image_store import store_image
except Exception:
    def store_image(src_url, key_hint="", timeout=30):
        return src_url

KST = timezone(timedelta(hours=9))
EDT = ZoneInfo("America/New_York")  # 뉴욕 시장 기준

# 뉴욕 시장 종가 기준: 현지 17:00 이후에만 실행(oil_price_writer.py와 동일 패턴)
MARKET_CLOSE_HOUR_LOCAL = 17


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def now_edt() -> datetime:
    return datetime.now(timezone.utc).astimezone(EDT)


# 2026-08-28 실사고(id=108312): cron-job.org로 30분마다 호출되게 바뀌면서,
# 자정 KST가 넘자마자(00:01) 그날치 기사가 즉시 생성됐다. 이때 뉴욕은
# 아직 전날 장중(뉴욕 기준 오전)이라 실제로 존재하지도 않는 "종가"를
# 근거로 기사가 나갔고, 리드 문장엔 아직 오지도 않은 "29일(현지시간)"이
# 날짜로 박혔다(now_kst().date()를 그대로 썼기 때문). oil_price_writer.py의
# market_is_closed()/get_target_price_date() 패턴을 그대로 가져와, 뉴욕
# 종가가 실제로 존재하는 시점에만 실행하고 그 날짜를 정확히 쓴다.
def market_is_closed() -> bool:
    """뉴욕 현지 17:00 이후인지 확인 (주말은 금요일 종가 사용)."""
    now = now_edt()
    if now.weekday() >= 5:
        return True
    return now.hour >= MARKET_CLOSE_HOUR_LOCAL


def get_target_market_date() -> date:
    """수집 대상 날짜 결정. 뉴욕 17:00 이후 → 당일, 그 전 → 전 영업일."""
    now = now_edt()
    if now.weekday() >= 5:
        days_back = now.weekday() - 4
        return (now - timedelta(days=days_back)).date()
    if now.hour >= MARKET_CLOSE_HOUR_LOCAL:
        return now.date()
    prev = now - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev.date()


# Supabase 헤더/URL 헬퍼는 article_store.py로 공용화(2026-09-02).
try:
    from article_store import sb_headers as _sb_headers
    from article_store import sb_url as _sb_articles_url
except Exception:
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_articles_url():
        return f"{SUPABASE_URL}/rest/v1/articles"


def call_gemini(prompt: str, max_tokens: int = 2500, start_tier: int = 0, use_search: bool = False,
                 max_stages: int | None = None) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.3, timeout=(10, 45), use_search=use_search,
                                max_stages=max_stages)


# ── 심볼 정의 (2026-08-21 야후 파이낸스 실측 검증) ──────────────
# 통화: USD 기준. 프론티어 마켓 12개, 전부 야후에서 직접 조회 가능.
CURRENCIES = [
    ("나이지리아", "나이라", "USDNGN=X"),
    ("카자흐스탄", "텡게", "USDKZT=X"),
    ("베트남", "동", "USDVND=X"),
    ("이집트", "파운드", "USDEGP=X"),
    ("파키스탄", "루피", "USDPKR=X"),
    ("케냐", "실링", "USDKES=X"),
    ("가나", "세디", "USDGHS=X"),
    ("아르헨티나", "페소", "USDARS=X"),
    ("튀르키예", "리라", "USDTRY=X"),
    ("인도네시아", "루피아", "USDIDR=X"),
    ("방글라데시", "타카", "USDBDT=X"),
    ("우즈베키스탄", "숨", "USDUZS=X"),
]

# 주요국 증시: 뉴욕·유럽·일본 등 대형 시장(2026-08-21 추가, 사용자 요청 —
# "프론티어 마켓 동향"이라는 이름과 이스라엘 등 포함이 안 맞아 "글로벌 마켓
# 동향"으로 개편하며 주요국도 함께 다루기로 함). 거래소명을 병기해 본문에서
# "뉴욕증시(NYSE)에서…" 식으로 쓸 수 있게 한다(사용자 요청 — 지수명만 말고
# 거래소도 언급).
MAJOR_INDICES = [
    ("미국", "S&P500", "^GSPC", "뉴욕증시"),
    ("미국", "나스닥종합", "^IXIC", "뉴욕증시"),
    ("미국", "다우존스", "^DJI", "뉴욕증시"),
    ("일본", "닛케이225", "^N225", "도쿄증권거래소(TSE)"),
    ("독일", "DAX", "^GDAXI", "프랑크푸르트증권거래소"),
    ("프랑스", "CAC40", "^FCHI", "유로넥스트 파리"),
    ("영국", "FTSE100", "^FTSE", "런던증권거래소(LSE)"),
    ("유로존", "유로스톡스50", "^STOXX50E", "범유럽 지수(특정 거래소 없음)"),
]

# 프론티어 마켓 증시: 야후 직접 커버 국가만. 베트남·나이지리아는 직접
# 지수가 없어 ETF 대리지표로 대체(거래소 표기 없음). 파키스탄·케냐·가나·
# 사우디·카자흐스탄은 야후에 지수 자체가 없어 제외(위 통화 목록엔 포함돼 있음).
FRONTIER_INDICES = [
    ("남아공", "Top40", "^JN0U.JO", "요하네스버그증권거래소(JSE)"),
    ("말레이시아", "KLCI", "^KLSE", "말레이시아거래소(Bursa Malaysia)"),
    ("인도네시아", "종합지수", "^JKSE", "인도네시아증권거래소(IDX)"),
    ("이집트", "EGX30", "^CASE30", "이집트증권거래소(EGX)"),
    ("이스라엘", "TA-125", "^TA125.TA", "텔아비브증권거래소(TASE)"),
    ("베트남", "VanEck 베트남 ETF", "VNM", ""),
    ("나이지리아", "MSCI 나이지리아 ETF", "NGE", ""),
]


# 2026-08-30 사용자 결정: 소스 하나만 믿다가 방향이 뒤집힌 사고(id=109658)를
# 겪은 뒤, "공인된 단일 소스가 아닌 이상 여러 곳에서 확인하자"는 방침.
# ① 야후 응답 자체 필드 간 정합성(regularMarketChangePercent vs
#    chartPreviousClose 유도값)을 항상 대조 — 공짜라 전량 실행.
# ② 주요국 증시(추적 ETF가 있는 8개)만 불일치 시에만 Alpha Vantage로
#    2차 확인(무료 쿼터가 작아서 — oil_price_writer.py와 공유 — 상시
#    호출은 안 하고 의심될 때만 호출).
# 프론티어 통화·증시는 대체 무료 소스가 사실상 없어 ②를 적용 못 하므로,
# 불일치 시 그 항목은 지어내지 말고 그냥 기사에서 뺀다(①만 적용).
MAJOR_INDEX_AV_PROXY = {
    "^GSPC": "SPY", "^IXIC": "QQQ", "^DJI": "DIA",
    "^N225": "EWJ", "^GDAXI": "EWG", "^FCHI": "EWQ",
    "^FTSE": "EWU", "^STOXX50E": "FEZ",
}


def _pct_disagree(pct_a: float, pct_b: float) -> bool:
    """두 등락률이 서로 모순되는지(부호가 다르거나 크게 벌어지는지) 판정.

    ⚠️ 실사고(2026-08-30) 첫 구현은 "±0.03% 이내는 노이즈로 무시"하는
    데드존을 뒀는데, 정작 오늘 사고 값(다우 -0.018%)이 그 데드존 안에
    들어가 부호 비교 자체가 스킵되며 잡아내지 못했다. 부호는 곱해서
    직접 비교하고, 둘 다 사실상 보합(0.01% 미만)인 경우에만 예외로 둔다."""
    if abs(pct_a) < 0.01 and abs(pct_b) < 0.01:
        return False
    if pct_a * pct_b < 0:
        return True
    return abs(pct_a - pct_b) > max(2.0, abs(pct_a) * 2)


def fetch_alphavantage_pct(av_symbol: str) -> float | None:
    """Alpha Vantage GLOBAL_QUOTE에서 전일 대비 등락률(%)만 가져온다.
    무료 쿼터가 작아 의심스러울 때만 호출하는 2차 확인용."""
    if not ALPHA_VANTAGE_API_KEY:
        return None
    try:
        res = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "GLOBAL_QUOTE", "symbol": av_symbol, "apikey": ALPHA_VANTAGE_API_KEY},
            timeout=(10, 30),
        )
        if res.status_code != 200:
            return None
        quote = res.json().get("Global Quote", {})
        raw = quote.get("10. change percent", "")
        if not raw:
            return None
        return float(raw.strip().rstrip("%"))
    except Exception as e:
        print(f"  [WARN] Alpha Vantage 2차 확인 실패 ({av_symbol}): {e}")
        return None


def fetch_yahoo_quote(symbol: str) -> dict | None:
    """야후 파이낸스 비공식 차트 API에서 최신가·전일 종가를 가져온다.
    키 불필요, 봇 검증 없음(oil_price_writer.py 2026-08-21 검증 패턴)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    try:
        res = requests.get(url, timeout=(10, 30), headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200:
            print(f"  [WARN] 야후 조회 실패 ({symbol}): HTTP {res.status_code}")
            return None
        results = res.json().get("chart", {}).get("result") or []
        if not results:
            print(f"  [WARN] 야후 조회 실패 ({symbol}): 결과 없음")
            return None
        meta = results[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        # 실사고(2026-08-29, id=109658): chartPreviousClose는 "전일 종가"가
        # 아니라 range=5d 요청의 시작점(5거래일 전) 기준값이라, 이걸로 등락률을
        # 계산하면 방향 자체가 뒤집힐 수 있다(다우 실제 -0.02% 하락인데
        # chartPreviousClose 기준으론 +0.53% 상승으로 나옴 — 종가 수치 자체는
        # 정확했는데 비교 기준일이 틀렸던 것). meta.regularMarketChangePercent는
        # 야후가 전일 대비로 직접 계산해 제공하는 필드라 실제 발표 등락률과
        # 정확히 일치함(S&P -0.25%, 다우 -0.02% 등 실측 확인) — 이걸 우선
        # 쓰고, 없을 때만 chartPreviousClose로 폴백한다.
        pct = meta.get("regularMarketChangePercent")
        chart_prev = meta.get("chartPreviousClose")
        if price is None:
            print(f"  [WARN] 야후 조회 실패 ({symbol}): 필드 누락")
            return None
        suspect = False
        if pct is not None:
            prev = price / (1 + pct / 100) if pct != -100 else None
            change = price - prev if prev else 0.0
            # 2026-08-30: regularMarketChangePercent(주 소스)와
            # chartPreviousClose 유도값(구 소스)이 서로 모순되면 의심 표시.
            if chart_prev:
                alt_pct = (price - chart_prev) / chart_prev * 100
                suspect = _pct_disagree(pct, alt_pct)
        else:
            prev = chart_prev
            if prev is None or not prev:
                print(f"  [WARN] 야후 조회 실패 ({symbol}): 필드 누락")
                return None
            change = price - prev
            pct = (change / prev * 100) if prev else 0.0
        return {"price": float(price), "prev": float(prev) if prev else None,
                "change": change, "pct": pct, "_suspect": suspect}
    except Exception as e:
        print(f"  [WARN] 야후 조회 실패 ({symbol}): {e}")
        return None


def fetch_all_data() -> dict:
    """통화·주요국 증시·프론티어 증시 전체를 조회해 각각 변동률 큰 순으로 정렬해 반환.

    2026-08-30: 자체 정합성 검사(_suspect)에 걸린 항목은 지어내는 것보다
    빼는 게 낫다는 원칙(사용자 확정)에 따라 처리한다.
    - 통화·프론티어 증시: 대체 무료 소스가 사실상 없어 의심되면 그냥 제외.
    - 주요국 증시: 추적 ETF가 있는 경우에만 Alpha Vantage로 2차 확인 —
      방향이 일치하면 살리고, 여전히 불일치하거나 확인 자체가 안 되면 제외.
    """
    currencies = []
    for country, name, symbol in CURRENCIES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if not q:
            continue
        if q.pop("_suspect", False):
            print(f"  ⚠️ [정합성 불일치 → 제외] {country} {name}({symbol})")
            continue
        currencies.append({"country": country, "name": name, "exchange": "", **q})

    major_indices = []
    for country, name, symbol, exchange in MAJOR_INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if not q:
            continue
        if q.pop("_suspect", False):
            av_symbol = MAJOR_INDEX_AV_PROXY.get(symbol)
            av_pct = fetch_alphavantage_pct(av_symbol) if av_symbol else None
            if av_pct is not None and not _pct_disagree(q["pct"], av_pct):
                print(f"  ✓ [Alpha Vantage 2차 확인 일치] {country} {name}: 야후 {q['pct']:+.2f}% / AV({av_symbol}) {av_pct:+.2f}%")
            else:
                print(f"  ⚠️ [정합성 불일치, 2차 확인도 실패/불일치 → 제외] {country} {name}({symbol})")
                continue
        major_indices.append({"country": country, "name": name, "exchange": exchange, **q})

    frontier_indices = []
    for country, name, symbol, exchange in FRONTIER_INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if not q:
            continue
        if q.pop("_suspect", False):
            print(f"  ⚠️ [정합성 불일치 → 제외] {country} {name}({symbol})")
            continue
        frontier_indices.append({"country": country, "name": name, "exchange": exchange, **q})

    currencies.sort(key=lambda x: abs(x["pct"]), reverse=True)
    major_indices.sort(key=lambda x: abs(x["pct"]), reverse=True)
    frontier_indices.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return {"currencies": currencies, "major_indices": major_indices, "frontier_indices": frontier_indices}


# ── 위키피디아 고유명사 검증 (다른 writer 스크립트와 동일 로직) ──────
def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    titles = []
    for lang in ("ko", "en"):
        try:
            res = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": name, "limit": 3, "namespace": 0, "format": "json"},
                headers={"User-Agent": "NewsFinal-EntityCheck/1.0 (+https://newsfinal.co.kr)"},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if len(data) >= 2 and isinstance(data[1], list):
                    titles.extend(data[1])
        except Exception:
            continue
    return any(fuzz.token_sort_ratio(name, t) >= threshold for t in titles)


def extract_candidate_names(body: str) -> list:
    if not body:
        return []
    prompt = f"""아래 기사 본문에서 실제 존재 여부를 확인해볼 만한 구체적 고유명사를 추출하세요.
특정 기관·단체·기업명, 지수명만 대상으로 합니다. 국가명·통화명 등 일반적인
경제 용어는 제외하세요. 쉼표로 구분해 나열만 하세요(설명 금지).
대상이 없으면 "없음"이라고만 답하세요.

본문:
{body[:2000]}

답변:"""
    result = call_gemini(prompt, max_tokens=150, start_tier=3)
    if not result:
        return []
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return []
    return [n.strip() for n in result.split(",") if n.strip() and len(n.strip()) >= 2][:15]


def verify_no_fabricated_names(source_prompt: str, body: str) -> str:
    if not body:
        return ""
    # 이 스크립트는 검색 그라운딩(use_search=True)을 쓰므로 본문에 [원본 자료]
    # 시세 표에 없는 실제 뉴스(기관·기업·인물명)가 정당하게 등장할 수 있다.
    # "원본에 없으면 무조건 지어낸 것"으로 보면 검색으로 찾은 진짜 뉴스까지
    # 오탐 처리하므로, 시세 수치(가격·통화·지수)를 잘못 지어낸 경우만 잡는다.
    check_prompt = f"""아래는 기사 작성에 쓰인 시세 데이터와, 그걸 바탕으로 검색 결과까지
반영해 생성된 한국어 기사 본문입니다. 본문에 나오는 가격·환율·지수 수치가
시세 데이터에 실제로 근거하는지만 확인하세요(수치를 잘못 지어냈거나
데이터에 없는 국가·통화·지수의 수치를 만들어낸 경우). 검색으로 찾은
기관명·기업명·인물명 등 뉴스 맥락은 시세 데이터에 없어도 정상이니
문제 삼지 마세요. 수치 오류가 있으면 "지어낸수치 → 원본수치" 형식으로
쉼표 구분해 나열하세요. 없으면 "없음"이라고만 답하세요.

[시세 데이터]
{source_prompt[:3000]}

[생성된 기사 본문]
{body[:2000]}

답변:"""
    result = call_gemini(check_prompt, max_tokens=150, start_tier=3)
    suspect = ""
    if result:
        result = result.strip()
        if result and not ("없음" in result and len(result) <= 12):
            suspect = result

    unconfirmed = [n for n in extract_candidate_names(body) if not wikipedia_confirms(n)]
    if unconfirmed:
        note = "[위키 미확인] " + ", ".join(unconfirmed)
        suspect = (suspect + "\n" + note) if suspect else note

    return suspect


# ── 논평/칼럼체 검출 ─────────────────────────────────────────
BANNED_STYLE_PATTERNS = [
    r"보여줍니다", r"보여주고 있습니다", r"보여준다",
    r"주목됩니다", r"주목된다", r"주목받고 있습니다",
    r"필요해 보입니다", r"필요할 것으로 보입니다",
    r"지켜볼 필요가 있습니다", r"기대됩니다",
]


def has_column_style(text: str) -> bool:
    if not text:
        return False
    return any(re.search(p, text) for p in BANNED_STYLE_PATTERNS)


# ── 기사 프롬프트 ────────────────────────────────────────────
def build_article_prompt(data: dict) -> str:
    # ⚠️ now_kst()가 아니라 실제 뉴욕 종가가 존재하는 거래일을 써야 한다
    # (2026-08-28 실사고, id=108312 참조).
    today = get_target_market_date()
    today_str = today.strftime("%Y년 %m월 %d일")

    def _idx_line(i: dict) -> str:
        exch = f" ({i['exchange']})" if i.get("exchange") else ""
        return f"- {i['country']} {i['name']}{exch}: {i['price']:.2f} (전일比 {i['pct']:+.2f}%)"

    cur_lines = "\n".join(
        f"- {c['country']} {c['name']}: {c['price']:.2f} (전일比 {c['pct']:+.2f}%)"
        for c in data["currencies"]
    )
    major_idx_lines = "\n".join(_idx_line(i) for i in data["major_indices"])
    frontier_idx_lines = "\n".join(_idx_line(i) for i in data["frontier_indices"])

    top_cur = data["currencies"][:3]
    top_major = data["major_indices"][:3]
    top_frontier = data["frontier_indices"][:3]
    top_cur_str = ", ".join(f"{c['country']} {c['pct']:+.1f}%" for c in top_cur)
    top_major_str = ", ".join(f"{i['country']} {i['name']} {i['pct']:+.1f}%" for i in top_major)
    top_frontier_str = ", ".join(f"{i['country']} {i['pct']:+.1f}%" for i in top_frontier)

    return f"""당신은 글로벌 마켓 전문 경제 기자입니다. 구글 검색으로 오늘 시장을
움직인 실제 뉴스(연준 발언, 기업 실적, 지정학 이슈, 경제 지표 발표 등)를
찾아서, 아래 시세 데이터와 결합해 기사를 작성하세요. 검색 없이 수치만
나열하지 말고, 왜 그렇게 움직였는지 실제 근거를 찾아 반영하세요.

오늘({today_str}) 기준 시세 데이터(달러 대비 환율, 전일 대비 변동률):

[주요국 증시 — 지수 포인트, 거래소 병기]
{major_idx_lines}

[프론티어 마켓 통화 — 달러 대비]
{cur_lines}

[프론티어 마켓 증시 — 현지통화 기준, 거래소 병기]
{frontier_idx_lines}

[변동폭 상위]
- 주요국 증시: {top_major_str}
- 프론티어 통화: {top_cur_str}
- 프론티어 증시: {top_frontier_str}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "[글로벌 마켓 동향] "으로 시작. 대괄호 포함 그대로 출력.
- ⚠️ 뉴욕증시(다우·나스닥·S&P500)가 핵심 소재여야 합니다(2026-08-26 사용자 지적 —
  영국 FTSE가 메인으로 올라온 사고: "뉴욕증시가 메인으로 올라오는게 맞다"). 그날
  변동폭이 가장 크다는 이유로 다른 지역(영국·일본 등)을 제목 앞자리에 놓지 마세요.
  나머지는 뉴욕 다음 소재로 다뤄도 됩니다. 50자 이내로.
- 예: "[글로벌 마켓 동향] 뉴욕증시 혼조…英 FTSE는 1.5% 상승"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 절대 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '지켜볼 필요가 있습니다'
2. 구조 — 아래 4개 섹션을 이 순서대로 다루세요. 각 섹션은 "◆ 섹션명" 한 줄로
   시작하고(번호나 대괄호 없이 ◆ 기호 + 섹션명만, 예: "◆ 뉴욕증시"), 그 다음
   줄부터 내용을 쓰세요. 섹션 제목 줄 앞에는 빈 줄을 하나씩 두세요. 아래
   섹션명 뒤에 붙은 설명은 그 섹션에 뭘 써야 하는지 알려주는 지시일 뿐이니
   지시 내용 자체를 본문에 옮겨 적지 마세요 — "◆ 섹션명" 한 줄만 그대로 쓰고
   바로 이어서 지시를 따른 기사 내용을 쓰세요.

   ◆ 뉴욕증시
   ⚠️ 반드시 뉴욕증시(다우존스·나스닥·S&P500 중 그날 가장 중요한 움직임)로
   시작하세요. 다른 지역 지수가 그날 변동폭이 더 크더라도 이 섹션은 뉴욕이어야
   합니다 — 뉴욕이 이 기사의 기준 시장입니다. 검색으로 찾은 실제 뉴스 기반으로
   왜 그렇게 움직였는지 반영하고, 반드시 "뉴욕증시(NYSE)"와 "{today.day}일
   (현지시간)"을 함께 언급하세요(예: "뉴욕증시(NYSE)에서 S&P500 지수는 {today.day}일
   (현지시간)…"). 뉴욕 3대 지수가 방향이 엇갈리면(혼조) 그 사실 자체를 리드로
   쓰세요(예: "뉴욕증시가 혼조세를 보인 가운데…").

   ◆ 주요국 증시
   뉴욕 외 변동폭 상위 지수 2~3개(영국·일본·독일·프랑스 등)를, 그 지수가 움직인
   실제 이유(검색으로 찾은 뉴스 — 연준 금리 발표, 주요 기업 실적, 경제 지표 등)와
   함께 서술. 거래소명 포함. 뉴욕 지수 상세 수치가 ◆ 뉴욕증시 섹션에서 다 안
   다뤄졌으면 여기서 마저 다루세요.

   ◆ 프론티어 마켓 통화
   변동폭 상위 통화 3~4개를 구체적 수치와 함께 서술. 검색으로 해당국의 실제
   통화·경제 뉴스를 찾을 수 있으면 반영하고, 못 찾으면 달러 강세/약세 같은
   공통 요인으로 설명하되 지어내지 마세요.

   ◆ 프론티어 마켓 증시
   변동폭 상위 지수 2~3개를 거래소명과 함께 서술.
3. ⚠️ 날짜: 반드시 ◆ 뉴욕증시 섹션에 "{today.day}일(현지시간)" 형식으로 날짜를
   명시하세요(2026-08-26 실사고 — id=98019 등 최근 발행분에 날짜가 한 번도 안
   들어감). "오늘", "현재", 절대연도(2026년 등)는 금지. 본문 전체에 날짜가 한
   번도 안 나오면 안 됩니다.
4. 수치는 위 데이터를 그대로 사용하고 절대 지어내지 마세요. 검색으로 찾은 뉴스도
   실제 사실만 반영하고, 확인 안 되는 내용은 지어내지 마세요.
5. 비라틴 문자 국가명·통화명·지수명·기업명은 정확한 한국어 표기로.
6. 분량: 900자 이상(검색 근거로 실질적 분석을 담아 앞선 수치 나열형 버전보다 풍부하게).
"""


TITLE_PREFIX = "[글로벌 마켓 동향]"


def enforce_title_prefix(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return t
    m = re.match(r"^\s*\[\s*글로벌\s*마켓\s*동향\s*\]\s*(.*)$", t)
    if m:
        t = m.group(1).strip()
    else:
        m2 = re.match(r"^글로벌\s*마켓\s*동향(?:이|은)?\s*[,·]?\s+(.+)$", t)
        if m2:
            t = m2.group(1).strip()
    return f"{TITLE_PREFIX} {t}" if t else TITLE_PREFIX


def parse_article_output(text: str) -> tuple[str, str]:
    title, body = "", ""
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    if m_title:
        title = m_title.group(1).strip()
    m_body = re.search(r"BODY:\s*(.+)$", text, re.S)
    if m_body:
        body = m_body.group(1).strip()
    return title, body


def call_gemini_article(prompt: str, max_tokens: int = 3500) -> str | None:
    # 검색 그라운딩(use_search)은 구글 쪽 쿼터 오분류 버그로 모델을 바꿔가며
    # 재시도해도 안 풀릴 때가 있다(2026-08-22 실사고, 5키×5모델=25연속 429).
    # max_stages=1로 헛된 재시도를 줄이고, 그래도 막히면 검색 없이(전체
    # 캐스케이드) 폴백해 최소한 기사는 나가게 한다 — 실시간 뉴스 맥락은
    # 빠지지만 시세 수치만으로도 기사 자체는 유효하다.
    # 2026-08-29 사용자 지적: 플래그십(3.7-flash, start_tier=0)이 무료
    # RPD가 작아 5키가 한꺼번에 소진되는 일이 잦았다(cron-job.org로 호출
    # 빈도가 늘면서 더 심해짐) — oil_price_writer.py/opinet_price_writer.py와
    # 같이 RPD 여유가 큰 lite 티어(start_tier=3)부터 시작하도록 변경.
    text = call_gemini(prompt, max_tokens=max_tokens, use_search=True, max_stages=1, start_tier=3)
    if not text:
        print("  ⚠️ 검색 그라운딩 실패 → 검색 없이 재시도")
        text = call_gemini(prompt, max_tokens=max_tokens, use_search=False, start_tier=3)
    time.sleep(5)
    if not text:
        return None

    if has_column_style(text):
        print("  ⚠️ 논평체 감지 → 재생성")
        retried = call_gemini(
            prompt + "\n\n[재작성 지시] 논평/칼럼 문체가 섞였습니다. 사실 전달 중심으로만 다시 작성하세요.",
            max_tokens=max_tokens, use_search=True, max_stages=1,
        )
        if retried:
            text = retried
        time.sleep(5)

    _, body_probe = parse_article_output(text)
    fabricated = verify_no_fabricated_names(prompt, body_probe or text)
    if fabricated:
        print(f"  ⚠️ 수치 오류 의심({fabricated}) → 재생성")
        retried = call_gemini(
            prompt + f"\n\n[재작성 지시] 다음 수치를 시세 데이터와 다르게 잘못 썼습니다: {fabricated}. "
                     "가격·환율·지수 수치는 반드시 위 [시세 데이터]에 나온 값 그대로 쓰세요.",
            max_tokens=max_tokens, use_search=True, max_stages=1,
        )
        if retried:
            text = retried
        time.sleep(5)

    return text


# ── 이미지 ───────────────────────────────────────────────────
_MARKET_IMAGE_KEYWORDS = [
    "stock market chart", "currency exchange", "financial district",
    "trading floor", "world map finance", "banknotes currency",
]


def fetch_market_image(seed_date: date) -> str:
    """로직은 article_image.py로 공용화(2026-09-02, 4개 writer 스크립트에
    복붙돼 있었음)."""
    from article_image import fetch_seeded_pixabay_image
    return fetch_seeded_pixabay_image(
        _MARKET_IMAGE_KEYWORDS, seed_date.toordinal(), f"frontier_markets_{seed_date.isoformat()}"
    )


# ── 중복 체크 & 저장 ──────────────────────────────────────────
def already_published(article_date: date) -> bool:
    internal_url = f"internal://frontier_markets_{article_date.isoformat()}"
    res = requests.get(
        f"{_sb_articles_url()}?url=eq.{internal_url}&is_published=eq.true&select=id",
        headers=_sb_headers(),
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(title_ko: str, summary_ko: str, data: dict, article_date: date, image_url: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    _unwrapped = unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            summary_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 저장 차단: {title_ko[:60]}")
            return -1

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    internal_url = f"internal://frontier_markets_{article_date.isoformat()}"

    top_countries = (
        [i["country"] for i in data["major_indices"][:2]]
        + [c["country"] for c in data["currencies"][:2]]
        + [i["country"] for i in data["frontier_indices"][:2]]
    )
    countries = list(dict.fromkeys(top_countries))  # 순서 유지 중복 제거

    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "경제",
        "subcategory": "글로벌마켓동향",
        "region": "global",
        "country": "",
        "country_flag": "🌍",
        "countries": countries,
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "글로벌 마켓 동향 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
        # 2026-08-30: 데스킹 단계에서 본문 수치와 대조하기 위해 실제로 가져온
        # 원본 시세를 그대로 저장(Gemini가 글로 옮기며 숫자를 잘못 쓰는
        # 사고를 잡는 용도 — 소스 데이터 자체 오류는 fetch_all_data()의
        # 다중검증이 이미 막음).
        "source_data": data,
    }

    return insert_final_article(payload)


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[frontier_markets_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not GEMINI_API_KEYS:
        print("  [SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    if not market_is_closed():
        edt_now = now_edt()
        print(f"  → 뉴욕 시장 미종료 ({edt_now.strftime('%H:%M')} EDT) → 스킵")
        return

    article_date = get_target_market_date()
    if already_published(article_date):
        print(f"  → {article_date} 글로벌 마켓 동향 기사 이미 존재 → 스킵")
        return

    print("  → 야후 파이낸스에서 시세 데이터 수집 중...")
    data = fetch_all_data()
    if len(data["currencies"]) < 3 or len(data["major_indices"]) < 2 or len(data["frontier_indices"]) < 2:
        print(f"  [ERROR] 데이터 수집 부족(통화 {len(data['currencies'])}건, "
              f"주요국 증시 {len(data['major_indices'])}건, "
              f"프론티어 증시 {len(data['frontier_indices'])}건) → 종료")
        return
    print(f"  → 통화 {len(data['currencies'])}건, 주요국 증시 {len(data['major_indices'])}건, "
          f"프론티어 증시 {len(data['frontier_indices'])}건 수집 완료")

    print("  → Gemini로 기사 생성 중...")
    prompt = build_article_prompt(data)
    article_text = call_gemini_article(prompt)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    title, body = parse_article_output(article_text)
    title = enforce_title_prefix(title)

    if not title or not body:
        print("  [ERROR] 응답 파싱 실패")
        return

    image_url = fetch_market_image(article_date)

    article_id = insert_article(title, body, data, article_date, image_url)
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[frontier_markets_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
