"""
oil_price_writer.py
-------------------
WTI·Brent 국제유가 일일 변동을 모니터링하여 뉴스 기사를 자동 생성합니다.

데이터 소스 (우선순위):
  1. EIA API (에너지정보청 공식, EIA_API_KEY 환경변수)
  2. Yahoo Finance 비공식 차트 API (키 불필요)
  3. Alpha Vantage (ALPHA_VANTAGE_API_KEY 환경변수)

⚠️ Stooq는 2026-08-21부로 제외했다. robots.txt가 `Disallow: /`(구글봇·
빙봇 제외 전체 차단)로 명시돼 있고, 실제로도 사이트 전체(메인 외 모든
데이터 엔드포인트)에 JS 연산증명 챌린지가 걸려 있어 접근이 막혀 있다
(2026-08-10, 2026-08-21 두 차례 확인). 정책적으로 막아둔 곳이라 우회
시도 자체를 하지 않는다.

실행: python scripts/oil_price_writer.py
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from rapidfuzz import fuzz

try:
    from news_context import fetch_headlines
except Exception:
    def fetch_headlines(*a, **k):
        return []

load_dotenv()

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

# ── 설정 ────────────────────────────────────────────────────
GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
SUPABASE_URL         = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
EIA_API_KEY          = os.getenv("EIA_API_KEY", "")  # https://www.eia.gov/opendata/ 무료 등록
ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")  # https://www.alphavantage.co/support/#api-key 무료 등록

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

KST = timezone(timedelta(hours=9))
EDT = ZoneInfo("America/New_York")  # 뉴욕 시장 기준

# 뉴욕 시장 종가 기준: 현지 17:00 이후에만 실행
MARKET_CLOSE_HOUR_LOCAL = 17   # 17:00 EDT/EST


# EIA 시리즈 ID
EIA_SERIES = {
    "WTI":   "PET.RWTC.D",    # WTI Crude Oil Spot Price (Dollars per Barrel)
    "Brent": "PET.RBRTE.D",   # Brent Europe Crude Oil Spot Price
}

# Alpha Vantage 원자재 함수명 (일별 종가 제공, 봇 검증 없음)
ALPHA_VANTAGE_FUNCTIONS = {
    "WTI":   "WTI",
    "Brent": "BRENT",
}

# 야후 파이낸스 선물 심볼 (키 불필요, 봇 검증 없음 — 2026-08-21 확인)
YAHOO_SYMBOLS = {
    "WTI":   "CL=F",
    "Brent": "BZ=F",
}


# ── 헬퍼 ────────────────────────────────────────────────────
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

# Pixabay 이미지 URL은 임시라 시간이 지나면 깨진다. R2에 영구 저장한 뒤 그 URL을 쓴다.
# import 실패에도 본 기능(유가 기사 생성)이 죽지 않도록 폴백을 둔다.
try:
    from image_store import store_image
except Exception:
    def store_image(src_url, key_hint="", timeout=30):
        return src_url   # 원본 그대로 반환 = 이미지 없이 동작


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

def now_edt() -> datetime:
    return datetime.now(timezone.utc).astimezone(EDT)

def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _sb_articles_url():
    return f"{SUPABASE_URL}/rest/v1/articles"


# ── 뉴욕 시장 종료 체크 ──────────────────────────────────────
def market_is_closed() -> bool:
    """뉴욕 현지 17:00 이후인지 확인 (주말은 금요일 종가 사용)."""
    now = now_edt()
    if now.weekday() >= 5:
        return True
    return now.hour >= MARKET_CLOSE_HOUR_LOCAL


def get_target_price_date() -> date:
    """
    수집 대상 날짜 결정.
    - 뉴욕 17:00 이후 → 당일
    - 그 전 → 전 영업일
    """
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


# ── 중복 기사 체크 (url 기준) ────────────────────────────────
def already_published(price_date: date) -> bool:
    """해당 날짜 유가 기사가 이미 존재하는지 확인 (url 필드 기준)."""
    internal_url = f"internal://oil_price_{price_date.isoformat()}"
    res = requests.get(
        f"{_sb_articles_url()}?url=eq.{internal_url}&is_published=eq.true&select=id",
        headers=_sb_headers(),
        timeout=10,
    )
    if res.status_code in (200, 206):
        return len(res.json()) > 0
    return False


# ── 데이터 신선도 ────────────────────────────────────────────
# 실사고(2026-08-10 발견): MAX_STALE_BUSINESS_DAYS=0(당일 데이터만 허용)으로
# 2026-08-04 이후 6일 연속 기사가 0건이었다. 원인 둘:
#  ① EIA 공식 spot price 시리즈는 관례적으로 익영업일 발행이다(당일 17:30
#     EDT 종가 조회 시점엔 아직 안 나와 있는 게 정상) — 매번 lag=1로 거절됐다.
#  ② Stooq는 이제 JS 연산증명(hashcash) 봇 검증을 요구한다(`/__verify`,
#     SHA-256 PoW) — 단순 requests.get()으로는 CSV 대신 이 검증 페이지만
#     받는다. 폴백 자체가 막혀 있었다(브라우저로 직접 확인, 2026-08-10).
# ②는 스크립트만으론 못 뚫으므로 손대지 않음(EIA가 1차 소스라 ①만 풀어도
# 정상화될 것으로 판단). article 본문은 prices["date"](실제 데이터 날짜)를
# "N일(현지시간)"으로 그대로 쓰므로, 하루 지연 데이터를 받아도 "어제 종가를
# 오늘 기사"로 둔갑시키는 게 아니라 정직하게 그 날짜로 표기된다 — 즉 1일
# 허용은 정확성을 해치지 않는다.
MAX_STALE_BUSINESS_DAYS = 1


def _parse_data_date(s: str) -> date | None:
    """'YYYY-MM-DD' / 'YYYY-MM' 형태 문자열을 date로 변환. 실패 시 None."""
    s = (s or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _business_days_between(older: date, newer: date) -> int:
    """older~newer 사이 영업일 수(주말 제외). newer가 더 과거면 음수."""
    if older == newer:
        return 0
    sign = 1 if newer > older else -1
    a, b = (older, newer) if newer > older else (newer, older)
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n * sign


def _accept_prices(src_name: str, wti: tuple, brent: tuple, target: date) -> dict | None:
    """소스 결과를 신선도 검사 후 채택. 부적합하면 None(→ 다음 소스로 폴백)."""
    wti_t, wti_p, wti_d = wti
    brent_t, brent_p, brent_d = brent

    if not (wti_t and wti_p and brent_t and brent_p):
        return None

    if wti_d is None or brent_d is None:
        print(f"  [SKIP] {src_name}: 데이터 날짜 확인 불가 → 사용 안 함")
        return None

    data_date = min(wti_d, brent_d)
    lag = _business_days_between(data_date, target)

    if lag < 0:
        print(f"  [SKIP] {src_name}: 데이터 날짜({data_date})가 대상({target})보다 미래")
        return None
    if lag > MAX_STALE_BUSINESS_DAYS:
        print(f"  [SKIP] {src_name}: 데이터 정체 — 최신 {data_date}, 대상 {target} ({lag}영업일 뒤처짐)")
        return None
    return {
        "wti":    _calc(wti_t, wti_p),
        "brent":  _calc(brent_t, brent_p),
        "date":   data_date,
        "source": src_name,
    }


# ── 유가 데이터 수집 ─────────────────────────────────────────
# ⚠️ Stooq 봇 검증 시도·실패 기록 (2026-08-10). 단순 GET에는 CSV 대신 JS
# 연산증명(해시캐시) 챌린지가 온다. 챌린지 자체(SHA-256 브루트포스로 앞 d자리
# 0인 해시 찾기, POST /__verify)는 Python으로 재현해 실제로 통과시켰다
# (verify 응답 "ok" 확인). 하지만 통과 후에도 실제 CSV 요청은 매번 빈 응답
# (Content-Length 0, "attachment;filename=error.txt")만 돌아왔고, auth
# 쿠키가 요청마다 계속 재발급되는 걸 봐서는 PoW보다 더 깊은 방어(세션 검증,
# 요청 패턴 분석 등)가 있는 것으로 보인다. 여기서 더 파고드는 건 투입 대비
# 실효가 낮다고 판단해 재현 코드는 되돌리고 원래 방식(단순 GET, 실패 시
# None 반환)으로 둔다 — 폴백이 계속 막혀 있어도 1차 소스인 EIA가 정상화되면
# 문제없다.
def fetch_alphavantage(function: str, retries: int = 1) -> tuple[float | None, float | None, date | None]:
    """Alpha Vantage 원자재 API에서 최근 2일 종가를 가져와 (오늘, 전일, 최신 데이터 날짜) 반환.
    실사고(2026-08-17~18): EIA 데이터가 4영업일 지연 + Stooq가 봇 검증으로 막혀
    유가 기사가 며칠 연속 스킵됐다. 봇 검증이 없는 독립 소스를 EIA·Stooq 사이
    3차 폴백으로 추가."""
    if not ALPHA_VANTAGE_API_KEY:
        return None, None, None
    url = (
        f"https://www.alphavantage.co/query"
        f"?function={function}&interval=daily&apikey={ALPHA_VANTAGE_API_KEY}"
    )
    for attempt in range(retries + 1):
        try:
            res = requests.get(url, timeout=(10, 30))
            if res.status_code != 200:
                print(f"  [WARN] Alpha Vantage 조회 실패 ({function}): HTTP {res.status_code} - {res.text[:200]}")
                return None, None, None
            body = res.json()
            data = body.get("data", [])
            if len(data) < 2:
                print(f"  [WARN] Alpha Vantage 조회 실패 ({function}): 데이터 없음 - {str(body)[:200]}")
                return None, None, None
            latest, prev = data[0], data[1]
            data_date = _parse_data_date(str(latest.get("date", "")))
            return float(latest["value"]), float(prev["value"]), data_date
        except requests.exceptions.Timeout:
            if attempt < retries:
                print(f"  [WARN] Alpha Vantage 타임아웃 ({function}) → 재시도 ({attempt+1}/{retries})")
                continue
            print(f"  [WARN] Alpha Vantage 조회 실패 ({function}): 재시도 후에도 타임아웃")
            return None, None, None
        except Exception as e:
            print(f"  [WARN] Alpha Vantage 조회 실패 ({function}): {e}")
            return None, None, None
    return None, None, None


def fetch_yahoo(symbol: str) -> tuple[float | None, float | None, date | None]:
    """야후 파이낸스 비공식 차트 API에서 최신가·전일 종가를 가져와
    (오늘, 전일, 데이터 시각의 뉴욕 현지 날짜) 반환. 키 불필요.
    실사고(2026-08-21): EIA·Alpha Vantage가 동시에 2영업일 지연됐고(둘 다
    같은 8/18에서 멈춤 — 사실상 독립 소스가 아닐 가능성), Stooq는 사이트
    전체가 JS 연산증명 챌린지로 막혀 있어(8/10 확인, /db/h/ 등 다른 경로도
    동일) 기사가 또 스킵됐다. 야후 파이낸스는 봇 검증 없이 거의 실시간
    선물가를 제공해 이번에 확인, 세 소스 사이 새 폴백으로 추가."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    try:
        res = requests.get(url, timeout=(10, 30), headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200:
            print(f"  [WARN] Yahoo Finance 조회 실패 ({symbol}): HTTP {res.status_code} - {res.text[:200]}")
            return None, None, None
        results = res.json().get("chart", {}).get("result") or []
        if not results:
            print(f"  [WARN] Yahoo Finance 조회 실패 ({symbol}): 결과 없음")
            return None, None, None
        meta = results[0].get("meta", {})
        today_price = meta.get("regularMarketPrice")
        prev_price = meta.get("chartPreviousClose")
        ts = meta.get("regularMarketTime")
        if today_price is None or prev_price is None or ts is None:
            print(f"  [WARN] Yahoo Finance 조회 실패 ({symbol}): 필드 누락 - {str(meta)[:200]}")
            return None, None, None
        data_date = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone(ZoneInfo("America/New_York"))
            .date()
        )
        return float(today_price), float(prev_price), data_date
    except Exception as e:
        print(f"  [WARN] Yahoo Finance 조회 실패 ({symbol}): {e}")
        return None, None, None



