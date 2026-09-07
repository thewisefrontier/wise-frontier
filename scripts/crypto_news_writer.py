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

# 비트코인/이더리움/솔라나는 오늘의 코인이 뭐든 항상 시세를 같이 보여준다
# (2026-09-04 사용자 요청 — "코인 시세 쓸때는 비트코인, 이더리움, 솔라나
# 시세를 무조건 넣어주도록"). 시장 벤치마크라 어떤 코인이 주인공이어도
# 참고 시세로서 가치가 있음.
BENCHMARK_COINS = [("BTC-USD", "비트코인"), ("ETH-USD", "이더리움"), ("SOL-USD", "솔라나")]

# 야후 심볼 → 머니파이널 응답의 coin id 매핑(CoinMarketCap 소스, id 필드는
# "bitcoin"/"ethereum" 식 slug 그대로 유지됨).
_YAHOO_TO_COINGECKO_ID = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "SOL-USD": "solana",
    "XRP-USD": "xrp",  # 실측 확인(2026-09-04): "ripple"이 아니라 "xrp"
}

# ⚠️ 2026-09-04 정정: 처음엔 data/crypto.json을 인증 없는 공개 정적
# 파일이라고 안내받았는데, 머니파이널 쪽에서 뒤늦게 CoinMarketCap 이용약관상
# 원본 데이터 재배포 금지 조항을 확인하고 정적 파일을 삭제, market_news와
# 동일한 인증 게이트웨이(/api/crypto, X-Api-Key)로 전환했다(공식 정정 공지로
# 통보받고, 응답이 실제로 20개 코인 실데이터를 반환하는지 직접 검증 후 반영
# — MONEYFINAL_FEED_KEY는 market_news_fetcher.py가 쓰는 것과 동일한 키를
# 재사용). us_company_info(/api/company-info)도 같은 방식으로 바뀌었으나
# 그쪽은 아직 이 저장소에서 소비하는 코드가 없어 손대지 않았다.
MONEYFINAL_CRYPTO_URL = "https://moneyfinal.pages.dev/api/crypto"
MONEYFINAL_FEED_KEY = os.getenv("MONEYFINAL_FEED_KEY", "")


def pick_coin(article_date: date) -> tuple[str, str]:
    idx = article_date.toordinal() % len(WATCHLIST)
    return WATCHLIST[idx]


def fetch_coin_data(symbol: str) -> dict | None:
    return fetch_yahoo_quote(symbol)


def fetch_benchmark_data(featured_symbol: str, featured_data: dict) -> list[tuple[str, str, dict]]:
    """BENCHMARK_COINS(BTC/ETH/SOL) 시세를 항상 가져온다. 오늘의 코인이
    이미 이 셋 중 하나면 중복 조회하지 않고 재사용. 실패한 코인은 조용히
    빠짐(있는 것만이라도 본문에 반영하면 되고, 전체 기사 생성을 막을
    이유는 아님)."""
    out = []
    for symbol, name_ko in BENCHMARK_COINS:
        if symbol == featured_symbol:
            out.append((symbol, name_ko, featured_data))
            continue
        d = fetch_coin_data(symbol)
        if d:
            out.append((symbol, name_ko, d))
        else:
            print(f"  ⚠️ 벤치마크 시세 조회 실패: {name_ko}({symbol})")
    return out


def fetch_moneyfinal_crypto_context(symbol: str) -> dict | None:
    """머니파이널 /api/crypto에서 이 코인의 시총 순위·시총·7일 변동률을
    가져온다(야후 시세엔 없는 정보). 키 없거나 실패하면 조용히 None —
    실패해도 기사 생성 자체는 야후 데이터만으로 계속 진행된다."""
    coin_id = _YAHOO_TO_COINGECKO_ID.get(symbol)
    if not coin_id or not MONEYFINAL_FEED_KEY:
        return None
    try:
        res = requests.get(
            MONEYFINAL_CRYPTO_URL,
            headers={"X-Api-Key": MONEYFINAL_FEED_KEY},
            timeout=10,
        )
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


