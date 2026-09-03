"""
scripts/crypto_news_writer.py
-------------------------------
가상자산(비트코인 등) 동향 기사 자동 생성(2026-09-04 신설 — 사용자 요청
"비트코인 등 가상화폐 관련 기사도 다루자").

stock_news_writer.py와 거의 동일한 구조를 그대로 따른다(같은 야후 조회·
팩트체크·번역 로직 재사용). 차이점은 딱 하나 — 가상자산은 24시간 거래되므로
"뉴욕 종가 이후"라는 시장-마감 게이트가 없다. 대신 하루 1회만 쓰도록
already_published(article_date)로 날짜 단위 중복만 막는다.

코인 선택: 고정 워치리스트를 날짜 기반으로 순환 선택(머니파이널 피드는
아직 크립토 뉴스를 다루지 않아 stock_news_writer.py 같은 뉴스-기반 선택은
데이터가 없음 — 추후 크립토 전용 뉴스 소스가 생기면 같은 방식으로 확장).

실행: python scripts/crypto_news_writer.py
권장: frontier_markets.yml에 같은 스텝으로 묶어 주기적으로 호출(자체
already_published 게이트로 하루 1회만 실제로 씀).
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

# 야후 조회·팩트체크·시간 유틸은 frontier_markets_writer.py 재사용(2026-09-04).
from frontier_markets_writer import (
    fetch_yahoo_quote, verify_no_fabricated_names, now_kst,
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


# ── 워치리스트 (주요 가상자산, 2026-09-04) ────────────────────
# 야후 파이낸스 심볼 형식(XXX-USD)을 그대로 쓴다 — fetch_yahoo_quote가
# 이미 이 형식으로 검증돼 있음(BTC-USD 실측 확인).
WATCHLIST = [
    ("BTC-USD", "비트코인"),
    ("ETH-USD", "이더리움"),
    ("SOL-USD", "솔라나"),
    ("XRP-USD", "리플"),
]

# 야후 심볼 → 머니파이널 crypto.json의 CoinGecko id 매핑(아래 참조).
_YAHOO_TO_COINGECKO_ID = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "SOL-USD": "solana",
    "XRP-USD": "ripple",
}

# 머니파이널(D:\thewise\moneyfinal)이 CoinGecko 기준 시총 상위 20개 코인
# 시세를 이 경로에 공개 정적 파일로 배포할 예정(2026-09-04, 사용자 공지 —
# crypto.html 프론트엔드는 이미 배포돼 이 파일을 fetch하고 있지만 데이터
# 파일 자체는 아직 없음, 요청하면 SPA 폴백 HTML이 200으로 옴). market_news
# 테이블과 달리 이건 인증 없는 공개 정적 파일이라 게이트웨이가 필요 없다 —
# 배포되는 즉시 아래 함수가 자동으로 살아난다(그 전까진 조용히 폴백).
MONEYFINAL_CRYPTO_URL = "https://moneyfinal.pages.dev/data/crypto.json"


def pick_coin(article_date: date) -> tuple[str, str]:
    idx = article_date.toordinal() % len(WATCHLIST)
    return WATCHLIST[idx]


def fetch_coin_data(symbol: str) -> dict | None:
    return fetch_yahoo_quote(symbol)


def fetch_moneyfinal_crypto_context(symbol: str) -> dict | None:
    """머니파이널 crypto.json에서 이 코인의 시총 순위·시총·7일 변동률을
    가져온다(야후 시세엔 없는 정보). 파일이 아직 없거나(404→SPA 폴백 HTML)
    형식이 안 맞으면 조용히 None — 실패해도 기사 생성 자체는 야후 데이터만
    으로 계속 진행된다."""
    coin_id = _YAHOO_TO_COINGECKO_ID.get(symbol)
    if not coin_id:
        return None
    try:
        res = requests.get(MONEYFINAL_CRYPTO_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200:
            return None
        if "json" not in (res.headers.get("content-type") or "").lower():
            return None
        data = res.json()
        for c in data.get("coins", []):
            if c.get("id") == coin_id:
                return c
    except Exception:
        pass
    return None


# ── 기사 프롬프트 ────────────────────────────────────────────
def build_article_prompt(symbol: str, name_ko: str, data: dict, mf_ctx: dict | None = None) -> str:
    today = now_kst().date()
    today_str = today.strftime("%Y년 %m월 %d일")

    mf_block = ""
    if mf_ctx:
        parts = []
        if mf_ctx.get("market_cap_rank"):
            parts.append(f"시가총액 순위: {mf_ctx['market_cap_rank']}위")
        if mf_ctx.get("market_cap"):
            parts.append(f"시가총액: {float(mf_ctx['market_cap']):,.0f}달러")
        if mf_ctx.get("change_pct_7d") is not None:
            parts.append(f"7일 변동률: {float(mf_ctx['change_pct_7d']):+.2f}%")
        if parts:
            mf_block = "\n추가 참고(머니파이널 CoinGecko 데이터): " + ", ".join(parts) + "\n"

    return f"""당신은 가상자산 시장 전문 기자입니다. 구글 검색으로 오늘
{name_ko}({symbol.replace('-USD','')}) 가격을 움직인 실제 뉴스(규제 동향,
기관 매수/매도, 거시경제 이슈, 관련 프로젝트 소식 등)를 찾아서, 아래 시세
데이터와 결합해 기사를 작성하세요. 검색 없이 수치만 나열하지 마세요.

