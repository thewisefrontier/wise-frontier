"""
gemini_writer.py
----------------
1. 오늘 기사를 키워드+유사도 기반으로 클러스터링
2. 클러스터별 이슈 기사 생성 (신규) 또는 업데이트 (추가 기사 있을 때)
3. 브리핑은 제거 — 클러스터 이슈 기사에 집중

실행: python scripts/gemini_writer.py
"""

import os
import re
import math
import time
import json
import hashlib
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from rapidfuzz import fuzz

try:
    from date_guard import check_date_hallucination
except Exception:
    def check_date_hallucination(body, sources, base_date=None):
        return False, ""   # 폴백 — 판정 없이 통과

# 카테고리 정규화 공통 모듈. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from category_guard import normalize_category
except Exception:
    def normalize_category(raw, default="글로벌"):
        return "" if raw is None else str(raw).strip()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

load_dotenv()

GEMINI_MODEL_PRIMARY  = "gemini-3.5-flash-lite"
GEMINI_MODEL_FALLBACK = "gemini-3.1-flash-lite"
CALL_INTERVAL      = 10
MAX_CLUSTERS_PER_RUN = 7  # 한 번 실행당 최대 처리 클러스터 수

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
NEWSFINAL_CHANNEL = "@newsfinal"  # NewsFinal 자체기사 전용 채널

def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _sb_url():
    return f"{SUPABASE_URL}/rest/v1/articles"


# ── 키릴 문자 감지 ────────────────────────────────────────
def has_cyrillic(text: str) -> bool:
    """제목/본문에 키릴 문자가 포함되어 있으면 True"""
    if not text:
        return False
    return bool(re.search(r'[\u0400-\u04FF]', text))


def send_to_newsfinal_channel(article_id, title, body, is_update=False):
    """NewsFinal 자체기사를 텔레그램 @newsfinal 채널에 발송"""
    if not TELEGRAM_TOKEN:
        return False
    try:
        preview = (body or "").strip().replace("\n\n", "\n")[:300]
        url = f"https://newsfinal.co.kr/article.html?id={article_id}"
        label = "🔄 업데이트" if is_update else "📋 NewsFinal"
        msg = f"{label}\n\n*{title}*\n\n{preview}{'…' if len(body or '') > 300 else ''}\n\n[전체 기사 보기]({url})"
        res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": NEWSFINAL_CHANNEL,
                "text": msg,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=15
        )
        data = res.json()
        if not data.get("ok"):
            print(f"  ⚠️ 텔레그램 발송 실패: {data}")
        return data.get("ok", False)
    except Exception as e:
        print(f"  ⚠️ 텔레그램 발송 예외: {e}")
        return False

# API 키 폴백 체인
GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

_current_key_idx = 0
_exhausted_keys_primary  = set()  # RPD 소진 키 (3.5)
_exhausted_keys_fallback = set()  # RPD 소진 키 (3.1)

# 프롬프트 캐시
_prompt_cache = {}

def load_prompt(name: str, fallback: str = "") -> str:
    """Supabase에서 활성 프롬프트 로드 (캐시 사용)"""
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

# 클러스터링 설정
SIMILARITY_HIGH         = 65
SIMILARITY_SAME_COUNTRY = 40
CLUSTER_MIN_SIZE        = 2

STOPWORDS = {
    "the","a","an","in","on","at","to","of","for","and","or","is","are",
    "was","were","has","have","been","will","with","by","from","this","that",
    "as","its","it","be","not","but","also","over","after","amid","says",
    "say","said","new","amid","following","기자","특파원","뉴스","오늘","이번",
}


# ── DB 헬퍼 (Supabase REST API) ───────────────────────────

def get_today_articles(limit=300):
    since = (now_kst() - timedelta(hours=96)).strftime("%Y-%m-%d %H:%M")
    articles = []
    offset = 0
    batch = 500
    while len(articles) < limit:
        res = requests.get(
            _sb_url(),
            headers={**_sb_headers(), "Range": f"{offset}-{offset+batch-1}"},
            params={
                "select": "id,title_ko,title_en,summary_ko,summary_en,source,category,subcategory,country,region,url,created_at,score,full_text,source_published_at",
                "sent_telegram": "eq.1",
                "source": "neq.NewsFinal",
                "created_at": f"gte.{since}",
                "order": "score.desc,created_at.desc",
            },
            timeout=30
        )
        if res.status_code not in (200, 206):
            break
        data = res.json()
        if not data:
            break
        articles.extend(data)
        if len(data) < batch:
            break
        offset += batch

    # 파킹된 토픽 기사도 클러스터링 소스로 포함
    try:
        parked_res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,title_en,summary_ko,summary_en,source,category,subcategory,country,region,url,created_at,score,full_text,source_published_at",
                "subcategory": "eq.parked_topic",
                "created_at": f"gte.{since}",
                "order": "created_at.desc",
                "limit": "200",
            },
            timeout=15
        )
        if parked_res.status_code in (200, 206):
            parked = parked_res.json()
            existing_ids = {a["id"] for a in articles}
            articles.extend(a for a in parked if a["id"] not in existing_ids)
    except Exception:
        pass

    return articles[:limit]


def get_existing_cluster(cluster_key):
    today = now_kst().strftime("%Y-%m-%d")
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_ko,summary_ko,subcategory,score",
            "source": "eq.NewsFinal",
            "subcategory": f"eq.{cluster_key}",
            "created_at": f"like.{today}%",
            "order": "created_at.desc",
            "limit": "1",
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        data = res.json()
        return data[0] if data else None
    return None


def get_article_by_id(article_id):
    """id로 기사 전체 정보(본문 포함) 조회 — 유사 기사 병합 시 기존 본문을 가져오기 위함"""
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,subcategory,score,country,category",
                "id": f"eq.{article_id}",
                "limit": "1",
            },
            timeout=15
        )
        if res.status_code in (200, 206):
            data = res.json()
            return data[0] if data else None
    except Exception as e:
        print(f"  ⚠️ get_article_by_id 실패: {e}")
    return None


def get_cluster_article_count(cluster_key):
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "score",
            "source": "eq.NewsFinal",
            "subcategory": f"eq.{cluster_key}",
            "order": "created_at.desc",
            "limit": "1",
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        data = res.json()
        return data[0].get("score", 0) if data else 0
    return 0


def get_today_own_articles():
    """하위 호환용 — 이제 find_similar_article이 RPC를 직접 호출하므로 빈 리스트 반환."""
    return []


def _strip_numbers(text: str) -> str:
    """제목에서 숫자(한글 수사 포함)를 제거 — 사망자 수 등 수치 변화로 인한 미탐지 방지"""
    text = re.sub(r'\d+', '', text)
    # 한글 수사 제거
    text = re.sub(r'[일이삼사오육칠팔구십백천만억]+\s*명', '명', text)
    return re.sub(r'\s+', ' ', text).strip()


def find_similar_article(title: str, own_articles: list, threshold: int = 70):
    """
    중복 기사 탐색 — 2단계:
    1차: DB RPC(find_duplicate_title) — pg_trgm 유사도 기반
    2차: 숫자 제거 후 같은 국가·날짜 기사와 키워드 재비교
         (사망자 수 등 수치가 바뀐 후속 보도 감지용)
    """
    if not title:
        return None, 0

    # ── 1차: RPC ──
    try:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/find_duplicate_title",
            headers=_sb_headers(),
            json={"p_title": title, "p_hours": 72, "p_threshold": 0.4},
            timeout=10,
        )
        if res.status_code in (200, 201) and res.json():
            top = res.json()[0]
            score = int(top["score"] * 100)
            if score >= threshold:
                return top, score
    except Exception as e:
        print(f"  ⚠️ [중복체크 경고] RPC 호출 실패: {e}")

    # ── 2차: 숫자 제거 + 국가+날짜+키워드 재비교 ──
    try:
        title_stripped = _strip_numbers(title)
        today = now_kst().strftime("%Y-%m-%d")
        since_48h = (now_kst() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")

        # 오늘 발행된 자체 기사 조회 (country 필터 없이 — 제목에서 국가명 추출 후 비교)
        res2 = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,country",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "created_at": f"gte.{since_48h}",
                "order": "created_at.desc",
                "limit": "100",
            },
            timeout=10,
        )
        if res2.status_code not in (200, 206):
            return None, 0

        candidates = res2.json()
        title_kws = set(w for w in re.sub(r'[^\w가-힣]', ' ', title_stripped).split() if len(w) >= 2)

        for cand in candidates:
            cand_title = cand.get("title_ko") or ""
            cand_stripped = _strip_numbers(cand_title)
            cand_kws = set(w for w in re.sub(r'[^\w가-힣]', ' ', cand_stripped).split() if len(w) >= 2)

            common = title_kws & cand_kws
            # 공통 키워드 4개 이상 + rapidfuzz 유사도 50 이상이면 중복
            sim = fuzz.token_sort_ratio(title_stripped, cand_stripped)
            if len(common) >= 4 and sim >= 50:
                print(f"  [2차 중복감지] 숫자제거 유사도 {sim}%, 공통키워드 {len(common)}개 → {cand_title[:50]}")
                return {"id": cand["id"], "title_ko": cand_title, "score": sim / 100}, sim

    except Exception as e:
        print(f"  ⚠️ [2차 중복체크 경고] {e}")

    return None, 0


# ── raw JSON 본문 차단 ──────────────────────────────────────────────────
# ⚠️ 2026-08-04 사고: Gemini가 JSON 응답의 body 필드 "안에" JSON 전문을 다시
#   써넣는 중첩 출력을 하는 경우가 있다(12건, 발행 8건). 바깥 JSON은 문법이
#   정상이라 parse_json_response()를 그대로 통과해 본문이 JSON 덩어리가 됐다.
#   ① 파싱 단계에서 언랩 시도 ② 저장/갱신 관문에서 최종 차단(키릴 차단과 동일 계열).
_JSON_BODY_KEY_RE = re.compile(r'"(?:body|\ubcf8\ubb38)"\s*:\s*"')


def _unwrap_json_body(text):
    """\ubcf8\ubb38\uc774 raw JSON\uc774\uba74 \ub0b4\ubd80 body\ub97c \uaebc\ub0b8\ub2e4.
    \ubc18\ud658: None=\uc815\uc0c1 \ubcf8\ubb38(\ubcc0\uacbd \ubd88\ud544\uc694) / str=\ubcf5\uad6c\ub41c \ubcf8\ubb38 / ""=JSON\uc774\uc9c0\ub9cc \ubcf5\uad6c \uc2e4\ud328"""
    if not text:
        return None
    s = str(text).strip()
    if not s.startswith("{"):
        return None
    head = s[:800]
    if not (_JSON_BODY_KEY_RE.search(head) or '"title"' in head or '"\uc81c\ubaa9"' in head):
        return None
    j = s.rfind("}")
    for cand in (s, s[:j + 1] if j > 0 else ""):
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except Exception:
            continue
        if isinstance(data, dict):
            inner = str(data.get("body") or data.get("\ubcf8\ubb38") or "").strip()
            if inner and not inner.startswith("{"):
                return inner
    return ""


def save_article(title_ko, summary_ko, cluster_key, category, region, country="", article_count=0, published=True, countries=None, image_url="", is_travel=False, summary_3lines="", investment_idea="", unpub_reason=""):
    # 키릴 문자 감지 — 저장 차단
    if has_cyrillic(title_ko) or has_cyrillic(summary_ko):
        print(f"  ⚠️ [키릴 감지] 저장 차단: {title_ko[:60]}")
        return -1

    # raw JSON 본문 차단 (2026-08-04 중첩 JSON 사고)
    _unwrapped = _unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            summary_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 저장 차단: {str(title_ko)[:60]}")
            return -1

    url = f"internal://{cluster_key}"
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": url,
        "source": "NewsFinal",
        "category": category,
        "subcategory": cluster_key,
        "region": region,
        "country": country,
        "country_flag": "",
        "countries": ([country] + [c for c in (countries or []) if c and c != country]) if country else (countries or []),
        "image_url": image_url,
        "score": 1,  # 최초 게시는 항상 1, 업데이트마다 +1
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": unpub_reason or "최초 게시"}],
        "sent_telegram": 0,
        "is_published": published,
        "posted_blog": 0,
        "is_travel": bool(is_travel),
        "summary_3lines": summary_3lines,
        "investment_idea": investment_idea,
    }
    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0].get("id", -1) if data else -1
    return -1


