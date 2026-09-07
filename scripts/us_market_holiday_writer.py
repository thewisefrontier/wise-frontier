"""
scripts/us_market_holiday_writer.py
------------------------------------
미국 증시(NYSE/NASDAQ) 휴장일 안내 기사 자동 생성.

사용자 요청(2026-09-07): "주말은 당연히 쉬는 거지만 근로자의 날 같은 연휴는
기사화할만 하다" — 연합인포맥스 스타일(휴장 사실 + 직전 거래일 마감 시황
요약)로 작성. 주말(토·일)은 매주 반복되는 당연한 휴장이라 대상에서 뺀다.

수치(지수 등락률·달러인덱스)는 전부 야후 파이낸스 실측값을 그대로 쓰고
Gemini 등 LLM은 개입시키지 않는다 — lotto_writer.py와 같은 이유(숫자가
핵심인 콘텐츠는 결정론적 템플릿이 안전하다).

휴장일 날짜는 NYSE 공식 캘린더(nyse.com/markets/hours-calendars)에서
매년 직접 확인해 하드코딩한다(연도가 바뀌면 갱신 필요 — HOLIDAYS_BY_YEAR
참고). 조기 폐장일(추수감사절 다음날, 7월 4일 관측일 등)은 "휴장"이 아니라
별도 취급 대상이라 이 스크립트에는 포함하지 않는다.

실행: python scripts/us_market_holiday_writer.py
권장: 뉴욕 현지 08:00~09:00 ET 경(정규장 개장 전) 1회 호출. already_published()가
같은 날짜 중복 발행을 막으므로 여러 번 호출돼도 안전하다.
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

# articles 테이블 삽입 및 Supabase 헤더/URL 공용 로직(article_store.py).
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

# 야후 파이낸스 조회 로직은 frontier_markets_writer.py 재사용(2026-08-21
# 검증된 fetch_yahoo_quote 패턴 — chartPreviousClose 버그 수정 포함).
from frontier_markets_writer import fetch_yahoo_quote

# 검증된 한국어 기사를 영어로 번역(translate_guard.py 공용화).
try:
    from translate_guard import translate_article
except Exception:
    def translate_article(title_ko: str, body_ko: str, call_gemini_fn=None, max_tokens: int = 3500):
        return "", ""

KST = timezone(timedelta(hours=9))
EDT = ZoneInfo("America/New_York")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def now_edt() -> datetime:
    return datetime.now(timezone.utc).astimezone(EDT)


# NYSE 휴장일 캘린더는 market_calendar.py로 공용화(2026-09-07,
# ny_market_open_writer.py도 같은 캘린더가 필요해 분리).
from market_calendar import holiday_name as _holiday_name, previous_trading_date


def today_holiday_name() -> str | None:
    """오늘(뉴욕 기준)이 NYSE 휴장일이면 휴장 사유명을, 아니면 None을 반환."""
    return _holiday_name(now_edt().date())


# ── 시세 데이터 ──────────────────────────────────────────────
INDICES = [
    ("다우존스30산업평균지수", "^DJI"),
    ("S&P500 지수", "^GSPC"),
    ("나스닥 지수", "^IXIC"),
]
DOLLAR_INDEX_SYMBOLS = ["DX-Y.NYB", "DX=F"]  # 지수 실패 시 선물로 폴백


def fetch_holiday_data() -> dict | None:
    # ⚠️ 2026-09-07 실측: 휴장일(비거래일)에 야후를 조회하면 chartPreviousClose
    # 유도값과 regularMarketChangePercent가 자주 어긋나며(_suspect=True) —
    # 정규 거래일 중간 조회를 겨냥해 만들어진 frontier_markets_writer.py의
    # 정합성 검사가 "휴장일이라 range=5d 창의 기준점 자체가 다르게 잡히는"
    # 상황까진 감안하지 않아서다. 이 스크립트는 이미 완결된 "직전 거래일"
    # 마감치만 다루므로(그날 중간 변동 우려 없음) regularMarketChangePercent
    # (야후가 직접 계산해 제공하는, 더 신뢰도 높은 필드)만 있으면 충분하고
    # _suspect 필터는 적용하지 않는다.
    indices = []
    for name, symbol in INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if not q or q.get("pct") is None:
            continue
        indices.append({"name": name, "symbol": symbol, **q})
    if len(indices) < len(INDICES):
        print(f"  [ERROR] 3대 지수 중 {len(indices)}개만 수집됨")
        return None

    dollar = None
    for symbol in DOLLAR_INDEX_SYMBOLS:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if q and q.get("pct") is not None:
            dollar = {"symbol": symbol, **q}
            break

    return {"indices": indices, "dollar": dollar}


# ── 기사 조립(결정론적 템플릿 — Gemini 미사용) ────────────────────
def build_article(holiday_name: str, holiday_date: date, prev_date: date, data: dict) -> tuple[str, str]:
    title = f"미국 증시, {holiday_date.day}일 '{holiday_name.split('(')[0]}'로 휴장"

    prev_str = f"{prev_date.month}월 {prev_date.day}일"
    lines = [
        f"미국 금융시장은 {holiday_date.day}일(현지시간) '{holiday_name}'을 맞아 하루 동안 휴장한다.",
        "",
        "이에 따라 이날 미국 주식시장과 채권시장 거래는 진행되지 않는다.",
        "",
        f"직전 장인 지난 {prev_str} 미국 3대 주가지수는 다음과 같이 마감했다.",
        "",
    ]

    # 연합인포맥스 스타일(사용자 제시 예시): 부호 대신 오른 폭/내린 폭을
    # 자연스러운 한국어 동사(밀렸다/올랐다)로 표현한다. 같은 방향으로 움직인
    # 지수는 한 문장에 묶어 "X%, Y% 밀렸다"처럼 쓰고, 방향이 갈리면 지수마다
    # 문장을 나눈다.
    dow, sp, nasdaq = data["indices"][0], data["indices"][1], data["indices"][2]
    if (dow["pct"] < 0) == (sp["pct"] < 0):
        verb = "밀렸다" if dow["pct"] < 0 else "올랐다"
        dow_sp_sentence = f"{dow['name']}는 {abs(dow['pct']):.2f}%, {sp['name']}는 {abs(sp['pct']):.2f}% {verb}."
    else:
        dow_sp_sentence = (f"{dow['name']}는 {abs(dow['pct']):.2f}% {'밀렸고' if dow['pct'] < 0 else '올랐고'}, "
                           f"{sp['name']}는 {abs(sp['pct']):.2f}% {'밀렸다' if sp['pct'] < 0 else '올랐다'}.")
    nasdaq_sentence = f"{nasdaq['name']}는 {abs(nasdaq['pct']):.2f}% {'하락' if nasdaq['pct'] < 0 else '상승'}했다."
    lines.append(f"{dow_sp_sentence} {nasdaq_sentence}")

    if data.get("dollar"):
        d = data["dollar"]
        lines.append("")
        lines.append(
            f"주요 6개 통화 대비 달러 가치를 보여주는 달러인덱스는 "
            f"전장보다 {abs(d['pct']):.2f}% {'상승' if d['pct'] >= 0 else '하락'}한 {d['price']:.3f}에 거래됐다."
        )

    body = "\n".join(lines)
    return title, body


# ── 이미지 ───────────────────────────────────────────────────
_IMAGE_KEYWORDS = ["closed stock exchange", "wall street empty", "new york stock exchange building"]


def fetch_holiday_image(article_date: date) -> str:
    try:
        from article_image import fetch_seeded_pixabay_image
        return fetch_seeded_pixabay_image(
            _IMAGE_KEYWORDS, article_date.toordinal(), f"us_market_holiday_{article_date.isoformat()}"
        )
    except Exception:
        return ""


# ── 기사 삽입 ────────────────────────────────────────────────
def already_published(article_date: date) -> bool:
    internal_url = f"internal://us_market_holiday_{article_date.isoformat()}"
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={"select": "id", "url": f"eq.{internal_url}", "limit": "1"},
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(title_ko: str, summary_ko: str, article_date: date, image_url: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    internal_url = f"internal://us_market_holiday_{article_date.isoformat()}"

    title_en, summary_en = translate_article(title_ko, summary_ko, None)

    payload = {
        "title_en": title_en or title_ko,
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "금융",
        "subcategory": "미국증시휴장",
        "region": "global",
        "country": "미국",
        "country_flag": "🇺🇸",
        "countries": ["미국"],
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "미국 증시 휴장 안내 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[us_market_holiday_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    holiday_name = today_holiday_name()
    if not holiday_name:
        print(f"  → 오늘({now_edt().strftime('%Y-%m-%d')} EDT/EST)은 NYSE 휴장일 아님 → 스킵")
        return

    article_date = now_edt().date()
    if already_published(article_date):
        print(f"  → {article_date} 휴장 안내 기사 이미 존재 → 스킵")
        return

    prev_date = previous_trading_date(article_date)
    print(f"  → 휴장 사유: {holiday_name}, 직전 거래일: {prev_date}")

    print("  → 야후 파이낸스에서 직전 거래일 마감 데이터 수집 중...")
    data = fetch_holiday_data()
    if not data:
        print("  [ERROR] 시세 데이터 수집 실패 → 종료")
        return

    title, body = build_article(holiday_name, article_date, prev_date, data)
    image_url = fetch_holiday_image(article_date)

    article_id = insert_article(title, body, article_date, image_url)
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")


if __name__ == "__main__":
    main()
