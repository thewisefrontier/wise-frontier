"""
scripts/jp_market_holiday_writer.py
------------------------------------
일본 증시(도쿄증권거래소/JPX) 휴장일 안내 기사 자동 생성.

us_market_holiday_writer.py와 같은 패턴(사용자 요청 2026-09-08: "휴장도
미국 증시 말고 주요국 증시의 경우는 써주는게 좋을 것 같은데")을 일본에
먼저 적용한다. 닛케이225 직전 거래일 종가만 다루는 단순한 버전 —
us_market_holiday_writer.py처럼 3대 지수+달러인덱스급 다항목은 아니다.

⚠️ JP_HOLIDAYS_BY_YEAR(market_calendar.py)는 JPX 공식 사이트가 자동
조회 도구에서 막혀 복수의 독립 집계 사이트 교차검증으로만 확보했다 —
미국 캘린더보다 신뢰도가 낮으니 매년 초 가능하면 재확인할 것.

실행: python scripts/jp_market_holiday_writer.py
권장: 도쿄 현지 09:00 JST 경(정규장 개장 전) 1회 호출.
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

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

from frontier_markets_writer import fetch_yahoo_quote

try:
    from translate_guard import translate_article
except Exception:
    def translate_article(title_ko: str, body_ko: str, call_gemini_fn=None, max_tokens: int = 3500):
        return "", ""

try:
    from market_calendar import jp_holiday_name, jp_previous_trading_date
except Exception:
    def jp_holiday_name(d):
        return None
    def jp_previous_trading_date(from_date):
        d = from_date - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

KST = timezone(timedelta(hours=9))
JST = ZoneInfo("Asia/Tokyo")  # KST와 시차는 없지만(둘 다 UTC+9) 명확성을 위해 분리

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(JST)


def today_jp_holiday_name() -> str | None:
    return jp_holiday_name(now_jst().date())


NIKKEI_SYMBOL = "^N225"


def fetch_nikkei_data() -> dict | None:
    q = fetch_yahoo_quote(NIKKEI_SYMBOL)
    if not q or q.get("pct") is None:
        return None
    return q


def build_article(holiday_name: str, holiday_date: date, prev_date: date, data: dict) -> tuple[str, str]:
    title = f"일본 증시, {holiday_date.day}일 '{holiday_name.split('(')[0]}'로 휴장"

    prev_str = f"{prev_date.month}월 {prev_date.day}일"
    verb = "하락" if data["pct"] < 0 else "상승"
    body = (
        f"일본 도쿄증권거래소(JPX)는 {holiday_date.day}일(현지시간) '{holiday_name}'을 맞아 하루 동안 휴장한다.\n\n"
        f"이에 따라 이날 일본 주식시장 거래는 진행되지 않는다.\n\n"
        f"직전 장인 지난 {prev_str} 닛케이225 지수는 {data['price']:.2f}로 마감해 "
        f"전장 대비 {abs(data['pct']):.2f}% {verb}했다."
    )
    return title, body


_IMAGE_KEYWORDS = ["tokyo stock exchange", "closed stock exchange", "japan financial district"]


def fetch_holiday_image(article_date: date) -> str:
    try:
        from article_image import fetch_seeded_pixabay_image
        return fetch_seeded_pixabay_image(
            _IMAGE_KEYWORDS, article_date.toordinal(), f"jp_market_holiday_{article_date.isoformat()}"
        )
    except Exception:
        return ""


def already_published(article_date: date) -> bool:
    internal_url = f"internal://jp_market_holiday_{article_date.isoformat()}"
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
    internal_url = f"internal://jp_market_holiday_{article_date.isoformat()}"

    title_en, summary_en = translate_article(title_ko, summary_ko, None)

    payload = {
        "title_en": title_en or title_ko,
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "금융",
        "subcategory": "일본증시휴장",
        "region": "asia",
        "country": "일본",
        "country_flag": "🇯🇵",
        "countries": ["일본"],
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "일본 증시 휴장 안내 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


def main():
    print(f"\n[jp_market_holiday_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    holiday_name = today_jp_holiday_name()
    if not holiday_name:
        print(f"  → 오늘({now_jst().strftime('%Y-%m-%d')} JST)은 JPX 휴장일 아님 → 스킵")
        return

    article_date = now_jst().date()
    if already_published(article_date):
        print(f"  → {article_date} 휴장 안내 기사 이미 존재 → 스킵")
        return

    prev_date = jp_previous_trading_date(article_date)
    print(f"  → 휴장 사유: {holiday_name}, 직전 거래일: {prev_date}")

    print("  → 야후 파이낸스에서 닛케이225 직전 거래일 마감 데이터 수집 중...")
    data = fetch_nikkei_data()
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
