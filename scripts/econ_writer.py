"""
econ_writer.py
--------------
econ_events 테이블의 이벤트 중 actual_value가 없고,
발표 예정 시각(현지시간 기준)이 이미 지난 항목을 대상으로
Gemini 검색을 두 번 독립 호출해 결과를 검증한 뒤 actual_value 업데이트 및
기사를 자동 생성합니다.

실행: python scripts/econ_writer.py
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

# Gemini 거부/placeholder 응답 감지(2026-09-08, 사용자 지적 — "메인툴에는
# 도입된 안전장치가 서브툴에 도입이 안된게 있는지 확인해봐"). id=142060
# 실사고(gemini_writer.py) 이후 만든 안전장치인데 이 파일엔 연결이 안
# 돼 있었다.
try:
    from content_guard import is_placeholder_response
except Exception:
    def is_placeholder_response(title, body):
        return False

# 날짜 환각 판정(2026-09-08, 사용자 지시 — "date_guard도 마저 붙여줘").
# event_date가 이미 정확히 알려진 값이라 sources를 굳이 크롤링할 필요 없이
# source_published_at 하나만 채운 합성 소스로 대조한다(gemini_writer.py의
# 실제 원문 리스트 대신 — 여기선 이벤트 발표일 자체가 유일하고 확실한 근거).
try:
    from date_guard import check_date_hallucination
except Exception:
    def check_date_hallucination(body, sources, base_date=None):
        return False, ""

# articles 테이블 삽입 공용 로직(2026-09-02, article_store.py로 공용화).
try:
    from article_store import insert_final_article
except Exception:
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

# 주요국 중앙은행 정책금리 공식 데이터 조회(2026-09-08, 사용자 신고로
# Gemini 검색 기반 actual_value 채우기가 실측상 거의 항상 실패하는 것을
# 발견해 도입 — central_bank_rates.py 참고). import 실패해도 죽지 않도록
# 항상 None(지원 안 됨 → 기존 Gemini 경로로 폴백)으로 처리한다.
try:
    from central_bank_rates import fetch_official_rate
except Exception:
    def fetch_official_rate(country: str, event_date: str | None = None):
        return None

# 배경 컨텍스트(총재 발언·결정 배경 등) 보강용 — gemini_writer.py의
# 트렌드 기사 배경보강과 같은 함수 재사용(2026-09-11, 사용자 지적 — "ECB 관련
# 기사가 이미 수십 건 수집돼 있는데 그걸 참고했다면 멘트도 넣을 수 있었을
# 텐데"). econ_writer.py는 그동안 econ_events 테이블만 보고 메인 RSS/GDELT
# 파이프라인이 이미 모아둔 articles 테이블을 전혀 참고하지 않아, 매번 맨땅에서
# 배경·인용문을 지어내고 있었다(id=156302 라가르드 발언 누락 등 여러 신고의
# 공통 원인). NVIDIA 우선 → 실패 시 검색 그라운딩 폴백 + 교차검증까지 이미
# 갖춰진 함수라 새로 안전장치를 만들 필요가 없다.
try:
    from gemini_summarizer import fetch_background_context
except Exception:
    def fetch_background_context(query: str, source_context: str = "") -> str:
        return ""

# ── 설정 ────────────────────────────────────────────────────
GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
SUPABASE_URL         = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

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

# 발표 시각 이후 얼마나 지나야 수집 시작하는지 (오류 방지 버퍼)
ANNOUNCEMENT_BUFFER_MINUTES = 30

# actual_value 없는 이벤트를 최대 며칠 전까지 소급 처리할지
MAX_LOOKBACK_DAYS = 5

# 두 Gemini 응답 숫자 허용 오차
RATE_TOLERANCE = 0.01


# ── Supabase 헬퍼 (article_store.py로 공용화, 2026-09-02) ──────────
try:
    from article_store import sb_headers as _sb_headers
    from article_store import sb_url as _sb_articles_url
except Exception:
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_articles_url():
        return f"{SUPABASE_URL}/rest/v1/articles"

def _sb_events_url():
    return f"{SUPABASE_URL}/rest/v1/econ_events"


# 2026-09-11 신설 — 메인 RSS/GDELT 파이프라인이 이미 수집해둔 관련 기사
# 제목을 찾아 배경 컨텍스트의 source_context로 넘긴다(위 fetch_background_context
# import 주석 참고). 본문 전체가 아니라 제목만 모아 넘기는 이유는 gemini_writer.py
# find_continuing_story() 등 기존 패턴과 동일 — 제목만으로도 "이런 각도의 보도가
# 실제로 있었다"는 근거는 충분하고, 본문까지 넣으면 프롬프트가 과도하게 길어진다.
def _fetch_related_article_titles(country: str, bank_ko: str, event_date: str, limit: int = 12) -> str:
    try:
        ev = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        return ""
    start = (ev - timedelta(days=10)).isoformat()

    m = re.search(r"\(([A-Za-z]+)\)", bank_ko)
    abbr = m.group(1) if m else ""
    or_filter = f"title_ko.ilike.*{abbr}*,title_ko.ilike.*{country}*" if abbr else f"title_ko.ilike.*{country}*"

    try:
        res = requests.get(
            _sb_articles_url(),
            headers=_sb_headers(),
            params={
                "select": "title_ko,created_at",
                "or": f"({or_filter})",
                "created_at": f"gte.{start}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            timeout=10,
        )
        if res.status_code not in (200, 206):
            return ""
        rows = res.json()
    except Exception as e:
        print(f"  [WARN] 관련기사 조회 실패: {e}")
        return ""

    titles = [r["title_ko"] for r in rows if r.get("title_ko")]
    return "\n".join(f"- {t}" for t in titles)


# ── 발표 시각 체크 ───────────────────────────────────────────
def announcement_has_passed(event: dict) -> bool:
    """
    이벤트의 발표 예정 시각(현지시간)이 현재 UTC 기준으로 이미 지났는지 확인.
    - event_date + announcement_offset_hours(일) + event_time(HH:MM) → 현지 naive datetime
    - timezone 컬럼으로 변환 → UTC 비교
    - timezone/event_time 없으면 True(당일이면 처리 허용)
    """
    tz_str   = event.get("timezone")
    ev_time  = event.get("event_time")   # "HH:MM"
    ev_date  = event.get("event_date")   # "YYYY-MM-DD"
    offset_d = event.get("announcement_offset_hours") or 0  # 발표일 오프셋(일)

    if not tz_str or not ev_time or not ev_date:
        return True

    try:
        tz        = ZoneInfo(tz_str)
        base_date = date.fromisoformat(ev_date)
        ann_date  = base_date + timedelta(days=offset_d)
        hh, mm    = map(int, ev_time.split(":"))
        ann_local = datetime(ann_date.year, ann_date.month, ann_date.day,
                             hh, mm, tzinfo=tz)
        ann_with_buffer = ann_local + timedelta(minutes=ANNOUNCEMENT_BUFFER_MINUTES)
        return datetime.now(timezone.utc) >= ann_with_buffer.astimezone(timezone.utc)
    except Exception as e:
        print(f"    [WARN] 시각 변환 오류 ({e}) → 스킵 처리")
        return False


# ── 대상 이벤트 조회 ─────────────────────────────────────────
def get_pending_events() -> list:
    """
    actual_value IS NULL 이고 event_date가 MAX_LOOKBACK_DAYS일 이내인 이벤트 조회.
    발표 시각 경과 여부는 Python에서 필터링.
    """
    today     = now_kst().date()
    date_from = (today - timedelta(days=MAX_LOOKBACK_DAYS)).isoformat()
    date_to   = today.isoformat()

    res = requests.get(
        f"{_sb_events_url()}"
        f"?select=id,title,event_date,event_time,timezone,announcement_offset_hours,"
        f"country,importance,previous_value,forecast_value,actual_value,description"
        f"&actual_value=is.null"
        f"&event_date=gte.{date_from}"
        f"&event_date=lte.{date_to}"
        f"&order=event_date.asc",
        headers=_sb_headers(),
        timeout=15,
    )
    if res.status_code not in (200, 206):
        print(f"[ERROR] 이벤트 조회 실패 {res.status_code}")
        return []

    all_events = res.json()
    passed  = [e for e in all_events if announcement_has_passed(e)]
    skipped = len(all_events) - len(passed)
    if skipped:
        print(f"  → {skipped}건 발표 시각 미경과로 스킵")
    return passed


# ── Gemini 호출 (키 로테이션) ────────────────────────────────
def call_gemini(prompt: str, max_tokens: int = 800,
                use_search: bool = False, start_tier: int = 4) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.1, timeout=(10, 45), use_search=use_search)


# 원문에 없는 고유명사·수식어 날조 검사 공용화(2026-09-08, 사용자 지적 —
# "공용모듈이 필요한 시스템이 더 있는지 점검해줘"). 이 파일 로컬 버전은
# gemini_writer.py의 2026-08-25 id=98010 수식어 날조 탐지 개선을 못 받은
# 옛 버전이었다 — fabrication_guard.py로 이식.
try:
    from fabrication_guard import wikipedia_confirms, verify_no_fabricated_names as _fg_verify_no_fabricated_names
except Exception:
    def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
        return False
    def _fg_verify_no_fabricated_names(source_prompt, body, call_gemini_fn):
        return ""


def verify_no_fabricated_names(source_prompt: str, body: str) -> str:
    return _fg_verify_no_fabricated_names(source_prompt, body, call_gemini)


# ── 숫자 파싱 ────────────────────────────────────────────────
def parse_rate(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{1,4})?)\s*%", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m2 = re.search(r"\b(\d{1,2}\.\d{1,4})\b", text)
    if m2:
        try:
            v = float(m2.group(1))
            if 0 <= v <= 50:
                return v
        except ValueError:
            pass
    return None


def rates_match(r1: float, r2: float) -> bool:
    return abs(r1 - r2) <= RATE_TOLERANCE


# ── 검색 프롬프트 ────────────────────────────────────────────
def build_search_prompt(event: dict) -> str:
    title      = event.get("title", "")
    country    = event.get("country", "")
    event_date = str(event.get("event_date", ""))
    prev       = event.get("previous_value") or "N/A"
    forecast   = event.get("forecast_value") or "N/A"

    return (
        f"다음 중앙은행 금리결정 이벤트의 실제 결정 금리(actual policy rate)를 검색해 알려주세요.\n\n"
        f"이벤트: {title}\n"
        f"국가: {country}\n"
        f"예정일: {event_date}\n"
        f"직전 금리: {prev}\n"
        f"예상 금리: {forecast}\n\n"
        f"오직 결정된 실제 금리 숫자(%)만 답변하세요. "
        f"결과가 아직 발표되지 않았다면 '미발표'라고만 답하세요. "
        f"다른 설명은 불필요합니다."
    )


# 중앙은행 한국어 정식명칭(영문 약칭 포함) — 2026-09-11 신설(실사고 id=156302:
# "제목은 국가명 중앙은행 형태로"라는 일반 지시만 주니 유로존을 "유로존
# 중앙은행"이라는 존재하지 않는 이름으로 지어냈다 — 정식명칭은 econ_events.title에
# 이미 정확히 들어있는데도 프롬프트가 이를 무시하고 매번 새로 조합하게 시켰던 게
# 근본 원인. 국가명 조합 대신 이 사전을 그대로 쓰도록 프롬프트에 못박는다.
CENTRAL_BANK_KO = {
    "나이지리아": "나이지리아 중앙은행(CBN)",
    "케냐": "케냐 중앙은행(CBK)",
    "인도네시아": "인도네시아 중앙은행(BI)",
    "태국": "태국 중앙은행(BOT)",
    "필리핀": "필리핀 중앙은행(BSP)",
    "남아공": "남아공 중앙은행(SARB)",
    "이집트": "이집트 중앙은행(CBE)",
    "미국": "미국 연방준비제도(Fed)",
    "유로존": "유럽중앙은행(ECB)",
    "일본": "일본은행(BOJ)",
    "영국": "영란은행(BOE)",
}


# ── 기사 생성 프롬프트 ───────────────────────────────────────
def build_article_prompt(event: dict, actual_value: str, context_block: str = "") -> str:
    title      = event.get("title", "")
    country    = event.get("country", "")
    event_date = str(event.get("event_date", ""))
    prev       = event.get("previous_value") or "N/A"
    forecast   = event.get("forecast_value") or "N/A"
    desc       = event.get("description") or ""
    bank_ko    = CENTRAL_BANK_KO.get(country, f"{country} 중앙은행")

    try:
        av = float(actual_value.replace("%", ""))
        pv = float(str(prev).replace("%", "")) if prev != "N/A" else None
        if pv is not None:
            diff = round(av - pv, 4)
            direction = "인상" if diff > 0 else ("인하" if diff < 0 else "동결")
            direction_line = (
                f"- 변화: 직전 {prev} 대비 {direction} (동결이면 변동폭 언급 불필요, "
                f"인상/인하면 {abs(diff):g}%p {direction})"
            )
        else:
            direction = "확인불가"
            direction_line = (
                "- 변화: 직전 금리 정보가 없어 인상/인하/동결 여부를 알 수 없습니다. "
                "단정적으로 서술하지 말고 방향을 명시하지 않는 중립적 표현("
                "'기준금리를 X%로 결정했다' 형태)만 쓰세요."
            )
    except Exception:
        direction = "확인불가"
        direction_line = "- 변화: 알 수 없음. 방향을 단정하지 마세요."

    context_section = (
        f"\n[참고 — 실제로 수집된 관련 보도 내용(이 안에 있는 사실·인용문만 사용)]\n{context_block}\n"
        if context_block else
        "\n[참고] 이번 회의에 대한 별도 수집 자료가 없습니다. 직접 인용문(따옴표)은 "
        "절대 지어내지 말고, 위 [이벤트 정보]에 없는 구체적 수치·발언·전망치도 "
        "새로 만들지 마세요.\n"
    )

    return f"""당신은 프론티어 마켓 전문 경제 뉴스 기자입니다.