def fetch_eia(series_id: str, retries: int = 2) -> tuple[float | None, float | None, date | None]:
    """EIA API에서 최근 2일 종가를 가져와 (오늘, 전일, 최신 데이터 날짜) 반환.
    실사고(2026-08-17~18): read timeout=15초가 너무 빡빡해서 WTI·Brent 둘 다
    타임아웃으로 실패 → Stooq 폴백은 봇 검증에 막혀있어 결국 기사가 이틀 연속
    스킵됐다. EIA는 키 문제가 아니라 단순 응답 지연이었으므로, 타임아웃을
    늘리고 일시적 지연에 대비해 짧은 재시도를 추가한다."""
    if not EIA_API_KEY:
        return None, None, None
    url = (
        f"https://api.eia.gov/v2/seriesid/{series_id}"
        f"?api_key={EIA_API_KEY}&data[0]=value&sort[0][column]=period&sort[0][direction]=desc&length=5"
    )
    for attempt in range(retries + 1):
        try:
            res = requests.get(url, timeout=(10, 30))
            if res.status_code != 200:
                print(f"  [WARN] EIA 조회 실패 ({series_id}): HTTP {res.status_code} - {res.text[:200]}")
                return None, None, None
            body = res.json()
            data = body.get("response", {}).get("data", [])
            if len(data) < 2:
                print(f"  [WARN] EIA 조회 실패 ({series_id}): 데이터 없음 - {str(body)[:200]}")
                return None, None, None
            data_date = _parse_data_date(str(data[0].get("period", "")))
            return float(data[0]["value"]), float(data[1]["value"]), data_date
        except requests.exceptions.Timeout:
            if attempt < retries:
                print(f"  [WARN] EIA 타임아웃 ({series_id}) → 재시도 ({attempt+1}/{retries})")
                continue
            print(f"  [WARN] EIA 조회 실패 ({series_id}): 재시도 후에도 타임아웃")
            return None, None, None
        except Exception as e:
            print(f"  [WARN] EIA 조회 실패 ({series_id}): {e}")
            return None, None, None
    return None, None, None


