"""
gemini_summarizer.py
--------------------
DB에 저장된 기사 중 summary_ko가 빈약한 기사를 골라
Gemini Flash로 고품질 한국어 요약을 재생성합니다.

실행: python scripts/gemini_summarizer.py
"""

import os
import re
import json
import math
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# 라이브 업데이트 상단 [업데이트] 블록 형식을 gemini_writer와 공유한다.
# 형식이 갈리면 한 기사에 상단 블록과 하단 이력이 동시에 생겨 파싱이 엉킨다.
# gemini_writer는 __main__ 가드가 있어 import 시 실행되지 않는다.
# import 실패에도 병합 자체는 죽지 않도록 폴백을 둔다.
try:
    from gemini_writer import _split_article, _compose_article, _prepend_update, _ensure_paragraphs
    _HAS_UPDATE_BLOCK = True
except Exception:
    _HAS_UPDATE_BLOCK = False

# 국가명 표기 정규화("대한민국"/"한국" 등 동의어를 하나로 통일). gemini_writer.py의
# 메인 클러스터링 경로는 이미 이걸 쓰고 있었는데, 이 파일의 트렌드 생성 3경로
# (run_trend_tracker/run_realtime_trend_tracker/run_external_trend_articles)는
# 빠져있었다(2026-09-02, 사용자 신고로 발견 — id=119633/120650, 같은 "한학자
# 총재 징역 2년" 사건이 country="한국"과 "대한민국"으로 각각 저장돼
# find_similar_trend()의 country=eq. 정확일치 필터를 통과 못 하고 중복 발행됨).
# import 실패해도 정규화 없이(=현재 버그와 동일하게) 죽지 않고 동작한다.
try:
    from country_guard import normalize_country
except Exception:
    def normalize_country(country: str) -> str:
        return (country or "").strip()

# 외부 트렌드 수집 모듈 (GDELT, Google Trends, Reddit)
try:
    from external_trends import collect_external_trends
    EXTERNAL_TRENDS_AVAILABLE = True
except ImportError:
    EXTERNAL_TRENDS_AVAILABLE = False

# 날짜 환각 판정 공통 모듈. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from date_guard import check_date_hallucination
except Exception:
    def check_date_hallucination(body, sources, base_date=None):
        return False, ""

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

# 저장 시점 raw JSON 본문 차단. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
# 실사고(2026-08-20, id=89758): update_summary()는 JSON을 요청한 적이 없는데도
# Gemini가 자발적으로 gemini_writer.py 스타일 JSON 전체를 응답했고, 이 파일엔
# gemini_writer.py의 _unwrap_json_body() 같은 가드가 전혀 없어 그대로 저장됐다.
try:
    from json_body_guard import unwrap_json_body
except Exception:
    def unwrap_json_body(text, _depth=0):
        return None

# articles 테이블 삽입 공용 로직(2026-09-02, 이 파일 안에만 같은 헤더구성+
# POST 블록이 3벌 있던 걸 article_store.py로 공용화). import 실패해도 죽지
# 않도록 이 파일 자체의 _sb_headers()/_sb_url()로 폴백한다.
try:
    from article_store import insert_final_article
except Exception:
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        try:
            res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
            if res.status_code in (200, 201):
                data = res.json()
                return data[0].get("id", -1) if data else -1
        except Exception as e:
            print(f"  ⚠️ 기사 저장 예외: {e}")
        return -1

# 카테고리 정규화 공통 모듈. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from category_guard import normalize_category
except Exception:
    def normalize_category(raw, default="글로벌"):
        return "" if raw is None else str(raw).strip()

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
NEWSFINAL_CHANNEL = "@newsfinal"

try:
    from gemini_client import GeminiClient
except Exception:
    class GeminiClient:  # import 실패해도 본 기능이 죽지 않도록 폴백을 둔다
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            return None

_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)
MAX_ARTICLES = 20
CALL_INTERVAL = 10


# Supabase 헤더/URL 헬퍼는 article_store.py로 공용화(2026-09-02).
try:
    from article_store import sb_headers as _sb_headers, sb_url as _sb_url
except Exception:
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_url():
        return f"{SUPABASE_URL}/rest/v1/articles"


def get_articles_to_summarize(limit: int) -> list:
    since = (now_kst() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_en,title_ko,summary_en,summary_ko,source,category,subcategory,region,country,full_text",
            "created_at": f"gte.{since}",
            "summary_ko": "is.null",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30
    )
    short_res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_en,title_ko,summary_en,summary_ko,source,category,subcategory,region,country,full_text",
            "created_at": f"gte.{since}",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30
    )
    articles = []
    if res.status_code in (200, 206):
        articles.extend(res.json())
    if short_res.status_code in (200, 206):
        for a in short_res.json():
            sk = a.get("summary_ko") or ""
            if len(sk) < 100 and a not in articles:
                articles.append(a)
    return articles[:limit]


def update_summary(article_id: int, summary_ko: str):
    if detect_script_leak("", summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 업데이트 차단: id={article_id}")
        return
    _unwrapped = unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print(f"  🔧 [raw JSON 본문] id={article_id} 내부 body 추출 → 복구")
            summary_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 업데이트 차단: id={article_id}")
            return
    requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={"summary_ko": summary_ko},
        timeout=15
    )


# 공식/공공 소스 목록
OFFICIAL_SOURCES = {
    "APO", "AfDB", "WHO", "ASEAN", "ADB", "IMF", "World Bank",
    "African Union", "UNCTAD", "IFC", "WFP",
    "Vietnam Government", "VietnamPlus", "Indonesia Setkab",
    "Kazakhstan Inform", "Kazinform", "Uzbekistan President",
    "Saudi Press Agency", "Qatar News Agency", "Kuwait News Agency",
    "Philippine Information Agency", "Bangkok Post Economics",
}

def is_official_source(source: str) -> bool:
    if not source:
        return False
    source_lower = source.lower()
    if source_lower.startswith("apo ") or "africa-newsroom" in source_lower:
        return True
    for official in OFFICIAL_SOURCES:
        if official.lower() in source_lower:
            return True
    return False

# 프롬프트 캐시
_prompt_cache = {}

def load_prompt(name: str, fallback: str = "") -> str:
    global _prompt_cache
    if name in _prompt_cache:
        return _prompt_cache[name]
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/prompts",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            },
            params={"name": f"eq.{name}", "is_active": "eq.true", "order": "version.desc", "limit": "1"},
            timeout=10
        )
        if res.status_code in (200, 206):
            data = res.json()
            if data:
                _prompt_cache[name] = data[0]["content"]
                return _prompt_cache[name]
    except Exception as e:
        print(f"[WARN] 프롬프트 로드 실패 ({name}): {e}")
    _prompt_cache[name] = fallback
    return fallback


def build_prompt(article: dict) -> str:
    title = article.get("title_en") or article.get("title_ko") or ""
    summary = article.get("summary_en") or ""
    full_text = article.get("full_text") or ""
    source = article.get("source") or ""
    category = article.get("category") or ""
    country = article.get("country") or ""
    region = article.get("region") or ""
    content = full_text if full_text else summary
    has_full_text = bool(full_text)

    FALLBACK_RULES = """원문이 길면 더 길게 써도 됩니다.
본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 절대 포함하지 마세요.
⚠️ 원문 RSS에 섞여 들어오는 워드프레스 안내문(예: "The post [제목] appeared first on [매체명].")은 기사 내용이 아니라 플랫폼이 자동으로 붙이는 꼬리말입니다. 번역하거나 본문에 포함하지 말고 완전히 무시하세요(2026-08-30 실사고 참조).
날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- "2026년 6월 24일 현재", "오늘", "현재" 등 절대 날짜를 본문에 쓰지 마세요. 소스 기사의 날짜 기준으로 "N일(현지시간)"으로만 표기하세요.
기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다" 같은 논평/칼럼 문체는 금지입니다.
출처 매체의 구독/앱/서비스 홍보 문장은 제거하세요.
⚠️ 모든 인명·지명·기관명·단체명·행사명은 반드시 한글로 음차하세요. 영어 원문이라도 예외 없이 음차 대상입니다(예: "Sir Mahesh Patel" → "마헤시 패텔 경"). 원문을 번역하지 않고 그대로 남기지 마세요.
⚠️ 원어를 괄호로 병기할 때는 반드시 "한글 음차(원어)" 순서로 쓰세요 — "원어(한글)"처럼 원어를 앞에 두지 마세요.
⚠️ 행사·상 이름처럼 뜻이 있는 고유명사는 음절을 억지로 음차하기보다 자연스러운 한글 명칭으로 옮기고 괄호로 원어를 병기하세요. 문자 그대로 직역해 어색하거나 원래 뜻과 다른 표현을 만들지 마세요.
단, 영문+숫자 코드·규격·모델명, 한국 기업 그룹명 약칭(SK, LG 등), 명칭 안의 영문 약어(OpenAI → 오픈AI 등)는 음차하지 말고 그 부분만 원문 그대로 쓰세요."""

    rules = load_prompt("summarizer_rules", fallback=FALLBACK_RULES)

    if is_official_source(source):
        template = load_prompt("summarizer_official", fallback="""당신은 프론티어 미디어 NewsFinal의 에디터입니다.

아래는 공식 기관/정부의 공식 발표 자료입니다.

[기사 정보]
- 제목(영문): {title}
- 출처: {source}
- 분야: {category}
- 국가/지역: {country} ({region})
- 원문 내용: {content}

원문 내용을 한국어로 정확하게 번역하세요. 팩트를 빠짐없이 살리고, 원문이 길면 번역도 충분히 길게 쓰세요.
{rules}
번역문만 출력하세요.""")
        return template.format(title=title, source=source, category=category,
                               country=country, region=region, content=content, rules=rules)

    elif has_full_text:
        template = load_prompt("summarizer_fulltext", fallback="""당신은 프론티어 미디어 NewsFinal의 에디터입니다.

아래는 {source}의 원문 기사입니다.

[기사 정보]
- 제목: {title}
- 분야: {category}
- 국가/지역: {country} ({region})
- 원문: {content}

원문의 팩트(수치, 인명, 날짜, 기관명)를 빠짐없이 살려서 한국어로 작성하세요.
{rules}
요약문만 출력하세요.""")
        return template.format(title=title, source=source, category=category,
                               country=country, region=region, content=content, rules=rules)

    else:
        template = load_prompt("summarizer_rss", fallback="""당신은 프론티어 미디어 NewsFinal의 에디터입니다.

아래 기사를 바탕으로 한국어 요약문을 작성하세요.

[기사 정보]
- 제목(영문): {title}
- 출처: {source}
- 분야: {category}
- 국가/지역: {country} ({region})
- 원문 요약(영문): {summary}

기사의 핵심 내용을 한국어로 작성하세요. 팩트를 중심으로 쓰되 억지로 줄이지 마세요.
{rules}
요약문만 출력하세요.""")
        return template.format(title=title, source=source, category=category,
                               country=country, region=region,
                               content=content, summary=summary, rules=rules)


def call_gemini(prompt: str, retry: int = 2, max_tokens: int = 500, start_tier: int = 2) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.4, timeout=(10, 30))

# 문체 검증/변환(논평·칼럼체 감지, 합쇼체 감지·해라체 변환)은 style_guard.py로
# 공용화(2026-09-02, gemini_writer.py와 완전히 동일한 코드가 각각 복붙돼
# 있었음). import 실패해도 죽지 않도록 최소 폴백을 둔다.
try:
    from style_guard import has_column_style, has_polite_ending, to_plain_style
    _HAS_STYLE_GUARD = True
except Exception:
    _HAS_STYLE_GUARD = False
    def has_column_style(text: str) -> bool:
        return False
    def has_polite_ending(text: str) -> bool:
        return False
    def to_plain_style(text: str) -> str:
        return text



def call_gemini_article(prompt, max_tokens=2000, style_retries=1):
    content = call_gemini(prompt, max_tokens=max_tokens)
    attempt = 0
    while content and (has_column_style(content) or has_polite_ending(content)) and attempt < style_retries:
        attempt += 1
        reason = "논평/칼럼체" if has_column_style(content) else "합쇼체(-습니다/-입니다)"
        print(f"  ⚠️ {reason} 감지 → 재생성 시도 ({attempt}/{style_retries})")
        retry_prompt = (
            prompt
            + "\n\n[재작성 지시] 방금 작성한 결과에 논평/칼럼 문체(예: '~를 보여줍니다', "
              "'~을 도모하고 있습니다', '~라는 평가다', '~지켜볼 필요가 있습니다' 등)이거나, "
              "'-습니다'/'-입니다' 같은 정중체(합쇼체) 종결이 섞여 있었습니다. "
              "감정·의견이 섞인 표현을 모두 배제하고, 모든 문장을 '-다'로 종결하는 "
              "스트레이트 뉴스 문체로만 다시 작성하세요."
        )
        retried = call_gemini(retry_prompt, max_tokens=max_tokens)
        if retried:
            content = retried
    if content and has_polite_ending(content):
        converted = to_plain_style(content)
        if converted != content:
            print("  🔧 재생성 실패 → 합쇼체 자동 변환 적용(-습니다 → -다)")
            content = converted
    if content and (has_column_style(content) or has_polite_ending(content)):
        print("  ⚠️ 재생성/변환 후에도 논평체·합쇼체 패턴이 남아있음 (그대로 진행)")
    return content


# ── 장기 이슈 트래커 ────────────────────────────────────────

_SECTION_LABELS = ["제목:", "국가:", "관련국가:", "분야:", "3줄요약:", "투자아이디어:", "본문:"]