아래 정보를 바탕으로 한국어 뉴스 스타일 기사를 작성하세요.

[이벤트 정보]
- 이벤트명: {title}
- 중앙은행 정식명칭: {bank_ko}
- 국가: {country}
- 예정일: {event_date}
- 결정 금리: {actual_value}
{direction_line}
- 예상 금리: {forecast}
- 추가 설명: {desc}
{context_section}
[작성 규칙]
1. 제목(title)과 본문(body)을 분리해 아래 형식으로 출력하세요:
   TITLE: <제목>
   BODY: <본문>

2. 제목은 반드시 위에 명시된 "중앙은행 정식명칭"을 그대로 사용해
   "{bank_ko}, 기준금리 {actual_value}로 {direction if direction != '확인불가' else '결정'}" 형태로 작성하세요.
   국가명과 '중앙은행'을 임의로 조합해 다른 명칭을 새로 만들지 마세요
   (예: 유로존이라고 "유로존 중앙은행"이라 쓰면 안 됩니다 — 정식명칭은 유럽중앙은행입니다).
3. 본문 첫 문장에서만 중앙은행 정식명칭 전체를 쓰고, 그 이후 문단에서는
   괄호 안 영문 약칭만 사용하세요(예: 첫 문장 "유럽중앙은행(ECB)은...", 이후 "ECB는...").
