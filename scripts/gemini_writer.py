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
import time
import hashlib
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from rapidfuzz import fuzz

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

load_dotenv()

GEMINI_MODEL       = "gemini-3.1-flash-lite"
CALL_INTERVAL      = 8
MAX_CLUSTERS_PER_RUN = 7  # 한 번 실행당 최대 처리 클러스터 수

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
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
] if k]

_current_key_idx = 0

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
                "select": "id,title_ko,title_en,summary_ko,summary_en,source,category,subcategory,country,region,url,created_at,score,full_text",
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
                "select": "id,title_ko,title_en,summary_ko,summary_en,source,category,subcategory,country,region,url,created_at,score,full_text",
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
    """최근 24시간(KST) 내 생성된 자체 기사 제목 목록 — 발행/미발행 모두 포함(중복 체크용)"""
    since = (now_kst() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,subcategory,is_published",
                "source": "eq.NewsFinal",
                "created_at": f"gte.{since}",
                "order": "created_at.desc",
            },
            timeout=15
        )
        if res.status_code in (200, 206):
            articles = res.json()
            print(f"  [중복체크 준비] 최근 24시간 자체 기사 {len(articles)}건 로드됨")
            return articles
        print(f"  ⚠️ [중복체크 경고] 기존 기사 조회 실패 (status={res.status_code}) — 중복 검사 건너뜀 위험")
        return []
    except Exception as e:
        print(f"  ⚠️ [중복체크 경고] 기존 기사 조회 예외 발생: {e} — 중복 검사 건너뜀 위험")
        return []


def find_similar_article(title: str, own_articles: list, threshold: int = 70):
    """유사한 자체 기사 찾기 — threshold 이상이면 중복으로 판단.
    token_sort_ratio(어순 무관 전체 비교)와 token_set_ratio(공통 단어 집합 비교) 중 높은 쪽을 사용해
    "삼성전자 워치 출시 전망" vs "삼성전자, 차세대 워치 출시 전망 분석"처럼
    표현이 늘어나거나 어순이 바뀐 같은 사건도 더 안정적으로 잡아냄."""
    for a in own_articles:
        existing_title = a.get("title_ko") or ""
        if not existing_title:
            continue
        score_sort = fuzz.token_sort_ratio(title, existing_title)
        score_set = fuzz.token_set_ratio(title, existing_title)
        score = max(score_sort, score_set)
        if score >= threshold:
            return a, score
    return None, 0


def save_article(title_ko, summary_ko, cluster_key, category, region, country="", article_count=0, published=True, countries=None):
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
        "countries": countries or ([country] if country else []),
        "score": 1,  # 최초 게시는 항상 1, 업데이트마다 +1
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "최초 게시"}],
        "sent_telegram": 0,
        "is_published": published,
        "posted_blog": 0,
    }
    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0].get("id", -1) if data else -1
    return -1