def get_oil_prices() -> dict | None:
    """WTI·Brent 종가(달러/배럴)와 전일 대비 변화량·변화율을 반환.

    소스가 반환한 실제 데이터 날짜를 검사해, 대상 날짜와 어긋나면
    그 소스를 버리고 다음 소스로 넘어간다.
    모든 소스가 정체돼 있으면 None(=기사 생성 중단)을 반환한다.
    """
    target = get_target_price_date()

    if EIA_API_KEY:
        print("  → EIA API 조회 시도...")
        wti = fetch_eia(EIA_SERIES["WTI"])
        time.sleep(1)
        brent = fetch_eia(EIA_SERIES["Brent"])
        result = _accept_prices("EIA", wti, brent, target)
        if result:
            print(f"  ✓ EIA({result['date']}): WTI ${wti[0]:.2f} (전일 ${wti[1]:.2f}), "
                  f"Brent ${brent[0]:.2f} (전일 ${brent[1]:.2f})")
            return result

    print("  → Yahoo Finance 조회 시도...")
    wti = fetch_yahoo(YAHOO_SYMBOLS["WTI"])
    time.sleep(1)
    brent = fetch_yahoo(YAHOO_SYMBOLS["Brent"])
    result = _accept_prices("Yahoo Finance", wti, brent, target)
    if result:
        print(f"  ✓ Yahoo Finance({result['date']}): WTI ${wti[0]:.2f} (전일 ${wti[1]:.2f}), "
              f"Brent ${brent[0]:.2f} (전일 ${brent[1]:.2f})")
        return result

    if ALPHA_VANTAGE_API_KEY:
        print("  → Alpha Vantage 조회 시도...")
        wti = fetch_alphavantage(ALPHA_VANTAGE_FUNCTIONS["WTI"])
        time.sleep(1)
        brent = fetch_alphavantage(ALPHA_VANTAGE_FUNCTIONS["Brent"])
        result = _accept_prices("Alpha Vantage", wti, brent, target)
        if result:
            print(f"  ✓ Alpha Vantage({result['date']}): WTI ${wti[0]:.2f} (전일 ${wti[1]:.2f}), "
                  f"Brent ${brent[0]:.2f} (전일 ${brent[1]:.2f})")
            return result

    print("  [ERROR] 신선한 유가 데이터 확보 실패 → 기사 생성 중단")
    return None