def _won_style_decimal(n: float) -> str:
    """79,979.98 같은 콤마 표기 대신 "7만9979.98" 식 억/만 그룹핑(소수부는
    그대로 유지)으로 변환. 벤치마크·현재가처럼 센트 단위가 있는 가격
    표시에 쓴다(2026-09-07 사용자 지적 — "79979.98달러"가 "7만9979.98달러"
    여야 한다고 지적, _won_style_amount는 정수 전용이라 별도로 둔다)."""
    sign = "-" if n < 0 else ""
    n = abs(n)
    int_part = int(n)
    frac = round((n - int_part) * 100)
    if frac == 100:
        int_part += 1
        frac = 0
    eok, rest = divmod(int_part, 100_000_000)
    man, remainder = divmod(rest, 10_000)
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man}만")
    if remainder or not parts:
        parts.append(f"{remainder}")
    return f"{sign}{''.join(parts)}.{frac:02d}"


def _won_style_amount(n: float) -> str:
    """91,256,495,759 같은 콤마 표기 대신 "912억5649만5759" 식 억/만
    그룹핑으로 변환(2026-09-04 사용자 지적 — 리플 기사에서 시가총액이
    콤마 원문 그대로 나가 "가시성이 너무 떨어진다"고 지적받음). 프롬프트에
    넣는 참고자료 자체를 이 형식으로 만들어서, Gemini가 콤마 원본을
    그대로 베낄 여지를 원천 차단한다."""
    n = int(round(n))
    eok, rest = divmod(n, 100_000_000)
    man, remainder = divmod(rest, 10_000)
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man}만")
    if remainder or not parts:
        parts.append(f"{remainder}")
    return "".join(parts)


