"""
frontier_markets_writer.py
----------------------------
프론티어 마켓 통화 12개·증시 지수 7개(ETF 대리지표 2개 포함)의 일일 변동을
추적해 "프론티어 마켓 동향" 기사를 자동 생성합니다. 국내외 언론이 거의 다루지
않는 프론티어 통화·증시 급변동을 짚어 사이트 차별화 콘텐츠로 삼습니다
(2026-08-21 신설 — "결국 콘텐츠 다양화만이 PV를 올릴 수 있다" 사용자 판단).

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


def call_gemini(prompt: str, max_tokens: int = 2500, start_tier: int = 3) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.3, timeout=(10, 45))


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

# 증시: 야후 직접 커버 국가만. 베트남·나이지리아는 직접 지수가 없어
# ETF 대리지표로 대체. 파키스탄·케냐·가나·사우디·카자흐스탄은 야후에
# 지수 자체가 없어 제외(위 통화 목록엔 포함돼 있음).
INDICES = [
    ("남아공", "Top40", "^JN0U.JO"),
    ("말레이시아", "KLCI", "^KLSE"),
    ("인도네시아", "종합지수", "^JKSE"),
    ("이집트", "EGX30", "^CASE30"),
    ("이스라엘", "TA-125", "^TA125.TA"),
    ("베트남", "VanEck 베트남 ETF", "VNM"),
    ("나이지리아", "MSCI 나이지리아 ETF", "NGE"),
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
    """통화·증시 전체를 조회해 변동률 큰 순으로 정렬해 반환."""
    currencies = []
    for country, name, symbol in CURRENCIES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if q:
            currencies.append({"country": country, "name": name, **q})

    indices = []
    for country, name, symbol in INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if q:
            indices.append({"country": country, "name": name, **q})

    currencies.sort(key=lambda x: abs(x["pct"]), reverse=True)
    indices.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return {"currencies": currencies, "indices": indices}


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
    check_prompt = f"""아래는 기사 작성에 쓰인 원본 자료와, 그걸 바탕으로 생성된 한국어 기사 본문입니다.
기사 본문에 나오는 고유명사(기관명·지수명·기업명)가 원본 자료에 실제로
근거하는지 확인하세요. 원본 자료에 없는 대상을 완전히 잘못 지어낸 경우만
찾으세요. 그런 이름이 있으면 "지어낸이름 → 원본표기" 형식으로 쉼표 구분해
나열하세요. 없으면 "없음"이라고만 답하세요.

[원본 자료]
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

    cur_lines = "\n".join(
        f"- {c['country']} {c['name']}: {c['price']:.2f} (전일比 {c['pct']:+.2f}%)"
        for c in data["currencies"]
    )
    idx_lines = "\n".join(
        f"- {i['country']} {i['name']}: {i['price']:.2f} (전일比 {i['pct']:+.2f}%)"
        for i in data["indices"]
    )

    top_cur = data["currencies"][:3]
    top_idx = data["indices"][:3]
    top_cur_str = ", ".join(f"{c['country']} {c['pct']:+.1f}%" for c in top_cur)
    top_idx_str = ", ".join(f"{i['country']} {i['pct']:+.1f}%" for i in top_idx)

    return f"""당신은 프론티어 마켓 전문 경제 기자입니다.
아래는 오늘({today_str}) 기준 프론티어 마켓 통화·증시 데이터입니다(달러 대비
환율, 전일 대비 변동률). 이 데이터를 바탕으로 뉴스 기사를 작성하세요.

[통화 — 달러 대비, 변동률 큰 순]
{cur_lines}

[증시 지수 — 현지통화 기준, 변동률 큰 순]
{idx_lines}

[변동폭 상위]
- 통화: {top_cur_str}
- 증시: {top_idx_str}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "[프론티어 마켓 동향] "으로 시작. 대괄호 포함 그대로 출력.
- 통화·증시 중 변동폭이 가장 큰 이슈를 담아 50자 이내로.
- 예: "[프론티어 마켓 동향] 나이지리아 나이라 급락, 아르헨티나 페소도 약세"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 절대 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '지켜볼 필요가 있습니다'
2. 구조:
   ① 리드: 오늘 프론티어 마켓에서 가장 두드러진 통화·증시 변동을 한 문장으로 요약
   ② [통화 동향] 변동폭 상위 통화 3~4개를 구체적 수치와 함께 서술(급락/급등 배경이
      데이터에 근거해 유추 가능하면 짧게 언급, 근거 없으면 수치만 사실 전달)
   ③ [증시 동향] 변동폭 상위 지수 2~3개를 구체적 수치와 함께 서술
   ④ 프론티어 마켓 공통 배경 요인이 있으면 짚기(달러 강세/약세, 원자재 가격, 글로벌
      금리 등) — 데이터에 없는 내용을 지어내지 말고 일반적으로 알려진 사실만
3. 날짜는 "{today.day}일" 형식으로만 표기. 위 대괄호 소제목("[통화 동향]", "[증시 동향]")은
   본문 안에 그대로 포함하고, 소제목 앞에는 빈 줄을 하나씩 두세요.
4. 수치는 위 데이터를 그대로 사용하고 절대 지어내지 마세요.
5. 비라틴 문자 국가명·통화명·지수명은 이미 한국어로 제공했으니 그대로 쓰세요.
6. 분량: 700자 이상.
"""


