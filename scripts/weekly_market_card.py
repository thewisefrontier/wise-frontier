"""
scripts/weekly_market_card.py
-----------------------------
주간 시세 카드 — 한 주 동안의 주요 증시·환율·유가·금·비트코인 등락을 숫자로만 정리한 결정론적 기사.

2026-09-22 신설(사용자 요청: 콘텐츠 다양화 — "숫자 카드형 콘텐츠"). 애드센스 "가치가 별로 없는
콘텐츠" 대응으로 스트레이트 기사 외에 독자가 참고할 수 있는 정형 데이터 콘텐츠를 늘린다.

설계 원칙 — "지연은 괜찮지만 숫자가 틀리면 절대 안돼":
  - LLM을 쓰지 않는다. 모든 문장·수치는 코드가 야후 일봉 종가에서 계산해 템플릿에 넣는다.
  - 주간 등락률 = (그 주 마지막 거래일 종가 / 직전 주 마지막 거래일 종가 - 1). chartPreviousClose 같은
    메타 필드는 쓰지 않는다(2026-08-29 방향이 뒤집힌 사고, frontier_markets_writer.py 참조).
  - 항목별 자체 검증: 종가 양수·기간 안의 거래일 존재·등락률 범위·(주말 실행 시) 최신 종가가 야후
    regularMarketPrice와 일치. 하나라도 어긋나면 그 항목은 지어내지 않고 뺀다. 표시 항목이 6개 미만이면
    기사 자체를 발행하지 않는다.
  - 날짜는 각 시장 현지 거래일(meta.gmtoffset) 기준이라 시간대 혼동이 없다.

실행: python scripts/weekly_market_card.py   (run.yml이 매시 호출 — 토요일 10시 KST 이후 주 1회만 발행)
      python scripts/weekly_market_card.py --dry-run   (저장 없이 본문 출력)
"""

import os
import sys
import time
import requests
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv

load_dotenv()

from article_store import insert_final_article, sb_headers, sb_url

KST = timezone(timedelta(hours=9))

# (그룹, 표시명, 야후 심볼, 시장/설명, 종가 표기 형식, 단위, 허용 주간 등락 상한 %)
ITEMS = [
    ("증시", "S&P500", "^GSPC", "뉴욕증시", "{:,.2f}", "", 15),
    ("증시", "나스닥종합", "^IXIC", "뉴욕증시", "{:,.2f}", "", 20),
    ("증시", "다우존스", "^DJI", "뉴욕증시", "{:,.2f}", "", 15),
    ("증시", "코스피", "^KS11", "한국거래소", "{:,.2f}", "", 20),
    ("증시", "코스닥", "^KQ11", "한국거래소", "{:,.2f}", "", 20),
    ("증시", "닛케이225", "^N225", "도쿄증권거래소", "{:,.2f}", "", 20),
    ("증시", "DAX", "^GDAXI", "프랑크푸르트증권거래소", "{:,.2f}", "", 15),
    ("증시", "CAC40", "^FCHI", "유로넥스트 파리", "{:,.2f}", "", 15),
    ("증시", "FTSE100", "^FTSE", "런던증권거래소", "{:,.2f}", "", 15),
    ("환율·원자재·가상자산", "원/달러 환율", "KRW=X", "", "{:,.2f}", "원", 8),
    ("환율·원자재·가상자산", "WTI 원유", "CL=F", "선물 기준", "{:,.2f}", "달러/배럴", 30),
    ("환율·원자재·가상자산", "금", "GC=F", "선물 기준", "{:,.2f}", "달러/온스", 15),
    ("환율·원자재·가상자산", "비트코인", "BTC-USD", "", "{:,.0f}", "달러", 40),
]
MIN_ITEMS = 6           # 검증을 통과한 항목이 이보다 적으면 발행하지 않는다
CRYPTO = {"BTC-USD"}    # 24시간 거래라 야후 regularMarketPrice 대조를 건너뛴다(대조 시점이 어긋남)