def _calc(today: float, prev: float) -> dict:
    change = today - prev
    pct    = (change / prev * 100) if prev else 0.0
    return {"price": today, "prev": prev, "change": change, "pct": pct}


# ── Gemini 호출 ──────────────────────────────────────────────
def call_gemini(prompt: str, max_tokens: int = 1500, start_tier: int = 2) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.15, timeout=(10, 60))


def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
    """이름과 충분히 비슷한 위키 문서 제목이 하나라도 있으면 True (결정론적 조회)."""
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
    """판단이 아니라 단순 추출 — LLM 위험도가 낮은 작업이라 위키 조회 대상을 뽑는 데만 쓴다."""
    if not body:
        return []
    prompt = f"""아래 기사 본문에서 실제 존재 여부를 확인해볼 만한 구체적 고유명사를 추출하세요.
영화·도서·게임 등 작품명, 특정 인물 실명, 특정 기관·단체·기업명만 대상으로 합니다.
국가명·일반 지명(도시·나라)이나 흔한 일반명사·직함은 제외하세요.
쉼표로 구분해 나열만 하세요(설명 금지). 대상이 없으면 "없음"이라고만 답하세요.

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
    """생성된 본문에 원문 자료에 없는 고유명사(작품명·인명·지명·기관명)가 새로 등장했는지 확인.
    두 신호를 같이 쓴다: ① 원본 자료 대조(Gemini 판단) ② 위키피디아 독립 조회(단순 추출 +
    결정론적 HTTP 조회 — Gemini가 오판해도 이 신호는 별개로 남는다). gemini_writer.py의
    동명 함수와 동일 로직(실사고 2026-08-16, id=79327)."""
    if not body:
        return ""
    check_prompt = f"""아래는 기사 작성에 쓰인 원본 자료와, 그걸 바탕으로 생성된 한국어 기사 본문입니다.
