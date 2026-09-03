"""
daily_digest.py
----------------
하루치 NewsFinal 자체 기사를 종합해 "오늘의 프론티어 마켓 다이제스트" 생성.
StockHub의 "오늘의 핵심 테마" 패턴을 프론티어 마켓에 맞게 적용.

실행: python scripts/daily_digest.py
하루 1회 실행 권장 (예: KST 22:00 / UTC 13:00)
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from rapidfuzz import fuzz

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

# articles 테이블 삽입 공용 로직(2026-09-02, article_store.py로 공용화).
try:
    from article_store import insert_final_article
except Exception:
    def insert_final_article(payload: dict) -> int:
        res = requests.post(_sb_url(), headers=_sb_headers(), json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

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
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

# Pixabay largeImageURL은 약 24시간짜리 임시 URL이라 그대로 저장하면 이미지가 사라진다.
# 받아서 R2에 영구 저장한 뒤 그 URL을 쓴다. (scripts/image_store.py)
try:
    from image_store import store_image
except Exception:  # 모듈 없거나 import 실패해도 다이제스트 발행 자체는 계속돼야 한다
    def store_image(src_url, key_hint="", timeout=30):
        return src_url

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


def get_yesterday_own_articles(limit=200):
    """직전 24시간 동안 발행된 NewsFinal 자체 기사 (다이제스트 제외) — 발행 시점 기준"""
    since = (now_kst() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_ko,summary_ko,category,country,countries,region,created_at,subcategory",
            "source": "eq.NewsFinal",
            "is_published": "eq.true",
            "created_at": f"gte.{since}",
            "subcategory": "not.like.digest_%",
            "order": "created_at.asc",
            "limit": str(limit),
        },
        timeout=30
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


def digest_exists_for_today() -> bool:
    """오늘(KST) 이미 다이제스트를 발행했는지 확인 — subcategory 키는 발행일 기준"""
    today_key = f"digest_{now_kst().strftime('%Y%m%d')}"
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={"select": "id", "subcategory": f"eq.{today_key}", "limit": "1"},
        timeout=15
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def call_gemini(prompt, max_tokens=3000, start_tier=0):
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.5, timeout=(10, 45))


# ── 논평/칼럼체 검출 및 재생성 (다이제스트는 "통찰/분석"을 요구하는 특성상
# 논평체가 특히 섞이기 쉬워 재생성 안전장치를 둔다) ──────────────
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


def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
    """이름과 충분히 비슷한 위키 문서 제목이 하나라도 있으면 True (결정론적 조회)."""
    name = (name or "").strip()
    if not name:
        return False
    titles = []
    for lang in ("ko", "en"):
        try:
            res = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": name, "limit": 3, "namespace": 0, "format": "json"},
                headers={"User-Agent": "NewsFinal-EntityCheck/1.0 (+https://newsfinal.co.kr)"},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if len(data) >= 2 and isinstance(data[1], list):
                    titles.extend(data[1])
        except Exception:
            continue
    return any(fuzz.token_sort_ratio(name, t) >= threshold for t in titles)


def extract_candidate_names(body: str) -> list:
    """판단이 아니라 단순 추출 — LLM 위험도가 낮은 작업이라 위키 조회 대상을 뽑는 데만 쓴다."""
    if not body:
        return []
    prompt = f"""아래 기사 본문에서 실제 존재 여부를 확인해볼 만한 구체적 고유명사를 추출하세요.
영화·도서·게임 등 작품명, 특정 인물 실명, 특정 기관·단체·기업명만 대상으로 합니다.
국가명·일반 지명(도시·나라)이나 흔한 일반명사·직함은 제외하세요.
쉼표로 구분해 나열만 하세요(설명 금지). 대상이 없으면 "없음"이라고만 답하세요.

본문:
{body[:2000]}

답변:"""
    result = call_gemini(prompt, max_tokens=150, start_tier=3)
    if not result:
        return []
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return []
    return [n.strip() for n in result.split(",") if n.strip() and len(n.strip()) >= 2][:15]


def verify_no_fabricated_names(source_prompt: str, body: str) -> str:
    """생성된 본문에 원문 자료에 없는 고유명사(작품명·인명·지명·기관명)가 새로 등장했는지 확인.
    두 신호를 같이 쓴다: ① 원본 자료 대조(Gemini 판단) ② 위키피디아 독립 조회(단순 추출 +
    결정론적 HTTP 조회 — Gemini가 오판해도 이 신호는 별개로 남는다). gemini_writer.py의
    동명 함수와 동일 로직(실사고 2026-08-16, id=79327)."""
    if not body:
        return ""
    check_prompt = f"""아래는 기사 작성에 쓰인 원본 자료와, 그걸 바탕으로 생성된 한국어 기사 본문입니다.