def _extract_section(text: str, label: str) -> str:
    """라벨 다음 줄부터, 다음으로 나오는 알려진 라벨 전까지의 텍스트를 추출(멀티라인 지원)."""
    lines = text.strip().split("\n")
    start_idx = None
    first_val = ""
    for i, line in enumerate(lines):
        if line.startswith(label):
            start_idx = i
            first_val = line[len(label):].strip()
            break
    if start_idx is None:
        return ""
    collected = [first_val] if first_val else []
    for line in lines[start_idx + 1:]:
        if any(line.startswith(lbl) for lbl in _SECTION_LABELS):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def _ensure_paragraphs(text: str, target: int = 3) -> str:
    """Gemini가 프롬프트의 '문단으로 나누어 작성' 지시를 어기고
    \\n\\n 없이 한 덩어리로 응답하는 경우가 있어(강제성 없는 지시라 준수율이
    들쭉날쭉함), 코드 단에서 문장(-다.) 단위로 강제 분할하는 안전장치.
    이미 \\n\\n이 있으면(모델이 지시를 따른 경우) 손대지 않고 그대로 반환.
    문장이 2개 이상이면 항상 최소 2개 문단으로 분할한다(짧은 리드 문단도 포함)."""
    if not text or "\n\n" in text:
        return text
    sentences = [s.strip() for s in re.split(r"(?<=다\.)\s+", text.strip()) if s.strip()]
    if len(sentences) < 2:
        return text  # 문장이 1개뿐이면 분할 불가
    actual_target = min(target, len(sentences) - 1)
    actual_target = max(actual_target, 2)
    n = len(sentences)
    size = math.ceil(n / actual_target)
    groups = [sentences[i:i + size] for i in range(0, n, size)]
    return "\n\n".join(" ".join(g) for g in groups)


# 추적할 키워드 그룹 — (그룹명, 카테고리, [키워드 목록])
# 2026-09-01: 4번째 필드는 위키미디어 커먼즈 이미지 검색용 영문 쿼리(article_image.py).
# keywords 리스트를 그대로 쓰면 "coup"·"al-shabaab" 등 검색 결과가 부정확해서
# (예: "al-shabaab"만으로 검색하면 관련 없는 사진이 걸릴 수 있음) 별도로 둔다.
TREND_KEYWORDS = [
    ("에볼라",       "사회",    ["ebola", "에볼라", "hemorrhagic fever", "출혈열", "MVD", "marburg"], "Ebola virus disease"),
    ("mpox",        "사회",    ["mpox", "monkeypox", "원숭이두창"], "Mpox"),
    ("콜레라",       "사회",    ["cholera", "콜레라"], "Cholera"),
    ("수단 분쟁",    "정치·외교", ["sudan", "rapid support forces", "수단", "다르푸르", "darfur", "khartoum", "신속지원군"], "Sudan conflict"),
    ("DRC 분쟁",    "정치·외교", ["DRC", "congo", "콩고", "M23", "키부", "kivu"], "Democratic Republic of the Congo conflict"),
    # 2026-08-31: 국가명(somalia/소말리아)만으로도 걸려 있어 인권위 회의·AU
    # 정상회의 참석·민간항공-UN 회담처럼 무관한 행정 기사가 선거무효화·
    # 알샤바브·가뭄 기아 위기와 섞이는 사고 발견(id=116437, 실제 운영
    # 확인) — 중앙아프리카·사헬과 같은 패턴. 국가명은 빼고 실제 위기
    # 행위자(알샤바브)만 남긴다.
    ("소말리아",     "정치·외교", ["al-shabaab", "알샤바브"], "Al-Shabaab Somalia"),
    ("미얀마",       "정치·외교", ["myanmar", "미얀마", "junta", "군부", "NUG"], "Myanmar civil war"),
    ("아이티",       "사회",    ["haiti", "아이티", "gang", "갱단"], "Haiti gang violence"),
    # 2026-08-31: 국가명(sahel/mali/burkina/니제르)만 걸려 있어 일본대사관 도로장비
    # 기부, 벨기에 외교장관 방문, 졸업식, 항공노선 확장, 금 생산량 통계처럼
    # 쿠데타와 무관한 행정·경제 기사가 전부 같은 기사로 묶이는 사고 발견
    # (전수 감사, 5개 표본 중 5개 전부 쿠데타 내용 0건) — 중앙아프리카와 같은
    # 방식으로 실제 군정 관련 행위자로 좁힌다.
    ("사헬 쿠데타",  "정치·외교", ["coup", "쿠데타", "junta", "군정",
                                "goïta", "goita", "고이타", "traoré", "traore", "트라오레",
                                "tiani", "티아니", "alliance of sahel", "AES", "사헬국가동맹"], "Sahel coup"),
    # 2026-08-31: 국가명·수도(중앙아프리카/bangui)만 걸려 있어 은행 진출·안보협력
    # 회의·재해관리 프로그램처럼 서로 무관한 행정 기사가 전부 같은 기사로
    # 묶이는 사고(id=114211) 발생 — 다른 항목들처럼 실제 분쟁 관련 행위자로
    # 좁힌다. 국가명·수도 자체는 뺐다(모든 정부 행사가 수도에서 열려 무의미).
    ("중앙아프리카",  "정치·외교", ["coalition of patriots", "wagner", "바그너",
                                "africa corps", "아프리카군단", "minusca", "미누스카",
                                "bozize", "bozizé", "보지제"], "Central African Republic conflict"),
]

# 추적 윈도우: 7일간 기사에서 키워드 빈도 분석
TREND_WINDOW_DAYS = 7
TREND_MIN_ARTICLES = 3   # 최소 N건 이상 등장해야 트렌드로 판단
TREND_CHECK_HOURS  = 12  # 마지막 추적기사 생성 후 N시간 이내면 스킵


def _fetch_source_details(rows: list) -> list:
    """날짜 판정에 필요한 컬럼(full_text/source_published_at)을 id로 보강 조회한다.

    trend/realtrend 경로의 소스 조회 select에는 full_text·source_published_at이
    없다(전량 조회 시 응답이 폭증하므로 일부러 뺀 것). 저장 직전 소수 건에 대해서만
    보강한다. 실패하면 원본을 그대로 돌려주어 date_guard가 '판정 불가 → 통과'로
    떨어지게 한다(정상 기사를 기술적 실패로 버리지 않는다).
    """
    ids = [str(r.get("id")) for r in (rows or []) if r.get("id")]
    if not ids:
        return rows or []
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,full_text,title_en,summary_en,source_published_at",
                "id": f"in.({','.join(ids)})",
                "limit": str(len(ids)),
            },
            timeout=15,
        )
        if res.status_code in (200, 206):
            got = res.json()
            if got:
                return got
    except Exception as e:
        print(f"  ⚠️ 소스 상세 조회 실패(날짜 판정 축소): {e}")
    return rows or []


def _merge_source_details(rows: list) -> list:
    """_fetch_source_details 결과를 원본 dict에 '병합'해 돌려준다.

    _fetch_source_details는 보강 컬럼만 담은 새 리스트로 원본을 대체하므로
    source·title_ko가 사라져 프롬프트 조립에 쓸 수 없다. 여기서 원본에 덮어쓰면
    프롬프트(보도일 표기)와 date_guard가 같은 리스트를 공유할 수 있고,
    보강 조회도 경로당 1회로 줄어든다.
    """
    got = _fetch_source_details(rows)
    if got is rows:
        return rows or []
    by_id = {r.get("id"): r for r in (got or []) if isinstance(r, dict)}
    out = []
    for r in (rows or []):
        if not isinstance(r, dict):
            out.append(r)
            continue
        extra = by_id.get(r.get("id"))
        if extra:
            merged = dict(r)
            merged.update({k: v for k, v in extra.items() if v})
            out.append(merged)
        else:
            out.append(r)
    return out


try:
    from style_guard import _pub_day_label
except Exception:
    def _pub_day_label(raw) -> str:
        s = str(raw or "")[:10]
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
        if not m:
            return ""
        try:
            mm, dd = int(m.group(2)), int(m.group(3))
        except ValueError:
            return ""
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            return ""
        return f"{mm}월 {dd}일"


_KW_RE_CACHE = {}


def _kw_regex(kw: str):
    """ASCII 키워드는 단어경계 정규식, 한국어는 None(부분문자열 검사).

    ⚠️ Python `\\b`는 한국어 문자를 단어 문자로 취급하므로 쓰지 않는다.
    ASCII-only lookaround로 대체한다.
    """
    k = (kw or "").strip()
    if not k:
        return None
    if k in _KW_RE_CACHE:
        return _KW_RE_CACHE[k]
    rx = None
    try:
        if all(ord(c) < 128 for c in k):
            rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])", re.I)
    except Exception:
        rx = None
    _KW_RE_CACHE[k] = rx
    return rx


def _trend_relevance(a: dict, keywords: list) -> int:
    """트렌드 그룹과의 관련도 점수. 제목 매칭 3점 / 요약 매칭 1점.

    PostgREST `ilike.*kw*`는 부분문자열이라 무관 기사가 대량 유입된다.
    실측(2026-08-04, 7일치): `mali` 129건 중 60건(46%)이 Somalia·Somaliland·
    Malice·abnormality·normalisasi(인니어)·Omar Malik 오탐, `gang` 33건 중
    28건(85%)이 perdagangan(인니 '무역')·pedagang('상인')·Ganggu 오탐.
    """
    title = f"{a.get('title_en') or ''} {a.get('title_ko') or ''}"
    body = f"{a.get('summary_en') or ''} {a.get('summary_ko') or ''}"
    score = 0
    for kw in (keywords or []):
        rx = _kw_regex(kw)
        if rx is not None:
            if rx.search(title):
                score += 3
            elif rx.search(body):
                score += 1
        else:
            if kw and kw in title:
                score += 3
            elif kw and kw in body:
                score += 1
    return score