기사 본문에 나오는 고유명사(영화·도서·게임 등 작품명, 인명, 지명, 기관명)가 원본 자료에
실제로 근거하는지 확인하세요. 정상적인 한글 음차나 공식 번역명은 문제가 아닙니다 —
원본 자료에 등장하는 대상을 다른 이름으로 완전히 잘못 지어낸 경우만 찾으세요.
그런 이름이 있으면 "지어낸이름 → 원본표기" 형식으로 쉼표 구분해 나열하세요.
없으면 "없음"이라고만 답하세요.

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


# ── 기사 프롬프트 ────────────────────────────────────────────
def build_article_prompt(prices: dict) -> str:
    wti   = prices["wti"]
    brent = prices["brent"]
    pdate = prices["date"]
    src   = prices["source"]

    wti_dir   = "상승" if wti["change"]   > 0 else ("하락" if wti["change"]   < 0 else "보합")
    wti_dollar   = int(round(wti["price"]))

    # 실제 데이터 출처에 맞는 한국어 인용구. src와 무관하게 항상 EIA를
    # 인용하면 Alpha Vantage·Stooq에서 받은 날도 EIA를 인용한 것처럼
    # 기사에 나가는 오귀속이 생긴다.
    # 2026-08-28 사용자 결정: EIA(미국 정부 공식 기관)는 이름을 그대로 밝히되,
    # Yahoo Finance·Alpha Vantage처럼 비공식 민간 집계 소스는 이름을 노출하지
    # 않고 "시장 집계에 따르면"으로 순화한다 — EIA가 간헐적으로 타임아웃돼
    # 폴백이 자주 발동되는데, 비공식 소스명이 기사에 그대로 노출되는 게
    # 부적절하다는 판단.
    src_citation = {
        "EIA": "미국 에너지정보청(EIA)에 따르면",
    }.get(src, "시장 집계에 따르면")

    # 2026-08-26 도입: 가격 변동 배경(③)을 예전엔 Gemini 자체 지식으로만
    # 추정 서술해야 했다(근거가 없어 "~로 풀이된다" 헤지 표현 강제). 구글
    # 뉴스에서 실제 헤드라인을 가져와 검증 가능한 근거로 쓴다. 추가 LLM
    # 호출·쿼터 비용 없음(단순 RSS 조회). 실패해도 빈 리스트라 기존
    # 방식(추정 서술)으로 조용히 폴백한다.
    headlines = fetch_headlines("crude oil price OPEC WTI Brent", limit=5)
    if headlines:
        headline_block = "\n[관련 실제 보도 헤드라인 (참고용)]\n" + "\n".join(f"- {h}" for h in headlines)
        bg_instruction = (
            "③ 변동 배경: 위 [관련 실제 보도 헤드라인]에 나온 사건·원인(예: OPEC+ 결정, 재고 발표, "
            "지정학적 사건)만 근거로 구체적으로 서술하세요. ⚠️ 헤드라인은 최신순 검색 결과라 오늘자가 "
            "아닐 수 있습니다 — 헤드라인에 나온 가격 수치(예: \"$70 붕괴\", \"$120 근접\")는 옛 시점 "
            "값일 수 있으니 절대 인용하지 마세요. 가격 수치는 오직 위 [유가 데이터]만 쓰고, 헤드라인은 "
            "'왜'(원인·사건)에만 쓰세요. ⚠️ 헤드라인에 없는 정책·제도·규제 조치를 지어내지 마세요(2026-08-26 "
            "실사고 — opinet_price_writer.py에서 헤드라인에 없는 \"정부 최고가격제 시행 검토\"를 지어내고 "
            "사실관계도 틀렸던 사례 참조). 헤드라인 중 실제로 관련된 내용이 하나도 없으면 억지로 배경을 "
            "채우지 말고 짧게 \"OPEC+ 정책과 글로벌 수급 상황에 따라 변동성을 보이고 있다\" 정도로만 "
            "간단히 쓰세요. 특정 매체를 출처로 직접 인용하지는 말고 \"~인 것으로 알려졌다\", "
            "\"~라는 보도가 나왔다\"처럼 자연스럽게 녹여 쓰세요."
        )
    else:
        headline_block = ""
        bg_instruction = "③ 변동 배경: OPEC+ 동향, 미국 원유 재고, 달러 지수, 지정학 요인 등"

    return f"""당신은 에너지·원자재 전문 기자입니다.
아래 유가 데이터를 바탕으로 뉴스 기사를 작성하세요.

[유가 데이터] (출처: {src})
- 기준일: {pdate.day}일(현지시간) — 뉴욕상업거래소(NYMEX) 종가
- WTI 원유: 배럴당 ${wti['price']:.2f} (전일比 {wti['pct']:+.2f}%, {'+' if wti['change']>0 else ''}{wti['change']:.2f}달러)
- 브렌트유: 배럴당 ${brent['price']:.2f} (전일比 {brent['pct']:+.2f}%, {'+' if brent['change']>0 else ''}{brent['change']:.2f}달러)
- WTI 전일 종가: ${wti['prev']:.2f} / 브렌트 전일 종가: ${brent['prev']:.2f}
{headline_block}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "[국제유가] "로 시작. 대괄호 포함 그대로 출력.
- "[국제유가] WTI 배럴당 {wti_dollar}달러…<핵심 동인>" 형태, 대괄호 포함 50자 이내
- 예: "[국제유가] WTI 배럴당 78달러…OPEC+ 감산 기대"
      "[국제유가] 이틀째 {wti_dir}…WTI 배럴당 {wti_dollar}달러대"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '시사합니다'
2. 구조:
   ① 리드: "국제유가가 {pdate.day}일(현지시간) {wti_dir}했다. WTI 원유 선물은 뉴욕상업거래소(NYMEX)에서 배럴당 {wti['price']:.2f}달러에 거래를 마쳤다."
      전일 종가를 다시 언급할 땐 "전일 종가는 X달러였으며, 전일 대비"처럼 "전일"을 두 번
      쓰지 말고 "전일 종가(X달러) 대비"로 한 번만 쓰세요.
   ② 브렌트유 가격·전일 대비 수치 (①과 같은 방식으로 "전일 종가(X달러) 대비"로 표기)
   {bg_instruction}
   ④ 원유 수출국·신흥시장 영향 (사우디아라비아, 나이지리아, 러시아 등 최소 1개국)
   ⑤ 향후 주시 요인 (사실 기반)
3. 날짜: "{pdate.day}일(현지시간)" 형식만. "오늘", "현재", 절대연도 금지.
4. 출처: "{src_citation}"는 ①·② 가격 수치를 언급하는 문장 중 **딱 한 곳에만** 자연스럽게
   붙이세요("뉴욕상업거래소(NYMEX)에서"도 마찬가지). 없는 거래소·기관 이름을 지어내
   덧붙이지 마세요 — 위 [유가 데이터]에 나온 출처명 그대로만 쓰세요.
   ③번 변동 배경은 이 가격 데이터 출처가 제공하는 정보가 아니므로, 가격 데이터 출처를
   원인 분석의 근거인 것처럼 잘못 인용하지 마세요. [관련 실제 보도 헤드라인]이 있으면
   그 내용을 근거로 구체적으로 쓰고, 없으면 "~인 것으로 풀이된다", "~로 분석된다"처럼
   추정 표현으로 쓰세요.
5. 비라틴 문자 국가명·지명은 한국어 음역.
6. 분량: 700자 이상.
7. 위 ①~⑤는 서로 다른 문단입니다. 문단 사이에는 반드시 빈 줄을 하나씩 넣어
   구분하세요(①②는 가격 리드이니 한 문단으로 묶어도 되지만, ③·④·⑤는 각각 별도
   문단). 전체를 한 문단으로 이어 쓰지 마세요.
"""


