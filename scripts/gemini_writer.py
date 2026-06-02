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


def save_article(title_ko, summary_ko, cluster_key, category, region, country="", article_count=0):
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
        "sent_telegram": 1,
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
    제목 유사도 + 키워드 겹침 + 국가/지역/카테고리 보정
    """
    title_a = (a.get("title_ko") or a.get("title_en") or "").lower()
    title_b = (b.get("title_ko") or b.get("title_en") or "").lower()

    # 1. 제목 유사도
    title_sim = fuzz.token_sort_ratio(title_a, title_b)

    # 2. 키워드 겹침
    kw_sim = keyword_overlap(a, b) * 100

    # 3. 국가/지역/카테고리 보정
    same_country  = bool(a.get("country") and a.get("country") == b.get("country"))
    same_region   = bool(a.get("region") and a.get("region") == b.get("region"))
    same_category = a.get("category") == b.get("category")

    # 핵심 키워드 단독 매칭 — 중요한 단어 하나만 겹쳐도 보정
    kw_a = extract_keywords(title_a) | extract_keywords(a.get("summary_ko") or "")
    kw_b = extract_keywords(title_b) | extract_keywords(b.get("summary_ko") or "")
    common_kw = kw_a & kw_b
    has_strong_overlap = len(common_kw) >= 2  # 공통 키워드 2개 이상

    # 종합 점수
    score = title_sim * 0.4 + kw_sim * 0.6
    if same_country:
        score += 10
    if same_region:
        score += 6
    if same_category:
        score += 5
    if has_strong_overlap:
        score += 8

    threshold = SIMILARITY_SAME_COUNTRY if same_country else SIMILARITY_HIGH
    return score >= threshold


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

def call_gemini(prompt, max_tokens=1000, retry=3):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": max_tokens},
    }

    for attempt in range(retry):
        try:
            res = requests.post(url, json=payload, timeout=(10, 30))
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif res.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"  [429] {wait}초 대기 후 재시도 ({attempt+1}/{retry})")
                time.sleep(wait)
            else:
                print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                return None
        except requests.exceptions.Timeout:
            print(f"  [TIMEOUT] {attempt+1}/{retry}회 — 넘어갑니다.")
            return None
        except Exception as e:
            print(f"[ERROR] {e}")
            return None
    return None


# ── 프롬프트 빌더 ─────────────────────────────────────────

def build_issue_prompt(cluster, existing_summary=None):
    # 원문 있는 기사 우선 정렬, 최대 5개 선택
    sorted_cluster = sorted(cluster, key=lambda a: bool(a.get("full_text")), reverse=True)
    main_articles = sorted_cluster[:5]  # 핵심 기사
    extra_titles = [a.get("title_ko") or a.get("title_en") or "" for a in sorted_cluster[5:]]

    article_list = ""
    for i, a in enumerate(main_articles, 1):
        t = a.get("title_ko") or a.get("title_en") or ""
        full_text = a.get("full_text") or ""
        s = full_text[:1500] if full_text else (a.get("summary_ko") or a.get("summary_en") or "")
        article_list += f"{i}. [{a.get('source','')}] {t}\n"
        if s:
            article_list += f"   {s}\n\n"

    # 추가 기사는 제목만
    if extra_titles:
        article_list += f"\n[추가 관련 기사 제목]\n"
        for t in extra_titles:
            article_list += f"- {t}\n"

    # 원문 있는 기사 수
    full_text_count = sum(1 for a in main_articles if a.get("full_text"))
    has_full = full_text_count > 0

    today_str = time.strftime("%Y년 %m월 %d일")
    country  = cluster[0].get("country") or ""
    category = cluster[0].get("category") or ""

    if existing_summary:
        return f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 기존에 작성된 이슈 분석 기사와 새로 추가된 관련 기사들입니다.({today_str})
{'원문 전문이 포함된 기사가 있습니다. 원문의 구체적인 수치, 인명, 사실을 최대한 활용하세요.' if has_full else ''}

[기존 기사]
{existing_summary}

[추가된 관련 기사]
{article_list}

[작성 규칙]
1. 기존 분석을 바탕으로 새 기사의 내용을 통합해 업데이트
2. 개별 사건들이 보여주는 공통 패턴과 트렌드를 중심으로 서술
3. {'1500~2000자' if has_full else '900~1200자'} 분량
4. 다음 구조로 작성:
   - 현재 상황: 어떤 사건들이 일어나고 있는가
   - 패턴 분석: 개별 사건들의 공통점과 의미
   - 투자/비즈니스 시사점: 프론티어 마켓 관점에서의 리스크 또는 기회
5. 한국어로만 작성
6. 반드시 2~3개 문단으로 나누고, 각 문단 사이에 빈 줄을 넣을 것
7. 반드시 아래 형식으로 출력:
   제목: (25자 이내의 핵심 제목)
   본문: (기사 본문)

업데이트된 기사:"""

    else:
        sources = list({a.get("source","") for a in cluster})
        same_event = len(sources) >= 2 and len(cluster) <= 4

        if same_event:
            return f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 같은 사건을 여러 매체가 다각도로 보도한 {len(cluster)}개의 기사입니다.({today_str})
