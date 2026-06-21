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
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

GEMINI_MODEL       = "gemini-3.1-flash-lite"
CALL_INTERVAL      = 5
MAX_CLUSTERS_PER_RUN = 10  # 한 번 실행당 최대 처리 클러스터 수

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _sb_url():
    return f"{SUPABASE_URL}/rest/v1/articles"

# API 키 폴백 체인
GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
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
SIMILARITY_HIGH         = 55
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
    since = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() - 48 * 3600))
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
    return articles[:limit]


def get_existing_cluster(cluster_key):
    today = time.strftime("%Y-%m-%d")
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
    """오늘 생성된 자체 기사 제목 목록"""
    today = time.strftime("%Y-%m-%d")
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_ko,subcategory",
            "source": "eq.NewsFinal",
            "created_at": f"like.{today}%",
            "order": "created_at.desc",
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


def find_similar_article(title: str, own_articles: list, threshold: int = 85):
    """유사한 자체 기사 찾기 — threshold 이상이면 중복으로 판단"""
    for a in own_articles:
        existing_title = a.get("title_ko") or ""
        score = fuzz.token_sort_ratio(title, existing_title)
        if score >= threshold:
            return a, score
    return None, 0


def save_article(title_ko, summary_ko, cluster_key, category, region, country="", article_count=0, published=True):
    url = f"internal://{cluster_key}"
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
        "score": article_count,
        "created_at": time.strftime("%Y-%m-%d %H:%M"),
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


