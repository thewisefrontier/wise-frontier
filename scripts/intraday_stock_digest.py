"""
scripts/intraday_stock_digest.py
---------------------------------
장중 "특징주" 기사 — 한국 증시 매체처럼 거래 시간 중 뉴스에 언급된 종목을
그때그때 짧은 기사로 소개한다(2026-10-15 신설, 사용자 요청 — "한국에서는
다양한 종목에 대해 장중 특징주 기사를 쓰는데, 그 형식을 빌려서 미국, 유럽
등 여러 나라들의 장중 특징주를 쓰는 건 어때?").

stock_news_writer.py(미국 대형주 1종목, 뉴욕 종가 이후 하루 1회)와 다른 점:
- 하루 여러 건(워치리스트 중 "뉴스에 새로 언급된" 종목마다 1건, 종목당 하루 1회)
- 미국뿐 아니라 영국·독일·프랑스 대형주도 다룬다
- 뉴욕 종가를 기다리지 않고, 그 종목의 거래소가 "지금 장중"일 때만 쓴다

데이터·검증 로직은 전부 기존 파일 재사용(중복 구현 금지):
- 시세: frontier_markets_writer.fetch_yahoo_quote / fetch_alphavantage_pct
  (2026-10-15: fetch_yahoo_quote가 이제 meta.currency도 돌려준다 — 런던
  상장 종목은 "GBp"(펜스) 단위로 내려와서(예: AZN.L 12300.0 = 123.00파운드)
  변환이 꼭 필요했다. 실측으로 직접 확인함.)
- 종목 선정·기사 프롬프트·Gemini 호출·팩트가드·이미지: stock_news_writer.py
  (build_article_prompt에 market_label/currency_label/article_date 선택
  인자를 추가해뒀다 — 기본값은 이 파일 자체의 미국 EOD 동작을 그대로 보존)
- 휴장일 캘린더: market_calendar.py의 is_uk_market_closed/is_de_market_closed/
  is_fr_market_closed(이미 영국·독일·프랑스 보유 — frontier_markets_writer.py가
  "글로벌 마켓 동향"에 쓰던 것과 동일)

조사 근거(2026-10-15): 미국은 Alpha Vantage/FMP 같은 "실시간 급등락 상위"
무료 API가 있지만, 유럽은 직접 확인해본 결과 전부 유료였다(FMP
biggest-gainers는 exchange 파라미터를 줘도 무시하고 나스닥/뉴욕만 반환,
Twelve Data market_movers는 공식 문서에 Pro 플랜 이상 전용이라고 명시,
Yahoo 비공식 스크리너도 region 파라미터가 무시되고 인증 필요한 커스텀
스크리너는 테스트 중에도 쿠키가 불안정했음). 그래서 "실제 급등락"이 아니라
stock_news_writer.py가 이미 쓰고 있던 "머니파이널(Finnhub) 실시간 뉴스에
언급됐는가"를 선정 기준으로 쓴다 — 느슨한 근사치이지만 무료이고 이미
검증된 방식이다.

실행: python scripts/intraday_stock_digest.py
권장: run.yml의 시간당 사이클에 얹는다(미국·영국·독일·프랑스 중 한 곳이라도
장중일 때만 실제로 글을 쓰고, 전부 장 마감이면 즉시 스킵하므로 별도
스케줄이 필요 없다).
"""

import os
import requests
from datetime import date, datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

from market_calendar import is_us_market_closed, is_uk_market_closed, is_de_market_closed, is_fr_market_closed

from stock_news_writer import (
    fetch_stock_data, build_article_prompt, call_gemini_article,
    parse_article_output, enforce_title_prefix, ensure_paragraphs,
    fetch_stock_image, check_date_hallucination, insert_final_article,
    detect_script_leak, is_placeholder_response, unwrap_json_body,
    _recent_moneyfinal_headlines, kw_match, now_kst,
    GEMINI_API_KEYS, SUPABASE_URL, SUPABASE_SERVICE_KEY,
    _sb_headers, _sb_url,
)