def get_trend_articles(keywords: list, days: int = 7) -> list:
    """지난 N일간 특정 키워드가 포함된 수집 기사 반환"""
    since = (now_kst() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    all_articles = []
    for kw in keywords:
        try:
            res = requests.get(
                _sb_url(),
                headers=_sb_headers(),
                params={
                    "select": "id,title_en,title_ko,summary_ko,summary_en,full_text,source,country,category,region,created_at",
                    "source": "neq.NewsFinal",
                    "created_at": f"gte.{since}",
                    "or": f"(title_en.ilike.*{kw}*,title_ko.ilike.*{kw}*,summary_en.ilike.*{kw}*)",
                    "order": "created_at.desc",
                    "limit": "50",
                },
                timeout=15
            )
            if res.status_code in (200, 206):
                all_articles.extend(res.json())
        except Exception as e:
            print(f"  ⚠️ 트렌드 조회 실패 ({kw}): {e}")

    # 중복 제거
    seen = set()
    unique = []
    for a in all_articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique.append(a)

    # 관련도 재검증 — 조회는 부분문자열이므로 여기서 단어경계로 걸러낸다.
    scored = []
    for a in unique:
        sc = _trend_relevance(a, keywords)
        if sc <= 0:
            continue
        a["_trend_score"] = sc
        scored.append(a)
    if len(unique) != len(scored):
        print(f"  ↳ 관련도 필터: {len(unique)}건 → {len(scored)}건 ({len(unique)-len(scored)}건 제외)")
    return scored



# find_similar_trend 2차(본문 리드) 경로 임계값 — 표본 측정 기반(2026-07-28)
LEAD_TITLE_MIN = 44    # 제목 token_sort_ratio 최소
LEAD_SIM_MIN = 40      # 본문 리드 300자 token_sort_ratio 최소
LEAD_JAC_MIN = 0.12    # 본문 리드 키워드 Jaccard 최소

# 3차 경로(제목 게이트 없음): 한글 음차 vs 영문 원문처럼 제목이 전혀 안 겹치는
# 동일 사건 탐지용. 제목 게이트를 빼는 대신 시간창을 좁히고 리드 임계를 올린다.
# 임계 근거: 2026-07-15~29 트렌드 기사 414건 전수 페어 실측(2026-07-29)
#   리드>=47 & Jac>=0.20 만 적용 시 신규탐지 31쌍(주간 종합형 오탐 다수)
#   + 시간차 <=6h 조건 추가 시 신규탐지 2쌍, 둘 다 실제 중복(오탐 0)
LEAD2_SIM_MIN = 47     # 본문 리드 token_sort_ratio 최소
LEAD2_JAC_MIN = 0.20   # 본문 리드 키워드 Jaccard 최소
LEAD2_MAX_HOURS = 6    # 기존 기사와의 최대 시간차(시간)


def _title_keywords(t: str) -> set:
    """제목에서 의미 키워드 추출(2자 이상 토큰, 불용어 제외)."""
    import re
    toks = re.findall(r"[가-힣A-Za-z0-9]+", (t or "").lower())
    return {w for w in toks if len(w) >= 2 and w not in FREQ_STOPWORDS}


def _hours_since(created_at: str) -> float:
    """created_at(16자 KST 텍스트 'YYYY-MM-DD HH:MM') 기준 경과 시간(시간).
    파싱 실패 시 무한대를 반환해 시간창 조건을 통과하지 못하게 한다."""
    try:
        dt = datetime.strptime((created_at or "")[:16], "%Y-%m-%d %H:%M")
        return abs((now_kst().replace(tzinfo=None) - dt).total_seconds()) / 3600.0
    except Exception:
        return float("inf")


def _lead_metrics(a: str, b: str, n: int = 300):
    """본문 리드(앞 n자) 기준 유사도 지표: (token_sort_ratio, 키워드 Jaccard)."""
    from rapidfuzz import fuzz
    a = (a or "")[:n].lower(); b = (b or "")[:n].lower()
    if not a or not b:
        return 0.0, 0.0
    ka, kb = _title_keywords(a), _title_keywords(b)
    jac = (len(ka & kb) / len(ka | kb)) if (ka and kb) else 0.0
    return fuzz.token_sort_ratio(a, b), jac


def find_similar_trend(title: str, country: str | None = None,
                       days: int = 14, sim_threshold: int = 60,
                       body: str | None = None) -> dict | None:
    """
    최근 N일 내 트렌드 기사 중 동일 사건의 '루트(최초 발행=최소 id)' 반환. 없으면 None.
    매칭: country 지정 시 country 일치 필수 + 제목 token_sort_ratio>=sim_threshold + 공유 키워드>=1.
          country=None이면 제목 유사도만으로 느슨히 탐색(사전 스킵 판단용).
    id 오름차순 조회 → 첫 매칭이 곧 루트.

    ⚠️ 2026-09-03 실사고(id=118418 vs 123782, 투팍 살해범 유죄평결 중복):
    이전엔 subcategory가 trend_/realtrend_/extrend_인 기사만 검색해서,
    gemini_writer.py의 클러스터링 경로(subcategory=cluster_*)가 먼저 다룬
    사건을 트렌드 트래커가 전혀 못 보고 새로 만들었다. merge_trend_article()은
    summary_ko/update_log만 다뤄 subcategory 형식에 의존하지 않으므로
    (구조적으로 안전), 이제 발행된 NewsFinal 기사 전체를 검색 대상으로 삼는다
    — 어느 파이프라인이 먼저 다뤘든 중복 판정이 적용돼야 한다.
    """
    from rapidfuzz import fuzz
    since = (now_kst() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    params = {
        "select": "id,title_ko,summary_ko,update_log,country,created_at",
        "source": "eq.NewsFinal",
        "is_published": "eq.true",
        "created_at": f"gte.{since}",
        "order": "id.asc",
        "limit": "200",
    }
    if country:
        params["country"] = f"eq.{country}"
    try:
        res = requests.get(_sb_url(), headers=_sb_headers(), params=params, timeout=10)
        if res.status_code not in (200, 206):
            return None
        new_kw = _title_keywords(title)
        candidates = res.json()
        for a in candidates:
            existing_title = a.get("title_ko") or ""
            if not existing_title:
                continue
            sim = fuzz.token_sort_ratio(title.lower(), existing_title.lower())
            shared = new_kw & _title_keywords(existing_title)
            if sim >= sim_threshold and (not country or len(shared) >= 1):
                print(f"    → 유사 트렌드 루트 발견 (id={a['id']}, 제목유사도 {sim:.0f}%, 공유KW {len(shared)}): {existing_title[:40]}")
                return a
            # 2차 경로: 제목 표현이 달라 1차를 통과하지 못한 동일 사건 탐지
            # (country 일치 + 제목 44%↑ + 본문 리드 40%↑ + 리드 Jaccard 0.12↑)
            # 임계 근거: 실제 중복 5쌍 / 비중복 12쌍 표본 측정(2026-07-28)
            #   중복 최소값  (제목 45.8 / 리드 40.8 / Jac 0.122)
            #   비중복 최대값 (제목 41.6 / 리드 43.1 / Jac 0.146)
            if country and body and len(shared) >= 1 and sim >= LEAD_TITLE_MIN:
                lead_sim, lead_jac = _lead_metrics(body, a.get("summary_ko") or "")
                if lead_sim >= LEAD_SIM_MIN and lead_jac >= LEAD_JAC_MIN:
                    print(f"    → 유사 트렌드 루트 발견[본문리드] (id={a['id']}, "
                          f"제목 {sim:.0f}% / 리드 {lead_sim:.0f}% / Jac {lead_jac:.2f}): {existing_title[:40]}")
                    return a
            # 3차 경로: 제목 표기 체계가 달라(한글 음차 vs 영문 원문) 제목 유사도가
            # 바닥인 동일 사건 탐지. 제목 게이트 없이 시간창 6h + 높은 리드 임계로 방어.
            if country and body and len(shared) >= 1 and sim < LEAD_TITLE_MIN:
                if _hours_since(a.get("created_at")) <= LEAD2_MAX_HOURS:
                    lead_sim, lead_jac = _lead_metrics(body, a.get("summary_ko") or "")
                    if lead_sim >= LEAD2_SIM_MIN and lead_jac >= LEAD2_JAC_MIN:
                        print(f"    → 유사 트렌드 루트 발견[표기불일치] (id={a['id']}, "
                              f"제목 {sim:.0f}% / 리드 {lead_sim:.0f}% / Jac {lead_jac:.2f}): {existing_title[:40]}")
                        return a

        # 4차 경로(2026-09-03 실사고, id=122653 "니제르 쿠데타" 중복):
        # 같은 사건이라도 매 생성마다 완전히 다른 한국어 표현이 나올 수 있어
        # ("통제권 재장악" vs "쿠데타 시도 격퇴" vs "정부 통제권 회복 주장" —
        # 실측 제목유사도 37~53%, 전부 sim_threshold=60/LEAD_TITLE_MIN=44 미달)
        # 위 토큰 기반 지표 3종이 전부 놓칠 수 있다. country가 일치하는 가장
        # 최근 기사 최대 3건에 한해서만 Gemini에게 "같은 사건인지"를 직접
        # 물어본다 — 전수 호출은 비용이 크므로 최근 소수로 제한(중복은
        # 거의 항상 가장 최근 보도를 재탕하는 경향).
        if country and body:
            recent_same_country = [a for a in candidates if (a.get("country") or "") == country][-3:]
            for a in reversed(recent_same_country):
                existing_title = a.get("title_ko") or ""
                existing_body = a.get("summary_ko") or ""
                if not existing_title or not existing_body:
                    continue
                if _same_event_llm(title, body, existing_title, existing_body):
                    print(f"    → 유사 트렌드 루트 발견[LLM 판정] (id={a['id']}): {existing_title[:40]}")
                    return a
    except Exception as e:
        print(f"    → 유사도 체크 실패: {e}")
    return None


def _same_event_llm(new_title: str, new_body: str, existing_title: str, existing_body: str) -> bool:
    """토큰 유사도 지표가 전부 실패했을 때만 쓰는 최후 판정 — 두 기사가
    같은 실제 사건을 다루는지 Gemini에게 직접 묻는다(위 find_similar_trend
    4차 경로 참조). 비용 때문에 최근 소수 후보에만 호출된다."""
    prompt = f"""아래 두 기사가 같은 실제 사건(같은 날짜·같은 구체적 사건)을
다루고 있습니까? 단순히 같은 나라·같은 종류의 사건(예: 둘 다 "쿠데타
시도"이지만 서로 다른 날짜의 별개 사건)이면 "다름"입니다. 정확히 "같음"
또는 "다름" 한 단어만 답하세요.

[기사 A] {existing_title}
{existing_body[:600]}

[기사 B] {new_title}
{new_body[:600]}

답변:"""
    try:
        result = call_gemini(prompt, max_tokens=10, start_tier=3)
    except Exception:
        return False
    return bool(result) and "같음" in result.strip()


def _summarize_delta(root_summary: str, new_title: str, new_body: str) -> str:
    """루트 기사에 없는 '새 전개'를 기사문으로 서술. 새 사실 없으면 '없음'.
    실사고(2026-08-09): "1~3문장 요약" 제약 때문에 실제로는 팩트가 더 있어도
    압축 요약만 붙었다 — 팩트 기반이기만 하면 길이는 문제되지 않는다는 방침에
    따라 문장 수 상한을 없애고 재료(입력 길이)도 넉넉히 준다."""
    # 본문을 앞에 두고 이미 반영된 업데이트를 뒤에 붙여 비교 기준으로 쓴다.
    # (상단 블록이 앞을 차지하면 본문 리드가 잘림 한도 밖으로 밀려난다)
    if _HAS_UPDATE_BLOCK:
        _u, _b, _l = _split_article(root_summary)
        base_summary = "\n".join(x for x in (_b, _u, _l) if x).strip()
    else:
        base_summary = root_summary.split("────────\n[업데이트 이력]")[0].strip()
    prompt = f"""아래는 진행 중인 사건의 기존 정리 기사와, 방금 수집된 새 기사입니다.
기존 기사에 '없는 새로운 사실'을 기사문으로 서술하세요. 이 항목 하나만 읽어도
무슨 일이 있었는지 충분히 이해되는 완결된 문단이어야 합니다. 새 기사에 담긴
사실 관계(배경·경위·수치·전망)를 빠짐없이 살려 쓰되, 새 기사에 없는 내용을
지어내지는 마세요 — 팩트에 근거하는 한 길이는 제한하지 않습니다.
- ⚠️ 기존 기사 텍스트에 문자 그대로 없다고 해서 전부 "새 전개"는 아닙니다
  (2026-08-25 id=97425 실사고 — 리튬 시장 시세·산업 배경 설명이 애초에
  최초 기사에 들어갔어야 할 정보인데 나중에 "업데이트"로 잘못 분류됨).
  아래 둘을 구분하세요:
    · 새 전개로 인정: 새로 발생한 사건·발표·조치·수치 갱신(예: 오늘 시세,
      새로운 발언, 새로운 결정) — 시점이 기존 기사 이후인 것
    · "없음"으로 처리: 배경 설명·맥락·통계적 현황처럼 시점과 무관하게
      원래 최초 기사에 담겼어야 할 정보. 새 기사가 같은 사건을 다른 각도로
      재서술했을 뿐 시간적으로 진전된 내용이 없으면 "없음"으로 답하세요.
- 날짜는 반드시 소스에 명시된 "N일(현지시간)" 형식만 사용. "오늘", "어제", "화요일", "월요일" 등 요일·상대적 표현 금지. 날짜 정보가 없으면 생략.
- 새로운 사실이 없으면 정확히 "없음" 한 단어만 출력.
- 논평·마크다운·헤더 금지, 사실 서술형 한국어로만.
- 모든 문장을 "-다"로 종결하세요. "-습니다", "-입니다" 등 정중체는 쓰지 마세요.

[기존 정리 기사]
{base_summary[:3000]}

[새 기사] {new_title}
{new_body[:3000]}

새 전개:"""
    try:
        out = call_gemini_article(prompt, max_tokens=1200)
        if out:
            out = out.strip()
            if out.startswith("새 전개 요약:") or out.startswith("새 전개:"):
                out = out.split(":", 1)[1].strip()
            out = out.strip()
            return _ensure_paragraphs(out) if _HAS_UPDATE_BLOCK else out
    except Exception as e:
        print(f"    → 델타 요약 실패: {e}")
    return ((new_body or "")[:200]).strip()


def _generate_update_headline(delta: str) -> str:
    """이번 업데이트에서 새로 확인된 내용을 25자 내외 한 줄로 요약한다.
    독자용 "업데이트 기록" 목록에 표시할 용도(2026-09-02, 사용자 요청 —
    "단순히 '내용 업데이트'만 적지 말고 한줄 요약을 추가로 적어주면 좋을 것 같은데").
    gemini_writer.py의 동명 함수와 같은 목적 — 스크립트마다 call_gemini
    인스턴스가 달라 공용화하지 않고 각자 둔다(article_image.py의
    call_gemini_fn 주입 패턴과 같은 이유)."""
    if not delta:
        return ""
    prompt = f"""아래는 기사에 새로 추가된 내용입니다. 이번 업데이트에서 무엇이
새로 확인됐는지 25자 내외 한 줄로 요약하세요. 완결된 문장(예: "사망자 969명으로 늘어")
형태로 헤드라인만 출력하고, 다른 말은 절대 쓰지 마세요.

{delta[:800]}"""
    try:
        headline = call_gemini(prompt, max_tokens=40, start_tier=3)
    except Exception:
        headline = None
    if not headline:
        return ""
    headline = headline.strip().strip('"').strip("'")
    headline = re.sub(r"^(헤드라인|요약)\s*[:：]\s*", "", headline)
    return headline[:60]


def merge_trend_article(existing: dict, new_title: str, new_body: str, note: str) -> bool:
    """기존 트렌드 루트 기사에 '새 전개'만 append(리빙 아티클). 제목·기존 본문은 덮어쓰지 않음."""
    art_id = existing["id"]
    existing_summary = existing.get("summary_ko") or ""
    existing_log = existing.get("update_log") or []

    delta = _summarize_delta(existing_summary, new_title, new_body)
    if not delta or delta.replace(".", "").strip() == "없음":
        print(f"    → 새 전개 없음, append 생략 (id={art_id})")
        return True  # 병합 성공 처리 → 신규 중복 생성 방지

    # delta 자체가 raw JSON이면 concat 후엔 "{"로 시작 안 해서 아래 new_summary
    # 검사로는 못 잡는다 — append되기 전, delta 단독일 때 확인해야 한다.
    _unwrapped_delta = unwrap_json_body(delta)
    if _unwrapped_delta is not None:
        if _unwrapped_delta:
            print(f"  🔧 [raw JSON 본문] id={art_id} 새 전개 내부 body 추출 → 복구")
            delta = _unwrapped_delta
        else:
            print(f"  ⛔ [raw JSON 본문] id={art_id} 병합 차단(새 전개)")
            return False

    if _HAS_UPDATE_BLOCK:
        u, b, l = _split_article(existing_summary)
        new_summary = _compose_article(_prepend_update(u, delta), b, l)
    elif "[업데이트 이력]" not in existing_summary:
        new_summary = existing_summary.rstrip() + "\n\n────────\n[업데이트 이력]\n■ " + delta
    else:
        new_summary = existing_summary.rstrip() + "\n■ " + delta

    if detect_script_leak("", new_summary):
        print(f"  ⚠️ [문자 혼입 감지] 병합 차단: id={art_id}")
        return False

    # 병합 후 재검증 — verify_single_topic()은 이제까지 병합 "전" 새 초안
    # 하나만 검사했다. 루트와 새 초안이 각각은 단일토픽이어도, 서로 다른
    # 사건이 같은 국가/그룹이라는 이유만으로 병합되면 최종 결과물이 복수
    # 토픽이 될 수 있다(2026-08-25 id=97425 실사고 — 리튬 허가 갱신 승인
    # 기사와 포로 교환 기사가 "말리"라는 이유로 하나로 합쳐짐). 병합된
    # 최종본을 다시 검사해, 실패하면 병합을 포기하고 별도 기사로 남긴다.
    root_title = existing.get("title_ko") or ""
    if not verify_single_topic(root_title, new_summary):
        print(f"  ⛔ [복수 토픽 혼입] 병합 후 재검증 실패 → 병합 취소, 별도 기사로 분리: id={art_id}")
        return False

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    headline = _generate_update_headline(delta)
    new_log = existing_log + [{"timestamp": now_str, "note": note, **({"headline": headline} if headline else {})}]
    try:
        res = requests.patch(
            f"{_sb_url()}?id=eq.{art_id}",
            headers=_sb_headers(),
            json={
                "summary_ko": new_summary,
                # created_at은 최초 게시일 보호 — 업데이트 시 변경 금지
                "update_log": new_log,
            },
            timeout=15
        )
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"    → 병합 실패: {e}")
        return False


def trend_article_exists(group_name: str) -> bool:
    """최근 N시간 내 해당 트렌드 추적 기사가 이미 있는지 확인"""
    since = (now_kst() - timedelta(hours=TREND_CHECK_HOURS)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id",
                "source": "eq.NewsFinal",
                "subcategory": f"eq.trend_{group_name}",
                "created_at": f"gte.{since}",
                "limit": "1",
            },
            timeout=10
        )
        if res.status_code in (200, 206):
            return len(res.json()) > 0
    except Exception:
        pass
    return False


