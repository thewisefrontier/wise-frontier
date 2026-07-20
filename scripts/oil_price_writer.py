"""
oil_price_writer.py
-------------------
WTI·Brent 국제유가 일일 변동을 모니터링하여 뉴스 기사를 자동 생성합니다.

데이터 소스 (우선순위):
  1. EIA API (에너지정보청 공식, GEMINI_EIA_KEY or EIA_API_KEY 환경변수)
  2. Stooq CSV (키 불필요 fallback)

실행: python scripts/oil_price_writer.py
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# ── 설정 ────────────────────────────────────────────────────
GEMINI_MODEL         = "gemini-2.5-flash"
SUPABASE_URL         = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
EIA_API_KEY          = os.getenv("EIA_API_KEY", "")  # https://www.eia.gov/opendata/ 무료 등록

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

_current_key_idx = 0
_exhausted_keys: set = set()

KST = timezone(timedelta(hours=9))
EDT = ZoneInfo("America/New_York")  # 뉴욕 시장 기준

# 뉴욕 시장 종가 기준: 현지 17:00 이후에만 실행
MARKET_CLOSE_HOUR_LOCAL = 17   # 17:00 EDT/EST
# 같은 날 기사 중복 방지를 위한 subcategory 접두어
CLUSTER_KEY_PREFIX = "oil_price_"

# Stooq 심볼
STOOQ_SYMBOLS = {
    "WTI":   "@CL.F",
    "Brent": "@BZ.F",
}

# EIA 시리즈 ID
EIA_SERIES = {
    "WTI":   "PET.RWTC.D",    # WTI Crude Oil Spot Price (Dollars per Barrel)
    "Brent": "PET.RBRTE.D",   # Brent Europe Crude Oil Spot Price
}


# ── 헬퍼 ────────────────────────────────────────────────────
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
    # 주말이면 종가 확정 상태로 간주
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
        # 주말: 직전 금요일
        days_back = now.weekday() - 4
        return (now - timedelta(days=days_back)).date()
    if now.hour >= MARKET_CLOSE_HOUR_LOCAL:
        return now.date()
    # 17:00 미만 → 전 영업일
    prev = now - timedelta(days=1)
    # 월요일이면 금요일
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev.date()


# ── 중복 기사 체크 ───────────────────────────────────────────
def already_published(price_date: date) -> bool:
    """해당 날짜 유가 기사가 이미 존재하는지 확인."""
    cluster_key = f"{CLUSTER_KEY_PREFIX}{price_date.isoformat()}"
    res = requests.get(
        f"{_sb_articles_url()}?subcategory=eq.{cluster_key}&is_published=eq.true&select=id",
        headers=_sb_headers(),
        timeout=10,
    )
    if res.status_code in (200, 206):
        return len(res.json()) > 0
    return False


# ── 유가 데이터 수집 ─────────────────────────────────────────

def fetch_stooq(symbol: str) -> tuple[float | None, float | None]:
    """
    Stooq CSV에서 최근 2일 종가를 가져와 (오늘, 전일) 반환.
    """
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    try:
        res = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200:
            return None, None
        lines = [l for l in res.text.strip().splitlines() if l and not l.startswith("Date")]
        if len(lines) < 2:
            return None, None
        # 마지막 두 줄: [날짜, 시가, 고가, 저가, 종가, 거래량]
        latest  = lines[-1].split(",")
        prev    = lines[-2].split(",")
        close_today = float(latest[4]) if len(latest) > 4 else None
        close_prev  = float(prev[4])   if len(prev)  > 4 else None
        return close_today, close_prev
    except Exception as e:
        print(f"  [WARN] Stooq 조회 실패 ({symbol}): {e}")
        return None, None


def fetch_eia(series_id: str) -> tuple[float | None, float | None]:
    """
    EIA API에서 최근 2일 종가를 가져와 (오늘, 전일) 반환.
    EIA_API_KEY 환경변수 필요 (https://www.eia.gov/opendata/ 무료 등록).
    """
    if not EIA_API_KEY:
        return None, None
    url = (
        f"https://api.eia.gov/v2/seriesid/{series_id}"
        f"?api_key={EIA_API_KEY}&data[0]=value&sort[0][column]=period&sort[0][direction]=desc&length=5"
    )
    try:
        res = requests.get(url, timeout=15)
        if res.status_code != 200:
            return None, None
        data = res.json().get("response", {}).get("data", [])
        if len(data) < 2:
            return None, None
        return float(data[0]["value"]), float(data[1]["value"])
    except Exception as e:
        print(f"  [WARN] EIA 조회 실패 ({series_id}): {e}")
        return None, None


def get_oil_prices() -> dict | None:
    """
    WTI·Brent 종가(달러/배럴)와 전일 대비 변화량·변화율을 반환.
    반환 형식:
      {
        "wti":   {"price": 78.45, "prev": 77.90, "change": 0.55, "pct": 0.71},
        "brent": {"price": 82.10, "prev": 81.50, "change": 0.60, "pct": 0.74},
        "date":  date(2025, 1, 15),
        "source": "EIA" | "Stooq",
      }
    """
    result = {}

    # ── EIA 우선 ──
    if EIA_API_KEY:
        print("  → EIA API 조회 시도...")
        wti_t, wti_p = fetch_eia(EIA_SERIES["WTI"])
        time.sleep(1)
        brent_t, brent_p = fetch_eia(EIA_SERIES["Brent"])
        if wti_t and wti_p and brent_t and brent_p:
            result = {
                "wti":   _calc(wti_t, wti_p),
                "brent": _calc(brent_t, brent_p),
                "date":  get_target_price_date(),
                "source": "EIA",
            }
            print(f"  ✓ EIA: WTI ${wti_t:.2f} (전일 ${wti_p:.2f}), Brent ${brent_t:.2f} (전일 ${brent_p:.2f})")
            return result

    # ── Stooq fallback ──
    print("  → Stooq 조회 시도...")
    wti_t, wti_p = fetch_stooq(STOOQ_SYMBOLS["WTI"])
    time.sleep(1)
    brent_t, brent_p = fetch_stooq(STOOQ_SYMBOLS["Brent"])

    if wti_t and wti_p and brent_t and brent_p:
        result = {
            "wti":   _calc(wti_t, wti_p),
            "brent": _calc(brent_t, brent_p),
            "date":  get_target_price_date(),
            "source": "Stooq",
        }
        print(f"  ✓ Stooq: WTI ${wti_t:.2f} (전일 ${wti_p:.2f}), Brent ${brent_t:.2f} (전일 ${brent_p:.2f})")
        return result

    print("  [ERROR] 모든 소스 유가 데이터 수집 실패")
    return None


def _calc(today: float, prev: float) -> dict:
    change = today - prev
    pct    = (change / prev * 100) if prev else 0.0
    return {"price": today, "prev": prev, "change": change, "pct": pct}


# ── Gemini 호출 ──────────────────────────────────────────────
def call_gemini(prompt: str, max_tokens: int = 1500, use_search: bool = False) -> str | None:
    global _current_key_idx, _exhausted_keys

    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": max_tokens,
        },
    }
    if use_search:
        payload["tools"] = [{"google_search": {}}]

    n         = len(GEMINI_API_KEYS)
    available = [i for i in range(n) if i not in _exhausted_keys]
    if not available:
        print("[ERROR] 모든 키 RPD 소진")
        return None

    ordered = sorted(available, key=lambda i: (i - _current_key_idx) % n)

    for idx in ordered:
        api_key = GEMINI_API_KEYS[idx]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={api_key}"
        )
        try:
            res = requests.post(url, json=payload, timeout=(10, 60))
            if res.status_code == 200:
                _current_key_idx = (idx + 1) % n
                cands = res.json().get("candidates", [])
                if not cands:
                    return None
                parts = cands[0].get("content", {}).get("parts", [])
                text  = "".join(p.get("text", "") for p in parts).strip()
                return text if text else None
            elif res.status_code == 429:
                print(f"  [429] 키 {idx+1} RPD 소진")
                _exhausted_keys.add(idx)
                continue
            elif res.status_code == 503:
                print(f"  [503] 키 {idx+1} 과부하 → 다음 키")
                continue
            else:
                print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                return None
        except requests.exceptions.Timeout:
            print(f"  [TIMEOUT] 키 {idx+1}")
            continue
        except Exception as e:
            print(f"[ERROR] {e}")
            return None

    return None


# ── 기사 프롬프트 ────────────────────────────────────────────
def build_article_prompt(prices: dict) -> str:
    wti   = prices["wti"]
    brent = prices["brent"]
    pdate = prices["date"]
    src   = prices["source"]

    wti_dir   = "상승" if wti["change"]   > 0 else ("하락" if wti["change"]   < 0 else "보합")
    brent_dir = "상승" if brent["change"] > 0 else ("하락" if brent["change"] < 0 else "보합")

    # 제목용 반올림 달러 (소수점 없이)
    wti_dollar   = int(round(wti["price"]))
    brent_dollar = int(round(brent["price"]))

    return f"""당신은 연합뉴스 에너지·원자재 담당 기자입니다.
먼저 google_search 도구로 "{pdate.day}일 국제유가 WTI Brent" 또는 "oil price {pdate.strftime('%B %d')} WTI Brent" 를 검색하여 당일 시장 동향을 파악한 뒤, 아래 데이터와 결합해 기사를 작성하세요.

[유가 데이터] (출처: {src} / 에너지정보청(EIA))
- 기준일: {pdate.day}일(현지시간) — 뉴욕상업거래소(NYMEX) 종가
- WTI 원유: 배럴당 ${wti['price']:.2f} (전일比 {wti['pct']:+.2f}%, {'+' if wti['change']>0 else ''}{wti['change']:.2f}달러)
- 브렌트유: 배럴당 ${brent['price']:.2f} (전일比 {brent['pct']:+.2f}%, {'+' if brent['change']>0 else ''}{brent['change']:.2f}달러)
- WTI 전일 종가: ${wti['prev']:.2f} / 브렌트 전일 종가: ${brent['prev']:.2f}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목 작성 규칙]
- 패턴: "국제유가, WTI 배럴당 {wti_dollar}달러…<핵심 동인 3~8자>" 형태
- 예시: "국제유가, WTI 배럴당 78달러…OPEC+ 감산 기대 반영"
        "국제유가 하락, 브렌트 배럴당 81달러…달러 강세·수요 우려"
        "국제유가 이틀째 {wti_dir}…WTI 배럴당 {wti_dollar}달러대"