기사 본문에 나오는 고유명사(영화·도서·게임 등 작품명, 인명, 지명, 기관명)가 원본 자료에
실제로 근거하는지 확인하세요. 정상적인 한글 음차나 공식 번역명은 문제가 아닙니다 —
원본 자료에 등장하는 대상을 다른 이름으로 완전히 잘못 지어낸 경우만 찾으세요.
그런 이름이 있으면 "지어낸이름 → 원본표기" 형식으로 쉼표 구분해 나열하세요.
없으면 "없음"이라고만 답하세요.

[원본 자료]
{source_prompt[:3000]}

[생성된 기사 본문]
{body[:2000]}

답변:"""
    result = call_gemini(check_prompt, max_tokens=150, start_tier=3)
    suspect = ""
    if result:
        result = result.strip()
        if result and not ("없음" in result and len(result) <= 12):
            suspect = result

    unconfirmed = [n for n in extract_candidate_names(body) if not wikipedia_confirms(n)]
    if unconfirmed:
        note = "[위키 미확인] " + ", ".join(unconfirmed)
        suspect = (suspect + "\n" + note) if suspect else note

    return suspect


def call_gemini_article(prompt, max_tokens=3000, style_retries=1):
    content = call_gemini(prompt, max_tokens=max_tokens)
    attempt = 0
    fabricated = verify_no_fabricated_names(prompt, content) if content else ""
    while content and (has_column_style(content) or fabricated) and attempt < style_retries:
        attempt += 1
        reasons = []
        if has_column_style(content):
            reasons.append("논평/칼럼체")
        if fabricated:
            reasons.append(f"원문에 없는 고유명사({fabricated})")
        print(f"  ⚠️ {', '.join(reasons)} 감지 → 재생성 시도 ({attempt}/{style_retries})")
        retry_prompt = (
            prompt
            + "\n\n[재작성 지시] 방금 작성한 결과에 논평/칼럼 문체(예: '~를 보여줍니다', "
              "'~을 도모하고 있습니다', '~라는 평가다', '~지켜볼 필요가 있습니다' 등)가 섞여 있었습니다. "
              "감정·의견이 섞인 표현을 모두 배제하고, 사실 전달 중심의 스트레이트 뉴스 문체로만 다시 작성하세요."
            + (f"\n또한 다음 이름을 원문에 없는 표현으로 잘못 지어냈습니다: {fabricated}. "
               "영화·도서 등 작품 제목이나 고유명사는 원본 자료에 나온 표기를 그대로 옮기고, "
               "정확한 한국어 정식 명칭을 확신할 수 없으면 지어내지 말고 원문 표기를 그대로 쓰세요."
               if fabricated else "")
        )
        retried = call_gemini(retry_prompt, max_tokens=max_tokens)
        if retried:
            content = retried
            fabricated = verify_no_fabricated_names(prompt, content)
    if content and has_column_style(content):
        print("  ⚠️ 재생성 후에도 논평체 패턴이 남아있음 (그대로 진행)")
    if content and fabricated:
        print(f"  ⚠️ 재생성 후에도 원문에 없는 고유명사 남아있음: {fabricated} (그대로 발행 — 수동 확인 필요)")
    return content


def build_digest_prompt(articles):
    today_str = now_kst().strftime("%Y년 %m월 %d일")  # 발행일(오늘) 기준 — 신문 날짜와 동일

    # 국가별로 그룹화 — 기사 ID 기준 중복 제거 (관련국가 많은 기사가 반복되지 않도록)
    by_country = {}
    seen_ids = set()
    for a in articles:
        main_country = a.get("country") or "글로벌"
        by_country.setdefault(main_country, [])
        if a["id"] not in seen_ids:
            by_country[main_country].append(a)
            seen_ids.add(a["id"])

    article_list = ""
    for country, items in by_country.items():
        if not items:
            continue
        article_list += f"\n[{country}]\n"
        for a in items:
            title = a.get("title_ko") or ""
            summary = (a.get("summary_ko") or "")[:200]
            # 관련국가가 여러 개면 참고용으로 표시
            related = a.get("countries") or []
            related = [c for c in related if c and c != country]
            related_str = f" (관련: {', '.join(related[:3])})" if related else ""
            article_list += f"- {title}{related_str}\n  {summary}\n"

    rules = load_prompt("digest_rules", fallback="""[작성 규칙]