def update_article(article_id, title_ko, summary_ko, note: str = "업데이트", countries=None):
    """기사 갱신(병합 업데이트) — update_log에 업데이트 기록 추가"""
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

    res = requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={
            "title_ko": title_ko,
            "title_en": title_ko,
            "summary_ko": summary_ko,
            "created_at": now_str,
            "update_log": new_log,
            **( {"countries": countries} if countries else {} ),
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

    # 조건 1: 제목이 매우 유사 + 본문 앞부분도 키워드 2개 이상 공유
    if title_sim >= SIMILARITY_HIGH and len(lead_common) >= 2:
        return True

    # 조건 2: 제목 키워드 2개 이상 공유 + 본문 앞부분 키워드 3개 이상 공유 + 같은 카테고리
    if len(title_common) >= 2 and len(lead_common) >= 3 and same_category:
        return True

    # 조건 3: 제목+본문 키워드 합산 4개 이상 공유 + 같은 카테고리
    all_common = (title_kw_a | lead_kw_a) & (title_kw_b | lead_kw_b)
    if len(all_common) >= 4 and same_category:
        return True

    return False


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

def build_issue_prompt(cluster, existing_summary=None):
    sorted_cluster = sorted(cluster, key=lambda a: bool(a.get("full_text")), reverse=True)
    main_articles = sorted_cluster[:5]
    extra_titles = [a.get("title_ko") or a.get("title_en") or "" for a in sorted_cluster[5:]]

    article_list = ""
    for i, a in enumerate(main_articles, 1):
        t = a.get("title_ko") or a.get("title_en") or ""
        full_text = a.get("full_text") or ""
        s = full_text if full_text else (a.get("summary_ko") or a.get("summary_en") or "")
        article_list += f"{i}. [{a.get('source','')}] {t}\n"
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
- 기사 문체로 작성하세요. 논평/칼럼 문체는 금지입니다.
아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (기사의 핵심 주체가 되는 국가 1개. 어느 나라 기업/정부/기관이 주체인가 기준. 글로벌 기업·국제기구가 주체면 "없음")
관련국가: (기사에서 유의미하게 다뤄지는 국가들. 쉼표로 구분, 최대 4개. 없으면 "없음". 예: 인도, 나이지리아)
분야: (경제/금융/자원·에너지/산업·기업/정치·외교/사회/IT·과학/글로벌 중 하나)
본문: (기사 본문)"""

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
    global _current_key_idx
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": max_tokens},
    }

    while _current_key_idx < len(GEMINI_API_KEYS):
        api_key = GEMINI_API_KEYS[_current_key_idx]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={api_key}"
        )
        try:
            res = requests.post(url, json=payload, timeout=(10, 30))
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif res.status_code == 429:
                print(f"  [429] 키 {_current_key_idx+1} 한도 초과 → 키 {_current_key_idx+2}로 전환")
                _current_key_idx += 1
            else:
                print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                return None
        except requests.exceptions.Timeout:
            print(f"  [TIMEOUT] 키 {_current_key_idx+1} — 넘어갑니다.")
            return None
        except Exception as e:
            print(f"[ERROR] {e}")
            return None

    print("[ERROR] 모든 키 소진")
    return None


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


# 국가명 별칭 통합 — Gemini가 매번 다른 표현을 쓰는 문제 방지
# (예: "대한민국"/"한국"/"South Korea" → "한국" 하나로 통일)
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
    # 별칭 테이블에 소문자로도 매칭 시도
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


def parse_title_and_body(text):
    """Gemini 응답에서 제목/본문/국가/관련국가/분야 분리"""
    title = ""
    country = ""
    countries = []
    category = ""
    body = text
    lines = text.strip().split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        if line.startswith("제목:"):
            title = line.replace("제목:", "").strip()
        elif line.startswith("국가:"):
            country = line.replace("국가:", "").strip()
            if country in ("없음", "글로벌", "-", "N/A", ""):
                country = ""
        elif line.startswith("관련국가:"):
            raw = line.replace("관련국가:", "").strip()
            if raw not in ("없음", "-", "N/A", ""):
                countries = [c.strip() for c in raw.split(",") if c.strip() and c.strip() not in ("없음", "-")]
        elif line.startswith("분야:"):
            category = line.replace("분야:", "").strip()
        elif line.startswith("본문:"):
            body = "\n".join(lines[i:]).replace("본문:", "", 1).strip()
            body_start = i
            break
    if not body_start and title:
        idx = next((i for i, l in enumerate(lines) if l.startswith("제목:")), -1)
        if idx >= 0:
            body = "\n".join(lines[idx+1:]).strip()
            body_lines = [l for l in body.split("\n") if not l.startswith("국가:") and not l.startswith("관련국가:") and not l.startswith("분야:")]
            body = "\n".join(body_lines).strip()
            if body.startswith("본문:"):
                body = body[3:].strip()
    return title, body, country, category, countries


# ── 기업 자동 감지·등록 ────────────────────────────────────────────────

def get_company_by_id(company_id: str) -> dict | None:
    """companies 테이블에서 기업 조회"""
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
    """companies 테이블에 기업 저장"""
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
        # None 값 제거
        payload = {k: v for k, v in payload.items() if v is not None}
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/companies",
            headers=_sb_headers(),
            json=payload,
            timeout=15
        )
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"  ⚠️ 기업 저장 실패: {e}")
    return False


def generate_update_note(existing_summary: str, new_summary: str) -> str:
    """기존 기사와 새 내용을 비교해서 '이번 업데이트에서 추가된 핵심 내용' 한 줄 생성"""
    prompt = f"""기존 기사와 업데이트된 기사를 비교해서, 이번 업데이트에서 새롭게 추가되거나 변경된 핵심 내용을 15자 이내 한 줄로 요약하세요.
예시: "현지 당국 공식 발표 추가", "사망자 수 214명으로 업데이트", "정부 대응 방안 발표"
마크다운 없이 텍스트만 출력하세요.

기존 내용: {(existing_summary or '')[:300]}
새 내용: {(new_summary or '')[:300]}"""
    result = call_gemini(prompt, max_tokens=50)
    return (result or "업데이트").strip().replace('\n', ' ')[:30]



def detect_and_register_companies(title: str, body: str, country: str):
    """
    기사 제목/본문에서 기업을 감지하고, companies 테이블에 없으면 자동 등록.
    Gemini에게 기업명·기업 개요 생성 요청.
    """
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
        import json, re
        # JSON 부분만 추출
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

            # 이미 등록된 기업이면 스킵
            existing = get_company_by_id(company_id)
            if existing:
                continue

            # 국가 정보 보완
            comp_country = country or ""
            flag_map = {
                "나이지리아": "🇳🇬", "케냐": "🇰🇪", "남아공": "🇿🇦", "남아프리카공화국": "🇿🇦",
                "베트남": "🇻🇳", "인도네시아": "🇮🇩", "태국": "🇹🇭", "필리핀": "🇵🇭",
                "이집트": "🇪🇬", "가나": "🇬🇭", "에티오피아": "🇪🇹", "탄자니아": "🇹🇿",
                "방글라데시": "🇧🇩", "파키스탄": "🇵🇰", "카자흐스탄": "🇰🇿",
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
    """
    Gemini에게 생성된 기사가 단일 토픽인지 검증.
    복수 토픽이면 False 반환.
    """
    if not title or not body:
        return True  # 판단 불가면 통과

    prompt = f"""아래 기사가 하나의 명확한 토픽(사건/이슈/기업/정책)만 다루는지 판단하세요.
서로 다른 국가나 전혀 관련 없는 사건 여러 개를 한 기사에 묶은 경우 "NO"라고만 답하세요.
하나의 토픽이면 "YES"라고만 답하세요.

제목: {title}
본문 앞부분: {body[:400]}

답변 (YES 또는 NO만):"""

    result = call_gemini(prompt, max_tokens=5)
    if not result:
        return True  # API 실패 시 통과
    return "YES" in result.upper()


def park_multi_topic_articles(articles: list) -> int:
    """
    복수 주제 RSS 기사를 토픽별로 분리해서 DB에 파킹.
    is_published=False, subcategory=parked_topic 으로 저장.
    나중에 관련 소스가 들어오면 클러스터링에서 자동으로 활용됨.
    """
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
            # 이미 같은 파킹 제목이 있으면 스킵
            try:
                check = requests.get(
                    f"{SUPABASE_URL}/rest/v1/articles",
                    headers=_sb_headers(),
                    params={
                        "select": "id",
                        "subcategory": "eq.parked_topic",
                        "title_en": f"ilike.*{part[:20]}*",
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
                "countries":   a.get("countries") or ([country] if country else []),
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

def get_stale_live_articles() -> list:
    """업데이트되지 않은 라이브 기사 조회 (score=1, 최근 48시간)"""
    since = (now_kst() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,country,category,score",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "score": "eq.1",
                "created_at": f"gte.{since}",
                "order": "created_at.desc",
                "limit": "20",
            },
            timeout=15
        )
        if res.status_code in (200, 206):
            return [a for a in res.json()
                    if not (a.get("subcategory") or "").startswith("digest_")]
    except Exception as e:
        print(f"  [라이브 업데이트] 조회 실패: {e}")
    return []


def search_followup(title: str, country: str) -> list:
    """제목/국가 키워드로 내부 DB + GDELT에서 후속 기사 검색"""
    import urllib.parse
    since = (now_kst() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")

    kw_list = [w for w in title.replace(",", "").replace("\xb7", " ").split() if len(w) >= 2]
    kw = kw_list[0] if kw_list else country

    results = []

    # 내부 DB
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

    # GDELT
    try:
        eng_words = [w for w in title.split() if not any("\uAC00" <= c <= "\uD7A3" for c in w)]
        eng_kw = " ".join(eng_words[:3]) if eng_words else country
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


def update_live_articles():
    """업데이트되지 않은 라이브 기사를 능동적으로 후속 검색해서 업데이트"""
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

        print(f"  → {title[:50]}")
        followups = search_followup(title, country)
        if not followups:
            print(f"     후속 없음")
            continue

        followup_text = ""
        for f in followups[:5]:
            t = f.get("title_ko") or f.get("title_en") or ""
            b = f.get("summary_ko") or f.get("summary_en") or ""
            followup_text += f"- {t}\n  {b[:200]}\n"

        prompt = f"""현재 기사와 후속 정보를 비교해서, 추가할 새로운 내용이 있으면 업데이트하세요.
새로운 내용이 없으면 "업데이트 불필요"라고만 답하세요.

[현재 기사]
제목: {title}
내용: {summary[:500]}

[후속 정보]
{followup_text}

새 내용이 있으면:
업데이트노트: (핵심 변경 15자 이내)
본문: (업데이트된 전체 본문)"""

        result = call_gemini(prompt, max_tokens=2000)
        if not result or "업데이트 불필요" in result:
            print(f"     업데이트 불필요")
            continue

        note = "후속 정보 업데이트"
        new_body = result
        for line in result.strip().split("\n"):
            if line.startswith("업데이트노트:"):
                note = line.replace("업데이트노트:", "").strip()
            elif line.startswith("본문:"):
                new_body = result[result.find("본문:")+3:].strip()
                break

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

    # 오늘 생성된 자체 기사 목록 (중복 체크용)
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
            content = call_gemini(prompt, max_tokens=4000 if has_full else 1500)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries = parse_title_and_body(content)
                new_title = gen_title if gen_title else titles[0][:50]
                note = generate_update_note(existing["summary_ko"], gen_body or content)
                update_article(existing["id"], new_title, gen_body or content, note=note, countries=gen_countries if gen_countries else None)
                update_article_count(existing["id"], prev_count + 1)
                # 국가/분야 재분류 업데이트
                if gen_country or gen_category:
                    update_fields = {}
                    if gen_country:
                        norm_country = normalize_country(gen_country)
                        update_fields["country"] = norm_country
                        update_fields["region"] = country_to_region(norm_country)
                    if gen_category:
                        update_fields["category"] = gen_category
                        if gen_category == "글로벌":
                            update_fields["region"] = "global"
                    if update_fields:
                        update_article_fields(existing["id"], update_fields)
                print(f"  ✅ 업데이트 완료: {new_title}\n")
                updated += 1
            else:
                print(f"  ❌ 업데이트 실패\n")

        else:
            # 최소 2건 이상일 때만 신규 생성
            if cur_count < CLUSTER_MIN_SIZE:
                print(f"  [SKIP] 기사 부족 ({cur_count}건)\n")
                continue

            # 같은 cluster_key는 아니지만 표현이 달라 같은 사건일 수 있는 기존 기사 탐색
            # (원문 클러스터의 대표 제목으로 먼저 검사 — 병합 가능하면 새로 만들지 않고 기존 글에 합침)
            probe_title = titles[0][:80] if titles else ""
            similar_existing, sim_score = find_similar_article(probe_title, today_own_articles) if probe_title else (None, 0)

            if similar_existing:
                print(f"  → 유사 기존 기사 발견 (유사도 {sim_score}%) → 병합 업데이트: {similar_existing.get('title_ko','')[:40]}")
                # 기존 기사 본문을 가져와서 병합 프롬프트 생성
                existing_full = get_article_by_id(similar_existing["id"])
                existing_summary = existing_full.get("summary_ko") if existing_full else None

                prompt = build_issue_prompt(cluster, existing_summary) if existing_summary else build_issue_prompt(cluster)
                has_full = any(a.get("full_text") for a in cluster)
                content = call_gemini(prompt, max_tokens=4000 if has_full else 1500)

                if content:
                    gen_title, gen_body, gen_country, gen_category, gen_countries = parse_title_and_body(content)
                    new_title = gen_title if gen_title else probe_title
                    note = generate_update_note(existing_summary, gen_body or content)
                    update_article(similar_existing["id"], new_title, gen_body or content, note=note, countries=gen_countries if gen_countries else None)
                    prev_count = existing_full.get("score", 0) if existing_full else 0
                    update_article_count(similar_existing["id"], max(prev_count, cur_count) + 1)
                    if gen_country or gen_category:
                        update_fields = {}
                        if gen_country:
                            norm_country = normalize_country(gen_country)
                            update_fields["country"] = norm_country
                            update_fields["region"] = country_to_region(norm_country)
                        if gen_category:
                            update_fields["category"] = gen_category
                            if gen_category == "글로벌":
                                update_fields["region"] = "global"
                        if update_fields:
                            update_article_fields(similar_existing["id"], update_fields)
                    print(f"  ✅ 병합 완료: {new_title}\n")
                    updated += 1
                    send_to_newsfinal_channel(similar_existing["id"], new_title, gen_body or content, is_update=True)
                else:
                    print(f"  ❌ 병합 실패\n")
                time.sleep(CALL_INTERVAL)
                processed += 1
                continue

            print(f"  → 신규 이슈 기사 생성")
            prompt  = build_issue_prompt(cluster)
            has_full = any(a.get("full_text") for a in cluster)
            content = call_gemini(prompt, max_tokens=4000 if has_full else 1500)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries = parse_title_and_body(content)
                full_title = gen_title if gen_title else titles[0][:50]

                # Gemini가 재분류한 국가/분야 우선 사용
                final_country = normalize_country(gen_country or country)
                final_category = gen_category or category or "종합"
                final_region = country_to_region(final_country) if final_country else (cluster[0].get("region") or "global")
                if final_category == "글로벌":
                    final_region = "global"

                # 제목 생성 후 한 번 더 유사 기사 체크 (이중 안전장치)
                similar, sim_score = find_similar_article(full_title, today_own_articles)
                if similar:
                    print(f"  ⚠️ 유사 기사 재발견 (유사도 {sim_score}%) → 미발행으로 저장: {similar.get('title_ko','')[:40]}")
                    published = False
                else:
                    published = True

                article_id = save_article(
                    title_ko      = full_title,
                    summary_ko    = gen_body or content,
                    cluster_key   = cluster_key,
                    category      = final_category,
                    region        = final_region,
                    country       = final_country,
                    article_count = cur_count,
                    published     = published,
                    countries     = gen_countries,
                )
                if article_id > 0:
                    status = "✅ 저장 완료" if published else "📋 미발행 저장"
                    print(f"  {status} (id={article_id}): {full_title}\n")
                    if published:
                        today_own_articles.append({"id": article_id, "title_ko": full_title})
                        generated += 1
                        send_to_newsfinal_channel(article_id, full_title, gen_body or content, is_update=False)
                        detect_and_register_companies(full_title, gen_body or content, final_country)
                else:
                    print(f"  ⚠️ 저장 실패\n")
            else:
                print(f"  ❌ 생성 실패\n")

        time.sleep(CALL_INTERVAL)
        processed += 1

    # ── 단독 기사화 — 원문 충분한 기사 (클러스터 여부 무관) ──
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
    for a in solo_candidates[:5]:  # 실행당 최대 5건
        if processed >= MAX_CLUSTERS_PER_RUN + 5:
            break

        title = a.get("title_ko") or a.get("title_en") or ""
        url = f"solo_{a.get('id')}"
        cluster_key = f"solo_{now_kst().strftime('%Y%m%d')}_{hashlib.md5(title.encode()).hexdigest()[:8]}"

        # 이미 생성된 단독 기사면 스킵
        existing = get_existing_cluster(cluster_key)
        if existing:
            continue

        print(f"  → 단독 기사 생성: {title[:60]}")

        rules = load_prompt("writer_rules", fallback="""[주의사항]
- 반드시 하나의 토픽(사건/이슈)만 다루는 기사를 작성하세요. 관련 없는 두 개 이상의 사건을 한 기사에 묶지 마세요.
- 여러 기사가 입력되더라도 가장 중요한 하나의 이슈에 집중하고, 나머지는 참고만 하세요.
- 본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
- 마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
- 매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 포함하지 마세요.
- 날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- 기사 문체로 작성하세요. 논평/칼럼 문체는 금지입니다.
아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (기사의 핵심 주체가 되는 국가 1개. 어느 나라 기업/정부/기관이 주체인가 기준. 글로벌 기업·국제기구가 주체면 "없음")
관련국가: (기사에서 유의미하게 다뤄지는 국가들. 쉼표로 구분, 최대 4개. 없으면 "없음". 예: 인도, 나이지리아)
분야: (경제/금융/자원·에너지/산업·기업/정치·외교/사회/IT·과학/글로벌 중 하나)
본문: (기사 본문)""")

        # 원본 제목으로 먼저 유사 기존 기사 탐색 — 매치되면 신규 생성 대신 병합
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

아래 형식으로 출력:
제목: (통합된 핵심을 담은 제목)
국가: (기사의 핵심 주체가 되는 국가 1개. 어느 나라 기업/정부/기관이 주체인가 기준. 글로벌 기업·국제기구가 주체면 "없음")
관련국가: (기사에서 유의미하게 다뤄지는 국가들. 쉼표로 구분, 최대 4개. 없으면 "없음". 예: 인도, 나이지리아)
분야: (경제/금융/자원·에너지/산업·기업/정치·외교/사회/IT·과학/글로벌 중 하나)
본문: (통합된 기사 본문)"""

            prompt = merge_template.format(
                today_str=now_kst().strftime('%Y년 %m월 %d일'),
                existing_summary=existing_summary or "",
                source=a.get('source', ''),
                full_text=a.get('full_text', ''),
                rules=rules,
            )
            content = call_gemini(prompt, max_tokens=4000)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries = parse_title_and_body(content)
                new_title = gen_title if gen_title else title[:50]
                existing_sum = existing_full.get("summary_ko") if existing_full else None
                note = generate_update_note(existing_sum, gen_body or content)
                update_article(similar_existing["id"], new_title, gen_body or content, note=note, countries=gen_countries if gen_countries else None)
                prev_count = existing_full.get("score", 0) if existing_full else 0
                update_article_count(similar_existing["id"], prev_count + 1)
                if gen_country or gen_category:
                    update_fields = {}
                    if gen_country:
                        norm_country = normalize_country(gen_country)
                        update_fields["country"] = norm_country
                        update_fields["region"] = country_to_region(norm_country)
                    if gen_category:
                        update_fields["category"] = gen_category
                        if gen_category == "글로벌":
                            update_fields["region"] = "global"
                    if update_fields:
                        update_article_fields(similar_existing["id"], update_fields)
                print(f"  ✅ 단독 병합 완료: {new_title}\n")
                updated += 1
                send_to_newsfinal_channel(similar_existing["id"], new_title, gen_body or content, is_update=True)
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

        content = call_gemini(prompt, max_tokens=4000)
        if content:
            gen_title, gen_body, gen_country, gen_category, gen_countries = parse_title_and_body(content)
            full_title = gen_title if gen_title else title[:50]

            final_country = normalize_country(gen_country or a.get("country") or "")
            final_category = gen_category or a.get("category") or "종합"
            final_region = country_to_region(final_country) if final_country else (a.get("region") or "global")
            if final_category == "글로벌":
                final_region = "global"

            # B. 생성 후 단일 토픽 검수
            if not verify_single_topic(full_title, gen_body or content):
                print(f"  ❌ 검수 실패 (복수 토픽) — 파킹: {full_title[:50]}")
                park_multi_topic_articles([{"title_en": full_title, "full_text": gen_body or content,
                    "country": final_country, "category": final_category, "region": final_region}])
                time.sleep(CALL_INTERVAL)
                continue

            # 제목 생성 후 한 번 더 유사 기사 체크 (이중 안전장치)
            similar, sim_score = find_similar_article(full_title, today_own_articles)
            if similar:
                print(f"  ⚠️ 유사 기사 재발견 (유사도 {sim_score}%) → 미발행으로 저장")
                published = False
            else:
                published = True

            article_id = save_article(
                title_ko=full_title,
                summary_ko=gen_body or content,
                cluster_key=cluster_key,
                category=final_category,
                region=final_region,
                country=final_country,
                article_count=1,
                published=published,
                countries=gen_countries,
            )
            if article_id > 0:
                status = "✅ 단독 저장" if published else "📋 단독 미발행"
                print(f"  {status} (id={article_id}): {full_title}\n")
                if published:
                    today_own_articles.append({"id": article_id, "title_ko": full_title})
                    solo_generated += 1
                    send_to_newsfinal_channel(article_id, full_title, gen_body or content, is_update=False)
                    detect_and_register_companies(full_title, gen_body or content, final_country)
        time.sleep(CALL_INTERVAL)

    print(f"✅ 완료 — 클러스터 {generated}건 생성 / {updated}건 업데이트 / 단독 {solo_generated}건 생성")

    # 라이브 기사 능동적 업데이트
    update_live_articles()


if __name__ == "__main__":
    run()