국가: {country} | 분야: {category}
{'원문 전문이 포함된 기사가 있습니다. 원문의 구체적인 수치, 인명, 사실을 최대한 활용하세요.' if has_full else ''}

[관련 기사]
{article_list}

[작성 규칙]
1. {'1500~2000자' if has_full else '800~1200자'} 분량의 단일 완성 기사
2. 여러 보도를 종합해 하나의 완성된 기사로 작성 (중복 내용 제거, 누락 정보 보완)
3. 다음 구조로 작성:
   - 핵심 사실: 무슨 일이 일어났는가 (2~3문장, 육하원칙 중심)
   - 배경과 맥락: 이 사건의 배경과 의미 (1~2문장)
   - 프론티어 마켓 시사점: 투자자/기업 관점의 리스크 또는 기회 (1~2문장)
4. 한국어로만 작성
5. 반드시 2~3개 문단으로 나누고, 각 문단 사이에 빈 줄을 넣을 것
6. 반드시 아래 형식으로 출력:
   제목: (25자 이내의 핵심 제목)
   본문: (기사 본문)

기사:"""
        else:
            return f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 같은 이슈/패턴을 보여주는 {len(cluster)}개의 기사입니다.({today_str})
국가: {country} | 분야: {category}
{'원문 전문이 포함된 기사가 있습니다. 원문의 구체적인 수치, 인명, 사실을 최대한 활용하세요.' if has_full else ''}

[관련 기사]
{article_list}

[작성 규칙]
1. {'1500~2000자' if has_full else '800~1200자'} 분량의 트렌드 분석 기사
2. 개별 사건을 단순 나열하지 말고, 공통 패턴과 그 의미를 분석
3. 다음 구조로 작성:
   - 현재 상황: 어떤 사건들이 일어나고 있는가 (1~2문장)
   - 패턴 분석: 이 사건들이 공통적으로 시사하는 것 (2~3문장)
   - 투자/비즈니스 시사점: 프론티어 마켓 투자자/기업이 주목해야 할 리스크 또는 기회 (1~2문장)
4. 거시적 트렌드 관점으로 서술
5. 한국어로만 작성
6. 반드시 2~3개 문단으로 나누고, 각 문단 사이에 빈 줄을 넣을 것
7. 반드시 아래 형식으로 출력:
   제목: (25자 이내의 핵심 제목)
   본문: (기사 본문)

기사:"""
# ── Gemini 호출 ───────────────────────────────────────────

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


def parse_title_and_body(text):
    """Gemini 응답에서 제목과 본문 분리"""
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
            content = call_gemini(prompt, max_tokens=2000 if has_full else 1500)

            if content:
                today_label = time.strftime("%Y.%m.%d")
                gen_title, gen_body = parse_title_and_body(content)
                new_title = gen_title if gen_title else titles[0][:50]
                update_article(existing["id"], new_title, gen_body or content)
                update_article_count(existing["id"], cur_count)
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
            content = call_gemini(prompt, max_tokens=2000 if has_full else 1500)

            if content:
                today_label = time.strftime("%Y.%m.%d")
                gen_title, gen_body = parse_title_and_body(content)
                full_title = gen_title if gen_title else titles[0][:50]
                article_id = save_article(
                    title_ko      = full_title,
                    summary_ko    = gen_body or content,
                    cluster_key   = cluster_key,
                    category      = category or "종합",
                    region        = cluster[0].get("region") or "global",
                    country       = country,
                    article_count = cur_count,
                )
                if article_id > 0:
                    print(f"  ✅ 저장 완료 (id={article_id}): {full_title}\n")
                    generated += 1
                else:
                    print(f"  ⚠️ 저장 실패\n")
            else:
                print(f"  ❌ 생성 실패\n")

        time.sleep(CALL_INTERVAL)
        processed += 1

    print(f"✅ 완료 — 신규 {generated}건 생성 / {updated}건 업데이트")


if __name__ == "__main__":
    run()
