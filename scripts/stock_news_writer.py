"""
scripts/stock_news_writer.py
-------------------------------
개별 종목(미국 대형주) 동향 기사 자동 생성(2026-09-03 신설 — "뉴스파이널에
지금 주식 관련 기사가 그다지 없다" 사용자 지적).

frontier_markets_writer.py(지수·통화 전체를 다루는 "글로벌 마켓 동향")와
달리, 하루 한 종목을 골라 그 종목 하나에 집중한 기사를 쓴다. 데이터·검증·
번역 로직은 frontier_markets_writer.py를 그대로 재사용한다(fetch_yahoo_quote,
wikipedia_confirms, extract_candidate_names, verify_no_fabricated_names,
has_column_style, translate_article — 같은 야후 조회·팩트체크·번역 로직을
또 복붙하지 않는다).

종목 선택: API 키 없이 시작하기 위해 고정 워치리스트를 날짜 기반으로
순환 선택한다(article_image.py의 date-seeded 패턴과 동일한 방식). Finnhub/
FMP의 "오늘 실적 발표"·"오늘 급등락" 같은 이벤트 기반 선택은 API 한도
문제도 있고(사용자 지적 — "머니파이널에서 받아올 수 있으면 그 자료를
이용하는게 좋겠다") 머니파이널 쪽에 유사한 export가 생기면 그때 연결한다.

실행: python scripts/stock_news_writer.py
권장: 뉴욕 종가(17:00 EDT) 이후 1일 1회 — frontier_markets_writer.py와
동일한 게이트(market_is_closed/get_target_market_date)를 그대로 쓴다.
"""

import os
import re
import time
import requests
from datetime import date
from dotenv import load_dotenv

load_dotenv()

try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

try:
    from json_body_guard import unwrap_json_body
except Exception:
    def unwrap_json_body(text, _depth=0):
        return None

try:
    from style_guard import has_column_style
except Exception:
    def has_column_style(text: str) -> bool:
        return False

try:
    from article_store import insert_final_article, sb_headers as _sb_headers, sb_url as _sb_url
except Exception:
    SUPABASE_URL_FALLBACK = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_SERVICE_KEY_FALLBACK = os.getenv("SUPABASE_SERVICE_KEY", "")
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY_FALLBACK,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY_FALLBACK}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_url(table: str = "articles"):
        return f"{SUPABASE_URL_FALLBACK}/rest/v1/{table}"
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

try:
    from translate_guard import translate_article
except Exception:
    def translate_article(title_ko: str, body_ko: str, call_gemini_fn, max_tokens: int = 3500) -> tuple[str, str]:
        return "", ""

# 야후 조회·팩트체크 로직은 frontier_markets_writer.py 재사용(2026-09-03).
from frontier_markets_writer import (
    fetch_yahoo_quote, fetch_alphavantage_pct, _pct_disagree,
    wikipedia_confirms, extract_candidate_names, verify_no_fabricated_names,
    now_kst, now_edt, market_is_closed, get_target_market_date,
)

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
    class GeminiClient:
        def __init__(self, *a, **k):
            pass
        def call(self, *a, **k):
            return None

_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)


def call_gemini(prompt: str, max_tokens: int = 2500, start_tier: int = 3, use_search: bool = False,
                 max_stages: int | None = None) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.3, timeout=(10, 45), use_search=use_search,
                                max_stages=max_stages)


# ── 워치리스트 (미국 대형주, 2026-09-03) ──────────────────────
# Finnhub/FMP 없이 시작하기 위한 고정 목록 — 날짜 기반으로 하루 한 종목만
# 순환 선택한다. AV_SYMBOL은 Alpha Vantage 2차 확인용 심볼(개별 주식은
# 티커 그대로 동일해 ETF 대리지표가 따로 필요 없음).
WATCHLIST = [
    ("AAPL", "애플", "나스닥"),
    ("MSFT", "마이크로소프트", "나스닥"),
    ("NVDA", "엔비디아", "나스닥"),
    ("GOOGL", "알파벳(구글)", "나스닥"),
    ("AMZN", "아마존", "나스닥"),
    ("META", "메타", "나스닥"),
    ("TSLA", "테슬라", "나스닥"),
    ("BRK-B", "버크셔 해서웨이", "뉴욕증권거래소"),
    ("JPM", "JP모건체이스", "뉴욕증권거래소"),
    ("V", "비자", "뉴욕증권거래소"),
    ("NFLX", "넷플릭스", "나스닥"),
    ("AMD", "AMD", "나스닥"),
]


