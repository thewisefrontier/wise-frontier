"""
scripts/frontier_market_holiday_writer.py
--------------------------------------------
남아공(요하네스버그)·인도네시아·말레이시아 증시 휴장일 안내 기사 자동 생성.
eu_market_holiday_writer.py와 완전히 같은 구조(MARKETS 설정 목록 순회).

사용자 요청(2026-09-08): "아프리카, 동남아쪽은 어떻게 소스를 못구하나?"
— exchange_calendars 오픈소스 라이브러리(XJSE/XIDX/XKLS)로 캘린더 확보.
나이지리아·케냐·이집트·베트남은 이 라이브러리 자체에 커버리지가 없어
보류(market_calendar.py 주석 참고).

지수 심볼은 frontier_markets_writer.py의 FRONTIER_INDICES와 동일한
것을 그대로 쓴다(남아공은 Top40 ETF 대리지표, 인니·말련은 지수 직접).

실행: python scripts/frontier_market_holiday_writer.py
권장: 한국시간 오전 중(각 시장 개장 전후) 1회 호출.
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
    za_holiday_name, za_previous_trading_date,
    id_holiday_name, id_previous_trading_date,
    my_holiday_name, my_previous_trading_date,
)

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


# eu_market_holiday_writer.py와 동일하게 KST 날짜를 각국 날짜로 그대로 쓴다.
MARKETS = [
    {
        "key": "za_market_holiday",
        "country": "남아공",
        "flag": "🇿🇦",
        "region": "africa",
        "exchange": "요하네스버그증권거래소(JSE)",
        "index_name": "Top40",
        "index_symbol": "^JN0U.JO",
        "holiday_fn": za_holiday_name,
        "prev_trading_fn": za_previous_trading_date,
    },
    {
        "key": "id_market_holiday",
        "country": "인도네시아",
        "flag": "🇮🇩",
        "region": "southeast_asia",
        "exchange": "증권거래소(IDX)",
        "index_name": "종합주가지수",  # 이미 "지수"로 끝남 — build_article()이 중복 접미사를 생략
        "index_symbol": "^JKSE",
        "holiday_fn": id_holiday_name,
        "prev_trading_fn": id_previous_trading_date,
    },
    {
        "key": "my_market_holiday",
        "country": "말레이시아",
        "flag": "🇲🇾",
        "region": "southeast_asia",
        "exchange": "거래소(Bursa Malaysia)",
        "index_name": "KLCI",
        "index_symbol": "^KLSE",
        "holiday_fn": my_holiday_name,
        "prev_trading_fn": my_previous_trading_date,
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
    # index_name이 이미 "지수"를 포함하면(예: 인도네시아 종합주가지수) 중복 접미사를 안 붙인다.
    index_label = index_name if index_name.endswith("지수") else f"{index_name} 지수"
    body = (
        f"{country} {exchange}는 {holiday_date.day}일(현지시간) '{holiday_name}'을 맞아 하루 동안 휴장한다.\n\n"
        f"이에 따라 이날 {country} 주식시장 거래는 진행되지 않는다.\n\n"
        f"직전 장인 지난 {prev_str} {index_label}는 {data['price']:.2f}로 마감해 "
        f"전장 대비 {abs(data['pct']):.2f}% {verb}했다."
    )
    return title, body


def fetch_holiday_image(market: dict, article_date: date) -> str:
    try:
        from article_image import fetch_seeded_pixabay_image
        keywords = [f"{market['country'].lower()} stock exchange", "closed stock exchange", "financial district skyline"]
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
    print(f"\n[frontier_market_holiday_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    for market in MARKETS:
        run_market(market)


if __name__ == "__main__":
    main()