try:
    from style_guard import verify_single_topic as _sg_verify_single_topic
except Exception:
    _sg_verify_single_topic = None


def verify_single_topic(title: str, body: str) -> bool:
    """style_guard.verify_single_topic()에 이 파일의 call_gemini를 주입해서 위임."""
    if _sg_verify_single_topic:
        return _sg_verify_single_topic(title, body, call_gemini)
    if not title or not body:
        return True
    prompt = f"""아래 기사가 하나의 명확한 토픽(사건/이슈/기업/정책)만 다루는지 판단하세요.
서로 다른 국가나 전혀 관련 없는 사건 여러 개를 한 기사에 묶은 경우 "NO"라고만 답하세요.
특히 기사 뒷부분 문단에 제목·앞문단과 무관한 다른 사건이 붙어 있으면(예: 영화 흥행 기사 뒤에 스포츠 경기 내용) 반드시 "NO"라고 답하세요.
하나의 토픽이면 "YES"라고만 답하세요.

제목: {title}
본문 전체:
{body[:2500]}

답변 (YES 또는 NO만):"""
    result = call_gemini(prompt, max_tokens=5, start_tier=3)
    if not result:
        return True
    return "YES" in result.upper()


MULTI_TOPIC_NOTE = "복수 토픽 혼입 — 무관한 사건이 한 기사에 묶임"


def save_trend_article(group_name: str, title: str, body: str,
                       category: str, country: str, region: str,
                       countries: list, summary_3lines: str = "", investment_idea: str = "",
                       published: bool = True, guard_note: str = "",
                       image_url: str = "", image_credit: str = "") -> int:
    """트렌드 추적 기사 저장"""
    if detect_script_leak(title, body):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title[:60]}")
        return -1
    _unwrapped = unwrap_json_body(body)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            body = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 저장 차단: {title[:60]}")
            return -1
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    payload = {
        "title_en": title, "title_ko": title,
        "summary_en": "", "summary_ko": body,
        "url": f"internal://trend_{group_name}_{now_kst().strftime('%Y%m%d%H')}",
        "source": "NewsFinal",
        "category": category,
        "subcategory": f"trend_{group_name}",
        "region": region,
        "country": country,
        "country_flag": "",
        "countries": ([country] + [c for c in (countries or []) if c and c != country]) if country else (countries or []),
        "image_url": image_url,
        "image_credit": image_credit,
        "score": 2,  # 라이브 탭에 바로 표시
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str,
                        "note": guard_note or "트렌드 추적 최초 게시"}],
        "sent_telegram": 0,
        "is_published": published,
        "posted_blog": 0,
        "summary_3lines": summary_3lines,
        "investment_idea": investment_idea,
    }
    return insert_final_article(payload)


def run_trend_tracker():
    """장기 이슈 트렌드 감지 및 추적 기사 생성"""
    if not GEMINI_API_KEYS:
        return

    print("\n[트렌드 트래커] 장기 이슈 분석 시작...")

    for group_name, category, keywords, image_query in TREND_KEYWORDS:
        articles = get_trend_articles(keywords, days=TREND_WINDOW_DAYS)

        if len(articles) < TREND_MIN_ARTICLES:
            print(f"  [{group_name}] {len(articles)}건 — 임계값 미달, 스킵")
            continue

        print(f"  [{group_name}] {len(articles)}건 감지 → 추적 기사 생성 검토")

        if trend_article_exists(group_name):
            print(f"  [{group_name}] 최근 {TREND_CHECK_HOURS}시간 내 이미 생성됨 — 스킵")
            continue

        # 최신 기사 최대 8건으로 Gemini 프롬프트 구성
        top = sorted(articles, key=lambda a: (a.get("_trend_score", 0), a.get("created_at", "")), reverse=True)[:8]
        # 보도일·원문을 여기서 한 번 보강해 프롬프트와 date_guard가 함께 쓰게 한다
        top = _merge_source_details(top)
        today_str = now_kst().strftime("%Y년 %m월 %d일")

        article_list = ""
        for i, a in enumerate(top, 1):
            t = a.get("title_ko") or a.get("title_en") or ""
            body = a.get("full_text") or a.get("summary_ko") or a.get("summary_en") or ""
            pub = _pub_day_label(a.get("source_published_at"))
            pub_tag = f" (보도 {pub})" if pub else ""
            article_list += f"{i}. [{a.get('source','')}]{pub_tag} {t}\n"
            if body:
                # 2026-08-30 사용자 지적: 300자 제한에 문서화된 근거가 없었음
                # (도입 커밋 2026-06-23도 이유 설명 없음) — 토큰 비용이 실제
                # 제약이 아니므로 소스 본문을 자르지 않고 그대로 씀.
                article_list += f"   {body}\n\n"

        prompt = f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 지난 {TREND_WINDOW_DAYS}일간 [{group_name}] 관련 기사 {len(articles)}건의 주요 내용입니다.

[수집된 관련 기사]
{article_list}

이 기사들을 종합해 현재 진행 중인 상황을 정리하는 추적 기사를 작성하세요.
- 현재 상황이 어떻게 전개되고 있는지 시간 순으로 정리하세요.
- 여러 국가·기관에서 같은 사안([{group_name}])이 동시에 벌어지고 있다면, 첫 문단은 그 사실을 압축해 요약하는 리드로 시작하세요(예: "나이지리아와 남수단 등 아프리카 여러 국가에서 콜레라 확산이 이어지며 방역 당국이 대응에 나섰다"). 이 리드는 반드시 구체적 사실(무엇이 몇 개국·몇 곳에서 벌어지고 있는지)을 담아야 하며, 아래에서 금지하는 화자 없는 추상적 개관 문장이어서는 안 됩니다. 리드 다음 문단부터 국가·기관별 구체적 내용을 전개하세요. 나라가 하나뿐이면 이 리드는 필요 없습니다.
- 위 기사 중 [{group_name}] 주제와 직접 관련이 없는 것은 본문에서 완전히 제외하세요. 무관한 사건을 같은 기사에 엮지 마세요. 하나의 기사는 하나의 사안만 다뤄야 합니다. 원문 기사 하나 안에 여러 소식이 섞여 있는 경우도 있습니다 — 그 중 [{group_name}]과 무관한 부분(예: 같은 기사에 실렸을 뿐인 다른 나라·다른 사안 소식)은 그 원문 기사가 전체적으로 관련 있어 보이더라도 옮겨 적지 마세요.\n- 본문은 각 사건마다 한 문장으로 끝내지 말고, 배경·경위·현재 상황을 구체적으로 풀어서 서술하세요. 관련 기사가 충분하면 800자 이상이 적당합니다. 다만 분량을 채우려고 위 기사에 없는 내용을 지어내거나 무관한 사건을 끌어오지는 마세요. 쓸 내용이 적으면 그만큼 짧게 쓰세요.\n- ⚠️ 위 기사들을 "종합"하라는 것은 문장을 순서대로 번역·요약해 이어붙이라는 뜻이 아닙니다(2026-09-02 계기: 참고자료로 준 원문 기사를 문장 구조만 살짝 바꿔 그대로 쓴 사례 발견). 사실관계(수치·인명·날짜·인용)는 정확히 유지하되, 문장 구성과 표현은 당신 자신의 방식으로 새로 짜서 쓰세요. 특정 원문 하나의 문장을 그 자리에서 옮기지 말고, 여러 기사의 사실관계를 먼저 전부 파악한 뒤 기사를 처음부터 다시 설계하듯 쓰세요.
- 문단을 나눌 때는 반드시 빈 줄(줄바꿈 2번)로 구분하세요. 한 문단에 모든 문장을 붙여 쓰지 마세요.
- 모든 날짜는 사건이 일어난 현지시간 기준으로만 표기하세요. 한국 시간(KST)이나 UTC로 환산·계산하지 말고, 소스 기사에 나온 날짜를 하루도 앞뒤로 옮기지 말고 그대로 "N일(현지시간)" 형식으로 쓰세요. "2026년 7월 15일", "오늘", "현재" 같은 절대 날짜나 오늘 날짜는 쓰지 말고, 날짜를 알 수 없으면 쓰지 마세요.\n- 요일(월요일~일요일)을 단독으로 쓰지 마세요. 원문에 "on Wednesday"처럼 요일만 있고 날짜가 없더라도 그 요일을 한국어로 옮겨 적지 마세요("수요일 밝혔다", "현지시각 수요일", "지난 금요일" 모두 금지). 요일은 "8일 토요일"처럼 날짜와 병기할 때만 쓸 수 있습니다.\n- 각 원문 제목 옆에 "(보도 M월 D일)"이 붙어 있을 수 있습니다. 그 기사가 실제로 보도된 날짜입니다. 원문 본문에 사건 날짜가 명시돼 있으면 그 날짜가 항상 우선이고, 본문에 날짜 근거가 없을 때만 이 보도일을 사건 시점으로 보아 "D일(현지시간)"으로 쓰세요(예: "보도 8월 3일" → "3일(현지시간)"). 보도일 표기가 없고 원문에도 날짜가 없으면 날짜를 쓰지 마세요. 보도일을 "8월 3일" 같은 절대 날짜 형태로 본문에 적지 마세요.
- "현지시각 기준으로", "현재 시점", "현 시점 기준", "최근 들어" 같은 모호한 시간 표현으로 날짜를 대체하지 마세요. 이런 표현은 금지어입니다. 구체적 날짜를 모르면 시간 표현 자체를 아예 쓰지 말고 사실만 서술하세요.
- 기념일·회고형 소재(집권 N주년, 사건 N년 등)처럼 특정 뉴스 발생일이 없는 경우에는 "N일(현지시간)"을 억지로 만들지 말고, 과거 사건은 연도만("2023년") 표기하세요.
- 수치, 인명, 날짜, 기관명 등 구체적 팩트를 최대한 살리세요.
- 부가가치 분석(메커니즘·규모 가늠·선례 비교·한국 연관성)은 본문에 넣지 말고, 아래 "투자아이디어:" 필드에 별도로 작성하세요. 본문(본문:)에는 순수 사실 서술만 담으세요.
- 본문 마지막 문장을 "~라는 분석이 나온다", "~전망이다", "~지속될 전망입니다", "~귀추가 주목된다" 같은 화자 없는 전망·분석형 문장으로 마무리하지 마세요. 그런 문장은 부가가치 분석이므로 위 규칙대로 "투자아이디어:" 필드로 옮기고, 본문은 마지막까지 구체적 사실(누가, 무엇을, 언제)로 끝내세요.
- 여러 국가·사건을 다룰 때 각 문단을 "~격동 속에서 위축된 상태입니다", "~전술적 변화가 감지되었습니다", "~목소리가 이어지고 있습니다" 같은 화자 없는 추상적 개관 문장으로 시작하지 마세요. 이런 문장으로 문을 열고 뒤에 구체적 사실을 붙이는 구조는 분석·피처 기사 문체이지 스트레이트 뉴스가 아닙니다. 각 문단을 곧바로 구체적 사실(누가, 언제, 무엇을 했는지)로 시작하세요.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- 모든 문장을 "-다"로 종결하세요(예: "발표했다", "밝혔다", "나타났다"). "-습니다", "-입니다", "-됩니다" 같은 정중체(합쇼체)는 절대 쓰지 마세요. 단, 인용문 안의 발언 자체(따옴표로 감싼 발언)는 예외로 원문 어투를 유지할 수 있습니다.
- 기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다", "~라는 평가다", "~지켜볼 필요가 있습니다" 같은 논평/칼럼 문체는 금지입니다.
- 인명·기업명·기관명·지명 등 고유명사는 반드시 한글로 음차해 표기하세요(예: Zedcrest Group → 제드크레스트 그룹, Leatherback → 레더백). 영문 원문을 그대로 쓰지 마세요. 키릴·아랍·데바나가리·태국·한자 등 한글이 아닌 문자를 이름 중간에 섞지 마세요("이스ام 파레스", "카르나타카州", "1万2341대" 같은 형태는 심각한 오류입니다). 다만 "고(故)", "대(對)중국"처럼 괄호 안에 넣는 한자 병기는 허용됩니다. 첫 등장 시에만 괄호로 원문을 병기할 수 있습니다(예: 제드크레스트 그룹(Zedcrest Group)).
- 영화·도서·게임 등 작품 제목은 원문에 등장한 원어 표기를 정확히 확인해서만 옮기세요. 공식 한국어 제목을 확신할 수 없으면 새 표현을 지어내거나 음절을 빠뜨리지 말고, 원어 제목을 괄호로 병기하거나(예: "브랜드 뉴 데이(Brand New Day)") 원어 그대로 쓰세요.
- 단, 다음 두 가지는 음차하지 말고 원문 그대로 쓰세요: ① 영문+숫자로 된 코드·규격·모델명(H-1B, 5G, F-35, GPT-5, 통화코드 등) ② 한국 기업 그룹명 약칭(SK, LG, GS, KT, CJ, DL, HD 등) ③ 명칭 안에 영문 약어가 들어간 기업·기관·기술명은 그 약어 부분을 알파벳 그대로 두세요(OpenAI → 오픈AI, xAI → xAI, AI, IT, UN, EU 등). 약어를 한글로 풀어 읽지 마세요(오픈아이·아이티 등은 오표기입니다).
- 한국어로만 작성하세요.