def pick_ticker(article_date: date) -> tuple[str, str, str]:
    idx = article_date.toordinal() % len(WATCHLIST)
    return WATCHLIST[idx]


def fetch_stock_data(symbol: str) -> dict | None:
    """야후에서 시세를 가져오고, 정합성 의심 시 Alpha Vantage로 2차 확인.
    frontier_markets_writer.py의 주요국 증시 처리와 동일한 검증 순서."""
    q = fetch_yahoo_quote(symbol)
    if not q:
        return None
    if q.pop("_suspect", False):
        av_pct = fetch_alphavantage_pct(symbol)
        if av_pct is not None and not _pct_disagree(q["pct"], av_pct):
            print(f"  ✓ [Alpha Vantage 2차 확인 일치] {symbol}: 야후 {q['pct']:+.2f}% / AV {av_pct:+.2f}%")
        else:
            print(f"  ⚠️ [정합성 불일치, 2차 확인도 실패/불일치] {symbol}")
            return None
    return q


# ── 기사 프롬프트 ────────────────────────────────────────────
def build_article_prompt(ticker: str, name_ko: str, exchange: str, data: dict) -> str:
    today = get_target_market_date()
    today_str = today.strftime("%Y년 %m월 %d일")
    company = data.get("long_name") or name_ko

    return f"""당신은 미국 증시 개별 종목 전문 기자입니다. 구글 검색으로 오늘
{name_ko}({ticker}) 주가를 움직인 실제 뉴스(실적, 신제품, 애널리스트 리포트,
소송, 규제, 거시경제 이슈 등)를 찾아서, 아래 시세 데이터와 결합해 기사를
작성하세요. 검색 없이 수치만 나열하지 마세요.

오늘({today_str}) 기준 {name_ko}({ticker}, {exchange}) 시세 데이터:
- 현재가: {data['price']:.2f}달러 (전일比 {data['pct']:+.2f}%)
- 당일 거래 범위: {data.get('day_low', 0):.2f} ~ {data.get('day_high', 0):.2f}달러
- 52주 최고/최저: {data.get('fifty_two_week_high', 0):.2f} / {data.get('fifty_two_week_low', 0):.2f}달러
- 거래량: {int(data.get('volume') or 0):,}주

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "[종목 동향] "으로 시작. 대괄호 포함 그대로 출력.
- {name_ko} 주가 등락률을 포함. 50자 이내로.
- 예: "[종목 동향] 애플 주가 3.2% 상승…신제품 기대감"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 절대 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '지켜볼 필요가 있습니다'
2. 구조 — 아래 2개 섹션을 이 순서대로 다루세요. 각 섹션은 "◆ 섹션명" 한 줄로
   시작하고(번호나 대괄호 없이 ◆ 기호 + 섹션명만), 그 다음 줄부터 내용을
   쓰세요. 섹션 제목 줄 앞에는 빈 줄을 하나씩 두세요. 아래 섹션명 뒤에 붙은
   설명은 그 섹션에 뭘 써야 하는지 알려주는 지시일 뿐이니 지시 내용 자체를
   본문에 옮겨 적지 마세요 — "◆ 섹션명" 한 줄만 그대로 쓰고 바로 이어서
   지시를 따른 기사 내용을 쓰세요.

   ◆ 주가 동향
   {name_ko}({ticker}) 주가가 오늘 왜 그렇게 움직였는지, 검색으로 찾은 실제
   뉴스(실적·신제품·애널리스트 리포트·소송·규제 등)를 근거로 설명하세요.
   반드시 "{exchange}"와 "{today.day}일(현지시간)"을 함께 언급하세요(예:
   "{exchange}에서 {name_ko} 주가는 {today.day}일(현지시간)…").

   ◆ 주요 지표
   당일 거래 범위, 52주 최고/최저 대비 현재 위치, 거래량 등을 구체적
   수치와 함께 서술하세요.
3. ⚠️ 날짜: 반드시 ◆ 주가 동향 섹션에 "{today.day}일(현지시간)" 형식으로
   날짜를 명시하세요. "오늘", "현재", 절대연도(2026년 등)는 금지.
4. 수치는 위 데이터를 그대로 사용하고 절대 지어내지 마세요. 검색으로 찾은
   뉴스도 실제 사실만 반영하고, 확인 안 되는 내용은 지어내지 마세요.
5. 분량: 500자 이상.
6. 회사 정식 명칭은 "{company}" 또는 "{name_ko}"로 통일해 쓰세요.
"""