# ── 워치리스트(종목, 한글명, 거래소, 국가, 국기, 매칭 키워드) ──────────
# 미국 12종목은 stock_news_writer.WATCHLIST와 동일(두 파일이 서로 다른
# 튜플 모양 — country/flag/keywords를 한 줄에 같이 둬야 이 파일 로직이
# 단순해져서, 작은 중복을 감수하고 이 파일 전용으로 다시 적었다).
WATCHLIST = [
    ("AAPL", "애플", "나스닥", "미국", "🇺🇸", ["apple"]),
    ("MSFT", "마이크로소프트", "나스닥", "미국", "🇺🇸", ["microsoft"]),
    ("NVDA", "엔비디아", "나스닥", "미국", "🇺🇸", ["nvidia"]),
    ("GOOGL", "알파벳(구글)", "나스닥", "미국", "🇺🇸", ["google", "alphabet"]),
    ("AMZN", "아마존", "나스닥", "미국", "🇺🇸", ["amazon"]),
    ("META", "메타", "나스닥", "미국", "🇺🇸", ["meta platforms", " meta "]),
    ("TSLA", "테슬라", "나스닥", "미국", "🇺🇸", ["tesla"]),
    ("BRK-B", "버크셔 해서웨이", "뉴욕증권거래소", "미국", "🇺🇸", ["berkshire"]),
    ("JPM", "JP모건체이스", "뉴욕증권거래소", "미국", "🇺🇸", ["jpmorgan", "jp morgan"]),
    ("V", "비자", "뉴욕증권거래소", "미국", "🇺🇸", ["visa inc", " visa "]),
    ("NFLX", "넷플릭스", "나스닥", "미국", "🇺🇸", ["netflix"]),
    ("AMD", "AMD", "나스닥", "미국", "🇺🇸", [" amd ", "advanced micro devices"]),
    # 영국(LSE)
    ("AZN.L", "아스트라제네카", "런던증권거래소(LSE)", "영국", "🇬🇧", ["astrazeneca"]),
    ("SHEL.L", "쉘", "런던증권거래소(LSE)", "영국", "🇬🇧", [" shell "]),
    ("HSBA.L", "HSBC", "런던증권거래소(LSE)", "영국", "🇬🇧", ["hsbc"]),
    # 독일(Xetra)
    ("SAP.DE", "SAP", "프랑크푸르트증권거래소", "독일", "🇩🇪", ["sap se", " sap "]),
    ("SIE.DE", "지멘스", "프랑크푸르트증권거래소", "독일", "🇩🇪", ["siemens"]),
    # 프랑스(Euronext Paris)
    ("MC.PA", "LVMH", "유로넥스트 파리", "프랑스", "🇫🇷", ["lvmh"]),
    ("TTE.PA", "토탈에너지스", "유로넥스트 파리", "프랑스", "🇫🇷", ["totalenergies"]),
]

MAX_PICKS_PER_RUN = 2  # Gemini 검색-그라운딩 호출(비쌈) 예산 보호
# EOD 버전(48h)보다는 좁지만, 실측(2026-10-15)으로는 머니파이널 피드 자체가
# 뉴스 공백 구간(12시간 이상 신규 0건)이 흔해 6h는 너무 좁았다 — 12h로 완화.
NEWS_LOOKBACK_HOURS = 12

US_TZ = ZoneInfo("America/New_York")
UK_TZ = ZoneInfo("Europe/London")
EU_TZ = ZoneInfo("Europe/Paris")  # 독일·프랑스 대표(대륙 유럽, 영국과 거래시간 비슷)


def _us_market_open_now() -> bool:
    now = datetime.now(US_TZ)
    if now.weekday() >= 5 or is_us_market_closed(now.date()):
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= m < 16 * 60


def _uk_market_open_now() -> bool:
    now = datetime.now(UK_TZ)
    if now.weekday() >= 5 or is_uk_market_closed(now.date()):
        return False
    m = now.hour * 60 + now.minute
    return 8 * 60 <= m < 16 * 60 + 30


def _de_market_open_now() -> bool:
    now = datetime.now(EU_TZ)
    if now.weekday() >= 5 or is_de_market_closed(now.date()):
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 <= m < 17 * 60 + 30


def _fr_market_open_now() -> bool:
    now = datetime.now(EU_TZ)
    if now.weekday() >= 5 or is_fr_market_closed(now.date()):
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 <= m < 17 * 60 + 30


_MARKET_OPEN_CHECK = {
    "미국": _us_market_open_now,
    "영국": _uk_market_open_now,
    "독일": _de_market_open_now,
    "프랑스": _fr_market_open_now,
}

_MARKET_LOCAL_DATE = {
    "미국": lambda: datetime.now(US_TZ).date(),
    "영국": lambda: datetime.now(UK_TZ).date(),
    "독일": lambda: datetime.now(EU_TZ).date(),
    "프랑스": lambda: datetime.now(EU_TZ).date(),
}

# GBp(펜스) → 파운드 환산. 나머지는 그대로(달러/유로는 야후가 이미 기본 단위로 줌.
_CURRENCY_LABEL = {"USD": "달러", "EUR": "유로", "GBP": "파운드"}