def update_article(article_id, title_ko, summary_ko):
    res = requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={
            "title_ko": title_ko,
            "title_en": title_ko,
            "summary_ko": summary_ko,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
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


def init_db():
    pass  # Supabase는 대시보드에서 테이블 관리


# ── 클러스터링 ────────────────────────────────────────────

def extract_keywords(text):
    """텍스트에서 의미 있는 키워드 추출"""
    if not text:
        return set()
    # 소문자, 특수문자 제거
    text = text.lower()
    text = re.sub(r'[^\w\s가-힣]', ' ', text)
    words = text.split()
    # 불용어 제거, 3자 이상만
    return {w for w in words if w not in STOPWORDS and len(w) >= 3}


def keyword_overlap(a, b):
    """두 기사 키워드 겹침 비율 (0~1)"""
    title_a = (a.get("title_ko") or a.get("title_en") or "")
    title_b = (b.get("title_ko") or b.get("title_en") or "")
    summ_a  = (a.get("summary_ko") or a.get("summary_en") or "")
    summ_b  = (b.get("summary_ko") or b.get("summary_en") or "")

    kw_a = extract_keywords(title_a) | extract_keywords(summ_a)
    kw_b = extract_keywords(title_b) | extract_keywords(summ_b)

    if not kw_a or not kw_b:
        return 0.0

    intersection = kw_a & kw_b
    union = kw_a | kw_b
    return len(intersection) / len(union)


def articles_are_related(a, b):
    """
    두 기사가 같은 이슈인지 판단
    제목 유사도 + 키워드 겹침 기반 — 국가만 같다고 묶지 않음
    """
    title_a = (a.get("title_ko") or a.get("title_en") or "").lower()
    title_b = (b.get("title_ko") or b.get("title_en") or "").lower()

    # 1. 제목 유사도
    title_sim = fuzz.token_sort_ratio(title_a, title_b)

    # 2. 키워드 겹침
    kw_sim = keyword_overlap(a, b) * 100

    # 3. 공통 키워드 — 제목+요약 기준
    kw_a = extract_keywords(title_a) | extract_keywords(a.get("summary_ko") or "")
    kw_b = extract_keywords(title_b) | extract_keywords(b.get("summary_ko") or "")
    common_kw = kw_a & kw_b

    # 같은 카테고리 여부 (약한 보정만)
    same_category = a.get("category") == b.get("category")

    # 종합 점수 — 토픽 유사도 중심
    score = title_sim * 0.4 + kw_sim * 0.6
    if same_category:
        score += 3  # 카테고리 보정 약하게
    if len(common_kw) >= 3:
        score += 5  # 공통 키워드 3개 이상일 때만 보정

    # 국가 보정 제거 — 국가만 같아서 묶이지 않도록
    # 대신 임계값 통일
    return score >= SIMILARITY_HIGH


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
    today = time.strftime("%Y%m%d")
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

    today_str = time.strftime("%Y년 %m월 %d일")
    country = cluster[0].get("country") or ""
    category = cluster[0].get("category") or ""

    FALLBACK_RULES = """[주의사항]
- 본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
- 마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
- 매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 포함하지 마세요.
- 날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- 기사 문체로 작성하세요. 논평/칼럼 문체는 금지입니다.
아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (기사의 핵심 주제가 되는 국가명 1개. 특정 국가 얘기가 아니면 "없음")
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

팩트(수치, 인명, 날짜, 기관명, 구체적 내용)를 빠짐없이 살려서 작성하세요.
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


def update_article_fields(article_id: int, fields: dict):
    requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json=fields,
        timeout=15
    )


def parse_title_and_body(text):
    """Gemini 응답에서 제목/본문/국가/분야 분리"""
    title = ""
    country = ""
    category = ""
    body = text
    lines = text.strip().split("\n")
    body_start = 0
    for i, line in enumerate(lines):
        if line.startswith("제목:"):
            title = line.replace("제목:", "").strip()
        elif line.startswith("국가:"):
            country = line.replace("국가:", "").strip()
            if country in ("없음", "글로벌", "-", "N/A"):
                country = ""
        elif line.startswith("분야:"):
            category = line.replace("분야:", "").strip()
        elif line.startswith("본문:"):
            body = "\n".join(lines[i:]).replace("본문:", "", 1).strip()
            body_start = i
            break
    if not body_start and title:
        # 본문: 라벨이 없으면 제목 이후 전체를 본문으로
        idx = next((i for i, l in enumerate(lines) if l.startswith("제목:")), -1)
        if idx >= 0:
            body = "\n".join(lines[idx+1:]).strip()
            # 국가:/분야: 라인 제거
            body_lines = [l for l in body.split("\n") if not l.startswith("국가:") and not l.startswith("분야:")]
            body = "\n".join(body_lines).strip()
            if body.startswith("본문:"):
                body = body[3:].strip()
    return title, body, country, category


# ── 메인 실행 ─────────────────────────────────────────────

def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return

    init_db()

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
                gen_title, gen_body, gen_country, gen_category = parse_title_and_body(content)
                new_title = gen_title if gen_title else titles[0][:50]
                update_article(existing["id"], new_title, gen_body or content)
                update_article_count(existing["id"], cur_count)
                # 국가/분야 재분류 업데이트
                if gen_country or gen_category:
                    update_fields = {}
                    if gen_country:
                        update_fields["country"] = gen_country
                        update_fields["region"] = country_to_region(gen_country)
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

            print(f"  → 신규 이슈 기사 생성")
            prompt  = build_issue_prompt(cluster)
            has_full = any(a.get("full_text") for a in cluster)
            content = call_gemini(prompt, max_tokens=4000 if has_full else 1500)

            if content:
                gen_title, gen_body, gen_country, gen_category = parse_title_and_body(content)
                full_title = gen_title if gen_title else titles[0][:50]

                # Gemini가 재분류한 국가/분야 우선 사용
                final_country = gen_country or country
                final_category = gen_category or category or "종합"
                final_region = country_to_region(final_country) if final_country else (cluster[0].get("region") or "global")
                if final_category == "글로벌":
                    final_region = "global"

                # 유사 기사 체크 — 85% 이상이면 미발행으로 저장
                similar, sim_score = find_similar_article(full_title, today_own_articles)
                if similar:
                    print(f"  ⚠️ 유사 기사 발견 (유사도 {sim_score}%) → 미발행으로 저장: {similar.get('title_ko','')[:40]}")
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
                )
                if article_id > 0:
                    status = "✅ 저장 완료" if published else "📋 미발행 저장"
                    print(f"  {status} (id={article_id}): {full_title}\n")
                    if published:
                        today_own_articles.append({"id": article_id, "title_ko": full_title})
                        generated += 1
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
    ]
    print(f"\n[단독 기사] 원문 충분한 기사 {len(solo_candidates)}건")

    solo_generated = 0
    for a in solo_candidates[:5]:  # 실행당 최대 5건
        if processed >= MAX_CLUSTERS_PER_RUN + 5:
            break

        title = a.get("title_ko") or a.get("title_en") or ""
        url = f"solo_{a.get('id')}"
        cluster_key = f"solo_{time.strftime('%Y%m%d')}_{hashlib.md5(title.encode()).hexdigest()[:8]}"

        # 이미 생성된 단독 기사면 스킵
        existing = get_existing_cluster(cluster_key)
        if existing:
            continue

        print(f"  → 단독 기사 생성: {title[:60]}")

        rules = load_prompt("writer_rules", fallback="""[주의사항]
- 본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
- 마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
- 매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 포함하지 마세요.
- 날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- 기사 문체로 작성하세요. 논평/칼럼 문체는 금지입니다.
아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (기사의 핵심 주제가 되는 국가명 1개. 특정 국가 얘기가 아니면 "없음")
분야: (경제/금융/자원·에너지/산업·기업/정치·외교/사회/IT·과학/글로벌 중 하나)
본문: (기사 본문)""")

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
            today_str=time.strftime('%Y년 %m월 %d일'),
            country=a.get('country', ''),
            category=a.get('category', ''),
            full_text=a.get('full_text', ''),
            rules=rules,
        )

        content = call_gemini(prompt, max_tokens=4000)
        if content:
            gen_title, gen_body, gen_country, gen_category = parse_title_and_body(content)
            full_title = gen_title if gen_title else title[:50]

            final_country = gen_country or a.get("country") or ""
            final_category = gen_category or a.get("category") or "종합"
            final_region = country_to_region(final_country) if final_country else (a.get("region") or "global")
            if final_category == "글로벌":
                final_region = "global"

            # 유사 기사 체크
            similar, sim_score = find_similar_article(full_title, today_own_articles)
            if similar:
                print(f"  ⚠️ 유사 기사 발견 (유사도 {sim_score}%) → 미발행으로 저장")
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
            )
            if article_id > 0:
                status = "✅ 단독 저장" if published else "📋 단독 미발행"
                print(f"  {status} (id={article_id}): {full_title}\n")
                if published:
                    today_own_articles.append({"id": article_id, "title_ko": full_title})
                    solo_generated += 1
        time.sleep(CALL_INTERVAL)

    print(f"✅ 완료 — 클러스터 {generated}건 생성 / {updated}건 업데이트 / 단독 {solo_generated}건 생성")


if __name__ == "__main__":
    run()