# 2026-09-22 추가(사용자 지적 — "주간 상승률이 가장 컸던 항목이 비트코인이니
# 비트코인 이미지 넣으면 되잖아"): 그 주 등락률이 가장 컸던 항목의 이미지를
# 넣는다. article_image.py의 fetch_seeded_pixabay_image(오일/환율 등 다른
# 데일리 템플릿 기사가 이미 쓰는 결정론적 Pixabay 헬퍼, Gemini 호출 없음)를
# 그대로 재사용 — seed=week_end.toordinal()이라 같은 주는 항상 같은 사진.
_IMAGE_KEYWORDS = {
    "S&P500": ["wall street stock exchange", "new york stock market"],
    "나스닥종합": ["technology stock market", "wall street"],
    "다우존스": ["wall street stock exchange", "new york stock market"],
    "코스피": ["seoul stock exchange", "korea stock market"],
    "코스닥": ["seoul stock exchange", "korea stock market"],
    "닛케이225": ["tokyo stock exchange", "japan stock market"],
    "DAX": ["frankfurt stock exchange", "germany stock market"],
    "CAC40": ["paris stock exchange", "france stock market"],
    "FTSE100": ["london stock exchange", "uk stock market"],
    "원/달러 환율": ["currency exchange dollar", "foreign exchange money"],
    "WTI 원유": ["oil rig petroleum", "crude oil barrel"],
    "금": ["gold bars bullion", "gold market"],
    # "cryptocurrency coin"처럼 인명을 뺀 일반 검색어를 썼더니 실제로는
    # 스팀(Steem) 코인 사진이 나온 적이 있어(2026-09-22), 비트코인만은
    # 두 키워드 모두 "bitcoin"을 명시해 다른 알트코인이 안 걸리게 한다.
    "비트코인": ["bitcoin coin", "bitcoin cryptocurrency logo"],
}


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def week_end_for(today: date) -> date | None:
    """직전에 끝난 주의 금요일. 금요일 당일(장중)이면 아직 주가 안 끝났으므로 None."""
    wd = today.weekday()  # 월0 … 금4 토5 일6
    if wd == 4:
        return None
    return today - timedelta(days=(wd - 4) % 7)


