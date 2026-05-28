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
import sqlite3
import hashlib
import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz
from db import init_db

load_dotenv()

GEMINI_MODEL       = "gemini-3.1-flash-lite"
DB_FILE            = "data/articles.db"
CALL_INTERVAL      = 5

# 클러스터링 설정
SIMILARITY_HIGH    = 70   # 제목 유사도 기준 (%)
SIMILARITY_SAME_COUNTRY = 55  # 같은 국가일 때 완화
CLUSTER_MIN_SIZE   = 2    # 최소 기사 수
MAX_CLUSTERS       = 8    # 하루 최대 처리 클러스터 수

# 키워드 추출용 불용어
STOPWORDS = {
    "the","a","an","in","on","at","to","of","for","and","or","is","are",
    "was","were","has","have","been","will","with","by","from","this","that",
    "as","its","it","be","not","but","also","over","after","amid","says",
    "say","said","new","amid","following","기자","특파원","뉴스","오늘","이번",
}


# ── DB 헬퍼 ──────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_today_articles(limit=150):
    conn = get_conn()
    c = conn.cursor()
    today = time.strftime("%Y-%m-%d")
    c.execute("""
        SELECT id, title_ko, title_en, summary_ko, summary_en,
               source, category, subcategory, country, region, url, created_at
        FROM articles
        WHERE created_at LIKE ? AND sent_telegram = 1
          AND source != 'The Wise Frontier'
        ORDER BY score DESC, created_at DESC LIMIT ?
    """, (f"{today}%", limit))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_existing_cluster(cluster_key):
    """오늘 생성된 같은 클러스터 기사 조회"""
    conn = get_conn()
    c = conn.cursor()
    today = time.strftime("%Y-%m-%d")
    c.execute("""
        SELECT id, title_ko, summary_ko, subcategory
        FROM articles
        WHERE source = 'The Wise Frontier'
          AND subcategory = ?
          AND created_at LIKE ?
        LIMIT 1
    """, (cluster_key, f"{today}%"))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def save_article(title_ko, summary_ko, cluster_key, category, region, country=""):
    conn = get_conn()
    c = conn.cursor()
    url = f"internal://{cluster_key}"
    try:
        c.execute("""
            INSERT INTO articles (
                title_en, title_ko, summary_en, summary_ko,
                url, source, category, subcategory, region,
                country, country_flag, score, sent_telegram, posted_blog
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            title_ko, title_ko, "", summary_ko,
            url, "The Wise Frontier", category, cluster_key,
            region, country, "", 100, 1, 0,
        ))
        conn.commit()
        return c.lastrowid
    except sqlite3.IntegrityError:
        return -1
    finally:
        conn.close()


def update_article(article_id, title_ko, summary_ko):
    """기존 클러스터 기사 업데이트"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        UPDATE articles SET title_ko = ?, title_en = ?, summary_ko = ?,
               created_at = strftime('%Y-%m-%d %H:%M', 'now')
        WHERE id = ?
    """, (title_ko, title_ko, summary_ko, article_id))
    conn.commit()
    conn.close()


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
    제목 유사도 + 키워드 겹침 + 국가/카테고리 보정
    """
    title_a = (a.get("title_ko") or a.get("title_en") or "").lower()
    title_b = (b.get("title_ko") or b.get("title_en") or "").lower()

    # 1. 제목 유사도
    title_sim = fuzz.token_sort_ratio(title_a, title_b)

    # 2. 키워드 겹침
    kw_sim = keyword_overlap(a, b) * 100  # 0~100 스케일

    # 3. 국가/카테고리 보정
    same_country  = bool(a.get("country") and a.get("country") == b.get("country"))
    same_category = a.get("category") == b.get("category")

    # 종합 점수
    score = title_sim * 0.5 + kw_sim * 0.5
    if same_country:
        score += 8
    if same_category:
        score += 5

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
    return clusters[:MAX_CLUSTERS]


def make_cluster_key(cluster):
    """클러스터 고유 키 생성 — 대표 기사 제목 기반 해시"""
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
    article_list = ""
    for i, a in enumerate(cluster, 1):
        t = a.get("title_ko") or a.get("title_en") or ""
        s = a.get("summary_ko") or a.get("summary_en") or ""
        article_list += f"{i}. [{a.get('source','')}] {t}\n"
        if s:
            article_list += f"   {s}\n"

    today_str = time.strftime("%Y년 %m월 %d일")
    country  = cluster[0].get("country") or ""
    category = cluster[0].get("category") or ""

    if existing_summary:
        return f"""당신은 프론티어 마켓 전문 미디어 The Wise Frontier의 수석 에디터입니다.
아래는 기존에 작성된 이슈 분석 기사와 새로 추가된 관련 기사들입니다.({today_str})

[기존 기사]
{existing_summary}

[추가된 관련 기사]
{article_list}

[작성 규칙]
1. 기존 분석을 바탕으로 새 기사의 내용을 통합해 업데이트
2. 개별 사건들이 보여주는 공통 패턴과 트렌드를 중심으로 서술
3. 500~700자 분량
4. 다음 구조로 작성:
   - 현재 상황: 어떤 사건들이 일어나고 있는가
   - 패턴 분석: 개별 사건들의 공통점과 의미
   - 투자/비즈니스 시사점: 프론티어 마켓 관점에서의 리스크 또는 기회
5. 한국어로만 작성
6. 기사 본문만 출력 (제목 제외)

업데이트된 기사 본문:"""
    else:
        return f"""당신은 프론티어 마켓 전문 미디어 The Wise Frontier의 수석 에디터입니다.
아래는 같은 이슈/패턴을 보여주는 {len(cluster)}개의 기사입니다.({today_str})
국가: {country} | 분야: {category}

[관련 기사]
{article_list}

[작성 규칙]
1. 400~600자 분량의 트렌드 분석 기사
2. 개별 사건을 단순 나열하지 말고, 공통 패턴과 그 의미를 분석
3. 다음 구조로 작성:
   - 현재 상황: 어떤 사건들이 일어나고 있는가 (1~2문장)
   - 패턴 분석: 이 사건들이 공통적으로 시사하는 것 (2~3문장)
   - 투자/비즈니스 시사점: 프론티어 마켓 투자자/기업이 주목해야 할 리스크 또는 기회 (1~2문장)
4. "나이지리아 치안 불안 확산", "인도네시아 금융 규제 강화 추세" 같은 
   거시적 트렌드 관점으로 서술
5. 한국어로만 작성
6. 기사 본문만 출력 (제목 제외)

기사 본문:"""


# ── 메인 실행 ─────────────────────────────────────────────

def run():
    if not os.getenv("GEMINI_API_KEY"):
        print("[SKIP] GEMINI_API_KEY 없음")
        return

    init_db()

    print("\n[클러스터링] 오늘 기사 분석 중...")
    all_articles = get_today_articles(limit=150)
    clusters = cluster_articles(all_articles)
    print(f"  → {len(all_articles)}건 중 {len(clusters)}개 클러스터 발견\n")

    generated = 0
    updated   = 0

    for i, cluster in enumerate(clusters):
        country  = cluster[0].get("country") or ""
        category = cluster[0].get("category") or ""
        titles   = [a.get("title_ko") or a.get("title_en") or "" for a in cluster]
        cluster_key = make_cluster_key(cluster)

        print(f"[클러스터 {i+1}/{len(clusters)}] {country} / {category} — {len(cluster)}건")
        for t in titles:
            print(f"  - {t[:60]}")

        # 기존 기사 확인
        existing = get_existing_cluster(cluster_key)

        if existing:
            # 기존 기사보다 새 기사가 더 많으면 업데이트
            existing_count = existing["summary_ko"].count("\n\n") + 1
            if len(cluster) <= existing_count:
                print(f"  [SKIP] 변경 없음 ({len(cluster)}건 동일)\n")
                continue

            print(f"  → 기존 기사 업데이트 ({existing_count}건 → {len(cluster)}건)")
            prompt  = build_issue_prompt(cluster, existing["summary_ko"])
            content = call_gemini(prompt, max_tokens=900)

            if content:
                today_label = time.strftime("%Y.%m.%d")
                new_title   = f"[이슈] {titles[0][:40]} 외 {len(cluster)-1}건 — {today_label}"
                update_article(existing["id"], new_title, content)
                print(f"  ✅ 업데이트 완료: {new_title}\n")
                updated += 1
            else:
                print(f"  ❌ 업데이트 실패\n")

        else:
            # 신규 기사 생성
            print(f"  → 신규 이슈 기사 생성")
            prompt  = build_issue_prompt(cluster)
            content = call_gemini(prompt, max_tokens=900)

            if content:
                today_label = time.strftime("%Y.%m.%d")
                full_title  = f"[이슈] {titles[0][:40]} 외 {len(cluster)-1}건 — {today_label}"
                article_id  = save_article(
                    title_ko    = full_title,
                    summary_ko  = content,
                    cluster_key = cluster_key,
                    category    = category or "종합",
                    region      = cluster[0].get("region") or "global",
                    country     = country,
                )
                if article_id > 0:
                    print(f"  ✅ 저장 완료 (id={article_id}): {full_title}\n")
                    generated += 1
                else:
                    print(f"  ⚠️ 저장 실패\n")
            else:
                print(f"  ❌ 생성 실패\n")

        time.sleep(CALL_INTERVAL)

    print(f"✅ 완료 — 신규 {generated}건 생성 / {updated}건 업데이트")


if __name__ == "__main__":
    run()