아래 형식으로 출력:
제목: (현재 상황을 담은 추적 기사 제목)
국가: (주요 대상 국가 1개, 없으면 "없음")
관련국가: (관련국 최대 4개, 없으면 "없음")
분야: ({category})
3줄요약: (핵심을 정확히 3줄로, 각 줄은 "\n"으로 구분, 각 줄 40자 내외)
투자아이디어: (3~5문장, 위에서 지시한 메커니즘·규모·선례·한국연관성 요소 포함, 막연한 문장 금지)
본문: (추적 기사 본문, 부가가치 분석 문단 제외 — 순수 사실 서술만)"""

        content = call_gemini_article(prompt, max_tokens=2000)
        if not content:
            print(f"  [{group_name}] ❌ Gemini 생성 실패")
            continue

        # 파싱
        title, body, country, gen_category, countries = "", content, "", category, []
        for line in content.strip().split("\n"):
            if line.startswith("제목:"):
                title = line.replace("제목:", "").strip()
            elif line.startswith("국가:"):
                c = line.replace("국가:", "").strip()
                if c not in ("없음", "-", ""):
                    country = normalize_country(c)
            elif line.startswith("관련국가:"):
                raw = line.replace("관련국가:", "").strip()
                if raw not in ("없음", "-", ""):
                    countries = [normalize_country(x.strip()) for x in raw.split(",") if x.strip()]
            elif line.startswith("분야:"):
                gen_category = normalize_category(line.replace("분야:", "").strip()) or category
            elif line.startswith("본문:"):
                idx = content.find("본문:")
                body = _ensure_paragraphs(content[idx + 3:].strip())
                summary_3lines = _extract_section(content, "3줄요약:")
                investment_idea = _extract_section(content, "투자아이디어:")
                break

        if not title:
            title = f"{group_name} 동향 — {today_str}"

        # 지역 추론
        region_map = {
            "아프리카": "africa", "나이지리아": "africa", "케냐": "africa",
            "수단": "africa", "콩고": "africa", "소말리아": "africa",
            "말리": "africa", "부르키나파소": "africa", "중앙아프리카": "africa",
            "미얀마": "southeast_asia", "아이티": "caribbean",
        }
        region = region_map.get(country, "africa")

        # 단일 토픽 검수 — 무관한 사건이 묶였으면 병합·발행 모두 차단
        _mt_bad = not verify_single_topic(title, body)
        if _mt_bad:
            print(f"  [{group_name}] ⛔ 복수 토픽 혼입 → 미발행: {title[:50]}")

        # 날짜 환각 판정 — 원문에 근거 없는 "N일(현지시간)"이면 미발행
        # ⚠️ 병합 분기보다 반드시 앞에 둘 것. 뒤에 두면 병합 경로가 가드를 통째로
        #    우회해 환각 날짜가 이미 발행된 기사에 append된다(별도 기사보다 나쁨).
        _dg_bad, _dg_reason = (False, "")
        if not _mt_bad:
            _dg_bad, _dg_reason = check_date_hallucination(
                body, top, base_date=now_kst().date()
            )
            if _dg_bad:
                print(f"  [{group_name}] ⛔ 날짜 환각 의심 → 미발행: {_dg_reason}")

        # 동일 사건 루트 있으면 신규 생성 대신 append 병합(리빙 아티클)
        root = find_similar_trend(title, country=country, days=14, body=body)
        if root and not (_mt_bad or _dg_bad):
            if merge_trend_article(root, title, body, f"트렌드 추적 업데이트 ({group_name})"):
                print(f"  [{group_name}] ✅ 기존 루트에 병합 (id={root['id']}): {title}")
                time.sleep(CALL_INTERVAL)
                continue

        image_url, image_credit = ("", "")
        if not (_mt_bad or _dg_bad):
            from article_image import fetch_article_image
            image_url, image_credit = fetch_article_image(title, body, image_query, call_gemini)

        article_id = save_trend_article(
            group_name=group_name, title=title, body=body,
            category=gen_category, country=country, region=region,
            countries=countries, summary_3lines=summary_3lines, investment_idea=investment_idea,
            published=not (_mt_bad or _dg_bad),
            image_url=image_url, image_credit=image_credit,
            guard_note=(MULTI_TOPIC_NOTE if _mt_bad
                        else (f"날짜 환각 의심 미발행 — {_dg_reason}" if _dg_bad else "")),
        )

        if article_id > 0:
            print(f"  [{group_name}] ✅ 추적 기사 생성 (id={article_id}): {title}")
            # 텔레그램 발송
            if TELEGRAM_TOKEN:
                try:
                    preview = body[:300]
                    url = f"https://newsfinal.co.kr/article?id={article_id}"
                    msg = f"📡 트렌드 추적\n\n*{title}*\n\n{preview}{'…' if len(body) > 300 else ''}\n\n[전체 기사 보기]({url})"
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": NEWSFINAL_CHANNEL, "text": msg,
                              "parse_mode": "Markdown",
                              "link_preview_options": json.dumps({"prefer_small_media": True, "url": url})},
                        timeout=15
                    )
                except Exception:
                    pass
        else:
            print(f"  [{group_name}] ❌ 저장 실패")

        time.sleep(CALL_INTERVAL)

    print("[트렌드 트래커] 완료")



# ── 실시간 트렌드 감지 (A+B) ─────────────────────────────────

# 불용어 — 빈도 분석에서 제외할 일반 단어
FREQ_STOPWORDS = {
    "the","a","an","in","on","at","to","of","for","and","or","is","are","was","were",
    "has","have","been","will","with","by","from","this","that","as","its","it","be",
    "not","but","also","over","after","amid","says","say","said","new","following",
    "government","minister","country","president","million","billion","year","years",
    "percent","growth","economy","economic","market","africa","report","reports",
    "kenya","nigeria","ghana","ethiopia","south","north","east","west","central",
    "기자","특파원","뉴스","오늘","이번","정부","대통령","장관","경제","시장","아프리카",
}

# 실시간 트렌드 설정
RT_WINDOW_NOW  = 2   # 현재 윈도우: 최근 N일
RT_WINDOW_PREV = 7   # 비교 윈도우: 이전 N일
RT_MIN_COUNT   = 3   # 현재 윈도우 최소 등장 건수
RT_SURGE_RATIO = 2.5 # 이전 대비 급증 배율
RT_TOP_TOPICS  = 5   # Gemini에 넘길 급증 토픽 수
RT_CHECK_HOURS = 6   # 중복 방지: 같은 토픽 N시간 이내 재생성 금지


def fetch_recent_titles(days: int) -> list:
    """최근 N일간 수집 기사 제목+요약 반환"""
    since = (now_kst() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    articles = []
    offset = 0
    while True:
        try:
            res = requests.get(
                _sb_url(),
                headers={**_sb_headers(), "Range": f"{offset}-{offset+499}"},
                params={
                    "select": "id,title_en,title_ko,summary_en,summary_ko,country,category,region,created_at,source",
                    "source": "neq.NewsFinal",
                    "created_at": f"gte.{since}",
                    "order": "created_at.desc",
                },
                timeout=20
            )
            if res.status_code not in (200, 206):
                break
            batch = res.json()
            if not batch:
                break
            articles.extend(batch)
            if len(batch) < 500:
                break
            offset += 500
        except Exception as e:
            print(f"  ⚠️ 기사 조회 실패: {e}")
            break
    return articles


def extract_ngrams(text: str, n: int = 2) -> list:
    """텍스트에서 n-gram 추출 (한글 2자 이상 단어, 영문 4자 이상)"""
    import re
    text = text.lower()
    text = re.sub(r'[^\w\s가-힣]', ' ', text)
    words = [w for w in text.split()
             if w not in FREQ_STOPWORDS
             and (
                 (re.search(r'[가-힣]', w) and len(w) >= 2) or
                 (not re.search(r'[가-힣]', w) and len(w) >= 4)
             )]
    # 단어 단위 + bigram
    tokens = words[:]
    for i in range(len(words) - 1):
        tokens.append(f"{words[i]} {words[i+1]}")
    return tokens


def count_keywords(articles: list) -> dict:
    """기사 목록에서 키워드 빈도 카운트"""
    from collections import Counter
    counter = Counter()
    for a in articles:
        text = " ".join(filter(None, [
            a.get("title_en") or "",
            a.get("title_ko") or "",
            (a.get("summary_en") or "")[:200],
            (a.get("summary_ko") or "")[:200],
        ]))
        for token in extract_ngrams(text):
            counter[token] += 1
    return dict(counter)


def detect_surging_keywords(now_articles: list, prev_articles: list) -> list:
    """
    현재 윈도우 vs 이전 윈도우 비교해서 급증 키워드 반환
    반환: [(keyword, now_count, prev_count, ratio), ...]
    """
    now_counts  = count_keywords(now_articles)
    prev_counts = count_keywords(prev_articles)

    # 현재 윈도우 기간이 더 짧으므로 일별 정규화
    now_days  = RT_WINDOW_NOW
    prev_days = RT_WINDOW_PREV

    surging = []
    for kw, now_cnt in now_counts.items():
        if now_cnt < RT_MIN_COUNT:
            continue
        prev_cnt = prev_counts.get(kw, 0)
        # 일별 정규화 비율
        now_rate  = now_cnt  / now_days
        prev_rate = (prev_cnt / prev_days) if prev_cnt > 0 else 0.1
        ratio = now_rate / prev_rate
        if ratio >= RT_SURGE_RATIO:
            surging.append((kw, now_cnt, prev_cnt, round(ratio, 1)))

    # 비율 높은 순 정렬
    surging.sort(key=lambda x: x[3], reverse=True)
    return surging


def _topic_tokens(t: str) -> set:
    """토픽 문자열에서 식별력 있는 토큰 추출(2자 이상, 불용어 제외).
    subcategory에는 토픽 원형(영문/한글)이 그대로 저장되므로 표기 불일치에 강함."""
    import re
    toks = re.findall(r"[가-힣A-Za-z0-9]+", (t or "").lower())
    return {w for w in toks if len(w) >= 2 and w not in FREQ_STOPWORDS}


def realtime_trend_article_exists(keyword: str) -> bool:
    """최근 N시간 내 같은 토픽으로 생성된 실시간 트렌드 기사가 있는지 확인.

    기존엔 title_ko ilike '*keyword*' 만 검사해, 영문 토픽 vs 한글 음차 제목이면
    전혀 매칭되지 않아 중복 생성을 허용했다(실사고: Zedcrest/제드크레스트).
    subcategory(=realtrend_{topic[:20]})에는 토픽 원형이 남으므로 이를 함께 대조한다.
    """
    since = (now_kst() - timedelta(hours=RT_CHECK_HOURS)).strftime("%Y-%m-%d %H:%M")
    kw_tokens = _topic_tokens(keyword)
    if not kw_tokens:
        return False
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,subcategory",
                "source": "eq.NewsFinal",
                "subcategory": "like.realtrend_%",
                "created_at": f"gte.{since}",
                "limit": "100",
            },
            timeout=10
        )
        if res.status_code not in (200, 206):
            return False
        for a in res.json():
            sub = (a.get("subcategory") or "")
            if sub.startswith("realtrend_"):
                sub = sub[len("realtrend_"):]
            existing = _topic_tokens(sub) | _topic_tokens(a.get("title_ko") or "")
            if kw_tokens & existing:
                print(f"    → 최근 {RT_CHECK_HOURS}h 내 동일 토픽 기사 존재 "
                      f"(id={a.get('id')}, 공유토큰 {sorted(kw_tokens & existing)[:3]})")
                return True
    except Exception:
        pass
    return False


def get_articles_for_keyword(keyword: str, articles: list, max_n: int = 10) -> list:
    """주어진 기사 목록에서 키워드 포함 기사 필터링"""
    import re
    kw_lower = keyword.lower()
    matched = []
    for a in articles:
        text = " ".join(filter(None, [
            a.get("title_en") or "",
            a.get("title_ko") or "",
            a.get("summary_en") or "",
            a.get("summary_ko") or "",
        ])).lower()
        if kw_lower in text:
            matched.append(a)
    # 최신순
    matched.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return matched[:max_n]


def run_realtime_trend_tracker():
    """실시간 트렌드 감지 및 기사 생성 (A+B)"""
    if not GEMINI_API_KEYS:
        return

    print("\n[실시간 트렌드] 분석 시작...")

    # A. 기사 수집
    now_articles  = fetch_recent_titles(days=RT_WINDOW_NOW)
    prev_articles = fetch_recent_titles(days=RT_WINDOW_NOW + RT_WINDOW_PREV)
    # prev는 전체 기간 - 현재 기간
    now_ids = {a["id"] for a in now_articles}
    prev_only = [a for a in prev_articles if a["id"] not in now_ids]

    print(f"  현재({RT_WINDOW_NOW}일): {len(now_articles)}건 / 이전({RT_WINDOW_PREV}일): {len(prev_only)}건")

    if len(now_articles) < 5:
        print("  [SKIP] 기사 수 부족")
        return

    # A. 급증 키워드 감지
    surging = detect_surging_keywords(now_articles, prev_only)
    print(f"  급증 키워드 {len(surging)}개 감지")
    for kw, nc, pc, r in surging[:15]:
        print(f"    '{kw}' — 현재 {nc}건 / 이전 {pc}건 / {r}x")

    if not surging:
        print("  [SKIP] 급증 키워드 없음")
        return

    # B. 급증 키워드 목록을 Gemini에 넘겨 실제 트렌드 이슈 판단
    today_str = now_kst().strftime("%Y년 %m월 %d일")
    top_surging = surging[:20]

    # 키워드별 대표 기사 제목도 같이 전달
    keyword_context = ""
    for kw, nc, pc, r in top_surging:
        sample_articles = get_articles_for_keyword(kw, now_articles, max_n=3)
        titles = [a.get("title_en") or a.get("title_ko") or "" for a in sample_articles]
        keyword_context += f"\n- '{kw}' ({r}x 급증, {nc}건): {' / '.join(titles[:2])}"

    screening_prompt = f"""당신은 프론티어 마켓 전문 에디터입니다. ({today_str})