# ── 파싱 ─────────────────────────────────────────────────────
TITLE_PREFIX = "[국제유가]"


def enforce_title_prefix(title: str) -> str:
    """제목 앞에 [국제유가] 태그를 강제 부착. 중복 부착 방지."""
    t = (title or "").strip()
    if not t:
        return t
    # 이미 붙어 있으면(공백 유무 무관) 정규화만
    m = re.match(r"^\s*\[\s*국제\s*유가\s*\]\s*(.*)$", t)
    if m:
        t = m.group(1).strip()
    else:
        # "국제유가, ..." / "국제유가 이틀째 ..." 등 기존 형태의 접두 표현 제거
        m2 = re.match(r"^국제유가(?:가|는)?\s*[,·]?\s+(.+)$", t)
        if m2 and m2.group(1)[:1] not in ("와", "과", "및"):
            t = m2.group(1).strip()
        else:
            t = re.sub(r"^국제유가\s*[,·]\s*", "", t).strip()
    return f"{TITLE_PREFIX} {t}" if t else TITLE_PREFIX


def parse_article_output(text: str) -> tuple[str, str]:
    title, body = "", ""
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    m_body  = re.search(r"BODY:\s*([\s\S]+)", text)
    if m_title:
        title = m_title.group(1).strip()
    if m_body:
        body = m_body.group(1).strip()
    return title, body


