"""
gemini_summarizer.py
--------------------
DB에 저장된 기사 중 summary_ko가 빈약한 기사를 골라
Gemini Flash로 고품질 한국어 요약을 재생성합니다.

실행: python scripts/gemini_summarizer.py
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# 외부 트렌드 수집 모듈 (GDELT, Google Trends, Reddit)
try:
    from external_trends import collect_external_trends
    EXTERNAL_TRENDS_AVAILABLE = True
except ImportError:
    EXTERNAL_TRENDS_AVAILABLE = False

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

GEMINI_MODEL_PRIMARY  = "gemini-3.5-flash-lite"
GEMINI_MODEL_FALLBACK = "gemini-3.1-flash-lite"
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

_current_key_idx = 0
_exhausted_keys_primary  = set()  # RPD 소진 키 (3.5)
_exhausted_keys_fallback = set()  # RPD 소진 키 (3.1)
MAX_ARTICLES = 20
CALL_INTERVAL = 10


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
날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- "2026년 6월 24일 현재", "오늘", "현재" 등 절대 날짜를 본문에 쓰지 마세요. 소스 기사의 날짜 기준으로 "N일(현지시간)"으로만 표기하세요.
기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다" 같은 논평/칼럼 문체는 금지입니다.
출처 매체의 구독/앱/서비스 홍보 문장은 제거하세요."""

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


def call_gemini(prompt: str, retry: int = 2, max_tokens: int = 500) -> str | None:
    global _current_key_idx, _exhausted_keys_primary, _exhausted_keys_fallback
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": max_tokens,
        }
    }

    n = len(GEMINI_API_KEYS)
    model_stages = [
        (GEMINI_MODEL_PRIMARY,  _exhausted_keys_primary),
        (GEMINI_MODEL_FALLBACK, _exhausted_keys_fallback),
    ]

    for model, exhausted in model_stages:
        available = [i for i in range(n) if i not in exhausted]
        if not available:
            print(f"  [{model}] 모든 키 RPD 소진 → 다음 모델로")
            continue

        ordered = sorted(available, key=lambda i: (i - _current_key_idx) % n)

        for idx in ordered:
            api_key = GEMINI_API_KEYS[idx]
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                res = requests.post(url, json=payload, timeout=(10, 30))
                if res.status_code == 200:
                    _current_key_idx = (idx + 1) % n
                    return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                elif res.status_code == 429:
                    print(f"  [429] {model} 키 {idx+1} RPD 소진 — 블랙리스트 추가")
                    exhausted.add(idx)
                    continue
                elif res.status_code == 503:
                    print(f"  [503] {model} 키 {idx+1} 과부하 → 다음 키로")
                    continue
                else:
                    print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                    return None
            except requests.exceptions.Timeout:
                print(f"  [TIMEOUT] {model} 키 {idx+1} — 다음 키로")
                continue
            except Exception as e:
                print(f"[ERROR] {e}")
                return None

    print("[ERROR] 모든 모델/키 소진 또는 응답 없음")
    return None


# ── 논평/칼럼체 검출 및 재생성 (트렌드/추적 기사는 "의미/중요성"을
# 서술하라는 지시가 섞여 있어 논평체가 특히 섞이기 쉬워 재생성 안전장치를 둔다) ──
BANNED_STYLE_PATTERNS = [
    r"보여줍니다", r"보여주고 있습니다", r"보여준다",
    r"도모하고 있습니다", r"도모한다",
    r"강조하고 있습니다", r"강조한다",
    r"시사합니다", r"시사한다",
    r"주목됩니다", r"주목된다", r"주목받고 있습니다",
    r"평가된다", r"평가받고 있습니다", r"라는 평가다", r"라는 분석이다",
    r"필요해 보입니다", r"필요할 것으로 보입니다",
    r"지켜볼 필요가 있습니다", r"지켜봐야 할 것입니다",
    r"기대됩니다", r"기대해 볼 만합니다",
]

def has_column_style(text: str) -> bool:
    if not text:
        return False
    return any(re.search(p, text) for p in BANNED_STYLE_PATTERNS)