def fetch_daily(symbol: str):
    """야후 일봉: (현지 거래일, 종가) 리스트(오름차순)와 meta. 실패 시 (None, None)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1mo&interval=1d"
    try:
        res = requests.get(url, timeout=(10, 30), headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200:
            print(f"  [WARN] {symbol}: HTTP {res.status_code}")
            return None, None
        r = (res.json().get("chart", {}).get("result") or [None])[0]
        if not r:
            return None, None
        meta = r.get("meta", {})
        off = int(meta.get("gmtoffset") or 0)
        closes = (r.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        rows = []
        for ts, c in zip(r.get("timestamp") or [], closes):
            if c is None:
                continue
            d = (datetime.fromtimestamp(ts, timezone.utc) + timedelta(seconds=off)).date()
            rows.append((d, float(c)))
        return rows, meta
    except Exception as e:
        print(f"  [WARN] {symbol}: {e}")
        return None, None


def weekly_change(symbol: str, week_end: date, limit_pct: float):
    """검증을 통과한 {'end','prev','pct','end_date','prev_date'} 또는 None."""
    rows, meta = fetch_daily(symbol)
    if not rows:
        return None
    if symbol in CRYPTO:
        # 2026-09-26 실사고(사용자 지적 — "비트코인은 5일이 아니라 7일 기준으로
        # 봐야 하는거 아닐까"): 비트코인은 24시간 거래되는데 주식과 똑같이
        # 월~금 창으로 자르면 야후가 주는 토·일 종가가 통째로 버려진다. 기사는
        # 토요일 오전(금요일 장 마감 확정 후)에 나가므로, "이번 주"는 그 시점의
        # 실제 최신 종가(토요일 새벽 것까지 포함)를, "지난주"는 거기서 정확히
        # 7일 전 종가를 쓴다 — 주말 변동을 반영하면서도 날짜 간격은 그대로 7일.
        end_date, end = rows[-1]
        target_prev = end_date - timedelta(days=7)
        prev_candidates = [x for x in rows if x[0] <= target_prev]
        if not prev_candidates:
            print(f"  [제외] {symbol}: 7일 전 비교 데이터 없음")
            return None
        prev_date, prev = prev_candidates[-1]
    else:
        week_start = week_end - timedelta(days=4)
        prev_end = week_end - timedelta(days=7)
        this_week = [x for x in rows if week_start <= x[0] <= week_end]
        prev_week = [x for x in rows if prev_end - timedelta(days=4) <= x[0] <= prev_end]
        if not this_week or not prev_week:
            print(f"  [제외] {symbol}: 해당 주 거래일 데이터 없음(휴장/데이터 누락)")
            return None
        end_date, end = this_week[-1]
        prev_date, prev = prev_week[-1]
    if end <= 0 or prev <= 0:
        print(f"  [제외] {symbol}: 비정상 종가")
        return None
    pct = (end / prev - 1) * 100
    if abs(pct) > limit_pct:
        print(f"  [제외] {symbol}: 주간 등락 {pct:+.2f}%가 허용 범위(±{limit_pct}%) 밖 — 데이터 오류 의심")
        return None
    # 주말 실행이면 이번 주 마지막 종가가 야후 최신가(regularMarketPrice)와 같아야 한다 — 어긋나면 데이터 이상
    if symbol not in CRYPTO and rows[-1][0] == end_date:
        live = meta.get("regularMarketPrice")
        if live and abs(live / end - 1) > 0.01:
            print(f"  [제외] {symbol}: 마지막 종가 {end}와 야후 현재가 {live} 불일치")
            return None
    return {"end": end, "prev": prev, "pct": pct, "end_date": end_date, "prev_date": prev_date}


def fmt_pct(p: float) -> str:
    return f"{p:+.2f}%"


def build_article(week_end: date, results: list):
    """results: [(group, name, market, fmt, unit, data)] → (title, body)."""
    week_start = week_end - timedelta(days=4)
    up = [x for x in results if x[5]["pct"] > 0]
    down = [x for x in results if x[5]["pct"] < 0]
    best = max(results, key=lambda x: x[5]["pct"])
    worst = min(results, key=lambda x: x[5]["pct"])
    rng = (f"{week_start.month}월 {week_start.day}일부터 {week_end.day}일까지" if week_start.month == week_end.month
           else f"{week_start.month}월 {week_start.day}일부터 {week_end.month}월 {week_end.day}일까지")
    title_rng = (f"{week_start.month}월 {week_start.day}~{week_end.day}일" if week_start.month == week_end.month
                 else f"{week_start.month}/{week_start.day}~{week_end.month}/{week_end.day}")
    title = f"[주간 시세] {title_rng} 글로벌 증시·환율·유가·금·비트코인 한 주 성적표"

    head = f"{rng} 한 주 동안 집계 대상 시세 {len(results)}개 가운데 {len(up)}개가 올랐고 {len(down)}개가 내렸다."
    # 조사(이/가·이었/였)는 항목명 받침에 따라 달라 오류가 나기 쉬워, 이름 뒤에 조사를 붙이지 않는 문형만 쓴다
    if best[5]["pct"] > 0 and worst[5]["pct"] < 0:
        lead = (f"{head} 주간 상승률이 가장 컸던 항목은 {best[1]}({fmt_pct(best[5]['pct'])}), "
                f"하락률이 가장 컸던 항목은 {worst[1]}({fmt_pct(worst[5]['pct'])})다.")
    elif best[5]["pct"] > 0:
        lead = f"{head} 주간 상승률이 가장 컸던 항목은 {best[1]}({fmt_pct(best[5]['pct'])})다."
    else:
        lead = f"{head} 주간 하락률이 가장 컸던 항목은 {worst[1]}({fmt_pct(worst[5]['pct'])})다."

    parts = [lead]
    for group in ("증시", "환율·원자재·가상자산"):
        rows = [x for x in results if x[0] == group]
        if not rows:
            continue
        lines = [f"[{group}]"]
        for _, name, market, fmt, unit, d in rows:
            label = f"{name}({market})" if market else name
            lines.append(
                f"- {label}: {d['end_date'].month}월 {d['end_date'].day}일 종가 {fmt.format(d['end'])}{unit}, "
                f"주간 {fmt_pct(d['pct'])} (직전 주 {d['prev_date'].month}월 {d['prev_date'].day}일 종가 {fmt.format(d['prev'])}{unit})"
            )
        parts.append("\n".join(lines))
    parts.append("주간 등락률은 이번 주 마지막 거래일 종가를 직전 주 마지막 거래일 종가와 비교해 계산했다. "
                 "각 시장의 현지 거래일 기준이며, 휴장이나 데이터 이상이 확인된 항목은 표에서 제외했다. 데이터 출처는 야후 파이낸스다.")
    return title, "\n\n".join(parts)


def already_published(url: str) -> bool:
    res = requests.get(sb_url(), headers=sb_headers(), params={"select": "id", "url": f"eq.{url}"}, timeout=15)
    return res.status_code in (200, 206) and bool(res.json())


def main():
    dry = "--dry-run" in sys.argv
    now = now_kst()
    week_end = week_end_for(now.date())
    if week_end is None:
        print("[weekly_market_card] 금요일 — 주가 아직 안 끝남, 스킵")
        return
    if not dry and now.weekday() == 5 and now.hour < 10:
        print("[weekly_market_card] 토요일 10시(KST) 이전 — 비트코인 금요일 봉 확정 전, 스킵")
        return
    if (now.date() - week_end).days > 6:
        return
    url = f"internal://weekly_market_{week_end.isoformat()}"
    if not dry:
        if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
            print("[SKIP] SUPABASE 환경변수 없음")
            return
        if already_published(url):
            print(f"[weekly_market_card] {week_end} 주 카드 이미 존재 — 스킵")
            return

    results = []
    for group, name, symbol, market, fmt, unit, limit in ITEMS:
        d = weekly_change(symbol, week_end, limit)
        time.sleep(0.5)
        if d:
            results.append((group, name, market, fmt, unit, d))
    print(f"[weekly_market_card] 검증 통과 {len(results)}/{len(ITEMS)}개")
    if len(results) < MIN_ITEMS:
        print(f"  → {MIN_ITEMS}개 미만이라 발행 보류")
        return

    title, body = build_article(week_end, results)
    if dry:
        print(title + "\n\n" + body)
        return

    # 그 주 등락률이 가장 컸던 항목(본문 리드 문장과 동일한 기준) 이미지를 넣는다.
    best_name = max(results, key=lambda x: x[5]["pct"])[1]
    image_url = ""
    try:
        from article_image import fetch_seeded_pixabay_image
        keywords = _IMAGE_KEYWORDS.get(best_name, ["stock market finance"])
        image_url = fetch_seeded_pixabay_image(keywords, seed=week_end.toordinal(), key_hint=f"weekly_market_{week_end.isoformat()}")
    except Exception as e:
        print(f"  [WARN] 이미지 조회 실패(본문은 그대로 발행): {e}")

    now_str = now.strftime("%Y-%m-%d %H:%M")
    art_id = insert_final_article({
        "title_en": title, "title_ko": title, "summary_en": "", "summary_ko": body, "url": url,
        "source": "NewsFinal", "category": "경제", "subcategory": "주간시세",
        "region": "global", "country": "", "country_flag": "", "image_url": image_url, "countries": [],
        "score": 1, "created_at": now_str, "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "주간 시세 카드 자동 기사(결정론적 집계)"}],
        "source_data": {"week_end": week_end.isoformat(), "items": [
            {"name": n, "end": d["end"], "prev": d["prev"], "pct": round(d["pct"], 4),
             "end_date": d["end_date"].isoformat(), "prev_date": d["prev_date"].isoformat()} for _, n, _, _, _, d in results]},
        "sent_telegram": 0, "is_published": True,
    })
    print(f"  ✅ 저장 id={art_id}" if art_id and art_id > 0 else "  ❌ 저장 실패")


if __name__ == "__main__":
    main()