- 줄임표(…) 뒤에 핵심 원인 한 가지를 간결하게 명시
- 50자 이내, 숫자는 반드시 포함

[본문 작성 규칙]
1. 문체: 연합뉴스 스트레이트 뉴스. 모든 문장 "-다" 종결. 감정·논평 표현 전면 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '시사합니다', '중요합니다', '~것으로 보인다'(단독 논평 시)
2. 구조 (역피라미드):
   ① 리드(1~2문장): "국제유가가 {pdate.day}일(현지시간) {wti_dir}했다. WTI 원유 선물은 뉴욕상업거래소(NYMEX)에서 배럴당 {wti['price']:.2f}달러에 거래를 마쳤다." 형태로 시작
   ② 브렌트유 가격 및 전일 대비 수치 명시
   ③ 변동 원인(검색 결과 기반): OPEC+ 동향, 미국 원유 재고, 달러 지수, 지정학 리스크 등 구체적 사실
   ④ 원유 수출국·신흥시장 영향: 사우디아라비아, 나이지리아, 러시아 등 최소 1개국 언급
   ⑤ 향후 시장 주시 요인(사실 기반, 논평 아님)
3. 날짜: "{pdate.day}일(현지시간)" 형식만 허용. "오늘", "현재", 절대연도(예: 2025년) 금지.
4. 출처: 첫 단락 또는 두 번째 단락에 "에너지정보청(EIA)에 따르면" 또는 "뉴욕상업거래소(NYMEX)에서" 반드시 포함.
5. 비라틴 문자 국가명·지명은 한국어 음역 필수.
6. 분량: 700자 이상.
"""


# ── 파싱 ─────────────────────────────────────────────────────
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


# ── 기사 삽입 ────────────────────────────────────────────────
def insert_article(title_ko: str, summary_ko: str, prices: dict) -> int:
    now_str     = now_kst().strftime("%Y-%m-%d %H:%M")
    price_date  = prices["date"].isoformat()
    cluster_key = f"{CLUSTER_KEY_PREFIX}{price_date}"

    payload = {
        "title_en":           title_ko,
        "title_ko":           title_ko,
        "summary_en":         "",
        "summary_ko":         summary_ko,
        "url":                f"internal://{cluster_key}",
        "source":             "NewsFinal",
        "category":           "경제",
        "subcategory":        cluster_key,
        "region":             "글로벌",
        "country":            "국제",
        "country_flag":       "🛢️",
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

    # 1. 뉴욕 시장 종료 여부 확인
    if not market_is_closed():
        edt_now = now_edt()
        print(f"  → 뉴욕 시장 미종료 ({edt_now.strftime('%H:%M')} EDT) → 스킵")
        return

    # 2. 대상 날짜 결정
    price_date = get_target_price_date()
    print(f"  → 대상 날짜: {price_date.isoformat()} (뉴욕 현지시간)")

    # 3. 중복 체크
    if already_published(price_date):
        print(f"  → {price_date} 유가 기사 이미 존재 → 스킵")
        return

    # 4. 유가 데이터 수집
    prices = get_oil_prices()
    if not prices:
        print("  [ERROR] 유가 데이터 수집 실패 → 종료")
        return

    # 5. 기사 생성
    print("  → Gemini로 기사 생성 중...")
    prompt       = build_article_prompt(prices)
    article_text = call_gemini(prompt, max_tokens=1500, use_search=True)
    time.sleep(8)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    # 논평체 감지
    if has_column_style(article_text):
        print("  ⚠️ 논평체 감지 → 재생성")
        retry_prompt = (
            prompt
            + "\n\n[재작성 지시] 논평/칼럼 문체가 섞였습니다. "
              "사실 전달 중심으로만 다시 작성하세요."
        )
        article_text = call_gemini(retry_prompt, max_tokens=1500, use_search=False) or article_text
        time.sleep(5)

    art_title, art_body = parse_article_output(article_text)

    if not art_title or not art_body:
        print(f"  [ERROR] TITLE/BODY 파싱 실패\n{article_text[:300]}")
        return

    if len(art_body) < 500:
        print(f"  ⚠️ 본문 너무 짧음 ({len(art_body)}자) → 스킵")
        return

    print(f"  → 제목: {art_title}")
    print(f"  → 본문 {len(art_body)}자")

    # 6. 기사 삽입
    art_id = insert_article(art_title, art_body, prices)
    if art_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={art_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[oil_price_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
