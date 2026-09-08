"""
scripts/ny_market_open_writer.py
-----------------------------------
뉴욕증시 개장(현지 09:30 ET) 시점 기사 자동 생성.
frontier_markets_writer.py("글로벌 마켓 동향", 마감 후 발행)의 개장판.

사용자 요청(2026-09-02): "뉴욕증시 개장 기사도 나갈 수 있나?" →
"개장 기사 프로세스도 크론잡에 붙여서 딱 정시에 돌아가게 하자".

데이터: 미국 3대 지수(S&P500·나스닥·다우) 개장 직후 실제가 + 아시아·유럽
주요 증시 마감(2026-09-08까지는 선물(ES=F 등)을 썼으나, "개장" 기사에
개장 전 선물 데이터를 쓰는 게 제목·시점과 어긋난다는 사용자 지적으로
실제 지수로 교체 — market_just_opened()가 09:30 ET 이후에만 실행을
허용하므로 이 시점엔 이미 실제 개장가를 조회할 수 있다).
(frontier_markets_writer.py의 fetch_yahoo_quote()/MAJOR_INDICES/
wikipedia_confirms()/verify_no_fabricated_names() 등을 그대로 import해서
쓴다 — 같은 야후 파이낸스 조회·검증 로직을 또 복붙하지 않는다).

실행: python scripts/ny_market_open_writer.py
권장: 뉴욕 09:30 ET 전후로 5~10분 간격으로 자주 호출(already_published()가
게이트하므로 자주 불러도 안전 — frontier_markets.yml과 동일한 방식).
market_just_opened()가 아니면 즉시 스킵하므로 GitHub Actions 낭비도 적다.
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

# 저장 시점 raw JSON 본문 차단. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from json_body_guard import unwrap_json_body
except Exception:
    def unwrap_json_body(text, _depth=0):
        return None

# 문체 검증은 style_guard.py 공용 모듈 사용(2026-09-02 공용화).
try:
    from style_guard import has_column_style
except Exception:
    def has_column_style(text: str) -> bool:
        return False

# Gemini 거부/placeholder 응답 감지(2026-09-08, 사용자 지적 — "메인툴에는
# 도입된 안전장치가 서브툴에 도입이 안된게 있는지 확인해봐"). id=142060
# 실사고(gemini_writer.py) 이후 만든 안전장치인데 이 파일엔 연결이 안
# 돼 있었다 — 이 스크립트도 검색 그라운딩(use_search)으로 원문을 못
# 찾으면 Gemini가 거부 응답을 낼 수 있다.
try:
    from content_guard import is_placeholder_response
except Exception:
    def is_placeholder_response(title, body):
        return False

# 날짜 환각 판정(2026-09-08, 사용자 지시 — "date_guard도 마저 붙여줘").
try:
    from date_guard import check_date_hallucination
except Exception:
    def check_date_hallucination(body, sources, base_date=None):
        return False, ""

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

# 검증된 한국어 기사를 영어로 번역(2026-09-03 신설, translate_guard.py로 공용화).
try:
    from translate_guard import translate_article
except Exception:
    def translate_article(title_ko: str, body_ko: str, call_gemini_fn, max_tokens: int = 3500) -> tuple[str, str]:
        return "", ""

# 야후 파이낸스 조회·주요국 지수 목록·검증 로직은 frontier_markets_writer.py
# 재사용(2026-08-21 검증된 fetch_yahoo_quote 패턴, chartPreviousClose 버그
# 수정 포함 — export_articles.py의 구버전 fetch_one()과 달리 정확함).
from frontier_markets_writer import (
    fetch_yahoo_quote, MAJOR_INDICES,
    wikipedia_confirms, extract_candidate_names, verify_no_fabricated_names,
)

# NYSE 휴장일 캘린더(market_calendar.py 공용화, 2026-09-07). import 실패해도
# 죽지 않도록 폴백은 주말만 거른다(기존 동작과 동일 — 휴장일 오탐 방지보다
# 스크립트 자체가 안 죽는 게 우선).
try:
    from market_calendar import is_us_market_closed, holiday_name as _us_holiday_name
except Exception:
    def is_us_market_closed(d) -> bool:
        return d.weekday() >= 5
    def _us_holiday_name(d):
        return None

GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

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


def call_gemini(prompt: str, max_tokens: int = 2500, start_tier: int = 3, use_search: bool = False,
                 max_stages: int | None = None) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.3, timeout=(10, 45), use_search=use_search,
                                max_stages=max_stages)


KST = timezone(timedelta(hours=9))
EDT = ZoneInfo("America/New_York")  # 뉴욕 시장 기준(서머타임 자동 반영)


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def now_edt() -> datetime:
    return datetime.now(timezone.utc).astimezone(EDT)


# 뉴욕증시 정규장 개장: 현지 09:30. cron 호출 간격(5~10분 권장)에 여유를 두려고
# 09:30~09:59 사이 아무 때나 호출되면 실행한다 — already_published()가 같은
# 날 중복 발행을 막으므로 이 30분 창 안에서 여러 번 걸려도 안전하다.
MARKET_OPEN_MINUTES = 9 * 60 + 30  # 09:30 → 570
MARKET_OPEN_WINDOW_MINUTES = 30    # 09:30 ~ 09:59


def market_just_opened() -> bool:
    now = now_edt()
    # 2026-09-07 실사고: 주말(weekday>=5)만 걸러내고 평일 NYSE 휴장일(노동절 등)은
    # 못 걸러내, 휴장 당일 09:30~09:59 창 동안 cron 호출마다 매번 개장 데이터
    # 수집을 시도하다 실패를 반복했다(시장이 안 열렸으니 정상 데이터가 없음) —
    # market_calendar.py의 공식 NYSE 캘린더로 휴장일도 함께 걸러낸다.
    if is_us_market_closed(now.date()):
        return False
    minutes = now.hour * 60 + now.minute
    return MARKET_OPEN_MINUTES <= minutes < MARKET_OPEN_MINUTES + MARKET_OPEN_WINDOW_MINUTES


def us_holiday_today() -> str | None:
    """오늘이 미국 증시 휴장일(평일 한정 — 주말은 별개)이면 휴장 사유명 반환."""
    now = now_edt()
    if now.weekday() >= 5:
        return None
    return _us_holiday_name(now.date())


def europe_focus_window() -> bool:
    """미국 증시 휴장일에 같은 09:30~09:59 ET 시간대를 유럽·아시아 동향
    기사로 대체할지 판단(사용자 요청 2026-09-07: "미국 증시 휴장인 날은
    유럽하고 다른 나라 위주로 쓰면 되는거 아닌가")."""
    if not us_holiday_today():
        return False
    now = now_edt()
    minutes = now.hour * 60 + now.minute
    return MARKET_OPEN_MINUTES <= minutes < MARKET_OPEN_MINUTES + MARKET_OPEN_WINDOW_MINUTES


# ── 미국 3대 지수 실제 개장가 ──────────────────────────────────
# 2026-09-08 사용자 지적: "개장을 쓰라고 했더니 왜 주요 지수 선물을 쓰고
# 있지?" — 원래 ES=F/NQ=F/YM=F(개장 전 선물) 데이터를 "개장" 기사에
# 썼던 게 제목·시점 표현과 어긋났다. market_just_opened()가 이미 09:30
# ET 이후에만 실행을 허용하므로, 선물 대신 실제 지수(MAJOR_INDICES의
# 미국 항목 — frontier_markets_writer.py와 동일 야후 조회 로직 재사용)로
# 개장 직후 실제 가격을 쓴다.
US_INDICES = [t for t in MAJOR_INDICES if t[0] == "미국"]  # (country, name, symbol, exchange)

# 개장 프리뷰에선 "밤사이(아시아·유럽) 무슨 일이 있었나"가 핵심이라 미국을 뺀
# 해외 주요 지수만 쓴다. MAJOR_INDICES는 (country, name, symbol, exchange) 4-튜플.
_OVERNIGHT_INDICES = [t for t in MAJOR_INDICES if t[0] != "미국"]


def fetch_open_data() -> dict:
    us_open = []
    for _country, name, symbol, _exchange in US_INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if not q or q.pop("_suspect", False):
            continue
        us_open.append({"name": name, "symbol": symbol, **q})

    overnight = []
    for country, name, symbol, exchange in _OVERNIGHT_INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if not q or q.pop("_suspect", False):
            continue
        overnight.append({"country": country, "name": name, "exchange": exchange, **q})

    overnight.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return {"us_open": us_open, "overnight": overnight}


# ── 기사 프롬프트 ────────────────────────────────────────────
TITLE_PREFIX = "[뉴욕증시 개장]"


def build_article_prompt(data: dict, today: date, fetch_time_str: str) -> str:
    today_str = today.strftime("%Y년 %m월 %d일")

    open_lines = "\n".join(
        f"- {f['name']}({f['symbol']}): {f['price']:.2f} (전일比 {f['pct']:+.2f}%)"
        for f in data["us_open"]
    )
    overnight_lines = "\n".join(
        f"- {i['country']} {i['name']} ({i['exchange']}): {i['price']:.2f} (전일比 {i['pct']:+.2f}%)"
        for i in data["overnight"]
    )
    top_overnight = data["overnight"][:3]
    top_overnight_str = ", ".join(f"{i['country']} {i['name']} {i['pct']:+.1f}%" for i in top_overnight)

    return f"""당신은 글로벌 마켓 전문 경제 기자입니다. 구글 검색으로 오늘 뉴욕증시
개장 직후 시장을 움직이는 실제 뉴스(연준 발언·경제지표 발표·기업 실적·
지정학 이슈 등)를 찾아서, 아래 실제 개장가·해외 증시 데이터와 결합해
"개장 기사"를 작성하세요. 검색 없이 수치만 나열하지 마세요.

오늘({today_str}) 뉴욕증시 개장(09:30 현지시간) 이후, {fetch_time_str}(현지시간) 기준 실제 지수 데이터:

[미국 3대 지수 — 개장 직후 실제가]
{open_lines}

[아시아·유럽 주요 증시 — 밤사이 마감]
{overnight_lines}

[해외 증시 변동폭 상위]
{top_overnight_str}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "{TITLE_PREFIX} "로 시작. 대괄호 포함 그대로 출력.
- 지수 방향(상승/하락/혼조)이 핵심 소재여야 합니다. 50자 이내로.
- 예: "{TITLE_PREFIX} 3대 지수 상승 출발…아시아 증시도 동반 강세"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 절대 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '시사합니다', '지켜볼 필요가 있습니다'
2. 구조 — 아래 섹션을 이 순서대로 다루세요. 각 섹션은 "◆ 섹션명" 한 줄로
   시작하고(번호나 대괄호 없이 ◆ 기호 + 섹션명만, 예: "◆ 뉴욕증시"), 그 다음
   줄부터 내용을 쓰세요. 섹션 제목 줄 앞에는 빈 줄을 하나씩 두세요. 아래
   섹션명 뒤에 붙은 설명은 그 섹션에 뭘 써야 하는지 알려주는 지시일 뿐이니
   지시 내용 자체를 본문에 옮겨 적지 마세요 — "◆ 섹션명" 한 줄만 그대로 쓰고
   바로 이어서 지시를 따른 기사 내용을 쓰세요.
   ⚠️ 한 문단이 3~4줄을 넘으면 안 됩니다(2026-09-09 사용자 지적). 섹션
   안에 문장이 여러 개면 의미 단위로 끊어 빈 줄로 문단을 나누세요.

   ◆ 뉴욕증시
   ⚠️ 반드시 미국 3대 지수(S&P500·나스닥·다우)의 실제 개장 이후 등락
   방향으로 시작하세요. "{today.day}일(현지시간) 뉴욕증시가 개장했다"처럼
   이미 개장한 시점임을 분명히 하고(선물·"개장을 앞두고" 같은 개장 전
   표현은 쓰지 마세요), 검색으로 찾은 실제 뉴스 기반으로 왜 그런 방향인지
   반영하세요. 지수가 엇갈리면(혼조) 그 사실 자체를 리드로 쓰세요.
   ⚠️ 장중 시황이라 시각이 지나면 수치가 달라진다 — 날짜만으론 부족하다.
   지수 수치를 언급하는 문장에 반드시 "{fetch_time_str}(현지시간) 기준"을
   함께 명시하세요(예: "{today.day}일(현지시간) {fetch_time_str} 기준 다우존스는...").

   ◆ 간밤 해외증시
   아시아·유럽 주요 증시의 마감 상황을 변동폭 상위 2~3개 중심으로, 실제
   이유(검색으로 찾은 뉴스)와 함께 서술. 거래소명 포함.

   ◆ 오늘의 관전 포인트
   검색으로 찾은 그날 예정된 경제지표 발표·연준 인사 발언·주요 기업 실적
   발표 등 개장 후 시장을 움직일 만한 일정을 서술. 확인 안 되는 내용은
   지어내지 말고, 못 찾으면 이 섹션 전체를 생략하세요(섹션 제목도 쓰지 마세요).
3. ⚠️ 날짜: 반드시 ◆ 뉴욕증시 섹션에 "{today.day}일(현지시간)" 형식으로 날짜를
   명시하세요. "오늘", "현재", 절대연도(2026년 등)는 금지.
4. 수치는 위 데이터를 그대로 사용하고 절대 지어내지 마세요.
5. 비라틴 문자 국가명·지수명·기업명은 정확한 한국어 표기로.
6. 분량: 600자 이상.
"""


def enforce_title_prefix(title: str) -> str:
    """제목 앞에 [뉴욕증시 개장] 태그를 강제 부착. 중복 부착 방지. (style_guard 공용화, 2026-09-09)"""
    return _sg_enforce_title_prefix(title, TITLE_PREFIX, "뉴욕증시개장")


# ── 미국 증시 휴장일 대체 콘텐츠(유럽·아시아 위주) ──────────────────
# 사용자 요청(2026-09-07): "미국 증시 휴장인 날은 유럽하고 다른 나라
# 위주로 쓰면 되는거 아닌가?" — 뉴욕이 안 열리는 날, 같은 09:30 ET
# 시간대(유럽은 이미 개장 후 몇 시간 지난 장중, 아시아는 이미 마감)를
# "뉴욕 개장 프리뷰" 대신 "유럽 증시 동향"으로 채운다. futures 없이
# _OVERNIGHT_INDICES(미국 제외 주요국)만 쓴다.
EUROPE_TITLE_PREFIX = "[유럽증시]"


def fetch_europe_data() -> dict:
    overnight = []
    for country, name, symbol, exchange in _OVERNIGHT_INDICES:
        q = fetch_yahoo_quote(symbol)
        time.sleep(0.5)
        if not q or q.pop("_suspect", False):
            continue
        overnight.append({"country": country, "name": name, "exchange": exchange, **q})
    overnight.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return {"overnight": overnight}


def build_europe_article_prompt(data: dict, today: date, holiday_name: str) -> str:
    today_str = today.strftime("%Y년 %m월 %d일")
    overnight_lines = "\n".join(
        f"- {i['country']} {i['name']} ({i['exchange']}): {i['price']:.2f} (전일比 {i['pct']:+.2f}%)"
        for i in data["overnight"]
    )
    top_str = ", ".join(f"{i['country']} {i['name']} {i['pct']:+.1f}%" for i in data["overnight"][:3])

    return f"""당신은 글로벌 마켓 전문 경제 기자입니다. 오늘은 미국 증시가
'{holiday_name}'으로 휴장하는 날입니다. 구글 검색으로 유럽·아시아 증시를
움직이는 실제 뉴스(ECB·BOE 발언, 유럽 경제지표, 아시아 마감 요인, 지정학
이슈 등)를 찾아서, 아래 데이터와 결합해 미국 증시가 쉬는 날의 "유럽·아시아
증시 동향" 기사를 작성하세요. 검색 없이 수치만 나열하지 마세요.

오늘({today_str}) 오전(한국시간 기준, 유럽은 장중·아시아는 마감 이후) 데이터:

[유럽·아시아 주요 증시]
{overnight_lines}

[변동폭 상위]
{top_str}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "{EUROPE_TITLE_PREFIX} "로 시작. 대괄호 포함 그대로 출력.
- 유럽 증시 방향(상승/하락/혼조)이 핵심 소재여야 합니다. 50자 이내로.
- 예: "{EUROPE_TITLE_PREFIX} 미국 휴장 속 유럽 증시 강세…ECB 발언 주목"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 절대 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '시사합니다', '지켜볼 필요가 있습니다'
2. 구조 — 아래 섹션을 이 순서대로 다루세요. 각 섹션은 "◆ 섹션명" 한 줄로
   시작하고, 그 다음 줄부터 내용을 쓰세요. 섹션 제목 줄 앞에는 빈 줄을
   하나씩 두세요. 섹션명 뒤 설명은 지시일 뿐이니 본문에 옮기지 마세요.
   ⚠️ 한 문단이 3~4줄을 넘으면 안 됩니다. 문장이 여러 개면 빈 줄로 나누세요.

   ◆ 미국 증시 휴장
   "{today.day}일(현지시간) 미국 금융시장은 '{holiday_name}'을 맞아 휴장한다"는
   사실을 짧게 한 문단으로 먼저 밝히세요.

   ◆ 유럽 증시
   ⚠️ 반드시 유럽 주요 지수(FTSE100·DAX·CAC40·유로스톡스50 등) 등락 방향으로
   시작하세요. 검색으로 찾은 실제 이유(ECB·BOE 발언, 경제지표, 개별 종목
   이슈 등)를 반영하세요. 거래소명을 포함하세요.

   ◆ 아시아 마감
   닛케이225 등 아시아 주요 증시의 이날 마감 상황을 실제 이유와 함께
   서술하세요.

   ◆ 오늘의 관전 포인트
   검색으로 찾은 그날 예정된 유럽 경제지표 발표·중앙은행 인사 발언 등
   시장을 움직일 만한 일정을 서술하세요. 못 찾으면 이 섹션 전체를
   생략하세요(섹션 제목도 쓰지 마세요).
3. ⚠️ 날짜: 반드시 ◆ 미국 증시 휴장 섹션에 "{today.day}일(현지시간)" 형식으로
   날짜를 명시하세요. "오늘", "현재", 절대연도(2026년 등)는 금지.
4. 수치는 위 데이터를 그대로 사용하고 절대 지어내지 마세요.
5. 비라틴 문자 국가명·지수명·기업명은 정확한 한국어 표기로.
6. 분량: 500자 이상.
"""


def enforce_europe_title_prefix(title: str) -> str:
    """제목 앞에 [유럽증시] 태그를 강제 부착. 중복 부착 방지. (style_guard 공용화, 2026-09-09)"""
    return _sg_enforce_title_prefix(title, EUROPE_TITLE_PREFIX, "유럽증시")


# 2026-09-08/09 공용화("공용모듈이 필요한 시스템이 더 있는지 점검해줘") —
# style_guard.parse_article_output()로 이식(8개 파일에 동일 코드 복붙).
# enforce_title_prefix도 동일 감사로 style_guard 공용 버전 사용 — 기존엔
# 대괄호([태그]) 형태만 인식해서, Gemini가 평문으로 접두어를 흘리면 태그가
# 중복 부착되는 결함이 있었다(본편/유럽판 둘 다 동일한 결함이었음).
try:
    from style_guard import parse_article_output, ensure_paragraphs, enforce_title_prefix as _sg_enforce_title_prefix
except Exception:
    def _sg_enforce_title_prefix(title, prefix, bare_name, particles=None):
        return f"{prefix} {(title or '').strip()}".strip()
    def ensure_paragraphs(text: str, target: int = 3, max_sentences_per_para: int = 4) -> str:
        return text
    def parse_article_output(text: str) -> tuple[str, str]:
        title, body = "", ""
        m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
        if m_title:
            title = m_title.group(1).strip()
        m_body = re.search(r"BODY:\s*(.+)$", text, re.S)
        if m_body:
            body = m_body.group(1).strip()
        return title, body


def call_gemini_article(prompt: str, max_tokens: int = 3000) -> str | None:
    text = call_gemini(prompt, max_tokens=max_tokens, use_search=True, max_stages=1, start_tier=3)
    if not text:
        print("  ⚠️ 검색 그라운딩 실패 → 검색 없이 재시도")
        text = call_gemini(prompt, max_tokens=max_tokens, use_search=False, start_tier=3)
    time.sleep(5)
    if not text:
        return None

    if has_column_style(text):
        print("  ⚠️ 논평체 감지 → 재생성")
        retried = call_gemini(
            prompt + "\n\n[재작성 지시] 논평/칼럼 문체가 섞였습니다. 사실 전달 중심으로만 다시 작성하세요.",
            max_tokens=max_tokens, use_search=True, max_stages=1,
        )
        if retried:
            text = retried
        time.sleep(5)

    _, body_probe = parse_article_output(text)
    fabricated = verify_no_fabricated_names(prompt, body_probe or text)
    if fabricated:
        print(f"  ⚠️ 수치 오류 의심({fabricated}) → 재생성")
        retried = call_gemini(
            prompt + f"\n\n[재작성 지시] 다음 수치를 시세 데이터와 다르게 잘못 썼습니다: {fabricated}. "
                     "가격·지수 수치는 반드시 위 데이터에 나온 값 그대로 쓰세요.",
            max_tokens=max_tokens, use_search=True, max_stages=1,
        )
        if retried:
            text = retried
        time.sleep(5)

    return text


# ── 이미지 ───────────────────────────────────────────────────
# 2026-09-08 사용자 지적: Pixabay 키워드 검색("stock market screen" 등
# 범용 키워드)이 뉴욕증시와 무관한 일반 스톡사진을 곧잘 골라옴 → "월가,
# NYSE 사진 몇 개를 돌려 쓰는 방향으로 가자" 지시로 위키미디어 커먼즈에서
# 실제 월스트리트/NYSE 랜드마크 사진만 검색하는 방식으로 교체
# (art_weekly_writer.py가 이미 쓰는 fetch_wikimedia_image() 패턴 재사용).
_WALLSTREET_IMAGE_QUERIES = [
    "New York Stock Exchange building facade",
    "Wall Street sign Manhattan",
    "Federal Hall Wall Street",
    "Charging Bull Wall Street",
    "New York Stock Exchange trading floor",
]


def fetch_open_image(article_date: date) -> tuple[str, str]:
    from article_image import fetch_wikimedia_image
    query = _WALLSTREET_IMAGE_QUERIES[article_date.toordinal() % len(_WALLSTREET_IMAGE_QUERIES)]
    url, credit = fetch_wikimedia_image(query)
    return url or "", credit or ""


# ── 기사 삽입 ────────────────────────────────────────────────
def already_published(article_date: date, url_key: str = "ny_market_open") -> bool:
    internal_url = f"internal://{url_key}_{article_date.isoformat()}"
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={"select": "id", "url": f"eq.{internal_url}", "limit": "1"},
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def insert_article(title_ko: str, summary_ko: str, article_date: date, image_url: str = "",
                    image_credit: str = "",
                    url_key: str = "ny_market_open", subcategory: str = "뉴욕증시개장",
                    country: str = "미국", country_flag: str = "🇺🇸",
                    note: str = "뉴욕증시 개장 자동 기사") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    if is_placeholder_response(title_ko, summary_ko):
        print(f"  ❌ Gemini 응답이 실제 기사가 아님(원문 부재 등 거부 응답) → 저장 차단: {title_ko[:60]}")
        return -1
    _unwrapped = unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            summary_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 저장 차단: {title_ko[:60]}")
            return -1

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    internal_url = f"internal://{url_key}_{article_date.isoformat()}"

    print("  → 영어 번역 생성 중...")
    title_en, summary_en = translate_article(title_ko, summary_ko, call_gemini)
    if not summary_en:
        print("  ⚠️ 영어 번역 실패 → 한국어만 저장")

    payload = {
        "title_en": title_en or title_ko,
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "금융",
        "subcategory": subcategory,
        "region": "global",
        "country": country,
        "country_flag": country_flag,
        "countries": [country] if country else [],
        "image_url": image_url,
        "image_credit": image_credit,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": note}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[ny_market_open_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not GEMINI_API_KEYS:
        print("  [SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    if europe_focus_window():
        _run_europe_focus()
        return

    if not market_just_opened():
        edt_now = now_edt()
        holiday = us_holiday_today()
        if holiday:
            print(f"  → 미국 증시 휴장일({holiday}), 유럽 포커스 시간대도 아님 → 스킵")
        else:
            print(f"  → 뉴욕 개장 시각(09:30 ET) 아님 ({edt_now.strftime('%H:%M')} EDT/EST) → 스킵")
        return

    article_date = now_edt().date()
    if already_published(article_date):
        print(f"  → {article_date} 뉴욕증시 개장 기사 이미 존재 → 스킵")
        return

    print("  → 야후 파이낸스에서 개장가·해외증시 데이터 수집 중...")
    fetch_time_str = now_edt().strftime("%H시 %M분")
    data = fetch_open_data()
    if len(data["us_open"]) < 2 or len(data["overnight"]) < 2:
        print(f"  [ERROR] 데이터 수집 부족(미국 지수 {len(data['us_open'])}건, "
              f"해외증시 {len(data['overnight'])}건) → 종료")
        return
    print(f"  → 미국 지수 {len(data['us_open'])}건, 해외증시 {len(data['overnight'])}건 수집 완료")

    print("  → Gemini로 기사 생성 중...")
    prompt = build_article_prompt(data, article_date, fetch_time_str)
    article_text = call_gemini_article(prompt)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    title, body = parse_article_output(article_text)
    title = enforce_title_prefix(title)

    if not title or not body:
        print("  [ERROR] 응답 파싱 실패")
        return

    body = ensure_paragraphs(body)

    _dg_bad, _dg_reason = check_date_hallucination(
        body, [{"source_published_at": article_date.isoformat()}], base_date=article_date
    )
    if _dg_bad:
        print(f"  ⚠️ [{_dg_reason}] → 스킵")
        return

    image_url, image_credit = fetch_open_image(article_date)

    article_id = insert_article(title, body, article_date, image_url, image_credit)
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")


def _run_europe_focus():
    holiday = us_holiday_today()
    article_date = now_edt().date()
    url_key = "ny_market_open_europe"

    if already_published(article_date, url_key=url_key):
        print(f"  → {article_date} 유럽증시 동향 기사 이미 존재 → 스킵")
        return

    print(f"  → 미국 증시 휴장일({holiday}) → 유럽·아시아 동향으로 대체")
    print("  → 야후 파이낸스에서 유럽·아시아 증시 데이터 수집 중...")
    data = fetch_europe_data()
    if len(data["overnight"]) < 2:
        print(f"  [ERROR] 데이터 수집 부족(해외증시 {len(data['overnight'])}건) → 종료")
        return
    print(f"  → 해외증시 {len(data['overnight'])}건 수집 완료")

    print("  → Gemini로 기사 생성 중...")
    prompt = build_europe_article_prompt(data, article_date, holiday)
    article_text = call_gemini_article(prompt)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    title, body = parse_article_output(article_text)
    title = enforce_europe_title_prefix(title)

    if not title or not body:
        print("  [ERROR] 응답 파싱 실패")
        return

    body = ensure_paragraphs(body)

    _dg_bad, _dg_reason = check_date_hallucination(
        body, [{"source_published_at": article_date.isoformat()}], base_date=article_date
    )
    if _dg_bad:
        print(f"  ⚠️ [{_dg_reason}] → 스킵")
        return

    image_url, image_credit = fetch_open_image(article_date)

    article_id = insert_article(
        title, body, article_date, image_url, image_credit,
        url_key=url_key, subcategory="유럽증시동향", country="", country_flag="",
        note="유럽증시 동향 자동 기사(미국 증시 휴장일 대체)",
    )
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")


if __name__ == "__main__":
    main()