# ── 기사 프롬프트 ────────────────────────────────────────────
def build_article_prompt(symbol: str, name_ko: str, data: dict, mf_ctx: dict | None = None,
                          benchmarks: list | None = None) -> str:
    now = now_kst()
    today = now.date()
    today_str = today.strftime("%Y년 %m월 %d일")
    # 코인은 24시간 거래라 "N일"만으로는 언제 시점 가격인지 모호하다(하루
    # 안에서도 크게 움직임) — 사용자 지적(2026-09-04) 반영, 시:분까지 명시.
    time_str = now.strftime("%H:%M")

    bench_block = ""
    if benchmarks:
        lines = [
            f"- {bname}({bsym.replace('-USD','')}): {_won_style_decimal(bd['price'])}달러 ({bd['pct']:+.2f}%)"
            for bsym, bname, bd in benchmarks
        ]
        bench_block = (
            "\n[벤치마크 시세 — 오늘의 주인공이 아니어도 본문에 반드시 언급]\n"
            + "\n".join(lines)
            + "\n이 시세들은 위 데이터와 같은 시각(같은 야후 파이낸스 조회) 기준입니다. "
              "본문 아무 곳에나 자연스럽게 전일 대비 등락률까지 포함해서(예: '한편 비트코인은 "
              "7만9979.98달러(-1.2%), 이더리움은 2510.62달러(+0.8%)에 거래됐다') 넣으세요 — "
              "이미 오늘의 주인공인 코인은 중복해서 다시 나열하지 마세요. 숫자에 3자리마다 "
              "콤마(,)를 찍지 말고 위 표기처럼 억/만 단위로 풀어 쓰세요.\n"
        )

    mf_block = ""
    if mf_ctx:
        parts = []
        if mf_ctx.get("market_cap_rank"):
            parts.append(f"시가총액 순위: {mf_ctx['market_cap_rank']}위")
        if mf_ctx.get("market_cap"):
            parts.append(f"시가총액: {_won_style_amount(float(mf_ctx['market_cap']))}달러")
        if mf_ctx.get("change_pct_7d") is not None:
            parts.append(f"7일 변동률: {float(mf_ctx['change_pct_7d']):+.2f}%")
        if parts:
            mf_block = "\n추가 참고(머니파이널 CoinGecko 데이터): " + ", ".join(parts) + "\n"

    return f"""당신은 가상자산 시장 전문 기자입니다. 구글 검색으로 오늘
{name_ko}({symbol.replace('-USD','')}) 가격을 움직인 실제 뉴스(규제 동향,
기관 매수/매도, 거시경제 이슈, 관련 프로젝트 소식 등)를 찾아서, 아래 시세
데이터와 결합해 기사를 작성하세요. 검색 없이 수치만 나열하지 마세요.

오늘({today_str} {time_str} 한국시간) 기준 {name_ko}({symbol.replace('-USD','')}) 시세 데이터
(24시간 거래 기준, 달러 — 코인은 하루 안에서도 가격이 크게 움직이니 이
시각 기준 데이터임에 유의):
- 현재가: {_won_style_decimal(data['price'])}달러 (전일比 {data['pct']:+.2f}%)
- 당일 거래 범위: {_won_style_decimal(data.get('day_low', 0))} ~ {_won_style_decimal(data.get('day_high', 0))}달러
- 52주 최고/최저: {_won_style_decimal(data.get('fifty_two_week_high', 0))} / {_won_style_decimal(data.get('fifty_two_week_low', 0))}달러
{mf_block}{bench_block}
⚠️ 위 시세 수치는 이미 억/만 단위로 풀어 쓴 표기입니다. 본문에 옮길 때 3자리마다 콤마(,)를 찍지 말고 위 표기 그대로(예: "7만9979.98") 쓰세요.

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
   기관 수급·거시경제 이슈 등)를 근거로 설명하세요. 첫 문장 또는 시세를
   처음 언급하는 지점에 반드시 "{today.day}일 {time_str}(한국시간) 야후
   파이낸스에 따르면"을 그대로 넣으세요(예: "{today.day}일 {time_str}
   (한국시간) 야후 파이낸스에 따르면 {name_ko} 가격은..." — 다른 국내외
   시세 기사에서 흔히 쓰는 통상적 인용 표현입니다). 코인은 24시간 거래에
   거래소마다 가격이 조금씩 달라서, 날짜만 쓰면 하루 중 어느 시점인지도,
   어느 출처 가격인지도 알 수 없습니다 — 시:분과 출처를 둘 다 반드시
   명시하세요.

   ◆ 주요 지표
   당일 거래 범위, 52주 최고/최저 대비 현재 위치 등을 구체적 수치와 함께
   서술하세요. 벤치마크 시세(위 [벤치마크 시세] 참고자료가 있다면)도 이
   섹션이나 가격 동향 섹션 어디든 자연스럽게 넣으세요.
3. ⚠️ 날짜·시각·출처: 반드시 ◆ 가격 동향 섹션 앞부분에 "{today.day}일
   {time_str}(한국시간) 야후 파이낸스에 따르면" 형식으로 날짜·시각·데이터
   출처를 함께 명시하세요(셋 중 하나라도 생략 금지). "오늘", "현재",
   절대연도(2026년 등)는 금지.
4. 수치는 위 데이터를 그대로 사용하고 절대 지어내지 마세요. 검색으로 찾은
   뉴스도 실제 사실만 반영하고, 확인 안 되는 내용은 지어내지 마세요.
5. ⚠️ [벤치마크 시세] 참고자료가 주어졌다면, 그 코인들의 시세를 본문에
   빠짐없이 언급하세요 — 생략 금지.
6. ⚠️ 숫자에 3자리마다 콤마(,)를 찍지 마세요. 시가총액·거래량처럼 큰
   금액은 "912억5649만5759달러"처럼 억/만 단위로 풀어 쓰세요 — "91,256,
   495,759달러"처럼 콤마로 구분된 원본 숫자를 그대로 베끼지 마세요. 위
   [추가 참고] 자료의 시가총액은 이미 이 형식으로 변환돼 있으니 그대로
   쓰면 됩니다.
7. 분량: 500자 이상.
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

    print("  → 벤치마크(BTC/ETH/SOL) 시세 수집 중...")
    benchmarks = fetch_benchmark_data(symbol, data)

    print("  → Gemini로 기사 생성 중...")
    prompt = build_article_prompt(symbol, name_ko, data, mf_ctx, benchmarks)
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