def update_article(article_id, title_ko, summary_ko, note: str = "업데이트", countries=None, country="", summary_3lines=None, investment_idea=None):
    """기사 갱신(병합 업데이트) — update_log에 업데이트 기록 추가"""
    # 키릴 문자 감지 — 업데이트 차단
    if has_cyrillic(title_ko) or has_cyrillic(summary_ko):
        print(f"  ⚠️ [키릴 감지] 업데이트 차단: {title_ko[:60]}")
        return False

    # raw JSON 본문 차단 (2026-08-04 중첩 JSON 사고)
    _unwrapped = _unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            summary_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 업데이트 차단: {str(title_ko)[:60]}")
            return False

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")

    # 기존 update_log 가져오기
    try:
        res = requests.get(
            f"{_sb_url()}?id=eq.{article_id}&select=update_log",
            headers=_sb_headers(), timeout=10
        )
        existing_log = []
        if res.status_code in (200, 206):
            data = res.json()
            if data and data[0].get("update_log"):
                existing_log = data[0]["update_log"]
    except Exception:
        existing_log = []

    new_log = existing_log + [{"timestamp": now_str, "note": note}]

    # 주체국(country)을 관련국(countries)에 항상 병합 — 결함 A 재발 방지
    merged_countries = ([country] + [c for c in (countries or []) if c and c != country]) if country else (countries or [])

    res = requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={
            "title_ko": title_ko,
            "title_en": title_ko,
            "summary_ko": summary_ko,
            "created_at": now_str,
            "update_log": new_log,
            **( {"countries": merged_countries} if merged_countries else {} ),
            **( {"summary_3lines": summary_3lines} if summary_3lines is not None else {} ),
            **( {"investment_idea": investment_idea} if investment_idea is not None else {} ),
        },
        timeout=15
    )
    return res.status_code in (200, 204)


def update_article_count(article_id, new_count):
    res = requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={"score": new_count},
        timeout=15
    )
    return res.status_code in (200, 204)


# ── 클러스터링 ────────────────────────────────────────────

def split_multi_topic_title(title: str) -> list:
    """
    복수 주제 제목을 개별 토픽으로 분리.
    예: "우간다 군 수뇌부 갈등 및 나이지리아 채용 사기 주의보"
    → ["우간다 군 수뇌부 갈등", "나이지리아 채용 사기 주의보"]
    단일 주제면 빈 리스트 반환.
    """
    if not title:
        return []
    separators = [' 및 ', ' and ', ' & ', ' et ', '…및', ', and ', '; ']
    for sep in separators:
        if sep.lower() in title.lower():
            parts = [p.strip() for p in title.split(sep) if p.strip() and len(p.strip()) > 5]
            if len(parts) >= 2:
                return parts
    return []


def is_multi_topic_title(title: str) -> bool:
    """복수 주제 제목 여부 — 분리 가능한 패턴 + 글로벌 종합 제목"""
    import re
    if not title:
        return False
    if len(split_multi_topic_title(title)) >= 2:
        return True
    if re.match(r'^글로벌\s+\S+.+(?:변화|동향|행보|흐름|속에서|격화|가속화)', title):
        return True
    if re.search(r'각국의?\s+(경제|사회|정치|행보|대응|현안)', title):
        return True
    if re.match(r'^전\s+세계\s+주요국', title):
        return True
    if '등 글로벌' in title or '등 주요 단신' in title or '등 주요 현안' in title:
        return True
    country_names = ['나이지리아','케냐','가나','에티오피아','필리핀','베트남',
                     '인도네시아','태국','이집트','우간다','탄자니아','수단',
                     '키르기스스탄','미얀마','캄보디아','인도','중국','미국',
                     '방글라데시','파키스탄','카자흐스탄','라오스','캄보디아']
    hits = [c for c in country_names if c in title]
    if len(hits) >= 3:
        return True
    return False


def is_multi_topic_body(text: str) -> bool:
    """
    본문 앞 3문단이 서로 다른 국가/주제를 다루는지 감지.
    각 문단에서 국가명을 추출해서 3개 이상 다른 국가가 나오면 복수 주제로 판단.
    """
    if not text:
        return False
    import re
    # 앞 600자만 분석
    lead = text[:600]
    paragraphs = [p.strip() for p in re.split(r'[.!?。]\s+', lead) if len(p.strip()) > 20][:6]

    country_names = ['나이지리아','케냐','가나','에티오피아','필리핀','베트남',
                     '인도네시아','태국','이집트','우간다','탄자니아','수단',
                     '키르기스스탄','미얀마','캄보디아','방글라데시','파키스탄',
                     '카자흐스탄','라오스','카메룬','코트디부아르','세네갈',
                     '잠비아','짐바브웨','앙골라','모잠비크','르완다']

    found_countries = set()
    for para in paragraphs:
        for c in country_names:
            if c in para:
                found_countries.add(c)

    # 3개 이상 다른 국가가 앞부분에 나오면 복수 주제
    return len(found_countries) >= 3


def extract_keywords(text):
    """텍스트에서 의미 있는 키워드 추출"""
    if not text:
        return set()
    text = text.lower()
    text = re.sub(r'[^\w\s가-힣]', ' ', text)
    words = text.split()
    return {w for w in words if w not in STOPWORDS and len(w) >= 3}


def title_keywords(text):
    """제목에서만 키워드 추출 — 고유명사(기관명·인명·지명) 중심"""
    if not text:
        return set()
    # 한글 2자 이상 단어 (고유명사 위주)
    text_clean = text.lower()
    text_clean = re.sub(r'[^\w\s가-힣]', ' ', text_clean)
    words = text_clean.split()
    # 불용어 제거, 한글은 2자 이상, 영문은 4자 이상
    result = set()
    for w in words:
        if w in STOPWORDS:
            continue
        # 한글 포함 단어
        if re.search(r'[가-힣]', w) and len(w) >= 2:
            result.add(w)
        # 영문 단어
        elif not re.search(r'[가-힣]', w) and len(w) >= 4:
            result.add(w)
    return result


def get_lead(text, chars=300):
    """본문 앞 2문단 추출 (약 300자)"""
    if not text:
        return ""
    # 문단 구분: 줄바꿈 또는 마침표+공백
    paragraphs = [p.strip() for p in re.split(r'\n+', text) if p.strip()]
    lead = " ".join(paragraphs[:2])
    return lead[:chars]


def articles_are_related(a, b):
    """
    두 기사가 같은 이슈인지 판단.
    제목 유사도 + 본문 앞 2문단 키워드 겹침 기반.
    국가가 다르면 즉시 제외.
    """
    title_a = (a.get("title_ko") or a.get("title_en") or "").lower()
    title_b = (b.get("title_ko") or b.get("title_en") or "").lower()

    if len(title_a) < 6 or len(title_b) < 6:
        return False

    country_a = a.get("country") or ""
    country_b = b.get("country") or ""
    diff_country = bool(country_a and country_b and country_a != country_b)

    # 국가가 명시적으로 다르면 즉시 제외 — 절대 조건
    if diff_country:
        return False

    same_category = a.get("category") == b.get("category")

    # 제목 유사도
    title_sim = fuzz.token_sort_ratio(title_a, title_b)

    # 본문 앞 2문단 키워드
    lead_a = get_lead(a.get("summary_ko") or a.get("summary_en") or "")
    lead_b = get_lead(b.get("summary_ko") or b.get("summary_en") or "")
    lead_kw_a = title_keywords(lead_a)
    lead_kw_b = title_keywords(lead_b)
    lead_common = lead_kw_a & lead_kw_b

    # 제목 핵심 키워드
    title_kw_a = title_keywords(title_a)
    title_kw_b = title_keywords(title_b)
    title_common = title_kw_a & title_kw_b

    # 국가 정보 없는 글로벌 기사는 더 엄격하게 판단
    both_global = not country_a and not country_b
    high_threshold = SIMILARITY_HIGH + 10 if both_global else SIMILARITY_HIGH

    # 조건 1: 제목이 매우 유사 + 본문 앞부분도 키워드 2개 이상 공유
    if title_sim >= high_threshold and len(lead_common) >= 2:
        return True

    # 조건 2: 제목 키워드 2개 이상 공유 + 본문 앞부분 키워드 3개 이상 공유 + 같은 카테고리
    # 글로벌 기사는 키워드 요건 강화
    req_title = 3 if both_global else 2
    req_lead  = 4 if both_global else 3
    if len(title_common) >= req_title and len(lead_common) >= req_lead and same_category:
        return True

    # 조건 3: 제목+본문 키워드 합산 — 글로벌은 더 많이 요구
    all_common = (title_kw_a | lead_kw_a) & (title_kw_b | lead_kw_b)
    req_all = 6 if both_global else 4
    if len(all_common) >= req_all and same_category:
        return True

    return False


def is_coherent_cluster(cluster: list) -> bool:
    """
    클러스터가 실제로 같은 이슈인지 키워드 기반으로 판단.
    기사들이 공통 핵심 키워드(주체/사건/기관명)를 공유하면 단일 이슈.
    공통 키워드 없이 각자 다른 주제면 엉터리.
    Gemini 호출 없이 규칙 기반으로 처리.
    """
    if len(cluster) < 4:
        return True  # 소규모는 통과

    import re

    def extract_core_kw(text):
        """제목에서 핵심 키워드 추출 (고유명사 위주)"""
        if not text:
            return set()
        text = text.lower()
        text = re.sub(r'[^\w\s가-힣]', ' ', text)
        words = text.split()
        stopwords = {
            'the','a','an','in','on','at','to','of','for','and','or','is',
            'are','was','with','by','from','as','its','new','says','said',
            '및','에서','으로','이후','위해','통해','대한','관련','주요',
            '발표','강화','확대','추진','계획','정부','시장','경제','기업',
        }
        result = set()
        for w in words:
            if w in stopwords:
                continue
            if re.search(r'[가-힣]', w) and len(w) >= 2:
                result.add(w)
            elif not re.search(r'[가-힣]', w) and len(w) >= 4:
                result.add(w)
        return result

    # 각 기사의 핵심 키워드 추출
    article_kws = []
    for a in cluster:
        if a.get("__needs_review__"):
            continue
        title = a.get("title_ko") or a.get("title_en") or ""
        kw = extract_core_kw(title)
        if kw:
            article_kws.append(kw)

    if len(article_kws) < 2:
        return True

    # 공통 키워드가 있는 기사 쌍 비율 계산
    # 절반 이상의 기사가 적어도 하나의 공통 키워드를 공유하면 단일 이슈
    n = len(article_kws)
    connected = 0
    for i in range(n):
        for j in range(i+1, n):
            if article_kws[i] & article_kws[j]:  # 공통 키워드 있으면
                connected += 1
                break  # 이 기사(i)는 연결됨
        else:
            # i번 기사가 어느 기사와도 키워드 안 겹치면 고립
            pass

    # 실제로 연결된 기사 수 계산
    has_connection = []
    for i in range(n):
        linked = any(article_kws[i] & article_kws[j] for j in range(n) if j != i)
        has_connection.append(linked)

    connected_ratio = sum(has_connection) / n

    # 절반 미만의 기사만 연결돼 있으면 엉터리 클러스터
    if connected_ratio < 0.5:
        return False
    return True


def cluster_articles(articles):
    """기사를 이슈별로 클러스터링"""
    clusters = []
    used = set()

    for i, a in enumerate(articles):
        if i in used:
            continue
        cluster = [a]
        used.add(i)

        for j, b in enumerate(articles):
            if j in used or j == i:
                continue
            if articles_are_related(a, b):
                cluster.append(b)
                used.add(j)

        if len(cluster) >= CLUSTER_MIN_SIZE:
            # 다국가 혼합 클러스터 → 검토 필요로 미발행 저장
            if not is_coherent_cluster(cluster):
                print(f"  [검토필요] 다국가 혼합 클러스터 ({len(cluster)}건) — 미발행 저장")
                cluster.append({"__needs_review__": True})
            clusters.append(cluster)

    # 큰 클러스터 우선
    clusters.sort(key=lambda c: len(c), reverse=True)
    return clusters