오늘({today_str}) 기준 {name_ko}({symbol.replace('-USD','')}) 시세 데이터
(24시간 거래 기준, 달러):
- 현재가: {data['price']:,.2f}달러 (전일比 {data['pct']:+.2f}%)
- 당일 거래 범위: {data.get('day_low', 0):,.2f} ~ {data.get('day_high', 0):,.2f}달러
- 52주 최고/최저: {data.get('fifty_two_week_high', 0):,.2f} / {data.get('fifty_two_week_low', 0):,.2f}달러
{mf_block}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "[가상자산 동향] "으로 시작. 대괄호 포함 그대로 출력.
- {name_ko} 가격 등락률을 포함. 50자 이내로.
- 예: "[가상자산 동향] 비트코인 4.4% 급등…기관 매수세"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 절대 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '지켜볼 필요가 있습니다'
2. 구조 — 아래 2개 섹션을 이 순서대로 다루세요. 각 섹션은 "◆ 섹션명" 한 줄로
   시작하고(번호나 대괄호 없이 ◆ 기호 + 섹션명만), 그 다음 줄부터 내용을
   쓰세요. 섹션 제목 줄 앞에는 빈 줄을 하나씩 두세요. 아래 섹션명 뒤에 붙은
   설명은 그 섹션에 뭘 써야 하는지 알려주는 지시일 뿐이니 지시 내용 자체를
   본문에 옮겨 적지 마세요 — "◆ 섹션명" 한 줄만 그대로 쓰고 바로 이어서
   지시를 따른 기사 내용을 쓰세요.

   ◆ 가격 동향
   {name_ko} 가격이 오늘 왜 그렇게 움직였는지, 검색으로 찾은 실제 뉴스(규제·
   기관 수급·거시경제 이슈 등)를 근거로 설명하세요. 반드시 "{today.day}일
   (한국시간)"을 함께 언급하세요.

   ◆ 주요 지표
   당일 거래 범위, 52주 최고/최저 대비 현재 위치 등을 구체적 수치와 함께
   서술하세요.
3. ⚠️ 날짜: 반드시 ◆ 가격 동향 섹션에 "{today.day}일(한국시간)" 형식으로
   날짜를 명시하세요. "오늘", "현재", 절대연도(2026년 등)는 금지.
4. 수치는 위 데이터를 그대로 사용하고 절대 지어내지 마세요. 검색으로 찾은
   뉴스도 실제 사실만 반영하고, 확인 안 되는 내용은 지어내지 마세요.
5. 분량: 500자 이상.
"""


TITLE_PREFIX = "[가상자산 동향]"


def enforce_title_prefix(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return t
    m = re.match(r"^\s*\[\s*가상자산\s*동향\s*\]\s*(.*)$", t)
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
                     "가격 등 수치는 반드시 위 시세 데이터에 나온 값 그대로 쓰세요.",
            max_tokens=max_tokens, use_search=True, max_stages=1,
        )
        if retried:
            text = retried
        time.sleep(5)

    return text


# ── 이미지 ───────────────────────────────────────────────────
_CRYPTO_IMAGE_KEYWORDS = [
    "cryptocurrency", "bitcoin", "blockchain technology", "digital currency",
    "crypto trading",
]


def fetch_crypto_image(seed_date: date) -> str:
    from article_image import fetch_seeded_pixabay_image
    return fetch_seeded_pixabay_image(
        _CRYPTO_IMAGE_KEYWORDS, seed_date.toordinal(), f"crypto_news_{seed_date.isoformat()}"
    )


# ── 중복 체크 & 저장 ──────────────────────────────────────────
def already_published(article_date: date) -> bool:
    internal_url = f"internal://crypto_news_{article_date.isoformat()}"
    res = requests.get(
        f"{_sb_url()}?url=eq.{internal_url}&is_published=eq.true&select=id",
        headers=_sb_headers(),
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(symbol: str, name_ko: str, title_ko: str, summary_ko: str,
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
    internal_url = f"internal://crypto_news_{article_date.isoformat()}"

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
        "subcategory": "가상자산동향",
        "region": "global",
        "country": "글로벌",
        "country_flag": "🌐",
        "countries": [],
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": f"{name_ko}({symbol}) 가상자산 동향 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[crypto_news_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not GEMINI_API_KEYS:
        print("  [SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    article_date = now_kst().date()
    if already_published(article_date):
        print(f"  → {article_date} 가상자산 동향 기사 이미 존재 → 스킵")
        return

    symbol, name_ko = pick_coin(article_date)
    print(f"  → 오늘의 코인: {name_ko}({symbol})")

    print("  → 야후 파이낸스에서 시세 데이터 수집 중...")
    data = fetch_coin_data(symbol)
    if not data:
        print(f"  [ERROR] {symbol} 시세 수집 실패 → 종료")
        return
    print(f"  → {data['price']:,.2f}달러 ({data['pct']:+.2f}%)")

    mf_ctx = fetch_moneyfinal_crypto_context(symbol)
    if mf_ctx:
        print(f"  → 머니파이널 데이터 참조 (시총순위 {mf_ctx.get('market_cap_rank', '-')}위)")

    print("  → Gemini로 기사 생성 중...")
    prompt = build_article_prompt(symbol, name_ko, data, mf_ctx)
    article_text = call_gemini_article(prompt)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    title, body = parse_article_output(article_text)
    title = enforce_title_prefix(title)

    if not title or not body:
        print("  [ERROR] 응답 파싱 실패")
        return

    image_url = fetch_crypto_image(article_date)

    article_id = insert_article(symbol, name_ko, title, body, article_date, image_url)
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[crypto_news_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