def _normalize_currency(data: dict) -> str:
    """GBp(펜스) 단위면 가격류 필드를 100으로 나눠 파운드로 맞추고 표시 단위를 반환.
    (제자리 수정 — data dict를 직접 고친다.) 실측(2026-10-15): AZN.L이 야후에서
    currency="GBp", price=12300.0으로 내려옴 — 실제론 123.00파운드다. 대소문자를
    먼저 구분해야 한다 — .upper()를 먼저 하면 "GBp"(펜스)와 "GBP"(파운드, 다른
    거래소에선 실제로 이 표기를 쓰기도 함)를 구별할 수 없게 된다."""
    cur_raw = data.get("currency") or ""
    if cur_raw == "GBp":
        for key in ("price", "change", "day_high", "day_low", "fifty_two_week_high", "fifty_two_week_low"):
            if data.get(key) is not None:
                data[key] = data[key] / 100
        return "파운드"
    return _CURRENCY_LABEL.get(cur_raw.upper(), "달러")


def already_published_ticker(ticker: str, article_date: date) -> bool:
    internal_url = f"internal://intraday_stock_{article_date.isoformat()}_{ticker}"
    res = requests.get(
        f"{_sb_url()}?url=eq.{internal_url}&select=id",
        headers=_sb_headers(),
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def pick_new_mentions(limit: int = MAX_PICKS_PER_RUN) -> list:
    """지금 장이 열려 있는 나라의 종목 중, 최근 뉴스에 언급됐고 오늘 아직
    기사화 안 한 종목을 언급 횟수 순으로 상위 limit개 고른다."""
    headlines = _recent_moneyfinal_headlines(hours=NEWS_LOOKBACK_HOURS)
    if not headlines:
        return []

    candidates = []
    for ticker, name_ko, exchange, country, flag, kws in WATCHLIST:
        open_check = _MARKET_OPEN_CHECK.get(country)
        if not open_check or not open_check():
            continue
        local_date = _MARKET_LOCAL_DATE[country]()
        if already_published_ticker(ticker, local_date):
            continue
        matched = [h for h in headlines if kw_match(h, kws)]
        if matched:
            candidates.append((ticker, name_ko, exchange, country, flag, local_date, len(matched), matched[:3]))

    candidates.sort(key=lambda c: -c[6])
    return candidates[:limit]


def insert_intraday_article(ticker, name_ko, country, flag, title_ko, summary_ko,
                             article_date: date, image_url: str, hit_count: int) -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"    ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    if is_placeholder_response(title_ko, summary_ko):
        print(f"    ❌ Gemini 응답이 실제 기사가 아님 → 저장 차단: {title_ko[:60]}")
        return -1
    _unwrapped = unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            summary_ko = _unwrapped
        else:
            print(f"    ⛔ [raw JSON 본문] 저장 차단: {title_ko[:60]}")
            return -1

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    internal_url = f"internal://intraday_stock_{article_date.isoformat()}_{ticker}"

    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "금융",
        "subcategory": "장중특징주",
        "region": "global",
        "country": country,
        "country_flag": flag,
        "countries": [country],
        "image_url": image_url,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str,
                        "note": f"{name_ko}({ticker}) 장중 특징주 — 머니파이널 뉴스 {hit_count}건 언급으로 선정"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


def main():
    print(f"\n[intraday_stock_digest] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not GEMINI_API_KEYS:
        print("  [SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    picks = pick_new_mentions()
    if not picks:
        print("  → 지금 장중인 시장 중 뉴스에 새로 언급된 미발행 종목 없음 → 스킵")
        return

    for ticker, name_ko, exchange, country, flag, article_date, hit_count, hook_headlines in picks:
        print(f"  → {name_ko}({ticker}, {exchange}) — 머니파이널 뉴스 {hit_count}건 언급")

        data = fetch_stock_data(ticker)
        if not data:
            print("    [ERROR] 시세 수집 실패 → 스킵")
            continue
        currency_label = _normalize_currency(data)
        print(f"    → {data['price']:.2f}{currency_label} ({data['pct']:+.2f}%)")

        market_label = "미국 증시" if country == "미국" else f"{country} 증시"
        prompt = build_article_prompt(
            ticker, name_ko, exchange, data, hook_headlines,
            article_date=article_date, market_label=market_label, currency_label=currency_label,
        )
        article_text = call_gemini_article(prompt)
        if not article_text:
            print("    [ERROR] 기사 생성 실패 → 스킵")
            continue

        title, body = parse_article_output(article_text)
        title = enforce_title_prefix(title)
        if not title or not body:
            print("    [ERROR] 응답 파싱 실패 → 스킵")
            continue
        body = ensure_paragraphs(body)

        dg_bad, dg_reason = check_date_hallucination(
            body, [{"source_published_at": article_date.isoformat()}], base_date=article_date
        )
        if dg_bad:
            print(f"    ⚠️ [{dg_reason}] → 스킵")
            continue

        image_url = fetch_stock_image(article_date)
        article_id = insert_intraday_article(
            ticker, name_ko, country, flag, title, body, article_date, image_url, hit_count
        )
        if article_id > 0:
            print(f"    ✓ 저장 완료 (articles.id={article_id})")
        else:
            print("    [ERROR] 저장 실패")

    print(f"[intraday_stock_digest] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