def has_column_style(text: str) -> bool:
    patterns = ["주목됩니다", "기대됩니다", "보여줍니다", "시사합니다", "중요합니다"]
    return any(p in text for p in patterns)


# ── 대표 이미지 ──────────────────────────────────────────────
# 유가 기사는 주제가 매일 동일해서 Gemini로 키워드를 뽑을 이유가 없다.
# 고정 키워드 풀을 가격일 기준으로 회전시켜 API 호출 없이 매일 다른 사진을 쓴다.
_OIL_IMAGE_KEYWORDS = [
    "oil refinery",
    "crude oil barrel",
    "oil pump jack",
    "offshore oil rig",
    "petroleum industry",
    "oil tanker ship",
    "oil pipeline",
    "gas station fuel",
]


def fetch_oil_image(price_date: date) -> str:
    """Pixabay에서 유가 기사용 대표 이미지를 받아 R2에 영구 저장한다.
    실패 시 빈 문자열(이미지 없이 발행)."""
    if not PIXABAY_API_KEY:
        return ""
    seed = price_date.toordinal()
    query = _OIL_IMAGE_KEYWORDS[seed % len(_OIL_IMAGE_KEYWORDS)]
    try:
        res = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "safesearch": "true",
                "per_page": 10,
            },
            timeout=15,
        )
        if res.status_code != 200:
            print(f"  ⚠️ Pixabay {res.status_code}: {res.text[:100]}")
            return ""
        hits = res.json().get("hits", [])
        if not hits:
            print(f"  ⚠️ Pixabay 결과 없음: {query}")
            return ""
        hit = hits[seed % len(hits)]
        raw_url = hit.get("largeImageURL", "")
        if not raw_url:
            return ""
        url = store_image(raw_url, key_hint=f"oil_{price_date.isoformat()}")
        print(f"  🖼️ 이미지: {query} → {url[:70]}")
        return url or ""
    except Exception as e:
        print(f"  ⚠️ Pixabay 실패: {e}")
    return ""