TITLE_PREFIX = "[종목 동향]"


def enforce_title_prefix(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return t
    m = re.match(r"^\s*\[\s*종목\s*동향\s*\]\s*(.*)$", t)
    if m:
        t = m.group(1).strip()
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


def call_gemini_article(prompt: str, max_tokens: int = 3000) -> str | None:
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
                     "가격·거래량 등 수치는 반드시 위 시세 데이터에 나온 값 그대로 쓰세요.",
            max_tokens=max_tokens, use_search=True, max_stages=1,
        )
        if retried:
            text = retried
        time.sleep(5)

    return text


# ── 이미지 ───────────────────────────────────────────────────
_STOCK_IMAGE_KEYWORDS = [
    "stock market chart", "trading floor", "financial district",
    "stock exchange board", "business technology",
]


def fetch_stock_image(seed_date: date) -> str:
    from article_image import fetch_seeded_pixabay_image
    return fetch_seeded_pixabay_image(
        _STOCK_IMAGE_KEYWORDS, seed_date.toordinal(), f"stock_news_{seed_date.isoformat()}"
    )


# ── 중복 체크 & 저장 ──────────────────────────────────────────
def already_published(article_date: date) -> bool:
    internal_url = f"internal://stock_news_{article_date.isoformat()}"
    res = requests.get(
        f"{_sb_url()}?url=eq.{internal_url}&is_published=eq.true&select=id",
        headers=_sb_headers(),
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(ticker: str, name_ko: str, title_ko: str, summary_ko: str,
                    article_date: date, image_url: str = "") -> int:
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
    internal_url = f"internal://stock_news_{article_date.isoformat()}"

    print("  → 영어 번역 생성 중...")
    title_en, summary_en = translate_article(title_ko, summary_ko, call_gemini)
    if not summary_en:
        print("  ⚠️ 영어 번역 실패 → 한국어만 저장")

    payload = {
        "title_en": title_en or title_ko,
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "금융",
        "subcategory": "종목동향",
        "region": "global",
        "country": "미국",
        "country_flag": "🇺🇸",
        "countries": ["미국"],
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": f"{name_ko}({ticker}) 종목 동향 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[stock_news_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

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
        print(f"  → {article_date} 종목 동향 기사 이미 존재 → 스킵")
        return

    ticker, name_ko, exchange = pick_ticker(article_date)
    print(f"  → 오늘의 종목: {name_ko}({ticker}, {exchange})")

    print("  → 야후 파이낸스에서 시세 데이터 수집 중...")
    data = fetch_stock_data(ticker)
    if not data:
        print(f"  [ERROR] {ticker} 시세 수집 실패 → 종료")
        return
    print(f"  → {data['price']:.2f}달러 ({data['pct']:+.2f}%)")

    print("  → Gemini로 기사 생성 중...")
    prompt = build_article_prompt(ticker, name_ko, exchange, data)
    article_text = call_gemini_article(prompt)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    title, body = parse_article_output(article_text)
    title = enforce_title_prefix(title)

    if not title or not body:
        print("  [ERROR] 응답 파싱 실패")
        return

    image_url = fetch_stock_image(article_date)

    article_id = insert_article(ticker, name_ko, title, body, article_date, image_url)
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[stock_news_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