TITLE_PREFIX = "[프론티어 마켓 동향]"


def enforce_title_prefix(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return t
    m = re.match(r"^\s*\[\s*프론티어\s*마켓\s*동향\s*\]\s*(.*)$", t)
    if m:
        t = m.group(1).strip()
    else:
        m2 = re.match(r"^프론티어\s*마켓\s*동향(?:이|은)?\s*[,·]?\s+(.+)$", t)
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


def call_gemini_article(prompt: str, max_tokens: int = 2500) -> str | None:
    text = call_gemini(prompt, max_tokens=max_tokens)
    time.sleep(5)
    if not text:
        return None

    if has_column_style(text):
        print("  ⚠️ 논평체 감지 → 재생성")
        retried = call_gemini(
            prompt + "\n\n[재작성 지시] 논평/칼럼 문체가 섞였습니다. 사실 전달 중심으로만 다시 작성하세요.",
            max_tokens=max_tokens,
        )
        if retried:
            text = retried
        time.sleep(5)

    _, body_probe = parse_article_output(text)
    fabricated = verify_no_fabricated_names(prompt, body_probe or text)
    if fabricated:
        print(f"  ⚠️ 원문에 없는 고유명사 감지({fabricated}) → 재생성")
        retried = call_gemini(
            prompt + f"\n\n[재작성 지시] 다음 이름을 원문에 없는 표현으로 잘못 지어냈습니다: {fabricated}. "
                     "데이터에 없는 기관명·지수명은 지어내지 말고 빼세요.",
            max_tokens=max_tokens,
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

    top_countries = [c["country"] for c in data["currencies"][:3]] + [i["country"] for i in data["indices"][:2]]
    countries = list(dict.fromkeys(top_countries))  # 순서 유지 중복 제거

    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "경제",
        "subcategory": "프론티어마켓동향",
        "region": "global",
        "country": "",
        "country_flag": "🌍",
        "countries": countries,
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "프론티어 마켓 동향 자동 기사"}],
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
        print(f"  → {article_date} 프론티어 마켓 동향 기사 이미 존재 → 스킵")
        return

    print("  → 야후 파이낸스에서 통화·증시 데이터 수집 중...")
    data = fetch_all_data()
    if len(data["currencies"]) < 3 or len(data["indices"]) < 2:
        print(f"  [ERROR] 데이터 수집 부족(통화 {len(data['currencies'])}건, "
              f"증시 {len(data['indices'])}건) → 종료")
        return
    print(f"  → 통화 {len(data['currencies'])}건, 증시 {len(data['indices'])}건 수집 완료")

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