아래는 최근 {RT_WINDOW_NOW}일간 프론티어 마켓 뉴스에서 이전 {RT_WINDOW_PREV}일 대비 급증한 키워드와 관련 기사 제목입니다.

[급증 키워드 목록]
{keyword_context}

위 키워드들을 분석해서 실제 주목할 만한 새로운 이슈 최대 {RT_TOP_TOPICS}개를 선별하세요.
단순 반복 보도(정기 경제지표, 일상적 기업 실적 등)는 제외하고,
실제로 새롭게 부상하는 사건·분쟁·위기·정책 변화·산업 동향만 선별하세요.

JSON 배열로만 응답하세요 (마크다운 없이):
[
  {{
    "topic": "이슈 핵심 키워드 (영문 또는 한글)",
    "issue_ko": "이슈 한 줄 설명 (한국어)",
    "category": "경제 | 금융 | 자원·에너지 | 산업·기업 | 정치·외교 | 사회 | IT·과학 중 정확히 하나. 여러 개를 나열하지 마세요.",
    "urgency": "high/medium/low"
  }}
]
선별할 이슈가 없으면 빈 배열 []을 반환하세요."""

    raw = call_gemini(screening_prompt, max_tokens=800)
    if not raw:
        print("  [SKIP] Gemini 스크리닝 응답 없음")
        return

    # JSON 파싱
    import json, re
    try:
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            print("  [SKIP] JSON 파싱 실패")
            return
        topics = json.loads(match.group())
        if not isinstance(topics, list) or not topics:
            print("  [SKIP] 선별된 이슈 없음")
            return
    except Exception as e:
        print(f"  [SKIP] JSON 오류: {e}")
        return

    print(f"  Gemini 선별 이슈 {len(topics)}개:")
    for t in topics:
        print(f"    [{t.get('urgency','?')}] {t.get('issue_ko','')} ({t.get('topic','')})")

    # B. 각 이슈별 기사 생성
    generated = 0
    for topic_info in topics:
        if generated >= 3:  # 1회 실행당 최대 3건
            break

        topic     = topic_info.get("topic", "")
        issue_ko  = topic_info.get("issue_ko", "")
        category  = normalize_category(topic_info.get("category", ""), default="사회") or "사회"
        urgency   = topic_info.get("urgency", "medium")

        if not topic or urgency == "low":
            continue

        similar = find_similar_trend(issue_ko, days=14)
        if realtime_trend_article_exists(topic) and not similar:
            print(f"  [{topic}] 최근 {RT_CHECK_HOURS}시간 내 이미 생성됨 — 스킵")
            continue

        # 관련 기사 수집
        related = get_articles_for_keyword(topic, now_articles, max_n=8)
        if len(related) < 2:
            related = get_articles_for_keyword(
                topic.split()[0] if ' ' in topic else topic,
                now_articles, max_n=8
            )
        if not related:
            print(f"  [{topic}] 관련 기사 없음 — 스킵")
            continue

        # 기사 생성 프롬프트
        # 보도일·원문을 여기서 한 번 보강해 프롬프트와 date_guard가 함께 쓰게 한다
        related = _merge_source_details(related)
        article_list = ""
        for i, a in enumerate(related, 1):
            t = a.get("title_ko") or a.get("title_en") or ""
            body = a.get("summary_ko") or a.get("summary_en") or ""
            pub = _pub_day_label(a.get("source_published_at"))
            pub_tag = f" (보도 {pub})" if pub else ""
            article_list += f"{i}. [{a.get('source','')}]{pub_tag} {t}\n"
            if body:
                # 2026-08-30 사용자 지적: 300자 제한에 문서화된 근거가 없었음
                # (도입 커밋 2026-06-23도 이유 설명 없음) — 토큰 비용이 실제
                # 제약이 아니므로 소스 본문을 자르지 않고 그대로 씀.
                article_list += f"   {body}\n\n"

        # 대표 국가 추론
        countries_in_articles = [a.get("country") for a in related if a.get("country")]
        from collections import Counter
        country = Counter(countries_in_articles).most_common(1)[0][0] if countries_in_articles else ""

        region_map = {
            "나이지리아":"africa","케냐":"africa","가나":"africa","에티오피아":"africa",
            "남아공":"africa","탄자니아":"africa","르완다":"africa","우간다":"africa",
            "수단":"africa","콩고":"africa","소말리아":"africa","이집트":"africa",
            "모로코":"africa","잠비아":"africa","짐바브웨":"africa","앙골라":"africa",
            "말리":"africa","부르키나파소":"africa","중앙아프리카":"africa",
            "베트남":"southeast_asia","인도네시아":"southeast_asia","태국":"southeast_asia",
            "필리핀":"southeast_asia","말레이시아":"southeast_asia","미얀마":"southeast_asia",
            "카자흐스탄":"central_asia","우즈베키스탄":"central_asia",
            "아이티":"caribbean","자메이카":"caribbean",
        }
        region = region_map.get(country, "africa")

        write_prompt = f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 최근 급부상한 이슈 [{issue_ko}]에 관한 기사들입니다.

[관련 기사]
{article_list}

이 기사들을 종합해 완성도 높은 한국어 기사를 작성하세요.
- 반드시 하나의 토픽만 다루세요.
- 본문은 부가가치 문단을 포함해 3개 문단 이상으로 충분히 작성하세요. 일반적인 스트레이트 기사의 표준 분량은 200자 원고지 10매, 즉 대략 2,000자 이상입니다(2026-08-26 사용자 지적: "죄다 단신급의 기사" → "적어도 기사 하나마다 분량이 2천자 정도는 됐으면", "더 길어도 상관 없다") — 700자는 최소 하한선이지 목표치가 아니니 그 근처에서 서둘러 마무리하지 마세요. 2,000자는 상한이 아니라 하한이라 소스에 사실관계가 더 있으면 넘어가도 됩니다. 배경·경과·전망(또는 파급 효과)을 각각 다루어 분량을 채우세요. 관련 기사에 나온 내용이 부족하면 배경 설명이나 맥락으로 보완하되, 소스에 없는 내용을 지어내 채우지는 마세요 — 소스가 짧은 단신이면 억지로 채우지 말고 가능한 만큼만 쓰세요. ⚠️ 분량 확보보다 환각 방지가 항상 우선입니다("핵심은 환각 현상이 일어나지 않도록 하는거야", "너무 길게 만들다가 헛소리가 나가면 안돼") — 분량과 정확성이 충돌하면 무조건 정확성을 택해 짧게 쓰세요.
- 문단을 나눌 때는 반드시 빈 줄(줄바꿈 2번)로 구분하세요. 한 문단에 모든 문장을 붙여 쓰지 마세요.
- 수치, 인명, 날짜, 기관명 등 구체적 팩트를 최대한 살리세요.
- 왜 지금 이 이슈가 중요한지 맥락을 담되, 사실 서술형으로만 쓰세요.
- 부가가치 분석(메커니즘·규모 가늠·선례 비교·한국 연관성)은 본문에 넣지 말고, 아래 "투자아이디어:" 필드에 별도로 작성하세요. 본문(본문:)에는 순수 사실 서술만 담으세요.
- 본문 마지막 문장을 "~라는 분석이 나온다", "~전망이다", "~지속될 전망입니다", "~귀추가 주목된다" 같은 화자 없는 전망·분석형 문장으로 마무리하지 마세요. 그런 문장은 부가가치 분석이므로 위 규칙대로 "투자아이디어:" 필드로 옮기고, 본문은 마지막까지 구체적 사실(누가, 무엇을, 언제)로 끝내세요.
- 여러 국가·사건을 다룰 때 각 문단을 "~격동 속에서 위축된 상태입니다", "~전술적 변화가 감지되었습니다", "~목소리가 이어지고 있습니다" 같은 화자 없는 추상적 개관 문장으로 시작하지 마세요. 이런 문장으로 문을 열고 뒤에 구체적 사실을 붙이는 구조는 분석·피처 기사 문체이지 스트레이트 뉴스가 아닙니다. 각 문단을 곧바로 구체적 사실(누가, 언제, 무엇을 했는지)로 시작하세요.
- 모든 날짜는 사건이 일어난 현지시간 기준으로만 표기하세요. 한국 시간(KST)이나 UTC로 환산·계산하지 말고, 소스 기사에 나온 날짜를 하루도 앞뒤로 옮기지 말고 그대로 "N일(현지시간)" 형식으로 쓰세요. "2026년 7월 15일", "오늘", "현재" 같은 절대 날짜나 오늘 날짜는 쓰지 말고, 날짜를 알 수 없으면 쓰지 마세요.\n- 요일(월요일~일요일)을 단독으로 쓰지 마세요. 원문에 "on Wednesday"처럼 요일만 있고 날짜가 없더라도 그 요일을 한국어로 옮겨 적지 마세요("수요일 밝혔다", "현지시각 수요일", "지난 금요일" 모두 금지). 요일은 "8일 토요일"처럼 날짜와 병기할 때만 쓸 수 있습니다.\n- 각 원문 제목 옆에 "(보도 M월 D일)"이 붙어 있을 수 있습니다. 그 기사가 실제로 보도된 날짜입니다. 원문 본문에 사건 날짜가 명시돼 있으면 그 날짜가 항상 우선이고, 본문에 날짜 근거가 없을 때만 이 보도일을 사건 시점으로 보아 "D일(현지시간)"으로 쓰세요(예: "보도 8월 3일" → "3일(현지시간)"). 보도일 표기가 없고 원문에도 날짜가 없으면 날짜를 쓰지 마세요. 보도일을 "8월 3일" 같은 절대 날짜 형태로 본문에 적지 마세요.
- "현지시각 기준으로", "현재 시점", "현 시점 기준", "최근 들어" 같은 모호한 시간 표현으로 날짜를 대체하지 마세요. 이런 표현은 금지어입니다. 구체적 날짜를 모르면 시간 표현 자체를 아예 쓰지 말고 사실만 서술하세요.
- 기념일·회고형 소재(집권 N주년, 사건 N년 등)처럼 특정 뉴스 발생일이 없는 경우에는 "N일(현지시간)"을 억지로 만들지 말고, 과거 사건은 연도만("2023년") 표기하세요.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- 모든 문장을 "-다"로 종결하세요(예: "발표했다", "밝혔다", "나타났다"). "-습니다", "-입니다", "-됩니다" 같은 정중체(합쇼체)는 절대 쓰지 마세요. 단, 인용문 안의 발언 자체(따옴표로 감싼 발언)는 예외로 원문 어투를 유지할 수 있습니다.
- 기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다", "~라는 평가다" 같은 논평/칼럼 문체는 금지입니다.
- 인명·기업명·기관명·지명 등 고유명사는 반드시 한글로 음차해 표기하세요(예: Zedcrest Group → 제드크레스트 그룹, Leatherback → 레더백). 영문 원문을 그대로 쓰지 마세요. 키릴·아랍·데바나가리·태국·한자 등 한글이 아닌 문자를 이름 중간에 섞지 마세요("이스ام 파레스", "카르나타카州", "1万2341대" 같은 형태는 심각한 오류입니다). 다만 "고(故)", "대(對)중국"처럼 괄호 안에 넣는 한자 병기는 허용됩니다. 첫 등장 시에만 괄호로 원문을 병기할 수 있습니다(예: 제드크레스트 그룹(Zedcrest Group)).
- 영화·도서·게임 등 작품 제목은 원문에 등장한 원어 표기를 정확히 확인해서만 옮기세요. 공식 한국어 제목을 확신할 수 없으면 새 표현을 지어내거나 음절을 빠뜨리지 말고, 원어 제목을 괄호로 병기하거나(예: "브랜드 뉴 데이(Brand New Day)") 원어 그대로 쓰세요.
- 단, 다음 두 가지는 음차하지 말고 원문 그대로 쓰세요: ① 영문+숫자로 된 코드·규격·모델명(H-1B, 5G, F-35, GPT-5, 통화코드 등) ② 한국 기업 그룹명 약칭(SK, LG, GS, KT, CJ, DL, HD 등) ③ 명칭 안에 영문 약어가 들어간 기업·기관·기술명은 그 약어 부분을 알파벳 그대로 두세요(OpenAI → 오픈AI, xAI → xAI, AI, IT, UN, EU 등). 약어를 한글로 풀어 읽지 마세요(오픈아이·아이티 등은 오표기입니다).

아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (주요 대상 국가 1개, 없으면 "없음")
관련국가: (관련국 최대 4개, 없으면 "없음")
분야: ({category})
3줄요약: (핵심을 정확히 3줄로, 각 줄은 "\n"으로 구분, 각 줄 40자 내외)
투자아이디어: (3~5문장, 메커니즘·규모·선례·한국연관성 요소 포함, 막연한 문장 금지)
본문: (기사 본문, 부가가치 분석 문단 제외 — 순수 사실 서술만)"""

        content_text = call_gemini_article(write_prompt, max_tokens=2000)
        if not content_text:
            print(f"  [{topic}] ❌ 기사 생성 실패")
            time.sleep(CALL_INTERVAL)
            continue

        # 파싱
        title, body, art_country, art_countries = "", content_text, country, []
        for line in content_text.strip().split("\n"):
            if line.startswith("제목:"):
                title = line.replace("제목:", "").strip()
            elif line.startswith("국가:"):
                c = line.replace("국가:", "").strip()
                if c not in ("없음", "-", ""):
                    art_country = normalize_country(c)
            elif line.startswith("관련국가:"):
                raw2 = line.replace("관련국가:", "").strip()
                if raw2 not in ("없음", "-", ""):
                    art_countries = [normalize_country(x.strip()) for x in raw2.split(",") if x.strip()]
            elif line.startswith("본문:"):
                idx = content_text.find("본문:")
                body = _ensure_paragraphs(content_text[idx + 3:].strip())
                summary_3lines = _extract_section(content_text, "3줄요약:")
                investment_idea = _extract_section(content_text, "투자아이디어:")
                break

        if not title:
            title = f"{issue_ko} — {today_str}"

        if detect_script_leak(title, body):
            print(f"  [{topic}] ⚠️ [문자 혼입 감지] 미발행: {title[:50]}")
            time.sleep(CALL_INTERVAL)
            continue

        _unwrapped = unwrap_json_body(body)
        if _unwrapped is not None:
            if _unwrapped:
                print(f"  [{topic}] 🔧 [raw JSON 본문] 내부 body 추출 → 복구")
                body = _unwrapped
            else:
                print(f"  [{topic}] ⛔ [raw JSON 본문] 미발행: {title[:50]}")
                time.sleep(CALL_INTERVAL)
                continue

        # 생성된 실제 제목+국가로 동일 사건 루트 재확인 (우선)
        similar = find_similar_trend(title, country=art_country, days=14, body=body)

        # 단일 토픽 검수 — 무관한 사건이 묶였으면 병합·발행 모두 차단
        _mt_bad = not verify_single_topic(title, body)
        if _mt_bad:
            print(f"  [{topic}] ⛔ 복수 토픽 혼입 → 미발행: {title[:50]}")

        # 날짜 환각 판정 — 원문에 근거 없는 "N일(현지시간)"이면 미발행
        # ⚠️ 병합 분기보다 반드시 앞에 둘 것. 뒤에 두면 병합 경로가 가드를 통째로
        #    우회해 환각 날짜가 이미 발행된 기사에 append된다(별도 기사보다 나쁨).
        _dg_bad, _dg_reason = (False, "")
        if not _mt_bad:
            _dg_bad, _dg_reason = check_date_hallucination(
                body, related, base_date=now_kst().date()
            )
            if _dg_bad:
                print(f"  [{topic}] ⛔ 날짜 환각 의심 → 미발행: {_dg_reason}")

        # 유사 기존 트렌드 기사 있으면 병합
        if similar and not (_mt_bad or _dg_bad):
            note = f"추가 정보 업데이트 ({topic})"
            ok = merge_trend_article(similar, title, body, note)
            if ok:
                print(f"  [{topic}] ✅ 기존 트렌드 기사에 병합 (id={similar['id']}): {title}")
                generated += 1
            time.sleep(CALL_INTERVAL)
            continue

        now_str = now_kst().strftime("%Y-%m-%d %H:%M")

        payload = {
            "title_en": title, "title_ko": title,
            "summary_en": "", "summary_ko": body,
            "url": f"internal://realtrend_{topic.replace(' ','_')}_{now_kst().strftime('%Y%m%d%H')}",
            "source": "NewsFinal",
            "category": category,
            "subcategory": f"realtrend_{topic[:20].replace(' ','_')}",
            "region": region_map.get(art_country, region),
            "country": art_country,
            "country_flag": "",
            "countries": ([art_country] + [c for c in (art_countries or []) if c and c != art_country]) if art_country else (art_countries or []),
            "score": 2,
            "created_at": now_str,
            "first_published_at": now_str,
            "update_log": [{"timestamp": now_str,
                            "note": (MULTI_TOPIC_NOTE if _mt_bad
                                     else (f"날짜 환각 의심 미발행 — {_dg_reason}" if _dg_bad
                                           else f"실시간 트렌드 감지 ({topic}, {urgency})"))}],
            "sent_telegram": 0,
            "is_published": not (_mt_bad or _dg_bad),
            "posted_blog": 0,
            "summary_3lines": summary_3lines,
            "investment_idea": investment_idea,
        }

        art_id = insert_final_article(payload)
        if art_id > 0:
            print(f"  [{topic}] ✅ 실시간 트렌드 기사 생성 (id={art_id}): {title}")
            generated += 1

            # 텔레그램 발송
            if TELEGRAM_TOKEN:
                try:
                    preview = body[:300]
                    url = f"https://newsfinal.co.kr/article?id={art_id}"
                    msg = f"📈 실시간 트렌드\n\n*{title}*\n\n{preview}{'…' if len(body) > 300 else ''}\n\n[전체 기사 보기]({url})"
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": NEWSFINAL_CHANNEL, "text": msg,
                              "parse_mode": "Markdown",
                              "link_preview_options": json.dumps({"prefer_small_media": True, "url": url})},
                        timeout=15
                    )
                except Exception:
                    pass
        else:
            print(f"  [{topic}] ❌ 저장 실패")

        time.sleep(CALL_INTERVAL)

    print(f"[실시간 트렌드] 완료 — {generated}건 생성")


