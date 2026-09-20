"""
scripts/jp_market_writer.py
------------------------------------
일본 증시(닛케이225) 정규 거래일 마감 기사 자동 생성.

2026-09-18 신설 — 사용자 지시: "그냥 해외 증시 종합기사에서 일본 닛케이를
빼자. 그리고 닛케이 기사를 따로 작업하면 어떨까" → "시간대 문제 때문에
자꾸 오류가 발생하는데" → "걍 따로 빼는게 나을 듯". 도쿄는 뉴욕보다
훨씬 앞서 마감해 frontier_markets_writer.py의 "글로벌 마켓 동향" 종합
기사 안에서 "같은 날" 시점 표현이 반복적으로 꼬였다(id=148194 최초 발견,
프롬프트 지시로 고쳤다고 판단했으나 id=194024에서 재발) — 프롬프트로
계속 패치하는 대신 아예 별도 기사로 분리한다.

jp_market_holiday_writer.py(휴장일 안내)의 자매 스크립트 — 같은 데이터
소스·삽입 패턴을 그대로 쓰되, 이쪽은 "정규 거래일 마감"만 다룬다(휴장일엔
그 스크립트가 대신 발행하므로 이 스크립트는 스킵). 지수 수치는
frontier_markets_writer.py가 이미 검증한 fetch_yahoo_quote()/
fetch_alphavantage_pct() 2단 교차검증을 그대로 재사용 — 새 안전장치를
따로 만들 필요가 없다. jp_market_holiday_writer.py와 동일하게 Gemini 없는
결정론적 데이터 기사(오피넷/국내유가 계열과 같은 패턴)로 유지한다.

실행: python scripts/jp_market_writer.py
권장: 도쿄 현지 15:30 JST 경(정규장 마감 후) 1회 호출.
"""

import os
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

from frontier_markets_writer import fetch_yahoo_quote, fetch_alphavantage_pct, _pct_disagree

try:
    from market_calendar import jp_holiday_name
except Exception:
    def jp_holiday_name(d):
        return None

KST = timezone(timedelta(hours=9))
JST = ZoneInfo("Asia/Tokyo")  # KST와 시차는 없지만(둘 다 UTC+9) 명확성을 위해 분리

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

NIKKEI_SYMBOL = "^N225"
NIKKEI_AV_PROXY = "EWJ"  # frontier_markets_writer.MAJOR_INDEX_AV_PROXY["^N225"]와 동일


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def now_jst() -> datetime:
    return datetime.now(timezone.utc).astimezone(JST)


def fetch_nikkei_data() -> dict | None:
    q = fetch_yahoo_quote(NIKKEI_SYMBOL)
    if not q or q.get("pct") is None:
        return None
    # frontier_markets_writer.py와 동일한 2차 교차검증 — 야후 내부 필드끼리
    # 이미 모순(_suspect)이면 Alpha Vantage로 한 번 더 확인, 그래도 크게
    # 어긋나면 지어내지 말고 발행 자체를 포기한다.
    if q.get("_suspect"):
        av_pct = fetch_alphavantage_pct(NIKKEI_AV_PROXY)
        if av_pct is not None and _pct_disagree(q["pct"], av_pct):
            print(f"  ⚠️ 야후·Alpha Vantage 등락률 불일치(야후 {q['pct']:.2f}% vs AV {av_pct:.2f}%) → 발행 보류")
            return None
    return q


def build_article(trade_date: date, data: dict) -> tuple[str, str]:
    verb = "하락" if data["pct"] < 0 else "상승"
    title = f"닛케이225, {trade_date.day}일 {abs(data['pct']):.2f}% {verb}…{data['price']:,.2f}로 마감"

    prev = data.get("prev")
    prev_line = f" 전일 종가는 {prev:,.2f}였다." if prev else ""
    body = (
        f"일본 도쿄증권거래소(TSE)에서 {trade_date.day}일(현지시간) 닛케이225 지수가 "
        f"전장 대비 {abs(data['pct']):.2f}% {verb}한 {data['price']:,.2f}로 거래를 마쳤다.{prev_line}\n\n"
        f"이날 도쿄 증시는 뉴욕 등 다른 주요국 증시보다 앞서 마감했다."
    )
    return title, body


_IMAGE_KEYWORDS = ["tokyo stock exchange", "nikkei", "japan financial district"]


def fetch_market_image(trade_date: date) -> str:
    try:
        from article_image import fetch_seeded_pixabay_image
        return fetch_seeded_pixabay_image(
            _IMAGE_KEYWORDS, trade_date.toordinal(), f"jp_market_{trade_date.isoformat()}"
        )
    except Exception:
        return ""


def already_published(trade_date: date) -> bool:
    internal_url = f"internal://jp_market_{trade_date.isoformat()}"
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={"select": "id", "url": f"eq.{internal_url}", "limit": "1"},
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(title_ko: str, summary_ko: str, trade_date: date, image_url: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    internal_url = f"internal://jp_market_{trade_date.isoformat()}"

    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "금융",
        "subcategory": "일본증시동향",
        "region": "asia",
        "country": "일본",
        "country_flag": "🇯🇵",
        "countries": ["일본"],
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "일본 증시 마감 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


def main():
    print(f"\n[jp_market_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    trade_date = now_jst().date()

    if trade_date.weekday() >= 5:
        print(f"  → {trade_date}는 주말 → 스킵")
        return

    if jp_holiday_name(trade_date):
        print(f"  → {trade_date}는 JPX 휴장일(jp_market_holiday_writer.py가 대신 발행) → 스킵")
        return

    if already_published(trade_date):
        print(f"  → {trade_date} 마감 기사 이미 존재 → 스킵")
        return

    print("  → 야후 파이낸스에서 닛케이225 마감 데이터 수집 중...")
    data = fetch_nikkei_data()
    if not data:
        print("  [ERROR] 시세 데이터 수집 실패/검증 불일치 → 종료")
        return

    title, body = build_article(trade_date, data)
    image_url = fetch_market_image(trade_date)

    article_id = insert_article(title, body, trade_date, image_url)
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")


if __name__ == "__main__":
    main()