def make_cluster_key(cluster):
    """클러스터 고유 키 생성 — 날짜 + 대표 기사 제목 해시"""
    rep = (cluster[0].get("title_ko") or cluster[0].get("title_en") or "")[:50]
    today = now_kst().strftime("%Y%m%d")
    h = hashlib.md5(f"{today}:{rep}".encode()).hexdigest()[:8]
    return f"cluster_{today}_{h}"


# ── Gemini 호출 ───────────────────────────────────────────

# ── 프롬프트 빌더 ─────────────────────────────────────────

JSON_OUTPUT_SPEC = r"""[출력 형식]
아래 JSON 객체 하나만 출력하세요. JSON 앞뒤에 설명·인사말·마크다운 코드펜스(```)를 절대 붙이지 마세요.
{
  "title": "핵심을 담은 제목",
  "country": "기사의 핵심 주체가 되는 국가 1개. 어느 나라 기업/정부/기관이 주체인가 기준. 글로벌 기업·국제기구가 주체면 빈 문자열",
  "countries": ["기사에서 직접 당사국으로 등장하는 국가들만. 본문에 단순 언급·비교 대상으로만 나오는 나라는 제외. 최대 3개. 없으면 빈 배열"],
  "category": "경제 | 금융 | 자원·에너지 | 산업·기업 | 정치·외교 | 사회 | IT·과학 | 문화·예술 | 글로벌 중 하나",
  "is_travel": false,
  "body": "기사 본문"
}
[분야 선택 기준]
- 경제: 거시경제, 무역, GDP, 통화정책, 인플레이션 등 국가 경제 전반
- 금융: 은행, 증권, 투자, 환율, 핀테크
- 자원·에너지: 광업, 채굴, 석유, 가스, 전력, 원자재, 재생에너지
- 산업·기업: 제조업, 기업 실적, 산업 정책, 공급망
- 정치·외교: 정부, 선거, 국제관계, 정책, 외교
- 사회: 인프라, 교육, 보건, 노동, 인구
- IT·과학: 기술, 통신, 연구개발, 우주산업
- 문화·예술: 미술, 문학, 음악, 영화, 공연, 문화유산, 문화산업
- 글로벌: 특정 국가에 국한되지 않는 세계적 이슈 (단, 위 카테고리로 분류 가능하면 해당 카테고리 우선)

[is_travel 판단 기준]
이 기사가 해외여행자에게 실질적으로 유의미한 정보 — 여행경보·치안·시위·테러, 비자·입국규정, 항공노선·공항 운영, 관광지 개방/폐쇄, 감염병, 자연재해 등 — 를 담고 있으면 true, 아니면 false. 단순 경제·산업 뉴스는 false.

[JSON 작성 규칙]
- body 안의 줄바꿈은 \n, 큰따옴표는 \" 로 이스케이프하세요.
- 값이 없어도 키를 생략하지 말고 빈 문자열("") 또는 빈 배열([])을 쓰세요.
- is_travel은 문자열이 아니라 boolean(true/false)로 쓰세요.
- JSON 이외의 텍스트는 한 글자도 출력하지 마세요."""


def _pub_day_label(raw) -> str:
    """source_published_at(UTC ISO8601) → '8월 3일'. 실패 시 빈 문자열.

    ⚠️ 값은 UTC 기준이라 현지시간과 최대 하루 어긋날 수 있다. 그럼에도 주입하는 이유:
      - country 컬럼이 원본 기사의 55.9%에서 비어 있어 국가별 오프셋 보정이 불가능하다
        (2026-08-05 실측, 8/1 이후 5,692건 기준)
      - date_guard도 발행일 당일과 전날을 모두 근거로 인정한다(±1일 수용)
      - 아무 날짜도 주지 않으면 Gemini가 요일로 도피하거나 날짜를 지어낸다.
        하루 오차는 그보다 명백히 낫다.
    소스 원문 본문에 날짜가 명시돼 있으면 그쪽이 우선이라는 규칙은 writer_rules에 있다.
    """
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