def call_gemini_article(prompt, max_tokens=2000, style_retries=1):
    content = call_gemini(prompt, max_tokens=max_tokens)
    attempt = 0
    while content and has_column_style(content) and attempt < style_retries:
        attempt += 1
        print(f"  ⚠️ 논평/칼럼체 감지 → 재생성 시도 ({attempt}/{style_retries})")
        retry_prompt = (
            prompt
            + "\n\n[재작성 지시] 방금 작성한 결과에 논평/칼럼 문체(예: '~를 보여줍니다', "
              "'~을 도모하고 있습니다', '~라는 평가다', '~지켜볼 필요가 있습니다' 등)가 섞여 있었습니다. "
              "감정·의견이 섞인 표현을 모두 배제하고, 사실 전달 중심의 스트레이트 뉴스 문체로만 다시 작성하세요."
        )
        retried = call_gemini(retry_prompt, max_tokens=max_tokens)
        if retried:
            content = retried
    if content and has_column_style(content):
        print("  ⚠️ 재생성 후에도 논평체 패턴이 남아있음 (그대로 진행)")
    return content


# ── 장기 이슈 트래커 ────────────────────────────────────────

# 추적할 키워드 그룹 — (그룹명, 카테고리, [키워드 목록])
TREND_KEYWORDS = [
    ("에볼라",       "사회",    ["ebola", "에볼라", "hemorrhagic fever", "출혈열", "MVD", "marburg"]),
    ("mpox",        "사회",    ["mpox", "monkeypox", "원숭이두창"]),
    ("콜레라",       "사회",    ["cholera", "콜레라"]),
    ("수단 분쟁",    "정치·외교", ["sudan", "RSF", "수단", "다르푸르", "darfur", "khartoum"]),
    ("DRC 분쟁",    "정치·외교", ["DRC", "congo", "콩고", "M23", "키부", "kivu"]),
    ("소말리아",     "정치·외교", ["somalia", "소말리아", "al-shabaab", "알샤바브"]),
    ("미얀마",       "정치·외교", ["myanmar", "미얀마", "junta", "군부", "NUG"]),
    ("아이티",       "사회",    ["haiti", "아이티", "gang", "갱단"]),
    ("사헬 쿠데타",  "정치·외교", ["sahel", "사헬", "mali", "말리", "niger", "burkina", "부르키나"]),
    ("중앙아프리카",  "정치·외교", ["central african", "중앙아프리카", "CAR", "bangui"]),
]

# 추적 윈도우: 7일간 기사에서 키워드 빈도 분석
TREND_WINDOW_DAYS = 7
TREND_MIN_ARTICLES = 3   # 최소 N건 이상 등장해야 트렌드로 판단
TREND_CHECK_HOURS  = 12  # 마지막 추적기사 생성 후 N시간 이내면 스킵


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
    return unique



def _title_keywords(t: str) -> set:
    """제목에서 의미 키워드 추출(2자 이상 토큰, 불용어 제외)."""
    import re
    toks = re.findall(r"[가-힣A-Za-z0-9]+", (t or "").lower())
    return {w for w in toks if len(w) >= 2 and w not in FREQ_STOPWORDS}


