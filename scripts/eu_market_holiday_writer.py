"""
scripts/eu_market_holiday_writer.py
------------------------------------
영국(런던증권거래소)·독일(프랑크푸르트)·프랑스(파리) 증시 휴장일 안내
기사 자동 생성. us_market_holiday_writer.py/jp_market_holiday_writer.py와
같은 패턴(사용자 요청 2026-09-08: "영국/독일/프랑스도 시간 될 때 마저
만들어줘").

3개국이 구조가 완전히 동일해(지수 1개 + 휴장 사유 + 직전 거래일 종가)
jp_market_holiday_writer.py처럼 나라마다 파일을 또 만들지 않고 이 파일
하나에서 설정 목록(MARKETS)을 순회한다 — 같은 날 두 나라가 동시에
휴장이어도(예: 성금요일) 각각 별도 기사로 낸다.

⚠️ MARKETS의 휴장일 캘린더(market_calendar.py)는 각 거래소 공식 사이트가
자동 조회 도구에서 막혀 exchange_calendars 오픈소스 라이브러리(pip
install exchange_calendars, quantopian/trading_calendars 후속)로
확보했다 — 미국 캘린더(NYSE 공식 사이트 직접 확인)보다 신뢰도가 낮으니
매년 초 가능하면 각 거래소 공식 캘린더로 재확인할 것.

실행: python scripts/eu_market_holiday_writer.py
권장: 한국시간 오후 5~6시경(유럽 각국 정규장 개장 전후) 1회 호출.
"""

import os
import requests
from datetime import datetime, timedelta, timezone, date
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

from market_calendar import (
    uk_holiday_name, uk_previous_trading_date,
    de_holiday_name, de_previous_trading_date,
    fr_holiday_name, fr_previous_trading_date,
)

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


# 각 시장은 KST 기준으로 판정한다(영국·독일·프랑스 현지 자정 전후는
# 한국시간 오전이라, 이 스크립트가 실행되는 한국 오후 시간대엔 이미 그
# 나라의 "오늘 날짜"가 확정돼 있다 — us_market_holiday_writer.py가 뉴욕
# 로컬시간 기준인 것과 달리, 여기선 실행 시점의 KST 날짜를 각국 날짜로
# 그대로 쓴다. 시차상 최대 몇 시간의 오차 가능성보다 단순함을 택했다).
MARKETS = [
    {
        "key": "uk_market_holiday",
        "country": "영국",
        "flag": "🇬🇧",
        "region": "europe",
        "exchange": "런던증권거래소(LSE)",
        "index_name": "FTSE100",
        "index_symbol": "^FTSE",
        "holiday_fn": uk_holiday_name,
        "prev_trading_fn": uk_previous_trading_date,
    },
    {
        "key": "de_market_holiday",
        "country": "독일",
        "flag": "🇩🇪",
        "region": "europe",
        "exchange": "프랑크푸르트증권거래소",
        "index_name": "DAX",
        "index_symbol": "^GDAXI",
        "holiday_fn": de_holiday_name,
        "prev_trading_fn": de_previous_trading_date,
    },
    {
        "key": "fr_market_holiday",
        "country": "프랑스",
        "flag": "🇫🇷",
        "region": "europe",
        "exchange": "유로넥스트 파리",
        "index_name": "CAC40",
        "index_symbol": "^FCHI",
        "holiday_fn": fr_holiday_name,
        "prev_trading_fn": fr_previous_trading_date,
    },
]


def fetch_index_data(symbol: str) -> dict | None:
    q = fetch_yahoo_quote(symbol)
    if not q or q.get("pct") is None:
        return None
    return q


def build_article(market: dict, holiday_name: str, holiday_date: date, prev_date: date, data: dict) -> tuple[str, str]:
    country = market["country"]
    exchange = market["exchange"]
    index_name = market["index_name"]
    title = f"{country} 증시, {holiday_date.day}일 '{holiday_name.split('(')[0]}'로 휴장"

    prev_str = f"{prev_date.month}월 {prev_date.day}일"
    verb = "하락" if data["pct"] < 0 else "상승"
    body = (
        f"{country} {exchange}는 {holiday_date.day}일(현지시간) '{holiday_name}'을 맞아 하루 동안 휴장한다.\n\n"
        f"이에 따라 이날 {country} 주식시장 거래는 진행되지 않는다.\n\n"
        f"직전 장인 지난 {prev_str} {index_name} 지수는 {data['price']:.2f}로 마감해 "
        f"전장 대비 {abs(data['pct']):.2f}% {verb}했다."
    )
    return title, body


def fetch_holiday_image(market: dict, article_date: date) -> str:
    try:
        from article_image import fetch_seeded_pixabay_image
        keywords = [f"{market['country'].lower()} stock exchange", "closed stock exchange", "european financial district"]
        return fetch_seeded_pixabay_image(
            keywords, article_date.toordinal(), f"{market['key']}_{article_date.isoformat()}"
        )
    except Exception:
        return ""


def already_published(market: dict, article_date: date) -> bool:
    internal_url = f"internal://{market['key']}_{article_date.isoformat()}"
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={"select": "id", "url": f"eq.{internal_url}", "limit": "1"},
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(market: dict, title_ko: str, summary_ko: str, article_date: date, image_url: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    internal_url = f"internal://{market['key']}_{article_date.isoformat()}"

    # 2026-09-09 제거(사용자 지시 — 다국어 채널이 이 번역을 재사용하지 않음).
    title_en, summary_en = "", ""

    payload = {
        "title_en": title_en or title_ko,
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "금융",
        "subcategory": f"{market['country']}증시휴장",
        "region": market["region"],
        "country": market["country"],
        "country_flag": market["flag"],
        "countries": [market["country"]],
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": f"{market['country']} 증시 휴장 안내 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


def run_market(market: dict):
    label = market["country"]
    today = now_kst().date()
    holiday_name = market["holiday_fn"](today)
    if not holiday_name:
        print(f"  → {label}: 오늘({today})은 휴장일 아님 → 스킵")
        return

    if already_published(market, today):
        print(f"  → {label}: {today} 휴장 안내 기사 이미 존재 → 스킵")
        return

    prev_date = market["prev_trading_fn"](today)
    print(f"  → {label}: 휴장 사유 {holiday_name}, 직전 거래일 {prev_date}")

    data = fetch_index_data(market["index_symbol"])
    if not data:
        print(f"  [ERROR] {label}: 시세 데이터 수집 실패 → 스킵")
        return

    title, body = build_article(market, holiday_name, today, prev_date, data)
    image_url = fetch_holiday_image(market, today)

    article_id = insert_article(market, title, body, today, image_url)
    if article_id > 0:
        print(f"  ✓ {label}: 기사 삽입 완료 (articles.id={article_id})")
    else:
        print(f"  [ERROR] {label}: 기사 삽입 실패")


def main():
    print(f"\n[eu_market_holiday_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    for market in MARKETS:
        run_market(market)


if __name__ == "__main__":
    main()