# ── 기사 삽입 ────────────────────────────────────────────────
def insert_article(title_ko: str, summary_ko: str, prices: dict, image_url: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    now_str    = now_kst().strftime("%Y-%m-%d %H:%M")
    price_date = prices["date"].isoformat()
    internal_url = f"internal://oil_price_{price_date}"

    payload = {
        "title_en":           title_ko,
        "title_ko":           title_ko,
        "summary_en":         "",
        "summary_ko":         summary_ko,
        "url":                internal_url,
        "source":             "NewsFinal",
        "category":           "경제",
        "subcategory":        "국제유가",
        "region":             "글로벌",
        "country":            "국제",
        "country_flag":       "🛢️",
        "image_url":          image_url,
        "countries":          ["미국", "사우디아라비아", "러시아"],
        "score":              1,
        "created_at":         now_str,
        "first_published_at": now_str,
        "update_log":         [{"timestamp": now_str, "note": "유가 자동 기사"}],
        "sent_telegram":      0,
        "is_published":       True,
    }

    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0].get("id", -1) if data else -1
    print(f"  [ERROR] 기사 삽입 실패 {res.status_code}: {res.text[:200]}")
    return -1


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[oil_price_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not market_is_closed():
        edt_now = now_edt()
        print(f"  → 뉴욕 시장 미종료 ({edt_now.strftime('%H:%M')} EDT) → 스킵")
        return

    price_date = get_target_price_date()
    print(f"  → 대상 날짜: {price_date.isoformat()} (뉴욕 현지시간)")

    if already_published(price_date):
        print(f"  → {price_date} 유가 기사 이미 존재 → 스킵")
        return

    prices = get_oil_prices()
    if not prices:
        print("  [ERROR] 유가 데이터 수집 실패 → 종료")
        return

    # 실제 데이터 날짜가 대상 날짜와 다르면 그 날짜로 중복 재검사
    # (소스 정체 시 같은 수치의 기사를 반복 생성하는 것을 차단)
    if prices["date"] != price_date:
        if already_published(prices["date"]):
            print(f"  → {prices['date']} 유가 기사 이미 존재 → 스킵")
            return
        price_date = prices["date"]

    print("  → Gemini로 기사 생성 중...")
    prompt       = build_article_prompt(prices)
    # thinking 토큰이 maxOutputTokens 예산을 나눠 쓰는 문제(opinet_price_writer.py에서
    # 먼저 발견, 2026-08-18) — 이 스크립트도 8/20 실측으로 동일 증상 확인.
    # non-lite 대신 lite 티어로 바로 시작 + max_tokens 상향으로 대응.
    article_text = call_gemini(prompt, max_tokens=2500, start_tier=3)
    time.sleep(8)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    if has_column_style(article_text):
        print("  ⚠️ 논평체 감지 → 재생성")
        article_text = call_gemini(
            prompt + "\n\n[재작성 지시] 논평/칼럼 문체가 섞였습니다. 사실 전달 중심으로만 다시 작성하세요.",
            max_tokens=2500, start_tier=3,
        ) or article_text
        time.sleep(5)

    fabricated = verify_no_fabricated_names(prompt, article_text)
    if fabricated:
        print(f"  ⚠️ 원문에 없는 고유명사 감지({fabricated}) → 재생성")
        article_text = call_gemini(
            prompt + f"\n\n[재작성 지시] 다음 이름을 원문에 없는 표현으로 잘못 지어냈습니다: {fabricated}. "
                     "고유명사는 원본 자료에 나온 표기를 그대로 옮기고, 확신할 수 없으면 지어내지 말고 원문 표기를 그대로 쓰세요.",
            max_tokens=2500, start_tier=3,
        ) or article_text
        time.sleep(5)

    art_title, art_body = parse_article_output(article_text)
    art_title = enforce_title_prefix(art_title)

    if not art_title or not art_body:
        print(f"  [ERROR] TITLE/BODY 파싱 실패\n{article_text[:300]}")
        return

    if len(art_body) < 500:
        print(f"  ⚠️ 본문 너무 짧음 ({len(art_body)}자) → 스킵")
        return

    print(f"  → 제목: {art_title}")
    print(f"  → 본문 {len(art_body)}자")

    image_url = fetch_oil_image(prices["date"])

    art_id = insert_article(art_title, art_body, prices, image_url)
    if art_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={art_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[oil_price_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