4. 첫 문장은 "언제(N일 현지시간) 무엇을 결정했다"는 핵심 사실을 바로 전달하세요.
   "~을 통해 ~을 점검했다/살펴봤다" 같은 우회적·완곡한 서술 대신 "~와 함께
   ~을 발표했다"처럼 직접적으로 쓰세요.
5. 본문은 700자 이상, 2~3문장으로 끊어 문단을 나누는 스트레이트 뉴스 문체
   (감정·논평 표현 금지).
6. 날짜는 '현지시간' 기준으로 "N일(현지시간)" 형식. 절대날짜 금지, '오늘'·'현재' 금지.
7. 금리 결정 내용, 전망치 대비 결과, 직전 금리 대비 변화(위 [이벤트 정보]의
   "변화" 항목 지시를 그대로 따를 것)를 포함.
8. 해당 국가 경제 맥락(물가, 환율, 경제성장 등)은 반드시 위 [참고] 자료나
   [이벤트 정보]에 근거해서만 쓰세요. 그런 자료가 없으면 "물가가 안정화되고
   있다/우려된다" 같은 구체적 방향성 주장을 지어내지 말고, 중앙은행이
   물가 안정을 위해 통화정책을 운영한다는 원론적 서술에 그치세요.
9. 비라틴 문자 국가명·지명은 반드시 한국어 음역.
10. 논평/칼럼 문체 금지: '~를 보여줍니다', '~기대됩니다', '~주목됩니다' 등 사용 금지.
"""


# ── 기사 삽입 ────────────────────────────────────────────────
COUNTRY_FLAG_MAP = {
    "나이지리아": "🇳🇬", "이집트": "🇪🇬", "남아공": "🇿🇦", "케냐": "🇰🇪",
    "가나": "🇬🇭", "에티오피아": "🇪🇹", "탄자니아": "🇹🇿", "모로코": "🇲🇦",
    "앙골라": "🇦🇴", "코트디부아르": "🇨🇮", "카메룬": "🇨🇲",
    "태국": "🇹🇭", "필리핀": "🇵🇭", "베트남": "🇻🇳", "인도네시아": "🇮🇩",
    "말레이시아": "🇲🇾", "방글라데시": "🇧🇩", "파키스탄": "🇵🇰", "스리랑카": "🇱🇰",
    "카자흐스탄": "🇰🇿", "우즈베키스탄": "🇺🇿", "조지아": "🇬🇪",
    "사우디아라비아": "🇸🇦", "아랍에미리트": "🇦🇪", "쿠웨이트": "🇰🇼", "카타르": "🇶🇦",
    "요르단": "🇯🇴", "이라크": "🇮🇶", "바레인": "🇧🇭", "오만": "🇴🇲",
    "튀르키예": "🇹🇷", "폴란드": "🇵🇱", "체코": "🇨🇿", "헝가리": "🇭🇺", "루마니아": "🇷🇴",
}

# 위키미디어 커먼즈 검색용 영문 기관명(article_image.py). econ_events는 소수의
# 고정된 중앙은행만 다루는 큐레이션 테이블이라 매핑으로 충분하다 — 새 국가가
# 추가되면 이 사전에 없어도 그냥 Wikimedia 단계를 건너뛰고 Pixabay로 대체된다.
CENTRAL_BANK_EN = {
    "나이지리아": "Central Bank of Nigeria",
    "남아공": "South African Reserve Bank",
    "미국": "Federal Reserve",
    "영국": "Bank of England",
    "유로존": "European Central Bank",
    "이집트": "Central Bank of Egypt",
    "일본": "Bank of Japan",
    "태국": "Bank of Thailand",
    "필리핀": "Bangko Sentral ng Pilipinas",
    "글로벌": "International Monetary Fund",
}


def insert_article(title_ko: str, summary_ko: str,
                   country: str, event_id: int,
                   image_url: str = "", image_credit: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    if is_placeholder_response(title_ko, summary_ko):
        print(f"  ❌ Gemini 응답이 실제 기사가 아님(원문 부재 등 거부 응답) → 저장 차단: {title_ko[:60]}")
        return -1
    now_str     = now_kst().strftime("%Y-%m-%d %H:%M")
    cluster_key = f"econ_rate_{event_id}"
    flag        = COUNTRY_FLAG_MAP.get(country, "")
    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": f"internal://{cluster_key}",
        "source": "NewsFinal",
        "category": "경제",
        "subcategory": cluster_key,
        "region": "글로벌",
        "country": country,
        "country_flag": flag,
        "countries": [country],
        "image_url": image_url,
        "image_credit": image_credit,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "금리결정 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


# ── econ_events 업데이트 ─────────────────────────────────────
def update_event_actual(event_id: int, actual_value: str) -> bool:
    res = requests.patch(
        f"{_sb_events_url()}?id=eq.{event_id}",
        headers=_sb_headers(),
        json={"actual_value": actual_value, "is_verified": True},
        timeout=15,
    )
    return res.status_code in (200, 204)


# ── 이미 기사가 있는지 확인 ──────────────────────────────────
def article_exists(event_id: int) -> bool:
    cluster_key = f"econ_rate_{event_id}"
    res = requests.get(
        _sb_articles_url(),
        headers=_sb_headers(),
        params={"select": "id", "subcategory": f"eq.{cluster_key}", "limit": "1"},
        timeout=10,
    )
    if res.status_code in (200, 206):
        return len(res.json()) > 0
    return False


# ── 논평체 검사 ──────────────────────────────────────────────
# 2026-09-08 수정("메인툴에는 도입된 안전장치가 서브툴에 도입이 안된게
# 있는지 확인해봐"): 이 파일이 style_guard.py 공용화(2026-09-02) 이전부터
# 있던 로컬 사본을 계속 쓰고 있어서, 그 뒤 추가된 패턴(화자 없는 전망/분석형
# 마무리 문장 7종, 2026-07-28)과 합쇼체(-습니다) 감지·자동변환을 못 받고
# 있었다.
try:
    from style_guard import has_column_style, has_polite_ending, to_plain_style
except Exception:
    BANNED_STYLE_PATTERNS = [
        r"보여줍니다", r"보여주고 있습니다", r"도모하고 있습니다",
        r"강조하고 있습니다", r"시사합니다", r"주목됩니다",
        r"평가된다", r"평가받고 있습니다", r"기대됩니다",
        r"지켜볼 필요가 있습니다", r"지켜봐야 할 것입니다",
    ]
    def has_column_style(text: str) -> bool:
        return bool(text and any(re.search(p, text) for p in BANNED_STYLE_PATTERNS))
    def has_polite_ending(text: str) -> bool:
        return False
    def to_plain_style(text: str) -> str:
        return text


# ── TITLE / BODY 파싱 ────────────────────────────────────────
# 2026-09-08 공용화("공용모듈이 필요한 시스템이 더 있는지 점검해줘") —
# style_guard.parse_article_output()로 이식(8개 파일에 동일 코드 복붙).
try:
    from style_guard import parse_article_output, ensure_paragraphs
except Exception:
    def ensure_paragraphs(text: str, target: int = 3, max_sentences_per_para: int = 4) -> str:
        return text
    def parse_article_output(text: str) -> tuple[str, str]:
        m_title = re.search(r"TITLE:\s*(.+)", text)
        m_body  = re.search(r"BODY:\s*([\s\S]+)", text)
        title   = m_title.group(1).strip() if m_title else ""
        body    = m_body.group(1).strip()  if m_body  else ""
        return title, body


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"[econ_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    events = get_pending_events()
    if not events:
        print("[econ_writer] 처리할 이벤트 없음 — 종료")
        return

    print(f"[econ_writer] 발표 시각 경과 이벤트 {len(events)}건")

    for event in events:
        eid     = event["id"]
        title   = event.get("title", "")
        country = event.get("country", "")
        edate   = event.get("event_date", "")
        tz_str  = event.get("timezone", "")
        ev_time = event.get("event_time", "")
        offset  = event.get("announcement_offset_hours") or 0

        print(f"\n  ▶ [{eid}] {title} ({edate}, 발표 +{offset}일 {ev_time} {tz_str})")

        if article_exists(eid):
            print(f"    → 이미 기사 존재, 스킵")
            continue

        # ── Step 0: 공식 데이터 소스 우선 조회(2026-09-08, 사용자 신고 —
        # "해외 금리 관련 기사도 거의 나오질 않고 있는데" 원인 진단: 아래
        # Gemini 검색 2회 일치 검증이 실측상 거의 항상 실패하고 있었다
        # (DB 확인 — 최근 발표된 모든 국가 금리 이벤트가 actual_value=null).
        # central_bank_rates.py로 미국(FRED)·유로존(FRED)·영국(BOE) 공식
        # 데이터를 직접 조회해, 성공하면 Gemini 검색 전체를 건너뛴다.
        # 지원 안 되는 국가(일본 포함)는 official_rate가 None이라 그대로
        # 아래 기존 경로로 폴백한다.
        #
        # 2026-09-11 수정(실사고 id=156302 — ECB 9/10 회의 기사가 실제 결정
        # 2.50%가 아니라 회의 직전 금리 2.25%를 그대로 오채택해 발행됨):
        # central_bank_rates.fetch_official_rate()가 이제 event_date를 받아
        # "이벤트일 직전 값"과 "지금 최신값"을 자체 비교해 실제로 안 바뀐
        # 경우(=아직 발효 전이라 반영이 안 됐을 수 있는 경우) None을 반환한다
        # — 여기서 추가 날짜 검증을 할 필요 없이 성공 시 그대로 신뢰한다.
        # 직전값(prev_official)도 함께 받아 아래 build_article_prompt에 넘겨
        # LLM이 인상/인하/동결을 직접 추론하게 두지 않고 결정론적으로 계산한다.
        actual_str = None
        prev_official = None
        official = fetch_official_rate(country, event_date=edate)
        if official:
            off_rate, off_date, prev_official = official
            actual_str = f"{off_rate:.2f}%"
            print(f"    ✓ 공식 소스 조회 성공: {actual_str} (기준일 {off_date}, 직전 {prev_official})")
        else:
            print(f"    → 공식 소스로 확정 불가(미지원 국가 또는 아직 발효 반영 전) → Gemini 검색으로 폴백")

        if prev_official is not None and event.get("previous_value") in (None, "", "N/A"):
            event["previous_value"] = f"{prev_official:.2f}%"

        if actual_str is None:
            # ── Step 1: Gemini 검색 1차
            prompt = build_search_prompt(event)
            print(f"    → Gemini 1차 검색...")
            resp1 = call_gemini(prompt, max_tokens=100, use_search=True)
            time.sleep(5)

            if not resp1:
                print(f"    → 1차 응답 없음, 스킵")
                continue
            print(f"    → 1차 응답: {resp1[:80]}")

            if "미발표" in resp1:
                print(f"    → 아직 미발표, 스킵")
                continue

            rate1 = parse_rate(resp1)
            if rate1 is None:
                print(f"    → 1차 숫자 파싱 실패 ({resp1[:60]}), 스킵")
                continue

            # ── Step 2: Gemini 검색 2차 (독립 검증)
            print(f"    → Gemini 2차 검색...")
            resp2 = call_gemini(prompt, max_tokens=100, use_search=True)
            time.sleep(5)

            if not resp2:
                print(f"    → 2차 응답 없음, 스킵")
                continue
            print(f"    → 2차 응답: {resp2[:80]}")

            if "미발표" in resp2:
                print(f"    → 2차 미발표 응답, 스킵")
                continue

            rate2 = parse_rate(resp2)
            if rate2 is None:
                print(f"    → 2차 숫자 파싱 실패 ({resp2[:60]}), 스킵")
                continue

            # ── Step 3: 검증
            if not rates_match(rate1, rate2):
                print(f"    → 불일치: {rate1}% vs {rate2}% → 스킵 (다음 사이클 재시도)")
                continue

            actual_str = f"{rate1:.2f}%"
            print(f"    ✓ 검증 완료: {actual_str}")

        # ── Step 4: econ_events 업데이트
        if not update_event_actual(eid, actual_str):
            print(f"    [ERROR] econ_events 업데이트 실패")
            continue
        print(f"    ✓ econ_events 업데이트 완료")

        # ── Step 4.5: 배경 컨텍스트 수집(2026-09-11 신설 — 사용자 지적:
        # "한국에서 나온 ECB 관련 기사도 수십 개에 달하는데" 메인 파이프라인이
        # 이미 모아둔 articles를 econ_writer.py가 전혀 안 보고 있었다). 먼저
        # 실제 수집된 관련기사 제목을 찾아 source_context로 주고, 그걸 근거로
        # fetch_background_context(NVIDIA→검색그라운딩+교차검증)가 총재 발언·
        # 결정 배경을 보강한다. 아무것도 못 찾으면 빈 문자열 — build_article_prompt가
        # 그 경우 "지어내지 말고 원론적 서술만" 쪽으로 자동 폴백한다.
        bank_ko = CENTRAL_BANK_KO.get(country, f"{country} 중앙은행")
        related_titles = _fetch_related_article_titles(country, bank_ko, edate)
        if related_titles:
            print(f"    → 관련기사 {related_titles.count(chr(10)) + 1}건 발견, 배경 보강 중...")
        context_block = fetch_background_context(
            f"{bank_ko} {edate} 통화정책회의 금리 {actual_str} 결정 배경, 총재 기자회견 핵심 발언",
            source_context=related_titles,
        )

        # ── Step 5: 기사 생성
        print(f"    → 기사 생성 중...")
        article_text = call_gemini(build_article_prompt(event, actual_str, context_block),
                                   max_tokens=1500, use_search=False)
        time.sleep(8)

        if not article_text:
            print(f"    [ERROR] 기사 생성 실패")
            continue

        if has_column_style(article_text):
            print(f"    ⚠️ 논평체 감지 → 재생성")
            retry_prompt = (
                build_article_prompt(event, actual_str, context_block)
                + "\n\n[재작성 지시] 앞서 작성한 결과에 논평/칼럼 문체가 섞였습니다. "
                  "감정·의견 표현을 완전히 배제하고 사실 전달 중심으로만 다시 작성하세요."
            )
            article_text = call_gemini(retry_prompt, max_tokens=1500, use_search=False) or article_text
            time.sleep(5)

        fabricated = verify_no_fabricated_names(build_article_prompt(event, actual_str, context_block), article_text)
        if fabricated:
            print(f"    ⚠️ 원문에 없는 고유명사 감지({fabricated}) → 재생성")
            retry_prompt2 = (
                build_article_prompt(event, actual_str, context_block)
                + f"\n\n[재작성 지시] 다음 이름을 원문에 없는 표현으로 잘못 지어냈습니다: {fabricated}. "
                  "고유명사는 원본 자료에 나온 표기를 그대로 옮기고, 확신할 수 없으면 지어내지 말고 원문 표기를 그대로 쓰세요."
            )
            article_text = call_gemini(retry_prompt2, max_tokens=1500, use_search=False) or article_text
            time.sleep(5)

        if has_polite_ending(article_text):
            converted = to_plain_style(article_text)
            if converted != article_text:
                print("    🔧 합쇼체(-습니다) 감지 → 자동 변환 적용")
                article_text = converted

        art_title, art_body = parse_article_output(article_text)

        if not art_title or not art_body:
            print(f"    [ERROR] TITLE/BODY 파싱 실패\n{article_text[:200]}")
            continue

        art_body = ensure_paragraphs(art_body)

        if len(art_body) < 300:
            print(f"    ⚠️ 본문 너무 짧음 ({len(art_body)}자), 스킵")
            continue

        _dg_bad, _dg_reason = check_date_hallucination(
            art_body, [{"source_published_at": edate}], base_date=now_kst().date()
        )
        if _dg_bad:
            print(f"    ⚠️ [{_dg_reason}] → 스킵")
            continue

        # ── Step 6: 기사 삽입
        from article_image import fetch_article_image
        entity = CENTRAL_BANK_EN.get(country, "")
        image_url, image_credit = fetch_article_image(art_title, art_body, entity, call_gemini)

        art_id = insert_article(art_title, art_body, country, eid,
                                image_url=image_url, image_credit=image_credit)
        if art_id > 0:
            print(f"    ✓ 기사 삽입 완료 (articles.id={art_id}): {art_title}")
        else:
            print(f"    [ERROR] 기사 삽입 실패")

        time.sleep(10)

    print(f"\n[econ_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