def find_similar_trend(title: str, country: str | None = None,
                       days: int = 14, sim_threshold: int = 60) -> dict | None:
    """
    최근 N일 내 트렌드 기사 중 동일 사건의 '루트(최초 발행=최소 id)' 반환. 없으면 None.
    매칭: country 지정 시 country 일치 필수 + 제목 token_sort_ratio>=sim_threshold + 공유 키워드>=1.
          country=None이면 제목 유사도만으로 느슨히 탐색(사전 스킵 판단용).
    id 오름차순 조회 → 첫 매칭이 곧 루트.
    """
    from rapidfuzz import fuzz
    since = (now_kst() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    params = {
        "select": "id,title_ko,summary_ko,update_log,country",
        "source": "eq.NewsFinal",
        "or": "(subcategory.like.trend_*,subcategory.like.realtrend_*,subcategory.like.extrend_*)",
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
        for a in res.json():
            existing_title = a.get("title_ko") or ""
            if not existing_title:
                continue
            sim = fuzz.token_sort_ratio(title.lower(), existing_title.lower())
            shared = new_kw & _title_keywords(existing_title)
            if sim >= sim_threshold and (not country or len(shared) >= 1):
                print(f"    → 유사 트렌드 루트 발견 (id={a['id']}, 유사도 {sim}%, 공유KW {len(shared)}): {existing_title[:40]}")
                return a
    except Exception as e:
        print(f"    → 유사도 체크 실패: {e}")
    return None


def _summarize_delta(root_summary: str, new_title: str, new_body: str) -> str:
    """루트 기사에 없는 '새 전개'만 1~3문장으로 요약. 새 사실 없으면 '없음'."""
    # [업데이트 이력] 이전 원본 본문만 비교 기준으로 사용 (기존 delta 제외)
    base_summary = root_summary.split("────────\n[업데이트 이력]")[0].strip()
    prompt = f"""아래는 진행 중인 사건의 기존 정리 기사와, 방금 수집된 새 기사입니다.
기존 기사에 '없는 새로운 사실'만 1~3문장으로 요약하세요.
- 날짜는 반드시 소스에 명시된 "N일(현지시간)" 형식만 사용. "오늘", "어제", "화요일", "월요일" 등 요일·상대적 표현 금지. 날짜 정보가 없으면 생략.
- 새로운 사실이 없으면 정확히 "없음" 한 단어만 출력.
- 논평·마크다운·헤더 금지, 사실 서술형 한국어로만.

[기존 정리 기사]
{base_summary[:1500]}

[새 기사] {new_title}
{new_body[:1200]}

새 전개 요약:"""
    try:
        out = call_gemini_article(prompt, max_tokens=300)
        if out:
            out = out.strip()
            if out.startswith("새 전개 요약:"):
                out = out.split(":", 1)[1].strip()
            return out.strip()
    except Exception as e:
        print(f"    → 델타 요약 실패: {e}")
    return ((new_body or "")[:200]).strip()


def merge_trend_article(existing: dict, new_title: str, new_body: str, note: str) -> bool:
    """기존 트렌드 루트 기사에 '새 전개'만 append(리빙 아티클). 제목·기존 본문은 덮어쓰지 않음."""
    art_id = existing["id"]
    existing_summary = existing.get("summary_ko") or ""
    existing_log = existing.get("update_log") or []

    delta = _summarize_delta(existing_summary, new_title, new_body)
    if not delta or delta.replace(".", "").strip() == "없음":
        print(f"    → 새 전개 없음, append 생략 (id={art_id})")
        return True  # 병합 성공 처리 → 신규 중복 생성 방지

    if "[업데이트 이력]" not in existing_summary:
        new_summary = existing_summary.rstrip() + "\n\n────────\n[업데이트 이력]\n■ " + delta
    else:
        new_summary = existing_summary.rstrip() + "\n■ " + delta

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    new_log = existing_log + [{"timestamp": now_str, "note": note}]
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


def save_trend_article(group_name: str, title: str, body: str,
                       category: str, country: str, region: str,
                       countries: list) -> int:
    """트렌드 추적 기사 저장"""
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
        "score": 2,  # 라이브 탭에 바로 표시
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "트렌드 추적 최초 게시"}],
        "sent_telegram": 0,
        "is_published": True,
        "posted_blog": 0,
    }
    try:
        res = requests.post(_sb_url(), headers=_sb_headers(), json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
    except Exception as e:
        print(f"  ⚠️ 트렌드 기사 저장 실패: {e}")
    return -1


def run_trend_tracker():
    """장기 이슈 트렌드 감지 및 추적 기사 생성"""
    if not GEMINI_API_KEYS:
        return

    print("\n[트렌드 트래커] 장기 이슈 분석 시작...")

    for group_name, category, keywords in TREND_KEYWORDS:
        articles = get_trend_articles(keywords, days=TREND_WINDOW_DAYS)

        if len(articles) < TREND_MIN_ARTICLES:
            print(f"  [{group_name}] {len(articles)}건 — 임계값 미달, 스킵")
            continue

        print(f"  [{group_name}] {len(articles)}건 감지 → 추적 기사 생성 검토")

        if trend_article_exists(group_name):
            print(f"  [{group_name}] 최근 {TREND_CHECK_HOURS}시간 내 이미 생성됨 — 스킵")
            continue

        # 최신 기사 최대 8건으로 Gemini 프롬프트 구성
        top = sorted(articles, key=lambda a: a.get("created_at", ""), reverse=True)[:8]
        today_str = now_kst().strftime("%Y년 %m월 %d일")

        article_list = ""
        for i, a in enumerate(top, 1):
            t = a.get("title_ko") or a.get("title_en") or ""
            body = a.get("full_text") or a.get("summary_ko") or a.get("summary_en") or ""
            article_list += f"{i}. [{a.get('source','')}] {t}\n"
            if body:
                article_list += f"   {body[:300]}\n\n"

        prompt = f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 지난 {TREND_WINDOW_DAYS}일간 [{group_name}] 관련 기사 {len(articles)}건의 주요 내용입니다.

[수집된 관련 기사]
{article_list}

이 기사들을 종합해 현재 진행 중인 상황을 정리하는 추적 기사를 작성하세요.
- 현재 상황이 어떻게 전개되고 있는지 시간 순으로 정리하세요.
- 모든 날짜는 사건이 일어난 현지시간 기준으로만 표기하세요. 한국 시간(KST)이나 UTC로 환산·계산하지 말고, 소스 기사에 나온 날짜를 하루도 앞뒤로 옮기지 말고 그대로 "N일(현지시간)" 형식으로 쓰세요. "2026년 7월 15일", "오늘", "현재" 같은 절대 날짜나 오늘 날짜는 쓰지 말고, 날짜를 알 수 없으면 쓰지 마세요.
- 수치, 인명, 날짜, 기관명 등 구체적 팩트를 최대한 살리세요.
- 한국 투자자/독자 관점에서 왜 중요한지 한 문단으로 마무리하되, 사실 서술형으로만 쓰세요.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- 기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다", "~라는 평가다", "~지켜볼 필요가 있습니다" 같은 논평/칼럼 문체는 금지입니다.
- 한국어로만 작성하세요.

아래 형식으로 출력:
제목: (현재 상황을 담은 추적 기사 제목)
국가: (주요 대상 국가 1개, 없으면 "없음")
관련국가: (관련국 최대 4개, 없으면 "없음")
분야: ({category})
본문: (추적 기사 본문)"""

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
                    country = c
            elif line.startswith("관련국가:"):
                raw = line.replace("관련국가:", "").strip()
                if raw not in ("없음", "-", ""):
                    countries = [x.strip() for x in raw.split(",") if x.strip()]
            elif line.startswith("분야:"):
                gen_category = line.replace("분야:", "").strip() or category
            elif line.startswith("본문:"):
                idx = content.find("본문:")
                body = content[idx + 3:].strip()
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

        # 동일 사건 루트 있으면 신규 생성 대신 append 병합(리빙 아티클)
        root = find_similar_trend(title, country=country, days=14)
        if root:
            if merge_trend_article(root, title, body, f"트렌드 추적 업데이트 ({group_name})"):
                print(f"  [{group_name}] ✅ 기존 루트에 병합 (id={root['id']}): {title}")
                time.sleep(CALL_INTERVAL)
                continue

        article_id = save_trend_article(
            group_name=group_name, title=title, body=body,
            category=gen_category, country=country, region=region,
            countries=countries
        )

        if article_id > 0:
            print(f"  [{group_name}] ✅ 추적 기사 생성 (id={article_id}): {title}")
            # 텔레그램 발송
            if TELEGRAM_TOKEN:
                try:
                    preview = body[:300]
                    url = f"https://newsfinal.co.kr/article.html?id={article_id}"
                    msg = f"📡 트렌드 추적\n\n*{title}*\n\n{preview}{'…' if len(body) > 300 else ''}\n\n[전체 기사 보기]({url})"
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": NEWSFINAL_CHANNEL, "text": msg,
                              "parse_mode": "Markdown", "disable_web_page_preview": False},
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


def realtime_trend_article_exists(keyword: str) -> bool:
    """최근 N시간 내 같은 키워드로 생성된 실시간 트렌드 기사가 있는지 확인"""
    since = (now_kst() - timedelta(hours=RT_CHECK_HOURS)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id",
                "source": "eq.NewsFinal",
                "subcategory": "like.realtrend_%",
                "title_ko": f"ilike.*{keyword[:10]}*",
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
    "category": "경제/금융/자원·에너지/산업·기업/정치·외교/사회/IT·과학 중 하나",
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
        category  = topic_info.get("category", "사회")
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
        article_list = ""
        for i, a in enumerate(related, 1):
            t = a.get("title_ko") or a.get("title_en") or ""
            body = a.get("summary_ko") or a.get("summary_en") or ""
            article_list += f"{i}. [{a.get('source','')}] {t}\n"
            if body:
                article_list += f"   {body[:300]}\n\n"

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
- 수치, 인명, 날짜, 기관명 등 구체적 팩트를 최대한 살리세요.
- 왜 지금 이 이슈가 중요한지 맥락을 담되, 사실 서술형으로만 쓰세요.
- 모든 날짜는 사건이 일어난 현지시간 기준으로만 표기하세요. 한국 시간(KST)이나 UTC로 환산·계산하지 말고, 소스 기사에 나온 날짜를 하루도 앞뒤로 옮기지 말고 그대로 "N일(현지시간)" 형식으로 쓰세요. "2026년 7월 15일", "오늘", "현재" 같은 절대 날짜나 오늘 날짜는 쓰지 말고, 날짜를 알 수 없으면 쓰지 마세요.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- 기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다", "~라는 평가다" 같은 논평/칼럼 문체는 금지입니다.

아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (주요 대상 국가 1개, 없으면 "없음")
관련국가: (관련국 최대 4개, 없으면 "없음")
분야: ({category})
본문: (기사 본문)"""

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
                    art_country = c
            elif line.startswith("관련국가:"):
                raw2 = line.replace("관련국가:", "").strip()
                if raw2 not in ("없음", "-", ""):
                    art_countries = [x.strip() for x in raw2.split(",") if x.strip()]
            elif line.startswith("본문:"):
                idx = content_text.find("본문:")
                body = content_text[idx + 3:].strip()
                break

        if not title:
            title = f"{issue_ko} — {today_str}"

        # 생성된 실제 제목+국가로 동일 사건 루트 재확인 (우선)
        similar = find_similar_trend(title, country=art_country, days=14)

        # 유사 기존 트렌드 기사 있으면 병합
        if similar:
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
            "update_log": [{"timestamp": now_str, "note": f"실시간 트렌드 감지 ({topic}, {urgency})"}],
            "sent_telegram": 0,
            "is_published": True,
            "posted_blog": 0,
        }

        try:
            res = requests.post(_sb_url(), headers=_sb_headers(), json=payload, timeout=15)
            if res.status_code in (200, 201):
                data = res.json()
                art_id = data[0].get("id", -1) if data else -1
                print(f"  [{topic}] ✅ 실시간 트렌드 기사 생성 (id={art_id}): {title}")
                generated += 1

                # 텔레그램 발송
                if TELEGRAM_TOKEN and art_id > 0:
                    try:
                        preview = body[:300]
                        url = f"https://newsfinal.co.kr/article.html?id={art_id}"
                        msg = f"📈 실시간 트렌드\n\n*{title}*\n\n{preview}{'…' if len(body) > 300 else ''}\n\n[전체 기사 보기]({url})"
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                            data={"chat_id": NEWSFINAL_CHANNEL, "text": msg,
                                  "parse_mode": "Markdown", "disable_web_page_preview": False},
                            timeout=15
                        )
                    except Exception:
                        pass
            else:
                print(f"  [{topic}] ❌ 저장 실패: {res.status_code}")
        except Exception as e:
            print(f"  [{topic}] ❌ 저장 예외: {e}")

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
        topic_list += f"{i}. [{s['score']}pt/{src_str}] {s['topic']} ({countries})\n   예시: {titles_str}\n"

    screen_prompt = f"""당신은 프론티어 마켓 전문 에디터입니다. ({today_str})
아래는 Google Trends, Reddit, GDELT에서 수집한 프론티어 마켓 트렌드 신호입니다.

{topic_list}

위 신호들 중 NewsFinal 독자(한국인 프론티어 마켓 투자자)에게 실제로 의미 있는 이슈 최대 {EXT_MAX_ARTICLES}개를 선별하세요.
- 단순 스포츠/연예/날씨/로또는 제외
- 경제·금융·정치·사회 분야 실질적 사건이나 정책 변화 우선
- 여러 소스에서 동시에 잡힌 토픽 우선

JSON 배열로만 응답 (마크다운 없이):
[
  {{
    "topic": "토픽 키워드",
    "issue_ko": "한 줄 설명",
    "category": "경제/금융/자원·에너지/산업·기업/정치·외교/사회/IT·과학 중 하나",
    "countries": ["국가1", "국가2"],
    "region": "africa/southeast_asia/central_asia/middle_east/south_asia/caribbean/global 중 하나"
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
        category = item.get("category", "경제")
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

        # 기사 생성
        write_prompt = f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
Google Trends, Reddit, GDELT에서 [{issue_ko}] 이슈가 급부상하고 있습니다.

관련 신호:
{chr(10).join(f"- {t}" for t in ref_titles)}

이 이슈에 대해 한국 투자자/독자를 위한 완성도 높은 기사를 작성하세요.
- 이슈의 배경, 현재 상황, 의미를 사실 서술형으로 담으세요.
- 확인된 팩트 중심으로, 추측은 최소화하세요.
- 모든 날짜는 사건이 일어난 현지시간 기준으로만 표기하세요. 한국 시간(KST)이나 UTC로 환산·계산하지 말고, 소스 기사에 나온 날짜를 하루도 앞뒤로 옮기지 말고 그대로 "N일(현지시간)" 형식으로 쓰세요. "2026년 7월 15일", "오늘", "현재" 같은 절대 날짜나 오늘 날짜는 쓰지 말고, 날짜를 알 수 없으면 쓰지 마세요.
- 반드시 하나의 토픽만 다루세요.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- 기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다", "~라는 평가다" 같은 논평/칼럼 문체는 금지입니다.
- 한국어로만 작성하세요.

아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (주요 국가 1개, 없으면 "없음")
관련국가: (관련국 최대 4개, 없으면 "없음")
분야: ({category})
본문: (기사 본문)"""

        content_text = call_gemini_article(write_prompt, max_tokens=1500)
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
                    art_country = c
            elif line.startswith("관련국가:"):
                raw2 = line.replace("관련국가:", "").strip()
                if raw2 not in ("없음", "-", ""):
                    art_countries = [x.strip() for x in raw2.split(",") if x.strip()]
            elif line.startswith("본문:"):
                idx = content_text.find("본문:")
                body = content_text[idx + 3:].strip()
                break

        if not title:
            title = f"{issue_ko} — {today_str}"

        # 생성된 실제 제목+국가로 동일 사건 루트 재확인 (우선)
        ext_similar = find_similar_trend(title, country=art_country, days=14)

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
                            "note": f"외부 트렌드 감지 (Google Trends+Reddit+GDELT)"}],
            "sent_telegram": 0,
            "is_published": True,
            "posted_blog": 0,
        }
        # 유사 기존 트렌드 기사 있으면 병합
        if ext_similar:
            note = f"외부 트렌드 추가 정보 ({topic})"
            ok = merge_trend_article(ext_similar, title, body, note)
            if ok:
                print(f"  [{topic}] ✅ 기존 트렌드 기사에 병합 (id={ext_similar['id']}): {title}")
                generated += 1
            time.sleep(CALL_INTERVAL)
            continue

        try:
            res = requests.post(_sb_url(), headers=_sb_headers(), json=payload, timeout=15)
            if res.status_code in (200, 201):
                data = res.json()
                art_id = data[0].get("id", -1) if data else -1
                print(f"  [{topic}] ✅ 외부 트렌드 기사 생성 (id={art_id}): {title}")
                generated += 1
                if TELEGRAM_TOKEN and art_id > 0:
                    try:
                        preview = body[:300]
                        url = f"https://newsfinal.co.kr/article.html?id={art_id}"
                        src_str = "+".join(matched_signal["sources"]) if matched_signal else "외부"
                        msg = f"🌐 외부 트렌드 [{src_str}]\n\n*{title}*\n\n{preview}{'…' if len(body)>300 else ''}\n\n[전체 기사 보기]({url})"
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                            data={"chat_id": NEWSFINAL_CHANNEL, "text": msg,
                                  "parse_mode": "Markdown"},
                            timeout=15
                        )
                    except Exception:
                        pass
            else:
                print(f"  [{topic}] ❌ 저장 실패: {res.status_code}")
        except Exception as e:
            print(f"  [{topic}] ❌ 예외: {e}")

        time.sleep(CALL_INTERVAL)

    print(f"[외부 트렌드 기사화] 완료 — {generated}건 생성")

def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음 — gemini_summarizer 건너뜀")
        return

    # API 연결 테스트
    global _current_key_idx
    print(f"[체크] Gemini API 연결 테스트... (키 {len(GEMINI_API_KEYS)}개)")
    test = call_gemini("ping", retry=1)
    if test is None:
        print("[SKIP] Gemini API 응답 없음 — 건너뜀")
        return
    _current_key_idx = 0  # ping으로 밀린 로테이션 인덱스 리셋 — 실제 작업은 항상 1번 키부터 순환
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