- 지난 하루 동안 NewsFinal이 다룬 프론티어 마켓 기사들을 종합해 오늘의 핵심 테마를 정리하는 일일 다이제스트를 작성하세요.
- 개별 기사를 단순 나열하지 말고, 여러 국가/기사에 걸쳐 공통적으로 나타나는 패턴, 테마, 트렌드로 묶어서 정리하세요.
- 본문은 반드시 테마별 섹션으로 구성하세요. 각 섹션은 대괄호 소제목으로 시작하세요(예: "[글로벌 무역 및 이주 정책의 변화]", "[아프리카 및 중동의 안보·보건 위기]"). 소제목은 그 섹션 내용을 정확히 요약하는 구체적인 문구로 쓰고, "기타"나 "기타 동향" 같은 모호한 제목은 쓰지 마세요.
- 각 대괄호 소제목 바로 아래에 관련 내용을 "- "로 시작하는 불릿 2~4개로 정리하세요. 각 불릿은 하나의 완결된 문장으로, 구체적 수치·국가명·기관명을 포함해 충분히 서술하세요(단답형 나열 금지).
- 섹션과 섹션 사이는 반드시 빈 줄로 구분하세요(대괄호 소제목 앞에 빈 줄 하나씩).
- 마크다운 문법(**굵게**, ##제목)은 쓰지 말고, 위에서 지시한 대괄호 소제목 + 불릿 형식만 사용하세요.
- 예: "이번 주 여러 아프리카 국가에서 통화 평가절하 압력이 동시에 나타남", "동남아 국가들의 외국인직접투자 유치 경쟁 심화" 같은 교차 비교형 분석을 우선하세요.
- 다룬 기사가 적으면 무리하게 늘리지 말고 섹션 수를 줄여서 간결하게 작성하세요.
- 기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다", "~라는 평가다" 같은 논평/칼럼 문체는 금지입니다. 패턴이나 트렌드를 설명할 때도 사실 서술형("~로 나타났다", "~가 확인됐다", "~로 집계됐다")으로 쓰세요.
- 날짜 표기는 반드시 사건 발생지의 현지시간 기준으로 "N일(현지시간)" 형식으로 쓰세요. "오늘", "어제", "2026년 7월 17일" 같은 절대날짜나 KST 기준 표기는 금지입니다.
- 한국어로만 작성하세요.""")

    return f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 지난 24시간 동안 NewsFinal이 다룬 프론티어 마켓 기사 목록입니다. 국가별로 정리되어 있습니다.

{article_list}

{rules}

아래 형식으로 출력:
제목: [데일리 다이제스트] 뒤에 오늘 핵심 테마를 담은 부제목을 붙이세요. 예: "[데일리 다이제스트] 글로벌 규제 재편과 지정학적 리스크 확산"
본문: (다이제스트 본문)"""


def parse_title_and_body(text):
    title = ""
    body = text
    lines = text.strip().split("\n")
    for i, line in enumerate(lines):
        if line.startswith("제목:"):
            title = line.replace("제목:", "").strip()
            body = "\n".join(lines[i+1:]).strip()
            if body.startswith("본문:"):
                body = body[3:].strip()
            break
    return title, body


def fetch_article_image(title: str, body: str) -> str:
    if not PIXABAY_API_KEY:
        return ""
    prompt = f"""아래는 여러 뉴스를 종합한 다이제스트 기사입니다. 이미지 검색용 영문 키워드를 2~3개 추출하세요.
일반적인 시각 소재 위주로 (예: stock market, world map, container port, business meeting).
인명·기업명·구체적 지명은 제외. 쉼표 구분, 키워드만 출력.

제목: {title}
본문 앞부분: {body[:300]}"""
    kw = call_gemini(prompt, max_tokens=30, start_tier=3)
    if not kw:
        return ""
    query = kw.strip().replace(",", " ").split("\n")[0][:100]
    if not query:
        return ""
    try:
        res = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "safesearch": "true",
                "per_page": 3,
            },
            timeout=15
        )
        if res.status_code == 200:
            hits = res.json().get("hits", [])
            if hits:
                raw_url = hits[0].get("webformatURL", "") or hits[0].get("largeImageURL", "")
                if raw_url:
                    # 임시 URL → R2 영구 URL. 실패 시 원본을 그대로 돌려받는다.
                    return store_image(
                        raw_url,
                        key_hint=f"digest_{now_kst().strftime('%Y%m%d')}",
                    )
                return ""
        else:
            print(f"  ⚠️ Pixabay {res.status_code}: {res.text[:100]}")
    except Exception as e:
        print(f"  ⚠️ Pixabay 실패: {e}")
    return ""


