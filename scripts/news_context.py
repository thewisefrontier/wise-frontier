"""
scripts/news_context.py
-------------------------
구글 뉴스 RSS에서 실제 최신 헤드라인 몇 개를 가져와, Gemini 프롬프트에
"추정이 아니라 실제 보도 근거"로 끼워 넣는 용도. 추가 LLM 호출이나
쿼터 비용 없이(단순 RSS 파싱) 실제 헤드라인을 그대로 인용 가능한
소재로 제공한다.

2026-08-26 도입 배경: oil_price_writer.py 등 자체 데이터 API만 쓰는
스크립트들은 "가격 변동 배경"을 Gemini 자체 지식으로만 추정 서술해야
했다(실제 근거가 없어 "~로 풀이된다" 같은 헤지 표현을 강제해야 했음).
실제 헤드라인을 근거로 주면 더 구체적이고 검증 가능한 서술이 가능해진다.

⚠️ 날씨처럼 "제공된 데이터에만 근거" 원칙이 핵심 안전장치인 스크립트에
쓸 때는, 헤드라인도 데이터와 동급의 "제공된 사실"로 취급하고 그 헤드라인
문구를 벗어난 해석·추측을 못 하게 프롬프트에서 명시해야 한다.
"""

import re
from datetime import date, timedelta
from urllib.parse import quote

import feedparser


def fetch_headlines(query: str, limit: int = 5, hl: str = "en-US", gl: str = "US", dated: bool = True) -> list:
    """구글 뉴스 검색 RSS에서 최신 헤드라인 목록 반환.
    dated=True면 "(2026-11-13) 제목"처럼 발행일을 붙인다 — 날짜가 없으면 모델이 과거 보도(예: 2024 대선)를
    이번 주 일로 읽는다(2026-11-15 "미국 대선" 오기 14건). 과거 보도도 날짜가 붙은 채 참고 자료로 쓸 수 있다.
    실패해도 예외를 삼키고 빈 리스트를 반환한다(호출부가 이 실패로
    죽지 않도록 — 헤드라인은 보강 재료일 뿐 핵심 데이터가 아니다)."""
    lang = hl.split("-")[0]
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={gl}:{lang}"
    try:
        d = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        out = []
        for e in d.entries[:limit]:
            t = getattr(e, "title", None)
            if not t:
                continue
            pp = getattr(e, "published_parsed", None)
            out.append(f"({pp.tm_year:04d}-{pp.tm_mon:02d}-{pp.tm_mday:02d}) {t}" if dated and pp else t)
        return out
    except Exception:
        return []


def _first_tuesday_after_first_monday(year: int) -> date:
    d = date(year, 11, 1)
    d += timedelta(days=(0 - d.weekday()) % 7)  # 11월 첫 월요일
    return d + timedelta(days=1)


def election_context(today: date | None = None) -> str:
    """프롬프트에 붙이는 "오늘 기준 미국 선거 일정" 한 줄 + 연도 표기 규칙. 날짜에서 계산하므로 해마다 고칠 필요 없다."""
    from datetime import datetime, timedelta as td, timezone
    today = today or datetime.now(timezone(td(hours=9))).date()
    y = today.year
    cands = []
    for yy in range(y - 4, y + 5):
        if yy % 2 == 0:
            cands.append((_first_tuesday_after_first_monday(yy), "대선" if yy % 4 == 0 else "중간선거"))
    past = [c for c in cands if c[0] <= today][-1]
    pres_past = [c for c in cands if c[0] <= today and c[1] == "대선"][-1]
    nxt = next(c for c in cands if c[0] > today)
    nxt_pres = next(c for c in cands if c[0] > today and c[1] == "대선")
    return (f"[오늘 기준 미국 선거 사실 — 코드 계산] 가장 최근 선거: {past[0]} 미국 {past[1]}. 가장 최근 대통령 선거: {pres_past[0].year}년. "
            f"다음 선거: {nxt[0]} 미국 {nxt[1]}, 다음 대통령 선거: {nxt_pres[0]}. "
            "참고 자료에 나온 과거 선거 보도는 오늘의 일로 옮기지 말고, 선거를 언급할 때는 반드시 연도를 붙이세요"
            "(예: \"2024년 미국 대선\", \"2026년 중간선거\").")


# 수식어 없는 "대선"(앞이 한글 단어+공백이 아닌 경우)도 포함 — 시뮬레이션에서 "대선 이후 가파른 상승세"가 "미국" 없이 나왔다.
_US_PRES = re.compile(r"(?:(?:미국|미|美)\s?(?:대통령\s?)?|(?<![가-힣])(?<![가-힣]\s))대선")
_PAST_CUE = re.compile(r"20\d\d|지난 ?(?:대선|선거)|당시|과거|차기|다음 (?:대선|선거)|내년 대선|후보 확정|예비선거|경선")


def flag_bare_us_presidential(text: str, today: date | None = None) -> list:
    """연도·과거/차기 단서 없이 "미국 대선"만 쓴 문장 목록. 중간선거 해(4로 나눠 2가 남는 해)에만 검사한다.
    글을 고치지 않고 경고만 한다 — 2028 대선 기사처럼 정당한 대선 기사는 연도·단서로 통과."""
    from datetime import datetime, timedelta as td, timezone
    today = today or datetime.now(timezone(td(hours=9))).date()
    if today.year % 4 != 2:
        return []
    return [s.strip() for s in re.split(r"(?<=[.!?다])\s+|\n+", text or "") if _US_PRES.search(s) and not _PAST_CUE.search(s)]


_DATED = re.compile(r"^\((\d{4})-(\d{2})-(\d{2})\)\s*")


def split_by_age(headlines: list, today: date | None = None, days: int = 8):
    """(최근 days일 이내, 그보다 오래됨/날짜 불명) 두 목록으로 나눈다. 시뮬레이션(2026-11-15): 2024년 대선 직후 헤드라인을
    섞어 주면 모델이 "이번 주 소식"으로 쓴다 — 오래된 보도는 '이번 주 소식' 근거에서 빼고 배경으로만 보여준다."""
    from datetime import datetime, timedelta as td, timezone
    today = today or datetime.now(timezone(td(hours=9))).date()
    fresh, old = [], []
    for h in headlines:
        m = _DATED.match(h)
        try:
            (fresh if m and (today - date(int(m[1]), int(m[2]), int(m[3]))).days <= days else old).append(h)
        except ValueError:
            old.append(h)
    return fresh, old