# ── 외부 트렌드 신호 기반 기사 생성 ──────────────────────────

EXT_MIN_SCORE     = 5   # 최소 합산 점수
EXT_MAX_ARTICLES  = 2   # 1회 최대 생성 건수
EXT_CHECK_HOURS   = 8   # 중복 방지 간격


def ext_trend_exists(topic: str) -> bool:
    """최근 N시간 내 같은 토픽 기사가 있는지 확인"""
    since = (now_kst() - timedelta(hours=EXT_CHECK_HOURS)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id",
                "source": "eq.NewsFinal",
                "subcategory": "like.extrend_%",
                "title_ko": f"ilike.*{topic[:12]}*",
                "created_at": f"gte.{since}",
                "limit": "1",
            },
            timeout=10
        )
        if res.status_code in (200, 206):
            return len(res.json()) > 0
    except Exception:
        pass
    return False


def run_external_trend_articles(signals: list):
    """외부 트렌드 신호 기반 기사 생성"""
    if not signals or not GEMINI_API_KEYS:
        return

    print(f"\n[외부 트렌드 기사화] 점수 {EXT_MIN_SCORE}pt 이상 토픽 처리...")
    today_str = now_kst().strftime("%Y년 %m월 %d일")

    # 점수 높은 순, 최대 처리 수 제한
    candidates = [s for s in signals if s["score"] >= EXT_MIN_SCORE][:10]
    if not candidates:
        print("  [SKIP] 임계값 이상 토픽 없음")
        return

    # Gemini로 실제 기사화할 토픽 최종 선별
    topic_list = ""
    for i, s in enumerate(candidates[:10], 1):
        src_str = "+".join(s["sources"])
        countries = ", ".join(list(s["countries"])[:3])
        titles_str = " / ".join(s["titles"][:2])
        is_global_major = "gdelt_global" in s["sources"]
        tag = "[초대형·국가무관] " if is_global_major else ""
        topic_list += f"{i}. {tag}[{s['score']}pt/{src_str}] {s['topic']} ({countries})\n   예시: {titles_str}\n"

    screen_prompt = f"""당신은 프론티어 마켓 전문 에디터입니다. ({today_str})
아래는 Google Trends, Reddit, GDELT에서 수집한 트렌드 신호입니다. 대부분 프론티어 마켓(아프리카·동남아시아 등) 신호지만, "[초대형·국가무관]" 태그가 붙은 항목은 프론티어 여부와 무관하게 전 세계에서 동시다발적으로 크게 보도되는 진짜 초대형 이슈(대형 재난·다수 사망·비상사태 선포 등)를 별도로 감지한 것입니다.

{topic_list}

위 신호들 중 최대 {EXT_MAX_ARTICLES}개를 선별하세요.
- "[초대형·국가무관]" 태그가 붙은 항목은 설령 선진국(유럽·미국·일본 등) 이슈라도, 실제로 대규모 인명·재산 피해나 국제적 파급력이 있는 진짜 초대형 사안이면 프론티어 마켓 여부와 무관하게 우선 선별하세요. 이미 국내 언론이 다룰 정도로 크다는 것 자체가 배제 사유가 아닙니다.
- 그 외 일반 신호는 기존대로 단순 스포츠/연예/날씨/로또는 제외, 경제·금융·정치·사회 분야 실질적 사건이나 정책 변화 우선, 여러 소스에서 동시에 잡힌 토픽 우선으로 선별하세요.

JSON 배열로만 응답 (마크다운 없이):
[
  {{
    "topic": "토픽 키워드",
    "issue_ko": "한 줄 설명",
    "category": "경제 | 금융 | 자원·에너지 | 산업·기업 | 정치·외교 | 사회 | IT·과학 중 정확히 하나. 여러 개를 나열하지 마세요.",
    "countries": ["국가1", "국가2"],
    "region": "africa/southeast_asia/central_asia/middle_east/south_asia/caribbean/europe/east_asia/north_america/latin_america/oceania/global 중 하나 (해당 국가의 실제 지역으로, global은 특정 지역에 국한 안 될 때만)"
  }}
]
선별할 이슈 없으면 []"""

    import json, re
    raw = call_gemini(screen_prompt, max_tokens=600)
    if not raw:
        return

    try:
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            return
        selected = json.loads(match.group())
        if not isinstance(selected, list) or not selected:
            print("  [SKIP] 선별된 토픽 없음")
            return
    except Exception as e:
        print(f"  [SKIP] JSON 파싱 실패: {e}")
        return

    print(f"  Gemini 선별: {len(selected)}개")

    generated = 0
    for item in selected:
        if generated >= EXT_MAX_ARTICLES:
            break

        topic    = item.get("topic", "")
        issue_ko = item.get("issue_ko", "")
        category = normalize_category(item.get("category", ""), default="경제") or "경제"
        countries_list = item.get("countries", [])
        region   = item.get("region", "global")
        country  = countries_list[0] if countries_list else ""

        if not topic:
            continue
        ext_similar = find_similar_trend(issue_ko, days=14)
        if ext_trend_exists(topic) and not ext_similar:
            continue

        # 관련 신호에서 대표 제목 수집
        matched_signal = next(
            (s for s in candidates if topic.lower() in s["topic"].lower()
             or s["topic"].lower() in topic.lower()), None
        )
        ref_titles = matched_signal["titles"] if matched_signal else []

        # ⚠️ 소스 신호가 없으면 기사를 만들지 않는다.
        # ref_titles가 비면 아래 프롬프트의 "관련 신호:" 항목이 통째로 비어,
        # Gemini가 근거 없이 학습 기억에서 기사를 창작한다.
        # 실제 사고: id=47705 — 토픽은 'Kenya-Ethiopia Coope'인데 본문은
        # "오픈AI가 GPT-5 출시를 내년 상반기로 연기" (GPT-5는 2025-08 이미 출시됨).
        # 날짜 지시("소스 기사에 나온 날짜를 그대로")도 소스가 없으면 무력하다.
        if not ref_titles:
            print(f"  [{topic}] ⏭️  관련 신호 없음 → 생성 건너뜀 (근거 없는 창작 방지)")
            continue
        is_global_major_topic = bool(matched_signal and "gdelt_global" in matched_signal.get("sources", []))
        frontier_impact_note = (
            "\n- 이 사건은 선진국에서 벌어진 초대형 이슈이지만, 프론티어 마켓(아프리카·동남아시아·중앙아시아 등)에도 실제로 영향이 미칩니다"
            "(예: 관광 의존 경제의 타격, 원자재·농산물 가격 변동, 보험·재보험 시장, 공급망 차질 등). "
            "\"투자아이디어:\" 필드에는 한국 연관성뿐 아니라 이 사건이 프론티어 마켓에 미치는 실질적 파급 효과도 구체적으로 다루세요."
            if is_global_major_topic else ""
        )

        # 기사 생성
        write_prompt = f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
Google Trends, Reddit, GDELT에서 [{issue_ko}] 이슈가 급부상하고 있습니다.

관련 신호:
{chr(10).join(f"- {t}" for t in ref_titles)}