def _digest_sources(articles: list) -> list:
    """다이제스트의 '원문'은 어제자 자체 발행 기사다.

    date_guard는 소스 dict의 full_text/title_en/summary_en에서 날짜 근거를 찾으므로,
    자체 기사의 summary_ko/title_ko를 그 자리에 매핑해 넘긴다. 다이제스트 본문의
    "N일(현지시간)"이 원기사 어디에도 없으면 요약 과정에서 지어낸 것이다.
    """
    out = []
    for a in (articles or []):
        out.append({
            "id": a.get("id"),
            "full_text": a.get("summary_ko") or "",
            "title_en": a.get("title_ko") or "",
            "summary_en": "",
            "source_published_at": a.get("created_at") or "",
        })
    return out


def save_digest(title, body, article_count, image_url="", published=True, guard_note=""):
    if detect_script_leak(title, body):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title[:60]}")
        return -1
    today_key = f"digest_{now_kst().strftime('%Y%m%d')}"
    payload = {
        "title_en": title,
        "title_ko": title,
        "summary_en": "",
        "summary_ko": body,
        "url": f"internal://{today_key}",
        "source": "NewsFinal",
        "category": "다이제스트",
        "subcategory": today_key,  # 발행일(오늘) 기준 키 — 홈 노출 판단 기준
        "region": "global",
        "country": "",
        "country_flag": "",
        "image_url": image_url,
        "score": article_count,
        "created_at": now_kst().strftime("%Y-%m-%d %H:%M"),  # 실제 발행 시각(오늘 새벽) — 홈 노출 판단 기준
        "sent_telegram": 0,
        "is_published": published,
        "posted_blog": 0,
    }
    if guard_note:
        payload["update_log"] = [{"timestamp": now_kst().strftime("%Y-%m-%d %H:%M"),
                                  "note": guard_note}]
    return insert_final_article(payload)


def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    if digest_exists_for_today():
        print("[SKIP] 오늘 다이제스트 이미 생성됨")
        return

    articles = get_yesterday_own_articles()
    print(f"[다이제스트] 최근 24시간 자체 기사 {len(articles)}건 발견")

    if len(articles) < 3:
        print("[SKIP] 다이제스트 작성에 충분한 기사가 없습니다 (최소 3건 필요)")
        return

    prompt = build_digest_prompt(articles)
    content = call_gemini_article(prompt, max_tokens=6000)

    if not content:
        print("[ERROR] Gemini 응답 없음")
        return

    title, body = parse_title_and_body(content)
    if not title:
        title = "[데일리 다이제스트] 주요 동향"

    image_url = fetch_article_image(title, body or content)

    # 날짜 환각 판정 — 원기사에 근거 없는 "N일(현지시간)"이면 미발행
    _dg_bad, _dg_reason = check_date_hallucination(
        body or content, _digest_sources(articles), base_date=now_kst().date()
    )
    if _dg_bad:
        print(f"⛔ 날짜 환각 의심 → 미발행: {_dg_reason}")

    article_id = save_digest(
        title, body or content, len(articles), image_url=image_url,
        published=not _dg_bad,
        guard_note=(f"날짜 환각 의심 미발행 — {_dg_reason}" if _dg_bad else ""),
    )
    if article_id > 0:
        print(f"✅ 다이제스트 저장 완료 (id={article_id}): {title}")
    else:
        print("❌ 다이제스트 저장 실패")


if __name__ == "__main__":
    run()
