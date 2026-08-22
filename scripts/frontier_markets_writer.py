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


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


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
        prev = meta.get("chartPreviousClose")
        if price is None or prev is None or not prev:
            print(f"  [WARN] 야후 조회 실패 ({symbol}): 필드 누락")
            return None
        change = price - prev
        pct = (change / prev * 100) if prev else 0.0
        return {"price": float(price), "prev": float(prev), "change": change, "pct": pct}
    except Exception as e:
        print(f"  [WARN] 야후 조회 실패 ({symbol}): {e}")
        return None


def fetch_all_data() -> dict:
    """통화·주요국 증시·프론티어 증시 전체를 조회해 각각 변동률 큰 순으로 정렬해 반환."""
    currencies = []
    for country, name, symbol in CURRENCIES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if q:
            currencies.append({"country": country, "name": name, "exchange": "", **q})

    major_indices = []
    for country, name, symbol, exchange in MAJOR_INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if q:
            major_indices.append({"country": country, "name": name, "exchange": exchange, **q})

    frontier_indices = []
    for country, name, symbol, exchange in FRONTIER_INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if q:
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
    today = now_kst()
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
- 오늘 가장 두드러진 이슈(주요국 증시든 프론티어 통화·증시든) 하나를 담아 50자 이내로.
- 예: "[글로벌 마켓 동향] 뉴욕증시 사흘째 상승…나이지리아 나이라는 약세"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 절대 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '지켜볼 필요가 있습니다'
2. 구조 — 아래 소제목을 본문에 그대로 포함하고 소제목 앞에는 빈 줄을 하나씩 두세요:
   ① 리드: 오늘 시장을 움직인 가장 중요한 이슈를 한 문장으로 요약(검색으로 찾은 실제
      뉴스 기반. 반드시 거래소명을 언급, 예: "뉴욕증시(NYSE)에서 S&P500 지수는…")
   ② [주요국 증시] 변동폭 상위 지수 2~3개를, 그 지수가 움직인 실제 이유(검색으로 찾은
      뉴스 — 연준 금리 발표, 주요 기업 실적, 경제 지표 등)와 함께 서술. 거래소명 포함.
   ③ [프론티어 마켓 통화] 변동폭 상위 통화 3~4개를 구체적 수치와 함께 서술. 검색으로
      해당국의 실제 통화·경제 뉴스를 찾을 수 있으면 반영하고, 못 찾으면 달러 강세/약세
      같은 공통 요인으로 설명하되 지어내지 마세요.
   ④ [프론티어 마켓 증시] 변동폭 상위 지수 2~3개를 거래소명과 함께 서술.
3. 날짜는 "{today.day}일" 형식으로만 표기.
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
    text = call_gemini(prompt, max_tokens=max_tokens, use_search=True, max_stages=1)
    if not text:
        print("  ⚠️ 검색 그라운딩 실패 → 검색 없이 재시도")
        text = call_gemini(prompt, max_tokens=max_tokens, use_search=False)
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
    if not PIXABAY_API_KEY:
        return ""
    seed = seed_date.toordinal()
    query = _MARKET_IMAGE_KEYWORDS[seed % len(_MARKET_IMAGE_KEYWORDS)]
    try:
        res = requests.get(
            "https://pixabay.com/api/",
            params={"key": PIXABAY_API_KEY, "q": query, "image_type": "photo",
                    "safesearch": "true", "per_page": 10},
            timeout=15,
        )
        if res.status_code != 200:
            return ""
        hits = res.json().get("hits", [])
        if not hits:
            return ""
        hit = hits[seed % len(hits)]
        raw_url = hit.get("largeImageURL", "")
        if not raw_url:
            return ""
        url = store_image(raw_url, key_hint=f"frontier_markets_{seed_date.isoformat()}")
        print(f"  🖼️ 이미지: {query} → {url[:70]}")
        return url or ""
    except Exception as e:
        print(f"  ⚠️ Pixabay 실패: {e}")
    return ""


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
    }

    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        result = res.json()
        return result[0].get("id", -1) if result else -1
    print(f"  [ERROR] 기사 삽입 실패 {res.status_code}: {res.text[:200]}")
    return -1


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[frontier_markets_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not GEMINI_API_KEYS:
        print("  [SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    article_date = now_kst().date()
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