이 이슈에 대해 한국 투자자/독자를 위한 완성도 높은 기사를 작성하세요.
- 이슈의 배경, 현재 상황, 의미를 사실 서술형으로 담으세요.
- 본문은 부가가치 문단을 포함해 3개 문단 이상으로 작성하세요. 일반적인 스트레이트 기사의 표준 분량은 200자 원고지 10매, 즉 대략 2,000자 이상입니다 — 700자는 최소 하한선이지 목표치가 아니니 그 근처에서 서둘러 마무리하지 마세요. 2,000자는 상한이 아니라 하한이라 소스에 사실관계가 더 있으면 넘어가도 됩니다. 배경·경과·의미(또는 파급 효과)를 각각 풀어서 서술하되, 소스가 짧은 단신이면 억지로 채우지 말고 가능한 만큼만 쓰세요. ⚠️ 분량 확보보다 환각 방지가 항상 우선입니다 — 분량과 정확성이 충돌하면 무조건 정확성을 택해 짧게 쓰세요.
- 문단을 나눌 때는 반드시 빈 줄(줄바꿈 2번)로 구분하세요. 한 문단에 모든 문장을 붙여 쓰지 마세요.
- 확인된 팩트 중심으로, 추측은 최소화하세요.
- 부가가치 분석(메커니즘·규모 가늠·선례 비교·한국 연관성)은 본문에 넣지 말고, 아래 "투자아이디어:" 필드에 별도로 작성하세요. 본문(본문:)에는 순수 사실 서술만 담으세요.{frontier_impact_note}
- 본문 마지막 문장을 "~라는 분석이 나온다", "~전망이다", "~지속될 전망입니다", "~귀추가 주목된다" 같은 화자 없는 전망·분석형 문장으로 마무리하지 마세요. 그런 문장은 부가가치 분석이므로 위 규칙대로 "투자아이디어:" 필드로 옮기고, 본문은 마지막까지 구체적 사실(누가, 무엇을, 언제)로 끝내세요.
- 여러 국가·사건을 다룰 때 각 문단을 "~격동 속에서 위축된 상태입니다", "~전술적 변화가 감지되었습니다", "~목소리가 이어지고 있습니다" 같은 화자 없는 추상적 개관 문장으로 시작하지 마세요. 이런 문장으로 문을 열고 뒤에 구체적 사실을 붙이는 구조는 분석·피처 기사 문체이지 스트레이트 뉴스가 아닙니다. 각 문단을 곧바로 구체적 사실(누가, 언제, 무엇을 했는지)로 시작하세요.
- 모든 날짜는 사건이 일어난 현지시간 기준으로만 표기하세요. 한국 시간(KST)이나 UTC로 환산·계산하지 말고, 소스 기사에 나온 날짜를 하루도 앞뒤로 옮기지 말고 그대로 "N일(현지시간)" 형식으로 쓰세요. "2026년 7월 15일", "오늘", "현재" 같은 절대 날짜나 오늘 날짜는 쓰지 말고, 날짜를 알 수 없으면 쓰지 마세요.\n- 요일(월요일~일요일)을 단독으로 쓰지 마세요. 원문에 "on Wednesday"처럼 요일만 있고 날짜가 없더라도 그 요일을 한국어로 옮겨 적지 마세요("수요일 밝혔다", "현지시각 수요일", "지난 금요일" 모두 금지). 요일은 "8일 토요일"처럼 날짜와 병기할 때만 쓸 수 있습니다.\n- 각 원문 제목 옆에 "(보도 M월 D일)"이 붙어 있을 수 있습니다. 그 기사가 실제로 보도된 날짜입니다. 원문 본문에 사건 날짜가 명시돼 있으면 그 날짜가 항상 우선이고, 본문에 날짜 근거가 없을 때만 이 보도일을 사건 시점으로 보아 "D일(현지시간)"으로 쓰세요(예: "보도 8월 3일" → "3일(현지시간)"). 보도일 표기가 없고 원문에도 날짜가 없으면 날짜를 쓰지 마세요. 보도일을 "8월 3일" 같은 절대 날짜 형태로 본문에 적지 마세요.
- "현지시각 기준으로", "현재 시점", "현 시점 기준", "최근 들어" 같은 모호한 시간 표현으로 날짜를 대체하지 마세요. 이런 표현은 금지어입니다. 구체적 날짜를 모르면 시간 표현 자체를 아예 쓰지 말고 사실만 서술하세요.
- 기념일·회고형 소재(집권 N주년, 사건 N년 등)처럼 특정 뉴스 발생일이 없는 경우에는 "N일(현지시간)"을 억지로 만들지 말고, 과거 사건은 연도만("2023년") 표기하세요.
- 반드시 하나의 토픽만 다루세요.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- 모든 문장을 "-다"로 종결하세요(예: "발표했다", "밝혔다", "나타났다"). "-습니다", "-입니다", "-됩니다" 같은 정중체(합쇼체)는 절대 쓰지 마세요. 단, 인용문 안의 발언 자체(따옴표로 감싼 발언)는 예외로 원문 어투를 유지할 수 있습니다.
- 기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다", "~라는 평가다" 같은 논평/칼럼 문체는 금지입니다.
- 인명·기업명·기관명·지명 등 고유명사는 반드시 한글로 음차해 표기하세요(예: Zedcrest Group → 제드크레스트 그룹, Leatherback → 레더백). 영문 원문을 그대로 쓰지 마세요. 키릴·아랍·데바나가리·태국·한자 등 한글이 아닌 문자를 이름 중간에 섞지 마세요("이스ام 파레스", "카르나타카州", "1万2341대" 같은 형태는 심각한 오류입니다). 다만 "고(故)", "대(對)중국"처럼 괄호 안에 넣는 한자 병기는 허용됩니다. 첫 등장 시에만 괄호로 원문을 병기할 수 있습니다(예: 제드크레스트 그룹(Zedcrest Group)).
- 영화·도서·게임 등 작품 제목은 원문에 등장한 원어 표기를 정확히 확인해서만 옮기세요. 공식 한국어 제목을 확신할 수 없으면 새 표현을 지어내거나 음절을 빠뜨리지 말고, 원어 제목을 괄호로 병기하거나(예: "브랜드 뉴 데이(Brand New Day)") 원어 그대로 쓰세요.
- 단, 다음 두 가지는 음차하지 말고 원문 그대로 쓰세요: ① 영문+숫자로 된 코드·규격·모델명(H-1B, 5G, F-35, GPT-5, 통화코드 등) ② 한국 기업 그룹명 약칭(SK, LG, GS, KT, CJ, DL, HD 등) ③ 명칭 안에 영문 약어가 들어간 기업·기관·기술명은 그 약어 부분을 알파벳 그대로 두세요(OpenAI → 오픈AI, xAI → xAI, AI, IT, UN, EU 등). 약어를 한글로 풀어 읽지 마세요(오픈아이·아이티 등은 오표기입니다).
- 한국어로만 작성하세요.

아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (주요 국가 1개, 없으면 "없음")
관련국가: (관련국 최대 4개, 없으면 "없음")
분야: ({category})
3줄요약: (핵심을 정확히 3줄로, 각 줄은 "\n"으로 구분, 각 줄 40자 내외)
투자아이디어: (3~5문장, 메커니즘·규모·선례·한국연관성 요소 포함, 막연한 문장 금지)
본문: (기사 본문, 부가가치 분석 문단 제외 — 순수 사실 서술만)"""

        content_text = call_gemini_article(write_prompt, max_tokens=2000)
        if not content_text:
            print(f"  [{topic}] ❌ 생성 실패")
            time.sleep(CALL_INTERVAL)
            continue

        # 파싱
        title, body, art_country, art_countries = "", content_text, country, countries_list
        for line in content_text.strip().split("\n"):
            if line.startswith("제목:"):
                title = line.replace("제목:", "").strip()
            elif line.startswith("국가:"):
                c = line.replace("국가:", "").strip()
                if c not in ("없음", "-", ""):
                    art_country = normalize_country(c)
            elif line.startswith("관련국가:"):
                raw2 = line.replace("관련국가:", "").strip()
                if raw2 not in ("없음", "-", ""):
                    art_countries = [normalize_country(x.strip()) for x in raw2.split(",") if x.strip()]
            elif line.startswith("본문:"):
                idx = content_text.find("본문:")
                body = _ensure_paragraphs(content_text[idx + 3:].strip())
                summary_3lines = _extract_section(content_text, "3줄요약:")
                investment_idea = _extract_section(content_text, "투자아이디어:")
                break

        if not title:
            title = f"{issue_ko} — {today_str}"

        if detect_script_leak(title, body):
            print(f"  [{topic}] ⚠️ [문자 혼입 감지] 미발행: {title[:50]}")
            time.sleep(CALL_INTERVAL)
            continue

        _unwrapped = unwrap_json_body(body)
        if _unwrapped is not None:
            if _unwrapped:
                print(f"  [{topic}] 🔧 [raw JSON 본문] 내부 body 추출 → 복구")
                body = _unwrapped
            else:
                print(f"  [{topic}] ⛔ [raw JSON 본문] 미발행: {title[:50]}")
                time.sleep(CALL_INTERVAL)
                continue

        # 생성된 실제 제목+국가로 동일 사건 루트 재확인 (우선)
        ext_similar = find_similar_trend(title, country=art_country, days=14, body=body)

        # 단일 토픽 검수 — 무관한 사건이 묶였으면 병합·발행 모두 차단
        _mt_bad = not verify_single_topic(title, body)
        if _mt_bad:
            print(f"  [{topic}] ⛔ 복수 토픽 혼입 → 미발행: {title[:50]}")

        now_str = now_kst().strftime("%Y-%m-%d %H:%M")
        payload = {
            "title_en": title, "title_ko": title,
            "summary_en": "", "summary_ko": body,
            "url": f"internal://extrend_{topic[:20].replace(' ','_')}_{now_kst().strftime('%Y%m%d%H')}",
            "source": "NewsFinal",
            "category": category,
            "subcategory": f"extrend_{topic[:20].replace(' ','_')}",
            "region": region,
            "country": art_country,
            "country_flag": "",
            "countries": ([art_country] + [c for c in (art_countries or []) if c and c != art_country]) if art_country else (art_countries or []),
            "score": 2,
            "created_at": now_str,
            "first_published_at": now_str,
            "update_log": [{"timestamp": now_str,
                            "note": (MULTI_TOPIC_NOTE if _mt_bad
                                     else "외부 트렌드 감지 (Google Trends+Reddit+GDELT)")}],
            "sent_telegram": 0,
            "is_published": not _mt_bad,
            "posted_blog": 0,
            "summary_3lines": summary_3lines,
            "investment_idea": investment_idea,
        }
        # 유사 기존 트렌드 기사 있으면 병합
        if ext_similar and not _mt_bad:
            note = f"외부 트렌드 추가 정보 ({topic})"
            ok = merge_trend_article(ext_similar, title, body, note)
            if ok:
                print(f"  [{topic}] ✅ 기존 트렌드 기사에 병합 (id={ext_similar['id']}): {title}")
                generated += 1
            time.sleep(CALL_INTERVAL)
            continue

        art_id = insert_final_article(payload)
        if art_id > 0:
            print(f"  [{topic}] ✅ 외부 트렌드 기사 생성 (id={art_id}): {title}")
            generated += 1
            if TELEGRAM_TOKEN:
                try:
                    preview = body[:300]
                    url = f"https://newsfinal.co.kr/article?id={art_id}"
                    src_str = "+".join(matched_signal["sources"]) if matched_signal else "외부"
                    msg = f"🌐 외부 트렌드 [{src_str}]\n\n*{title}*\n\n{preview}{'…' if len(body)>300 else ''}\n\n[전체 기사 보기]({url})"
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": NEWSFINAL_CHANNEL, "text": msg,
                              "parse_mode": "Markdown",
                              "link_preview_options": json.dumps({"prefer_small_media": True, "url": url})},
                        timeout=15
                    )
                except Exception:
                    pass
        else:
            print(f"  [{topic}] ❌ 저장 실패")

        time.sleep(CALL_INTERVAL)

    print(f"[외부 트렌드 기사화] 완료 — {generated}건 생성")

def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음 — gemini_summarizer 건너뜀")
        return

    # API 연결 테스트
    print(f"[체크] Gemini API 연결 테스트... (키 {len(GEMINI_API_KEYS)}개)")
    test = call_gemini("ping", retry=1, start_tier=3)
    if test is None:
        print("[SKIP] Gemini API 응답 없음 — 건너뜀")
        return
    _gemini_client._current_key_idx = 0  # ping으로 밀린 로테이션 인덱스 리셋 — 실제 작업은 항상 1번 키부터 순환
    print("[체크] ✅ API 연결 확인")

    articles = get_articles_to_summarize(MAX_ARTICLES)
    print(f"[요약 고도화] 대상 기사 {len(articles)}건")

    success = 0
    for i, article in enumerate(articles):
        prompt = build_prompt(article)
        has_full = bool(article.get("full_text"))
        summary_ko = call_gemini(prompt)

        if summary_ko:
            update_summary(article["id"], summary_ko)
            src = "원문" if has_full else "RSS요약"
            print(f"[{i+1}/{len(articles)}] ✅ [{src}] {article['title_ko'] or article['title_en'][:50]}")
            success += 1
        else:
            print(f"[{i+1}/{len(articles)}] ❌ 실패 — {article['title_en'][:50]}")

        # API 한도 준수
        if i < len(articles) - 1:
            time.sleep(CALL_INTERVAL)

    print(f"\n✅ 요약 고도화 완료: {success}/{len(articles)}건 성공")

    # 장기 이슈 트렌드 추적
    run_trend_tracker()

    # 실시간 트렌드 감지 (A+B)
    run_realtime_trend_tracker()

    # 외부 트렌드 신호 수집 및 기사화 (GDELT + Google Trends + Reddit)
    if EXTERNAL_TRENDS_AVAILABLE:
        ext_signals = collect_external_trends(verbose=True)
        run_external_trend_articles(ext_signals)
    else:
        print("[외부 트렌드] external_trends.py 모듈 없음 — 스킵")


if __name__ == "__main__":
    run()