def build_issue_prompt(cluster, existing_summary=None):
    sorted_cluster = sorted(cluster, key=lambda a: bool(a.get("full_text")), reverse=True)
    main_articles = sorted_cluster[:5]
    extra_titles = [a.get("title_ko") or a.get("title_en") or "" for a in sorted_cluster[5:]]

    article_list = ""
    for i, a in enumerate(main_articles, 1):
        t = a.get("title_ko") or a.get("title_en") or ""
        full_text = a.get("full_text") or ""
        s = full_text if full_text else (a.get("summary_ko") or a.get("summary_en") or "")
        pub = _pub_day_label(a.get("source_published_at"))
        pub_tag = f" (보도 {pub})" if pub else ""
        article_list += f"{i}. [{a.get('source','')}]{pub_tag} {t}\n"
        if s:
            article_list += f"   {s}\n\n"

    if extra_titles:
        article_list += "\n[추가 관련 기사 제목]\n"
        for t in extra_titles:
            article_list += f"- {t}\n"

    today_str = now_kst().strftime("%Y년 %m월 %d일")
    country = cluster[0].get("country") or ""
    category = cluster[0].get("category") or ""

    FALLBACK_RULES = """[주의사항]
- 반드시 하나의 토픽(사건/이슈)만 다루는 기사를 작성하세요. 관련 없는 두 개 이상의 사건을 한 기사에 묶지 마세요.
- 여러 기사가 입력되더라도 가장 중요한 하나의 이슈에 집중하고, 나머지는 참고만 하세요.
- 본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
- 마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
- 매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 포함하지 마세요.
- 날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- "2026년 6월 24일 현재", "오늘", "현재" 등 절대 날짜를 본문에 쓰지 마세요. 소스 기사의 날짜 기준으로 "N일(현지시간)"으로만 표기하세요.
- 요일(월요일~일요일)을 단독으로 쓰지 마세요. 원문에 "on Wednesday"처럼 요일만 있으면 그 요일을 한국어로 옮기지 말고, 아래 보도일 규칙에 따라 날짜로 바꾸거나 시점 표현을 생략하세요. 요일은 "8일 토요일"처럼 날짜와 병기할 때만 허용됩니다.
- 각 원문 제목 옆에 "(보도 M월 D일)"이 붙어 있을 수 있습니다. 원문 본문에 사건 날짜가 명시돼 있으면 그 날짜가 우선이고, 명시된 날짜가 없을 때만 보도일을 사건 시점으로 보고 "D일(현지시간)" 형식으로 쓰세요. 보도일 표기가 없고 원문에도 날짜가 없으면 날짜를 쓰지 마세요.
- 기사 문체로 작성하세요. 논평/칼럼 문체는 금지입니다.
- 모든 인명·지명은 반드시 한글로 음차하세요. 키릴 문자, 아랍 문자, 데바나가리 등 비라틴 문자를 그대로 쓰지 마세요. 한자도 마찬가지입니다. 다만 "고(故)", "대(對)중국"처럼 괄호 안에 넣는 병기는 허용됩니다.
""" + JSON_OUTPUT_SPEC

    rules = load_prompt("writer_rules", fallback=FALLBACK_RULES)

    if existing_summary:
        template = load_prompt("writer_update", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
기존 기사에 새로 들어온 관련 기사들을 반영해 업데이트하세요. ({today_str})

[기존 기사]
{existing_summary}

[추가된 관련 기사]
{article_list}

새로 들어온 기사의 팩트를 기존 기사에 자연스럽게 통합해 완성도 높은 기사로 다시 써주세요.
팩트(수치, 인명, 날짜, 기관명)를 최대한 살리고, 한국어로 작성하세요.
{rules}""")
        return template.format(today_str=today_str, existing_summary=existing_summary,
                               article_list=article_list, rules=rules,
                               country=country, category=category)

    elif len(main_articles) == 1 or (len(main_articles) <= 4 and len({a.get("source","") for a in main_articles}) >= 2):
        template = load_prompt("writer_single", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래 기사를 바탕으로 완성도 높은 한국어 기사를 작성하세요. ({today_str})
국가: {country} | 분야: {category}

[기사 원문]
{article_list}

원문이 여러 주제나 사건을 다루더라도 반드시 하나의 핵심 주제만 골라 작성하세요.
팩트(수치, 인명, 날짜, 기관명, 구체적 내용)를 빠짐없이 살려서 작성하세요.
원문이 프랑스어·아랍어·포르투갈어·인도네시아어 등 비영어인 경우 그대로 이해하고 한국어로 작성하세요.
원문이 길수록 기사도 충분히 길게 쓰세요. 억지로 줄이지 마세요.
한국어로만 작성하세요.
{rules}""")
        return template.format(today_str=today_str, article_list=article_list,
                               rules=rules, country=country, category=category,
                               count=len(main_articles))
    else:
        template = load_prompt("writer_cluster", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래 {count}개 기사는 같은 이슈를 다루고 있습니다. ({today_str})
국가: {country} | 분야: {category}

[관련 기사]
{article_list}

여러 기사의 팩트를 종합해 하나의 완성된 기사로 작성하세요.
각 기사의 구체적인 수치, 인명, 날짜, 기관명을 최대한 살려주세요.
원문이 풍부할수록 기사도 충분히 길게 쓰세요. 억지로 줄이지 마세요.
한국어로만 작성하세요.
{rules}""")
        return template.format(today_str=today_str, article_list=article_list,
                               rules=rules, country=country, category=category,
                               count=len(main_articles))


def call_gemini(prompt, max_tokens=1000, retry=2):
    global _current_key_idx, _exhausted_keys_primary, _exhausted_keys_fallback
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": max_tokens},
    }

    n = len(GEMINI_API_KEYS)
    model_stages = [
        (GEMINI_MODEL_PRIMARY,  _exhausted_keys_primary),
        (GEMINI_MODEL_FALLBACK, _exhausted_keys_fallback),
    ]

    for model, exhausted in model_stages:
        # 소진되지 않은 키만 후보로
        available = [i for i in range(n) if i not in exhausted]
        if not available:
            print(f"  [{model}] 모든 키 RPD 소진 → 다음 모델로")
            continue

        # 현재 인덱스부터 순환
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
                    _cand = res.json()["candidates"][0]
                    # maxOutputTokens 초과로 잘린 응답을 정상 취급하면 문장·JSON이
                    # 중간에서 끊긴 채 저장된다(실사고 id=47879: 본문이 절단되고
                    # 파싱 실패로 "제목:/내용:" 라벨이 그대로 기사에 노출).
                    _finish = _cand.get("finishReason", "")
                    if _finish and _finish != "STOP":
                        print(f"  [WARN] {model} 응답 비정상 종료(finishReason={_finish}) — 폐기")
                        return None
                    return _cand["content"]["parts"][0]["text"].strip()
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


# ── 논평/칼럼체 검출 및 재생성 ────────────────────────────
BANNED_STYLE_PATTERNS = [
    r"보여줍니다", r"보여주고 있습니다", r"보여준다",
    r"도모하고 있습니다", r"도모한다",
    r"강조하고 있습니다", r"강조한다",
    r"시사합니다", r"시사한다", r"시사하며",
    r"주목됩니다", r"주목된다", r"주목받고 있습니다",
    r"평가된다", r"평가받고 있습니다", r"라는 평가다", r"라는 분석이다",
    r"필요해 보입니다", r"필요할 것으로 보입니다",
    r"지켜볼 필요가 있습니다", r"지켜봐야 할 것입니다",
    r"기대됩니다", r"기대해 볼 만합니다",
    # 화자 없는 전망/분석형 마무리 문장 (트렌드 기사에서 재발 확인, 2026-07-28)
    r"분석이 나온다", r"분석이 나옵니다", r"분석도 나온다", r"분석도 나옵니다",
    r"관측이 나온다", r"관측이 나옵니다",
    r"우려가 나온다", r"우려가 나옵니다",
    r"지속될 전망이다", r"지속될 전망입니다",
    r"이어질 전망이다", r"이어질 전망입니다",
    r"귀추가 주목된다", r"귀추가 주목됩니다",
]

def has_column_style(text: str) -> bool:
    """생성된 기사 본문에 논평/칼럼체 어미가 섞여 있는지 검사"""
    if not text:
        return False
    return any(re.search(p, text) for p in BANNED_STYLE_PATTERNS)


# ── 합쇼체(-습니다/-입니다) 탐지·변환 ────────────────────────────────
# gemini_summarizer.py와 동일 로직. 문장 종결부만 대상으로 하므로
# 인용문 내부 발언("문제없습니다"라고 말했다)은 보존된다.
_SENT_END_LA = r'(?=[.!?\n]|$)'  # 문장 종결 위치 (인용문 내부 제외용)
_POLITE_ENDING_RE = re.compile(r'(?:습니다|입니다|됩니다)[")\u2018\u2019\u201c\u201d]*' + _SENT_END_LA)


def has_polite_ending(text: str) -> bool:
    """합쇼체 종결이 있는지 검사.
    변환기(to_plain_style)가 실제로 고칠 수 있는 패턴과 정확히 일치시킨다.
    (구 버전은 습니다/입니다/됩니다만 탐지해 '개최합니다.'·'아닙니다.'를 놓쳤음)"""
    if not text:
        return False
    return to_plain_style(text) != text


_JONG_B, _JONG_N = 17, 4  # 종성 ㅂ, ㄴ

_POLITE_CONV_RULES = [
    (re.compile(r'아닙니다' + _SENT_END_LA), '아니다'),
    (re.compile(r'입니다' + _SENT_END_LA), '이다'),
    (re.compile(r'습니다' + _SENT_END_LA), '다'),
]
_BNIDA_RE = re.compile(r'([가-힣])니다' + _SENT_END_LA)


def _bnida_to_nda(m) -> str:
    """'합니다'→'한다', '됩니다'→'된다' 등 종성 ㅂ + 니다 → 종성 ㄴ + 다."""
    ch = m.group(1)
    code = ord(ch) - 0xAC00
    if not (0 <= code < 11172):
        return m.group(0)
    cho, jung, jong = code // 588, (code % 588) // 28, code % 28
    if jong != _JONG_B:
        return m.group(0)
    return chr(0xAC00 + cho * 588 + jung * 28 + _JONG_N) + '다'


def to_plain_style(text: str) -> str:
    """문장 종결부의 합쇼체를 해라체(-다)로 변환."""
    if not text:
        return text
    for rx, rep in _POLITE_CONV_RULES:
        text = rx.sub(rep, text)
    return _BNIDA_RE.sub(_bnida_to_nda, text)


def call_gemini_article(prompt, max_tokens=1500, style_retries=1):
    """기사 본문 생성 전용 호출. 논평/칼럼체·합쇼체 감지 시 최대 style_retries회 재생성."""
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
    if content and (has_column_style(content) or has_polite_ending(content)):
        print("  ⚠️ 재생성 후에도 논평체·합쇼체 패턴이 남아있음 (파싱 단계에서 변환)")
    return content


# 국가 → 지역 매핑
COUNTRY_TO_REGION = {
    "나이지리아": "africa", "케냐": "africa", "가나": "africa", "남아공": "africa",
    "에티오피아": "africa", "르완다": "africa", "탄자니아": "africa", "우간다": "africa",
    "이집트": "africa", "모로코": "africa", "알제리": "africa", "튀니지": "africa",
    "세네갈": "africa", "코트디부아르": "africa", "잠비아": "africa", "짐바브웨": "africa",
    "보츠와나": "africa", "나미비아": "africa", "모잠비크": "africa", "앙골라": "africa",
    "베트남": "southeast_asia", "인도네시아": "southeast_asia", "태국": "southeast_asia",
    "필리핀": "southeast_asia", "말레이시아": "southeast_asia", "캄보디아": "southeast_asia",
    "미얀마": "southeast_asia", "라오스": "southeast_asia", "동티모르": "southeast_asia",
    "카자흐스탄": "central_asia", "우즈베키스탄": "central_asia", "키르기스스탄": "central_asia",
    "타지키스탄": "central_asia", "투르크메니스탄": "central_asia",
    "사우디아라비아": "middle_east", "아랍에미리트": "middle_east", "카타르": "middle_east",
    "쿠웨이트": "middle_east", "이라크": "middle_east", "이란": "middle_east",
    "이스라엘": "middle_east", "요르단": "middle_east", "오만": "middle_east", "튀르키예": "middle_east",
    "방글라데시": "south_asia", "파키스탄": "south_asia", "스리랑카": "south_asia", "네팔": "south_asia",
    "자메이카": "caribbean", "트리니다드": "caribbean", "바베이도스": "caribbean",
    "아이티": "caribbean", "쿠바": "caribbean", "도미니카공화국": "caribbean",
    "콜롬비아": "latin_america", "페루": "latin_america", "칠레": "latin_america",
    "아르헨티나": "latin_america", "브라질": "latin_america", "멕시코": "latin_america",
    "가이아나": "latin_america", "수리남": "latin_america",
}

def country_to_region(country: str) -> str:
    return COUNTRY_TO_REGION.get(country, "global")


COUNTRY_ALIASES = {
    "대한민국": "한국", "남한": "한국", "south korea": "한국", "korea": "한국",
    "미국": "미국", "usa": "미국", "united states": "미국",
    "중국": "중국", "china": "중국",
    "일본": "일본", "japan": "일본",
    "나이지리아": "나이지리아", "nigeria": "나이지리아",
    "케냐": "케냐", "kenya": "케냐",
    "남아프리카공화국": "남아공", "남아프리카": "남아공", "south africa": "남아공",
    "베트남": "베트남", "vietnam": "베트남",
    "인도네시아": "인도네시아", "indonesia": "인도네시아",
    "태국": "태국", "thailand": "태국",
    "필리핀": "필리핀", "philippines": "필리핀",
    "이집트": "이집트", "egypt": "이집트",
    "사우디": "사우디아라비아", "사우디아라비아": "사우디아라비아", "saudi arabia": "사우디아라비아",
    "uae": "아랍에미리트", "아랍에미리트": "아랍에미리트",
    "튀르키예": "튀르키예", "터키": "튀르키예", "turkey": "튀르키예",
    "인도": "인도", "india": "인도",
}

def normalize_country(country: str) -> str:
    """Gemini가 생성한 국가명을 표준 표기로 통일"""
    if not country:
        return ""
    key = country.strip().lower()
    for alias, standard in COUNTRY_ALIASES.items():
        if alias.lower() == key:
            return standard
    return country.strip()


def update_article_fields(article_id: int, fields: dict):
    requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json=fields,
        timeout=15
    )


_FENCE_RE = re.compile(r"```(?:json)?", re.I)
_LABEL_LINE_RE = re.compile(
    r"^[\s*#>\-]*\**\s*(제목|국가|관련\s*국가|분야|카테고리|여행|본문|내용|기사\s*본문)\s*\**\s*[:：]\s*(.*)$"
)
_NULLISH = ("없음", "-", "N/A", "n/a", "없다", "null", "None", "")


def _strip_leaked_labels(text: str) -> str:
    """파싱이 완전히 실패했을 때 응답에 남은 라벨 줄을 제거해 본문만 남긴다."""
    if not text:
        return ""
    raw = _FENCE_RE.sub("", text).strip()
    out = []
    for line in raw.split("\n"):
        m = _LABEL_LINE_RE.match(line)
        if m:
            key = m.group(1).replace(" ", "")
            rest = (m.group(2) or "").strip()
            if key in ("본문", "내용", "기사본문"):
                if rest:
                    out.append(rest)
            continue
        out.append(line)
    cleaned = "\n".join(out).strip()
    return cleaned or raw


def _coerce_countries(val, limit: int = 4) -> list:
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        parts = [str(p).strip() for p in val]
    else:
        parts = [p.strip() for p in re.split(r"[,;/·]", str(val))]
    return [p for p in parts if p and p not in _NULLISH][:limit]


def _coerce_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    s = str(val).strip().lower()
    return s.startswith("예") or s.startswith("y") or s in ("true", "1")


def _json_candidates(text: str):
    raw = _FENCE_RE.sub("", text).strip()
    yield raw
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i and (i, j + 1) != (0, len(raw)):
        yield raw[i:j + 1]


def parse_json_response(text: str):
    """Gemini JSON 응답 파싱. 성공 시 6-튜플, 실패 시 None."""
    if not text:
        return None
    for cand in _json_candidates(text):
        try:
            data = json.loads(cand)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        body = str(data.get("body") or data.get("본문") or "").strip()
        # body 필드 안에 JSON 전문이 중첩 출력되는 사고가 있어 먼저 언랩한다.
        _inner_body = _unwrap_json_body(body)
        if _inner_body:
            print("  🔧 body 중첩 JSON 감지 → 내부 본문 추출")
            body = _inner_body
        if not body:
            continue
        first = next((l for l in body.split("\n") if l.strip()), "")
        if _LABEL_LINE_RE.match(first):
            body = _strip_leaked_labels(body)
        title = str(data.get("title") or data.get("제목") or "").strip()
        country = str(data.get("country") or data.get("국가") or "").strip()
        if country in _NULLISH or country == "글로벌":
            country = ""
        category = str(data.get("category") or data.get("분야") or "").strip()
        if category in _NULLISH:
            category = ""
        raw_countries = data.get("countries")
        if raw_countries is None:
            raw_countries = data.get("관련국가")
        countries = _coerce_countries(raw_countries)
        raw_travel = data.get("is_travel")
        if raw_travel is None:
            raw_travel = data.get("여행")
        summary_3lines = str(data.get("summary_3lines") or data.get("3줄요약") or "").strip()
        investment_idea = str(data.get("investment_idea") or data.get("투자아이디어") or "").strip()
        return title, body, country, category, countries, _coerce_bool(raw_travel), summary_3lines, investment_idea
    return None


def _parse_labeled_response(text: str):
    """레거시 라벨 형식(제목:/본문: 등) 폴백 파서."""
    title = country = category = ""
    countries = []
    is_travel = False
    raw = _FENCE_RE.sub("", text).strip()
    lines = raw.split("\n")
    body_lines = None
    for i, line in enumerate(lines):
        m = _LABEL_LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1).replace(" ", "")
        val = (m.group(2) or "").strip()
        if key == "제목":
            title = val
        elif key == "국가":
            country = "" if (val in _NULLISH or val == "글로벌") else val
        elif key == "관련국가":
            countries = _coerce_countries(val)
        elif key in ("분야", "카테고리"):
            category = "" if val in _NULLISH else normalize_category(val)
        elif key == "여행":
            is_travel = _coerce_bool(val)
        elif key in ("본문", "내용", "기사본문"):
            body_lines = ([val] if val else []) + lines[i + 1:]
            break
    body = "\n".join(body_lines).strip() if body_lines is not None else _strip_leaked_labels(raw)
    return title, body, country, category, countries, is_travel, "", ""


def _ensure_paragraphs(text: str, target: int = 3) -> str:
    """Gemini가 문단 구분(\n\n) 지시를 어기고 한 덩어리로 응답하는 경우가 있어
    (강제성 없는 프롬프트 지시라 준수율이 들쭉날쭉함), 코드 단에서 문장(-다.)
    단위로 강제 분할하는 안전장치. 이미 \n\n이 있으면 그대로 반환.
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


# ── 절대날짜 후처리 ─────────────────────────────────────────────────────
# 원칙: 모든 날짜는 사건 발생지 현지시간 기준 "N일(현지시간)".
# 프롬프트 지시(writer_rules)만으론 준수율이 낮아 "2026년 7월 29일 보도했다" 형태가
# 계속 새어나오므로 코드 단에서 최종 차단한다.
#
# ⚠️ 무조건 변환은 금지. 실측(2026-07-30, 발행 자체기사 중 연월일 표기 830건 전수):
#     보도일 기준 0~3일 전 : 606건 → 전부 위반 (보도 시점을 절대날짜로 쓴 것)
#     미래(예정일)         :  54건 → 전부 정당 (배당 지급일·결선 투표일·행사 기간)
#     4일 이상 과거        : 170건 → 전부 정당 (과거 사건 시점 특정. "2015년 7월 18일 구속")
#   따라서 "보도 시점 근처"만 변환하고 과거·미래는 손대지 않는다.
#   창을 넓히면 과거 사건 시점이 소실돼 기사가 망가진다.
_ABS_DATE_RE = re.compile(r"(?<![0-9])(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# 연도 없는 "8월 3일" 형태. 연도는 기사 기준일의 연도로 가정한다.
# 연말·연초 경계에서는 gap이 음수(미래)로 나와 자동으로 축약 대상에서 빠진다.
_ABS_MD_RE = re.compile(r"(?<![0-9])(?<!년)(?<!년 )(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# "8월 4일 12:31" = 라이브 업데이트 스탬프(_update_stamp). 절대 축약하지 않는다.
_TIME_TAIL_RE = re.compile(r"^\s*\d{1,2}:\d{2}")
ABS_DATE_WINDOW_DAYS = int(os.getenv("ABS_DATE_WINDOW_DAYS", "3"))


def _normalize_recent_abs_dates(text: str, base=None, window_days: int = None,
                                add_local_time: bool = True) -> str:
    """보도 시점 근처(0~window_days일 전)의 절대날짜만 "N일(현지시간)"으로 축약."""
    if not text or ("년" not in text and "월" not in text):
        return text
    if window_days is None:
        window_days = ABS_DATE_WINDOW_DAYS
    if window_days < 0:
        return text
    base_date = (base or now_kst()).date()
    # "(현지시간)"은 관례상 한 필드 내 첫 언급에만 붙인다.
    # 이미 본문 어딘가에 표기가 있으면 새로 붙이지 않는다(검수 통과 조건도 충족).
    state = {"marked": "(현지시간)" in text}

    def _sub(m):
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return m.group(0)          # 2월 30일 등 비정상 날짜는 원문 유지
        delta = (base_date - d).days
        if delta < 0 or delta > window_days:
            return m.group(0)          # 미래 예정일·과거 사건은 그대로 둔다
        # 뒤에 이미 "(현지시간)"이 붙어 있으면 중복 삽입하지 않는다.
        tail = text[m.end():m.end() + 6]
        if tail.startswith("(현지시간)") or state["marked"] or not add_local_time:
            suffix = ""
        else:
            suffix = "(현지시간)"
            state["marked"] = True
        return f"{d.day}일{suffix}"

    out = _ABS_DATE_RE.sub(_sub, text)

    def _sub_md(m):
        """연도 없는 "N월 N일" → 보도 시점 근처면 "N일(현지시간)"."""
        try:
            d = datetime(base_date.year, int(m.group(1)), int(m.group(2))).date()
        except ValueError:
            return m.group(0)
        tail = out[m.end():m.end() + 8]
        if _TIME_TAIL_RE.match(tail):
            return m.group(0)      # 업데이트 스탬프는 손대지 않는다
        delta = (base_date - d).days
        if delta < 0 or delta > window_days:
            return m.group(0)      # 미래·과거 사건 시점은 월 정보가 필요하다
        if tail.startswith("(현지시간)") or state["marked"] or not add_local_time:
            suffix = ""
        else:
            suffix = "(현지시간)"
            state["marked"] = True
        return f"{d.day}일{suffix}"

    return _ABS_MD_RE.sub(_sub_md, out)


def _plainify_parsed(parsed):
    """파싱 결과의 텍스트 필드에 합쇼체 → 해라체 변환을 강제 적용.
    프롬프트 지시·재생성이 모두 실패해도 DB에는 '-다' 체만 저장되도록 하는 최종 안전장치."""
    title, body, country, category, countries, is_travel, summary3, investment = parsed

    # 보도 시점 절대날짜 → "N일(현지시간)" 축약 (과거·미래 날짜는 불변)
    _before = (title, body, summary3, investment)
    title, body, summary3, investment = (_normalize_recent_abs_dates(t) for t in _before)
    if (title, body, summary3, investment) != _before:
        print("  🔧 보도 시점 절대날짜 감지 → 'N일(현지시간)' 축약 적용")

    if any(has_polite_ending(t) for t in (title, body, summary3, investment)):
        print("  🔧 합쇼체 감지 → 자동 변환 적용(-습니다 → -다)")
        title = to_plain_style(title)
        body = to_plain_style(body)
        summary3 = to_plain_style(summary3)
        investment = to_plain_style(investment)
    return title, body, country, category, countries, is_travel, summary3, investment


def parse_title_and_body(text):
    """Gemini 응답 파싱. 1순위 JSON, 실패 시 레거시 라벨 파서로 폴백."""
    if not text:
        return "", "", "", "", [], False, "", ""
    parsed = parse_json_response(text)
    if parsed:
        return _plainify_parsed(parsed)
    print("  ⚠️ JSON 파싱 실패 → 레거시 라벨 파서로 폴백")
    return _plainify_parsed(_parse_labeled_response(text))


# ── 기업 자동 감지·등록 ────────────────────────────────────────────────

def get_company_by_id(company_id: str) -> dict | None:
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/companies",
            headers=_sb_headers(),
            params={"id": f"eq.{company_id}", "limit": "1"},
            timeout=10
        )
        if res.status_code in (200, 206):
            data = res.json()
            return data[0] if data else None
    except Exception as e:
        print(f"  ⚠️ 기업 조회 실패: {e}")
    return None


def save_company(company_id: str, name: str, name_ko: str, country: str,
                 country_flag: str, exchange: str, ticker: str, sector: str,
                 description: str, founded_year: int = None,
                 headquarters: str = None, website: str = None) -> bool:
    try:
        payload = {
            "id": company_id,
            "name": name,
            "name_ko": name_ko,
            "country": country,
            "country_flag": country_flag,
            "exchange": exchange,
            "ticker": ticker,
            "sector": sector,
            "description": description,
            "founded_year": founded_year,
            "headquarters": headquarters,
            "website": website,
            "is_published": True,
            "created_at": now_kst().strftime("%Y-%m-%d %H:%M"),
            "updated_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/companies",
            headers=_sb_headers(),
            json=payload,
            timeout=15
        )
        if res.status_code in (200, 201):
            return True
        print(f"  ⚠️ 기업 저장 실패: HTTP {res.status_code} - {res.text[:300]}")
        return False
    except Exception as e:
        print(f"  ⚠️ 기업 저장 실패(예외): {e}")
    return False


def generate_update_note(existing_summary: str, new_summary: str) -> str:
    prompt = f"""기존 기사와 업데이트된 기사를 비교해서, 이번 업데이트에서 새롭게 추가되거나 변경된 핵심 내용을 15자 이내 한 줄로 요약하세요.
예시: "현지 당국 공식 발표 추가", "사망자 수 214명으로 업데이트", "정부 대응 방안 발표"
마크다운 없이 텍스트만 출력하세요.

기존 내용: {(existing_summary or '')[:300]}
새 내용: {(new_summary or '')[:300]}"""
    result = call_gemini(prompt, max_tokens=50)
    return (result or "업데이트").strip().replace('\n', ' ')[:30]


def fetch_article_image(title: str, body: str) -> str:
    if not PIXABAY_API_KEY:
        return ""
    prompt = f"""아래 뉴스 기사의 이미지 검색용 영문 키워드를 2~3개 추출하세요.
일반적인 시각 소재 위주로 (예: oil refinery, stock market, container port, farmland).
인명·기업명·구체적 지명은 제외. 쉼표 구분, 키워드만 출력.

제목: {title}
본문 앞부분: {body[:300]}"""
    kw = call_gemini(prompt, max_tokens=30)
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
                return hits[0].get("largeImageURL", "")
        else:
            print(f"  ⚠️ Pixabay {res.status_code}: {res.text[:100]}")
    except Exception as e:
        print(f"  ⚠️ Pixabay 실패: {e}")
    return ""


def detect_and_register_companies(title: str, body: str, country: str):
    if not title and not body:
        return

    prompt = f"""아래 뉴스 기사에서 언급된 주요 기업(상장사 또는 대형 민간기업)을 최대 3개 추출하세요.
프론티어 마켓(아프리카, 동남아시아, 중동, 동유럽 등) 기업만 대상으로 합니다.
글로벌 대기업(애플, 구글, 삼성 등)은 제외합니다.

기사 제목: {title}
기사 본문: {body[:800]}
기사 국가: {country}

각 기업에 대해 아래 JSON 형식으로만 응답하세요 (마크다운, 추가 설명 없이):
[
  {{
    "id": "영문_소문자_언더스코어_ID (예: safaricom, dangote_cement)",
    "name": "공식 영문 기업명",
    "name_ko": "한국어 기업명",
    "exchange": "거래소 약칭 (예: NSE, NGX, IDX, SET, PSE, EGX, HOSE, JSE)",
    "ticker": "티커 심볼 (모르면 빈 문자열)",
    "sector": "업종 (예: 통신, 은행, 에너지, 부동산)",
    "description": "한국 투자자를 위한 3문장 이내 기업 소개. 설립연도, 핵심 사업, 시장 내 위상 포함. 투자 권유 없이 사실만.",
    "founded_year": 설립연도_숫자_또는_null,
    "headquarters": "본사 도시, 국가 (예: 나이로비, 케냐)"
  }}
]
추출할 기업이 없으면 빈 배열 []을 반환하세요."""

    raw = call_gemini(prompt, max_tokens=800)
    if not raw:
        return

    try:
        import json
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            return
        companies = json.loads(match.group())
        if not isinstance(companies, list):
            return

        for comp in companies[:3]:
            company_id = comp.get("id", "").strip().lower().replace(" ", "_")
            if not company_id or not comp.get("name"):
                continue

            existing = get_company_by_id(company_id)
            if existing:
                continue

            comp_country = country or ""
            flag_map = {
                "나이지리아": "🇳🇬", "케냐": "🇰🇪", "남아공": "🇿🇦", "남아프리카공화국": "🇿🇦",
                "베트남": "🇻🇳", "인도네시아": "🇮🇩", "태국": "🇹🇭", "필리핀": "🇵🇭",
                "이집트": "🇪🇬", "가나": "🇬🇭", "에티오피아": "🇪🇹", "탄자니아": "🇹🇿",
                "방글라데시": "🇧🇩", "파키스탄": "🇵🇰", "카자흐스탄": "🇰🇿",
                "몽골": "🇲🇳",
            }
            country_flag = flag_map.get(comp_country, "🌍")

            ok = save_company(
                company_id=company_id,
                name=comp.get("name", ""),
                name_ko=comp.get("name_ko", ""),
                country=comp_country,
                country_flag=country_flag,
                exchange=comp.get("exchange", ""),
                ticker=comp.get("ticker", ""),
                sector=comp.get("sector", ""),
                description=comp.get("description", ""),
                founded_year=comp.get("founded_year"),
                headquarters=comp.get("headquarters", ""),
            )
            if ok:
                print(f"  🏢 새 기업 등록: {comp.get('name')} ({company_id})")

    except Exception as e:
        print(f"  ⚠️ 기업 감지 파싱 오류: {e}")


# ── 메인 실행 ─────────────────────────────────────────────

def verify_single_topic(title: str, body: str) -> bool:
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

    result = call_gemini(prompt, max_tokens=5)
    if not result:
        return True
    return "YES" in result.upper()


def park_multi_topic_articles(articles: list) -> int:
    parked = 0
    for a in articles:
        title_en = a.get("title_en") or a.get("title_ko") or ""
        parts = split_multi_topic_title(title_en)
        if not parts:
            continue

        full_text = a.get("full_text") or a.get("summary_en") or a.get("summary_ko") or ""
        country   = a.get("country") or ""
        category  = a.get("category") or "글로벌"
        region    = a.get("region") or "global"

        print(f"  [파킹] 복수 주제 분리: {title_en[:60]}")
        for part in parts:
            try:
                safe_kw = part[:20].replace("'", "").replace('"', '').replace('(', '').replace(')', '')
                check = requests.get(
                    f"{SUPABASE_URL}/rest/v1/articles",
                    headers=_sb_headers(),
                    params={
                        "select": "id",
                        "subcategory": "eq.parked_topic",
                        "title_en": f"ilike.*{safe_kw}*",
                        "limit": "1",
                    },
                    timeout=10
                )
                if check.status_code in (200, 206) and check.json():
                    print(f"    → 이미 파킹됨: {part[:50]}")
                    continue
            except Exception:
                pass

            now_str = now_kst().strftime("%Y-%m-%d %H:%M")
            payload = {
                "title_en":    part,
                "title_ko":    part,
                "summary_en":  full_text[:500],
                "summary_ko":  "",
                "full_text":   full_text,
                "url":         a.get("url", f"parked://{part[:30]}"),
                "source":      a.get("source", ""),
                "category":    category,
                "subcategory": "parked_topic",
                "region":      region,
                "country":     country,
                "country_flag":"",
                "countries":   ([country] + [c for c in (a.get("countries") or []) if c and c != country]) if country else (a.get("countries") or []),
                "score":       0,
                "created_at":  a.get("created_at", now_str),
                "first_published_at": now_str,
                "update_log":  [{"timestamp": now_str, "note": f"복수주제 분리 파킹 (원제: {title_en[:60]})"}],
                "sent_telegram": 0,
                "is_published":  False,
                "posted_blog":   0,
            }
            try:
                res = requests.post(
                    f"{SUPABASE_URL}/rest/v1/articles",
                    headers=_sb_headers(),
                    json=payload,
                    timeout=15
                )
                if res.status_code in (200, 201):
                    data = res.json()
                    art_id = data[0].get("id", -1) if data else -1
                    print(f"    → 파킹 완료 (id={art_id}): {part[:60]}")
                    parked += 1
                else:
                    print(f"    → 파킹 실패: {res.status_code}")
            except Exception as e:
                print(f"    → 파킹 예외: {e}")

    return parked


# ── 라이브 기사 능동적 업데이트 ──────────────────────────────

# 라이브 업데이트 대상 subcategory 접두 (트렌드 기사 전용).
# 트렌드는 사건이 며칠에 걸쳐 전개되므로 후속 정보를 능동 검색해 반영하는 것이
# 기사의 핵심이다. 반대로 일반 기사(cluster_/solo_)는 후속 반영이 필요 없고,
# 정기 자동생성물(weather_/국제유가/digest_)은 외부 API 실측값이 본문이라
# Gemini가 덮어쓰면 실측이 창작으로 대체된다.
# (실사고 id=47879: 기상청 기반 한국 날씨 기사가 "역대 최고기온 42.5도 경신
#  반영"이라는 근거 없는 내용으로 전면 교체됨)
LIVE_UPDATE_PREFIXES = ("trend_", "realtrend_", "extrend_")


def get_stale_live_articles() -> list:
    since     = (now_kst() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
    not_after = (now_kst() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            # dict를 쓰면 같은 키("created_at")가 뒤엣것으로 덮어써져 48시간 하한이
            # 통째로 사라진다. 튜플 리스트로 보내야 PostgREST가 두 조건을 AND로 묶는다.
            # select에 subcategory를 포함해야 아래 접두 필터가 실제로 동작한다.
            #
            # ⚠️ 기존에는 score=eq.1 로 걸렀는데, 트렌드 기사는 score=2로 저장되어
            # 정작 대상이어야 할 트렌드가 통째로 빠지고 일반·자동생성 기사만
            # 돌고 있었다(2026-08-04 실측: trend_/realtrend_ 216건 전부 score=2,
            # weather_ 105건이 score=1이라 대상에 포함). score 조건을 버리고
            # subcategory 접두로 직접 지정한다.
            params=[
                ("select", "id,title_ko,summary_ko,country,category,score,subcategory,update_log"),
                ("source", "eq.NewsFinal"),
                ("is_published", "eq.true"),
                ("subcategory", "like.*trend_*"),
                ("created_at", f"gte.{since}"),
                ("created_at", f"lte.{not_after}"),
                ("order", "created_at.desc"),
                ("limit", "20"),
            ],
            timeout=15
        )
        if res.status_code in (200, 206):
            # PostgREST의 like.*trend_* 는 앞뒤 와일드카드라 오탐 여지가 있어
            # 접두를 코드에서 다시 정확히 검증한다.
            return [a for a in res.json()
                    if (a.get("subcategory") or "").startswith(LIVE_UPDATE_PREFIXES)]
    except Exception as e:
        print(f"  [라이브 업데이트] 조회 실패: {e}")
    return []


def search_followup(title: str, country: str) -> list:
    import urllib.parse
    since = (now_kst() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")

    # 제목은 "국가명, 본문" 형식이라 첫 단어가 거의 항상 국가명이다. 국가명만으로
    # 검색하면 완전히 무관한 기사까지 "후속 정보"로 오매칭돼 엉뚱한 업데이트가
    # 붙는 사고가 났다(실사고 2026-08-09, id=63237: 기니 광산 협약 기사에
    # 무관한 기니 기상특보가 "후속 정보 추가"로 붙음). 국가명 다음의 실제
    # 핵심어를 우선 쓰고, 본문에 남는 단어가 없을 때만 국가명으로 폴백한다.
    body_part = title.split(",", 1)[1] if "," in title else title
    kw_list = [w for w in body_part.replace(",", "").replace("\xb7", " ").split() if len(w) >= 2]
    kw = kw_list[0] if kw_list else country

    results = []

    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "title_en,title_ko,summary_en,summary_ko,full_text,source",
                "source": "neq.NewsFinal",
                "created_at": f"gte.{since}",
                "or": f"(title_ko.ilike.*{kw}*,title_en.ilike.*{kw}*)",
                "order": "created_at.desc",
                "limit": "8",
            },
            timeout=15
        )
        if res.status_code in (200, 206):
            results.extend(res.json())
    except Exception:
        pass

    try:
        # \uC2E4\uC0AC\uACE0(2026-08-09, id=61698): \uD55C\uAE00\uB85C\uB9CC \uB41C \uC81C\uBAA9(\uAC70\uC758 \uC804\uBD80)\uC740 eng_words\uAC00
        # \uD56D\uC0C1 \uBE44\uC5B4\uC11C country\uB85C \uD3F4\uBC31\uD588\uB294\uB370, \uADF8\uB7EC\uBA74 GDELT\uAC00 "\uB0A8\uC544\uD504\uB9AC\uCE74\uACF5\uD654\uAD6D" \uAD00\uB828
        # \uC544\uBB34 \uAE30\uC0AC\uB098 5\uAC74 \uBB3C\uC5B4\uC640 \uC9D0\uBC14\uBE0C\uC6E8 \uC774\uC8FC\uBBFC\u00B7\uCD95\uAD6C\u00B7\uB7ED\uBE44 \uAC19\uC740 \uC644\uC804 \uBB34\uAD00\uD55C \uB0B4\uC6A9\uC774
        # "\uD6C4\uC18D \uC815\uBCF4"\uB85C 5\uCC28\uB840 \uC5F0\uC18D \uBD99\uC5C8\uB2E4. country \uD3F4\uBC31\uC744 \uC5C6\uC560\uACE0, \uC601\uBB38 \uD0A4\uC6CC\uB4DC\uAC00 \uC5C6\uC73C\uBA74
        # (\uAC70\uC758 \uD56D\uC0C1 \uADF8\uB7F0 \uACBD\uC6B0) \uC774 \uAC80\uC0C9 \uC790\uCCB4\uB97C \uAC74\uB108\uB6F4\uB2E4 \u2014 \uB193\uCE58\uB294 \uAC8C \uC624\uB9E4\uCE6D\uBCF4\uB2E4 \uB0AB\uB2E4.
        eng_words = [w for w in title.split() if not any("\uAC00" <= c <= "\uD7A3" for c in w)]
        eng_kw = " ".join(eng_words[:3]) if eng_words else ""
        if eng_kw:
            gres = requests.get(
                f"https://api.gdeltproject.org/api/v2/doc/doc"
                f"?query={urllib.parse.quote(eng_kw)}&mode=artlist&maxrecords=5&timespan=2d&format=json",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=12
            )
            if gres.status_code == 200:
                for a in gres.json().get("articles", []):
                    results.append({
                        "title_en": a.get("title", ""),
                        "summary_en": a.get("title", ""),
                        "source": a.get("domain", "GDELT"),
                    })
    except Exception:
        pass

    return results


# ── 라이브 업데이트: 상단 블록 구조 ──────────────────────────────────
#
#   [업데이트]
#   ■ 8월 4일 12:31 — 최신
#   ■ 8월 3일 09:10 — 그 이전
#   ────────
#   (원 본문 — 최초 생성 이후 불변)
#
# BBC·CNN 라이브 기사와 같은 배치다. 최신이 맨 위에 오므로 본문의 낡은 수치가
# 아래 남아도 독자는 갱신된 값을 먼저 읽는다. 덕분에 **본문 전면 재작성이
# 아예 필요 없어진다** — Gemini가 이미 검수를 통과한 문장을 다시 쓸 일이
# 없으므로 누락·변형·창작 리스크가 구조적으로 제거된다.
#
# ⚠️ 원 본문은 어떤 경로로도 덮어쓰지 않는다. 이 불변식을 깨면 위 이점이 전부 사라진다.

UPDATE_HEAD = "[업데이트]"
UPDATE_SEP  = "────────"

# 구형(하단) 이력 구분자. gemini_summarizer가 붙여 온 기존 발행분과의 읽기 호환용.
# 신규 append는 전부 상단 블록으로 가지만, 이미 붙어 있는 하단 이력은 보존한다.
HISTORY_SEP = "────────\n[업데이트 이력]"

MIN_DELTA_LEN     = 60    # 이보다 짧으면 새 내용으로 치지 않는다
DELTA_DUP_OVERLAP = 0.65  # 문장 겹침률이 이 이상이면 재탕으로 보고 폐기
                          # (실측: 조사·어미만 바꾼 환언 0.69 / 무관한 문장 0.18 이하)

# 한 기사에 붙는 "후속 정보 추가" 최대 횟수. update_article()이 매번 created_at을
# 지금 시각으로 갱신해서 get_stale_live_articles()의 "3~48시간 전" 창에 계속 다시
# 걸리므로, 이 캡이 없으면 무한히 반복 적용될 수 있다(실사고 id=61698: 매 회차마다
# 걸려 24시간 동안 5차례 연속 붙음 — search_followup 매칭 버그와 별개로, 매칭이
# 완벽해도 한 기사가 끝없이 늘어나는 걸 막을 장치가 없었다).
MAX_LIVE_UPDATES_PER_ARTICLE = 3

# 업데이트 항목의 머리표·타임스탬프. 내용 비교 전에 떼어내지 않으면
# 같은 문장인데도 접두 때문에 겹침률이 떨어져 중복이 새어 나간다.
# (summarizer가 붙인 구형 항목은 타임스탬프가 없으므로 선택 그룹으로 둔다)
_HIST_PREFIX_RE = re.compile(
    r"^■\s*(?:\d{1,2}월\s*\d{1,2}일\s*\d{1,2}:\d{2}\s*(?:업데이트)?\s*[—\-–]\s*)?"
)


def _split_article(summary: str) -> tuple[str, str, str]:
    """(상단 업데이트 블록, 원 본문, 구형 하단 이력)으로 분리한다.
    없는 구간은 빈 문자열. 어느 것도 없으면 전부 본문으로 취급한다."""
    s = (summary or "").lstrip()
    updates = ""
    if s.startswith(UPDATE_HEAD):
        parts = s.split("\n" + UPDATE_SEP, 1)
        if len(parts) == 2:
            updates = parts[0].rstrip()
            # 구분선 줄의 나머지를 버리고 다음 줄부터 본문
            s = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
            s = s.lstrip("\n")

    parts = s.split(HISTORY_SEP, 1)
    body   = parts[0].rstrip()
    legacy = (HISTORY_SEP + parts[1]) if len(parts) > 1 else ""
    return updates, body, legacy


def _compose_article(updates: str, body: str, legacy: str) -> str:
    """_split_article()의 역연산."""
    out = ""
    if updates:
        out += updates.rstrip() + "\n" + UPDATE_SEP + "\n\n"
    out += (body or "").rstrip()
    if legacy:
        out += "\n\n" + legacy
    return out


def _update_stamp() -> str:
    d = now_kst()
    return f"{d.month}월 {d.day}일 {d.strftime('%H:%M')}"


def _prepend_update(updates: str, delta: str) -> str:
    """새 항목을 블록 맨 위(헤더 바로 아래)에 넣는다. 최신이 위."""
    entry = f"■ {_update_stamp()} — {delta}"
    if updates:
        lines = updates.split("\n")
        return "\n".join([lines[0], entry] + lines[1:])
    return UPDATE_HEAD + "\n" + entry


def _norm_sent(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s or "")


def _char_ngrams(s: str, n: int = 3) -> set:
    s = _norm_sent(s)
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def _overlap(a: set, b: set) -> float:
    """작은 쪽 기준 포함률. 길이가 다른 두 서술의 '같은 말인가'를 자카드보다 잘 잡는다."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _split_sents(text: str) -> list:
    return [x for x in re.split(r"(?<=다\.)\s*|\n+", text or "") if x.strip()]


def _dedupe_delta(delta: str, existing: str) -> str:
    """기존 본문·업데이트에 이미 있는 내용을 문장 단위로 delta에서 제거한다.
    완전일치뿐 아니라 조사·어미만 바꾼 재탕도 겹침률로 걸러낸다.
    delta 자체의 문장 간 중복도 같이 제거한다.

    ⚠️ 한계: 문자 3-gram 기반이라 어휘를 통째로 바꾼 환언은 못 잡는다
       (예: "사임 의사를 밝혔다" vs "사임하겠다는 뜻을 나타냈다" → 겹침률 0).
    """
    existing = "\n".join(_HIST_PREFIX_RE.sub("", ln.strip())
                         for ln in (existing or "").split("\n"))
    seen_norm, seen_ng = set(), []
    for x in _split_sents(existing):
        ns = _norm_sent(x)
        if not ns:
            continue
        seen_norm.add(ns)
        seen_ng.append(_char_ngrams(x))

    kept = []
    for x in _split_sents(delta):
        ns = _norm_sent(x)
        if not ns or ns in seen_norm:
            continue
        ng = _char_ngrams(x)
        if any(_overlap(ng, g) >= DELTA_DUP_OVERLAP for g in seen_ng):
            continue
        kept.append(x.strip())
        seen_norm.add(ns)
        seen_ng.append(ng)
    return " ".join(kept).strip()


def update_live_articles():
    if not GEMINI_API_KEYS:
        return

    stale = get_stale_live_articles()
    if not stale:
        print("[라이브 업데이트] 대상 없음")
        return

    print(f"\n[라이브 업데이트] 대상 {len(stale)}건 → 최대 5건 처리")
    updated = 0

    for a in stale[:5]:
        title   = a.get("title_ko") or ""
        summary = a.get("summary_ko") or ""
        country = a.get("country") or ""
        art_id  = a["id"]

        prior_live_updates = sum(
            1 for l in (a.get("update_log") or [])
            if (l or {}).get("note") == "후속 정보 추가"
        )
        if prior_live_updates >= MAX_LIVE_UPDATES_PER_ARTICLE:
            print(f"  → {title[:50]}")
            print(f"     [SKIP] 후속 정보 추가 {prior_live_updates}회로 상한 도달 — 더 이상 붙이지 않음")
            continue

        # 원 본문은 어떤 경우에도 Gemini에 넘기지 않고 덮어쓰지도 않는다.
        updates, base_body, legacy = _split_article(summary)

        print(f"  → {title[:50]}")
        followups = search_followup(title, country)
        if not followups:
            print(f"     후속 없음")
            continue

        # search_followup()이 full_text(원문 전문)까지 조회해오는데도 여태 안 쓰고
        # summary만, 그것도 200자로 잘라 넘겼다 — 재료 자체가 부족하니 압축 요약밖에
        # 못 나온 것도 당연하다. full_text가 있으면 그걸 우선 쓰고, 길이 상한도
        # 모델 컨텍스트 기준으로 충분히 넉넉하게 잡는다(5건 합쳐도 15000자 안쪽).
        followup_text = ""
        for f in followups[:5]:
            t = f.get("title_ko") or f.get("title_en") or ""
            b = f.get("full_text") or f.get("summary_ko") or f.get("summary_en") or ""
            followup_text += f"- {t}\n  {b[:3000]}\n"

        applied = "\n".join(x for x in (updates, legacy) if x)
        applied_block = f"\n[이미 반영된 업데이트]\n{applied}\n" if applied else ""

        # 증분만 생성한다. 본문 재작성 경로는 존재하지 않는다.
        # BBC·CNN 라이브 기사처럼 상단에 쌓이는 구조 자체는 유지하되(위치는 그대로),
        # 각 업데이트 항목은 압축 요약이 아니라 그 자체로 완결된 기사문이어야 한다.
        # 실사고(2026-08-09): "2~4문장" 상한과 후속 정보 200자 절단 때문에 매번
        # 짧은 요약 블럿만 생성돼 "기사가 아니라 요약이 올라온다"는 지적을 받음.
        delta_prompt = f"""아래 기사에 이어 붙일 '새로 확인된 내용'을 기사문으로 쓰세요.
BBC·CNN 라이브 업데이트처럼, 이 항목 하나만 읽어도 무슨 일이 있었는지 충분히
이해되는 완결된 기사 문단이어야 합니다. 사실을 나열만 하는 압축 요약이 아니라,
후속 정보에 담긴 배경·경위·수치·전망까지 살려서 서술하세요.

[현재 기사]
제목: {title}
내용: {base_body}
{applied_block}
[후속 정보]
{followup_text}

규칙:
- 현재 기사와 이미 반영된 업데이트에 있는 내용은 절대 반복하지 마세요.
- 새로 확인된 사실이 없으면 정확히 "업데이트 불필요" 한 줄만 출력하세요.
- 후속 정보에 담긴 사실 관계를 빠짐없이 살려 쓰세요. 문장 수를 인위적으로 줄이지 말고,
  근거가 있는 만큼 충분히 서술하세요(보통 3문단 이상 분량이 나옵니다). 논평·마크다운·헤더·소제목 금지.
- 수치가 갱신됐으면 갱신된 값을 명시하세요(예: "사망자는 30명으로 늘었다").
- 모든 문장을 "-다"로 종결하세요. "-습니다", "-입니다" 등 정중체는 쓰지 마세요.
- 날짜는 후속 정보에 명시된 "N일(현지시간)" 형식만 쓰세요. 날짜 근거가 없으면 생략하고 추측하지 마세요.
- 고유명사는 한글 음차로 쓰되, 명칭에 든 영문 약어는 알파벳 그대로 두세요(오픈AI, xAI, UN, EU).
  영문+숫자 코드(H-1B, 5G)와 한국 기업 약칭(SK, LG)도 그대로 둡니다.

새로 확인된 내용:"""

        delta = call_gemini_article(delta_prompt, max_tokens=1500)
        if not delta or "업데이트 불필요" in delta:
            print("     업데이트 불필요")
            continue

        delta = delta.strip()
        if delta.startswith("새로 확인된 내용:"):
            delta = delta.split(":", 1)[1].strip()
        delta = _strip_leaked_labels(delta)
        delta = _normalize_recent_abs_dates(delta)
        if has_polite_ending(delta):
            print("     🔧 합쇼체 감지 → 자동 변환 적용")
            delta = to_plain_style(delta)

        # 중복 방어 — 같은 내용이 매 실행마다 append되는 것을 막는다.
        # 비교 대상은 본문 + 기존 업데이트 전체(summary)다.
        # _dedupe_delta는 문장을 공백으로 다시 이어붙이므로(단락 구분 소실),
        # 여러 문단 분량 기사문을 만든 뒤에는 _ensure_paragraphs로 단락을 복원한다.
        delta = _dedupe_delta(delta, summary)
        if len(delta) < MIN_DELTA_LEN:
            print(f"     새 내용 부족({len(delta)}자) → 생략")
            continue
        delta = _ensure_paragraphs(delta)

        note = "후속 정보 추가"
        new_body = _compose_article(_prepend_update(updates, delta), base_body, legacy)

        if update_article(art_id, title, new_body, note=note):
            update_article_count(art_id, 2)
            print(f"     ✅ {note}")
            updated += 1

        time.sleep(CALL_INTERVAL)

    print(f"[라이브 업데이트] {updated}건 완료")

def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return

    print("\n[클러스터링] 오늘 기사 분석 중...")
    all_articles = get_today_articles(limit=300)
    clusters = cluster_articles(all_articles)
    print(f"  → {len(all_articles)}건 중 {len(clusters)}개 클러스터 발견\n")

    today_own_articles = get_today_own_articles()

    generated = 0
    updated   = 0
    processed = 0

    for i, cluster in enumerate(clusters):
        if processed >= MAX_CLUSTERS_PER_RUN:
            print(f"[STOP] 이번 실행 최대 처리 수 도달 ({MAX_CLUSTERS_PER_RUN}개) — 다음 실행에 계속")
            break

        country     = cluster[0].get("country") or ""
        category    = cluster[0].get("category") or ""
        titles      = [a.get("title_ko") or a.get("title_en") or "" for a in cluster]
        cluster_key = make_cluster_key(cluster)
        cur_count   = len(cluster)

        print(f"[클러스터 {i+1}/{len(clusters)}] {country} / {category} — {cur_count}건")
        for t in titles:
            print(f"  - {t[:60]}")

        existing      = get_existing_cluster(cluster_key)
        prev_count    = get_cluster_article_count(cluster_key)

        if existing:
            if cur_count <= prev_count:
                print(f"  [SKIP] 새 기사 없음 ({cur_count}건 동일)\n")
                continue

            print(f"  → 기존 기사 업데이트 ({prev_count}건 → {cur_count}건)")
            prompt  = build_issue_prompt(cluster, existing["summary_ko"])
            has_full = any(a.get("full_text") for a in cluster)
            content = call_gemini_article(prompt, max_tokens=4000 if has_full else 1500)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment = parse_title_and_body(content)
                gen_body = _ensure_paragraphs(gen_body)
                new_title = gen_title if gen_title else titles[0][:50]
                note = generate_update_note(existing["summary_ko"], gen_body or _strip_leaked_labels(content))
                update_article(existing["id"], new_title, gen_body or _strip_leaked_labels(content), note=note, countries=gen_countries if gen_countries else None, country=gen_country or "", summary_3lines=gen_summary3 or None, investment_idea=gen_investment or None)
                update_article_count(existing["id"], prev_count + 1)
                if gen_country or gen_category or gen_travel:
                    update_fields = {}
                    if gen_country:
                        norm_country = normalize_country(gen_country)
                        update_fields["country"] = norm_country
                        update_fields["region"] = country_to_region(norm_country)
                    if gen_category:
                        update_fields["category"] = gen_category
                        if gen_category == "글로벌":
                            update_fields["region"] = "global"
                    if gen_travel:
                        update_fields["is_travel"] = True
                    if update_fields:
                        update_article_fields(existing["id"], update_fields)
                print(f"  ✅ 업데이트 완료: {new_title}\n")
                updated += 1
            else:
                print(f"  ❌ 업데이트 실패\n")

        else:
            if cur_count < CLUSTER_MIN_SIZE:
                print(f"  [SKIP] 기사 부족 ({cur_count}건)\n")
                continue

            probe_title = titles[0][:80] if titles else ""
            similar_existing, sim_score = find_similar_article(probe_title, today_own_articles) if probe_title else (None, 0)

            if similar_existing:
                print(f"  → 유사 기존 기사 발견 (유사도 {sim_score}%) → 병합 업데이트: {similar_existing.get('title_ko','')[:40]}")
                existing_full = get_article_by_id(similar_existing["id"])
                existing_summary = existing_full.get("summary_ko") if existing_full else None

                prompt = build_issue_prompt(cluster, existing_summary) if existing_summary else build_issue_prompt(cluster)
                has_full = any(a.get("full_text") for a in cluster)
                content = call_gemini_article(prompt, max_tokens=4000 if has_full else 1500)

                if content:
                    gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment = parse_title_and_body(content)
                    gen_body = _ensure_paragraphs(gen_body)
                    new_title = gen_title if gen_title else probe_title
                    note = generate_update_note(existing_summary, gen_body or _strip_leaked_labels(content))
                    update_article(similar_existing["id"], new_title, gen_body or _strip_leaked_labels(content), note=note, countries=gen_countries if gen_countries else None, country=gen_country or "", summary_3lines=gen_summary3 or None, investment_idea=gen_investment or None)
                    prev_count = existing_full.get("score", 0) if existing_full else 0
                    update_article_count(similar_existing["id"], max(prev_count, cur_count) + 1)
                    if gen_country or gen_category or gen_travel:
                        update_fields = {}
                        if gen_country:
                            norm_country = normalize_country(gen_country)
                            update_fields["country"] = norm_country
                            update_fields["region"] = country_to_region(norm_country)
                        if gen_category:
                            update_fields["category"] = gen_category
                            if gen_category == "글로벌":
                                update_fields["region"] = "global"
                        if gen_travel:
                            update_fields["is_travel"] = True
                        if update_fields:
                            update_article_fields(similar_existing["id"], update_fields)
                    print(f"  ✅ 병합 완료: {new_title}\n")
                    updated += 1
                    send_to_newsfinal_channel(similar_existing["id"], new_title, gen_body or _strip_leaked_labels(content), is_update=True)
                else:
                    print(f"  ❌ 병합 실패\n")
                time.sleep(CALL_INTERVAL)
                processed += 1
                continue

            print(f"  → 신규 이슈 기사 생성")
            prompt  = build_issue_prompt(cluster)
            has_full = any(a.get("full_text") for a in cluster)
            content = call_gemini_article(prompt, max_tokens=4000 if has_full else 1500)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment = parse_title_and_body(content)
                gen_body = _ensure_paragraphs(gen_body)
                full_title = gen_title if gen_title else titles[0][:50]

                final_country = normalize_country(gen_country or country)
                final_category = gen_category or category or "종합"
                final_region = country_to_region(final_country) if final_country else (cluster[0].get("region") or "global")
                if final_category == "글로벌":
                    final_region = "global"

                # published 기본값. 아래 조건들이 하나도 걸리지 않는 정상 경로에서
                # 미정의 상태가 되어 UnboundLocalError가 나던 버그를 막는다.
                published = True

                needs_review = any(a.get("__needs_review__") for a in cluster)
                if needs_review:
                    published = False
                    final_subcategory = "needs_review"
                else:
                    final_subcategory = cluster_key

                similar, sim_score = find_similar_article(full_title, today_own_articles)
                if similar:
                    print(f"  ⚠️ 유사 기사 재발견 (유사도 {sim_score}%) → 미발행으로 저장: {similar.get('title_ko','')[:40]}")
                    published = False

                _dg_reason = ""
                if published and not verify_single_topic(full_title, gen_body or _strip_leaked_labels(content)):
                    print(f"  ❌ 검수 실패 (복수 토픽) → 미발행으로 저장: {full_title[:50]}")
                    published = False
                    _dg_reason = "복수 토픽 혼입 — 무관한 사건이 한 기사에 묶임"

                if published:
                    _dg_bad, _dg_reason = check_date_hallucination(
                        gen_body or _strip_leaked_labels(content), cluster, now_kst().date())
                    if _dg_bad:
                        print(f"  ⚠️ [{_dg_reason}] → 미발행으로 저장")
                        published = False

                image_url = fetch_article_image(full_title, gen_body or _strip_leaked_labels(content)) if published else ""

                article_id = save_article(
                    unpub_reason  = _dg_reason,
                    title_ko      = full_title,
                    summary_ko    = gen_body or _strip_leaked_labels(content),
                    cluster_key   = final_subcategory if 'final_subcategory' in dir() else cluster_key,
                    category      = final_category,
                    region        = final_region,
                    country       = final_country,
                    summary_3lines = gen_summary3,
                    investment_idea = gen_investment,
                    article_count = cur_count,
                    published     = published,
                    countries     = gen_countries,
                    image_url     = image_url,
                    is_travel     = gen_travel,
                )
                if article_id > 0:
                    status = "✅ 저장 완료" if published else "📋 미발행 저장"
                    print(f"  {status} (id={article_id}): {full_title}\n")
                    if published:
                        today_own_articles.append({"id": article_id, "title_ko": full_title})
                        generated += 1
                        send_to_newsfinal_channel(article_id, full_title, gen_body or _strip_leaked_labels(content), is_update=False)
                        detect_and_register_companies(full_title, gen_body or _strip_leaked_labels(content), final_country)
                else:
                    print(f"  ⚠️ 저장 실패\n")
            else:
                print(f"  ❌ 생성 실패\n")

        time.sleep(CALL_INTERVAL)
        processed += 1

    # ── 단독 기사화 ──
    solo_candidates = [
        a for a in all_articles
        if len(a.get("full_text") or "") >= 1000
        and not is_multi_topic_title(a.get("title_en","") or a.get("title_ko",""))
        and not is_multi_topic_body(a.get("full_text","") or a.get("summary_en",""))
    ]

    multi_topic_skipped = [
        a for a in all_articles
        if len(a.get("full_text") or "") >= 1000
        and is_multi_topic_title(a.get("title_en","") or a.get("title_ko",""))
    ]
    if multi_topic_skipped:
        print(f"  [파킹] 복수 주제 제목 {len(multi_topic_skipped)}건 → DB 파킹")
        parked_count = park_multi_topic_articles(multi_topic_skipped)
        print(f"  [파킹] {parked_count}개 토픽 파킹 완료")

    print(f"\n[단독 기사] 원문 충분한 기사 {len(solo_candidates)}건")

    solo_generated = 0
    for a in solo_candidates[:5]:
        if processed >= MAX_CLUSTERS_PER_RUN + 5:
            break

        title = a.get("title_ko") or a.get("title_en") or ""
        url = f"solo_{a.get('id')}"
        cluster_key = f"solo_{now_kst().strftime('%Y%m%d')}_{hashlib.md5(title.encode()).hexdigest()[:8]}"

        existing = get_existing_cluster(cluster_key)
        if existing:
            continue

        print(f"  → 단독 기사 생성: {title[:60]}")

        rules = load_prompt("writer_rules", fallback="""[주의사항]
- 반드시 하나의 토픽(사건/이슈)만 다루는 기사를 작성하세요.
- 본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
- 마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
- 매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 포함하지 마세요.
- 날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- "2026년 6월 24일 현재", "오늘", "현재" 등 절대 날짜를 본문에 쓰지 마세요.
- 기사 문체로 작성하세요. 논평/칼럼 문체는 금지입니다.
- 모든 인명·지명은 반드시 한글로 음차하세요. 키릴 문자, 아랍 문자 등 비라틴 문자를 그대로 쓰지 마세요.
""" + JSON_OUTPUT_SPEC)

        similar_existing, pre_sim_score = find_similar_article(title, today_own_articles) if title else (None, 0)

        if similar_existing:
            print(f"  → 유사 기존 기사 발견 (유사도 {pre_sim_score}%) → 병합 업데이트: {similar_existing.get('title_ko','')[:40]}")
            existing_full = get_article_by_id(similar_existing["id"])
            existing_summary = existing_full.get("summary_ko") if existing_full else None

            merge_template = """당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
기존 기사에 새로 들어온 관련 기사를 반영해 업데이트하세요. ({today_str})

[기존 기사]
{existing_summary}

[새로 들어온 원문 — {source}]
{full_text}

새로 들어온 기사의 팩트를 기존 기사에 자연스럽게 통합해 완성도 높은 기사로 다시 써주세요.
팩트(수치, 인명, 날짜, 기관명)를 최대한 살리고, 한국어로 작성하세요.
{rules}

{json_spec}"""

            prompt = merge_template.format(
                today_str=now_kst().strftime('%Y년 %m월 %d일'),
                existing_summary=existing_summary or "",
                source=a.get('source', ''),
                full_text=a.get('full_text', ''),
                rules=rules,
                json_spec=JSON_OUTPUT_SPEC,
            )
            content = call_gemini_article(prompt, max_tokens=4000)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment = parse_title_and_body(content)
                gen_body = _ensure_paragraphs(gen_body)
                new_title = gen_title if gen_title else title[:50]
                existing_sum = existing_full.get("summary_ko") if existing_full else None
                note = generate_update_note(existing_sum, gen_body or _strip_leaked_labels(content))
                update_article(similar_existing["id"], new_title, gen_body or _strip_leaked_labels(content), note=note, countries=gen_countries if gen_countries else None, country=gen_country or "", summary_3lines=gen_summary3 or None, investment_idea=gen_investment or None)
                prev_count = existing_full.get("score", 0) if existing_full else 0
                update_article_count(similar_existing["id"], prev_count + 1)
                if gen_country or gen_category or gen_travel:
                    update_fields = {}
                    if gen_country:
                        norm_country = normalize_country(gen_country)
                        update_fields["country"] = norm_country
                        update_fields["region"] = country_to_region(norm_country)
                    if gen_category:
                        update_fields["category"] = gen_category
                        if gen_category == "글로벌":
                            update_fields["region"] = "global"
                    if gen_travel:
                        update_fields["is_travel"] = True
                    if update_fields:
                        update_article_fields(similar_existing["id"], update_fields)
                print(f"  ✅ 단독 병합 완료: {new_title}\n")
                updated += 1
                send_to_newsfinal_channel(similar_existing["id"], new_title, gen_body or _strip_leaked_labels(content), is_update=True)
            else:
                print(f"  ❌ 단독 병합 실패\n")
            time.sleep(CALL_INTERVAL)
            continue

        template = load_prompt("writer_solo", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 {source}의 원문 기사입니다. ({today_str})
국가: {country} | 분야: {category}

[원문]
{full_text}

원문의 팩트(수치, 인명, 날짜, 기관명, 구체적 내용)를 빠짐없이 살려서 한국어 기사로 작성하세요.
원문이 길면 기사도 충분히 길게 쓰세요. 억지로 줄이지 마세요.
{rules}""")

        prompt = template.format(
            source=a.get('source', ''),
            today_str=now_kst().strftime('%Y년 %m월 %d일'),
            country=a.get('country', ''),
            category=a.get('category', ''),
            full_text=a.get('full_text', ''),
            rules=rules,
        )

        content = call_gemini_article(prompt, max_tokens=4000)
        if content:
            gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment = parse_title_and_body(content)
            gen_body = _ensure_paragraphs(gen_body)
            full_title = gen_title if gen_title else title[:50]

            final_country = normalize_country(gen_country or a.get("country") or "")
            final_category = gen_category or a.get("category") or "종합"
            final_region = country_to_region(final_country) if final_country else (a.get("region") or "global")
            if final_category == "글로벌":
                final_region = "global"

            if not verify_single_topic(full_title, gen_body or _strip_leaked_labels(content)):
                print(f"  ❌ 검수 실패 (복수 토픽) — 파킹: {full_title[:50]}")
                park_multi_topic_articles([{"title_en": full_title, "full_text": gen_body or _strip_leaked_labels(content),
                    "country": final_country, "category": final_category, "region": final_region}])
                time.sleep(CALL_INTERVAL)
                continue

            similar, sim_score = find_similar_article(full_title, today_own_articles)
            if similar:
                print(f"  ⚠️ 유사 기사 재발견 (유사도 {sim_score}%) → 미발행으로 저장")
                published = False
            else:
                published = True

            _dg_reason = ""
            if published:
                _dg_bad, _dg_reason = check_date_hallucination(
                    gen_body or _strip_leaked_labels(content), [a], now_kst().date())
                if _dg_bad:
                    print(f"  ⚠️ [{_dg_reason}] → 미발행으로 저장")
                    published = False

            image_url = fetch_article_image(full_title, gen_body or _strip_leaked_labels(content)) if published else ""

            article_id = save_article(
                unpub_reason=_dg_reason,
                title_ko=full_title,
                summary_ko=gen_body or _strip_leaked_labels(content),
                cluster_key=cluster_key,
                category=final_category,
                region=final_region,
                country=final_country,
                summary_3lines=gen_summary3,
                investment_idea=gen_investment,
                article_count=1,
                published=published,
                countries=gen_countries,
                image_url=image_url,
                is_travel=gen_travel,
            )
            if article_id > 0:
                status = "✅ 단독 저장" if published else "📋 단독 미발행"
                print(f"  {status} (id={article_id}): {full_title}\n")
                if published:
                    today_own_articles.append({"id": article_id, "title_ko": full_title})
                    solo_generated += 1
                    send_to_newsfinal_channel(article_id, full_title, gen_body or _strip_leaked_labels(content), is_update=False)
                    detect_and_register_companies(full_title, gen_body or _strip_leaked_labels(content), final_country)
        time.sleep(CALL_INTERVAL)

    print(f"✅ 완료 — 클러스터 {generated}건 생성 / {updated}건 업데이트 / 단독 {solo_generated}건 생성")

    # 라이브 기사 능동적 업데이트
    update_live_articles()


if __name__ == "__main__":
    run()
