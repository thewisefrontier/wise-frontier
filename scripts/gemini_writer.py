"""
gemini_writer.py
----------------
1. 당일 기사들을 rapidfuzz로 클러스터링
2. 클러스터별 자체 종합 기사 생성 (최대 5개)
3. 일별 브리핑 기사 생성 (전체/지역별)

실행: python scripts/gemini_writer.py
하루 1회 실행 권장
"""

import os
import sys
import time
import sqlite3
import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz
from db import init_db

load_dotenv()

GEMINI_MODEL  = "gemini-3.1-flash-lite"
DB_FILE       = "data/articles.db"
CALL_INTERVAL = 5

# 클러스터링 설정
CLUSTER_SIMILARITY = 60   # 제목 유사도 기준 (%)
CLUSTER_MIN_SIZE   = 2    # 최소 기사 수
MAX_CLUSTERS       = 5    # 하루 최대 클러스터 기사 생성 수

# 브리핑 유형
BRIEFING_TYPES = [
    {
        "type":    "daily_briefing",
        "title":   "오늘의 프론티어 마켓 브리핑",
        "category":"브리핑",
        "region":  "global",
        "regions": None,
        "max_src": 15,
    },
    {
        "type":    "africa_briefing",
        "title":   "아프리카 경제 동향",
        "category":"브리핑",
        "region":  "africa",
        "regions": ["africa"],
        "max_src": 8,
    },
    {
        "type":    "asia_briefing",
        "title":   "동남아·중앙아시아 동향",
        "category":"브리핑",
        "region":  "southeast_asia",
        "regions": ["southeast_asia", "central_asia", "south_asia"],
        "max_src": 8,
    },
]


# ── DB 헬퍼 ──────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_today_articles(regions=None, limit=50):
    conn = get_conn()
    c = conn.cursor()
    today = time.strftime("%Y-%m-%d")
    if regions:
        ph = ",".join("?" * len(regions))
        c.execute(f"""
            SELECT id, title_ko, title_en, summary_ko, summary_en,
                   source, category, subcategory, country, region, url
            FROM articles
            WHERE created_at LIKE ? AND sent_telegram = 1
              AND source != 'The Wise Frontier'
              AND region IN ({ph})
            ORDER BY score DESC, created_at DESC LIMIT ?
        """, [f"{today}%"] + regions + [limit])
    else:
        c.execute("""
            SELECT id, title_ko, title_en, summary_ko, summary_en,
                   source, category, subcategory, country, region, url
            FROM articles
            WHERE created_at LIKE ? AND sent_telegram = 1
              AND source != 'The Wise Frontier'
            ORDER BY score DESC, created_at DESC LIMIT ?
        """, (f"{today}%", limit))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def already_generated_today(article_type):
    conn = get_conn()
    c = conn.cursor()
    today = time.strftime("%Y-%m-%d")
    c.execute("""
        SELECT id FROM articles
        WHERE source = 'The Wise Frontier' AND subcategory = ?
          AND created_at LIKE ? LIMIT 1
    """, (article_type, f"{today}%"))
    row = c.fetchone()
    conn.close()
    return row is not None


def save_article(title_ko, summary_ko, article_type, category, region, country=""):
    conn = get_conn()
    c = conn.cursor()
    url = f"internal://{article_type}/{time.strftime('%Y%m%d%H%M%S')}"
    try:
        c.execute("""
            INSERT INTO articles (
                title_en, title_ko, summary_en, summary_ko,
                url, source, category, subcategory, region,
                country, country_flag, score, sent_telegram, posted_blog
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            title_ko, title_ko, "", summary_ko,
            url, "The Wise Frontier", category, article_type,
            region, country, "", 100, 1, 0,
        ))
        conn.commit()
        return c.lastrowid
    except sqlite3.IntegrityError:
        return -1
    finally:
        conn.close()


# ── 클러스터링 ────────────────────────────────────────────

def cluster_articles(articles):
    """
    rapidfuzz 유사도 + 국가/카테고리 조건으로 기사 클러스터링
    같은 사건/토픽 기사들을 묶어 반환
    """
    clusters = []
    used = set()

    for i, a in enumerate(articles):
        if i in used:
            continue
        cluster = [a]
        used.add(i)
        title_a = (a.get("title_ko") or a.get("title_en") or "").lower()

        for j, b in enumerate(articles):
            if j in used or j == i:
                continue
            title_b = (b.get("title_ko") or b.get("title_en") or "").lower()

            # 제목 유사도
            sim = fuzz.token_sort_ratio(title_a, title_b)

            # 같은 국가이면 유사도 기준 낮춤
            same_country = (
                a.get("country") and b.get("country") and
                a.get("country") == b.get("country")
            )
            threshold = CLUSTER_SIMILARITY - 10 if same_country else CLUSTER_SIMILARITY

            # 같은 카테고리이면 추가 완화
            same_cat = a.get("category") == b.get("category")
            if same_cat:
                threshold -= 5

            if sim >= threshold:
                cluster.append(b)
                used.add(j)

        if len(cluster) >= CLUSTER_MIN_SIZE:
            clusters.append(cluster)

    # 큰 클러스터 우선 정렬
    clusters.sort(key=lambda c: len(c), reverse=True)
    return clusters[:MAX_CLUSTERS]


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

def build_cluster_prompt(cluster):
    """클러스터 기사 → 종합 분석 기사 프롬프트"""
    article_list = ""
    for i, a in enumerate(cluster, 1):
        t = a.get("title_ko") or a.get("title_en") or ""
        s = a.get("summary_ko") or a.get("summary_en") or ""
        article_list += f"{i}. [{a.get('source','')}] {t}\n"
        if s:
            article_list += f"   {s}\n"

    country = cluster[0].get("country") or ""
    category = cluster[0].get("category") or ""
    today_str = time.strftime("%Y년 %m월 %d일")

    return f"""당신은 프론티어 마켓 전문 미디어 The Wise Frontier의 수석 에디터입니다.
아래는 같은 사건/토픽을 다룬 {len(cluster)}개의 기사입니다.({today_str})

[관련 기사]
{article_list}

[작성 규칙]
1. 400~600자 분량의 종합 분석 기사
2. 여러 기사의 내용을 종합해 하나의 완성된 기사로 작성
3. 사실 나열이 아닌 의미와 맥락 중심으로 서술
4. 프론티어 마켓 투자자/분석가 시각에서 중요한 포인트 강조
5. 한국어로만 작성
6. 기사 본문만 출력 (제목 제외)

기사 본문:"""


def build_briefing_prompt(articles, title):
    article_list = ""
    for i, a in enumerate(articles, 1):
        t = a.get("title_ko") or a.get("title_en") or ""
        s = a.get("summary_ko") or a.get("summary_en") or ""
        article_list += f"{i}. [{a.get('category','')}] {a.get('country','')} — {t}\n"
        if s:
            article_list += f"   {s}\n"

    today_str = time.strftime("%Y년 %m월 %d일")
    return f"""당신은 프론티어 마켓 전문 미디어 The Wise Frontier의 수석 에디터입니다.
오늘({today_str}) 수집된 기사들을 바탕으로 "{title}" 기사를 작성하세요.

[오늘의 수집 기사]
{article_list}

[작성 규칙]
1. 500~700자 분량의 분석적 브리핑 기사
2. 오늘의 핵심 이슈 2~3개를 중심으로 구성
3. 단순 나열이 아닌 트렌드와 맥락을 연결해서 서술
4. 프론티어 마켓 투자자/분석가가 주목해야 할 포인트 강조
5. 한국어로만 작성
6. 기사 본문만 출력 (제목 제외)
7. 문단 구분은 빈 줄로

기사 본문:"""


# ── 메인 실행 ─────────────────────────────────────────────

def run():
    if not os.getenv("GEMINI_API_KEY"):
        print("[SKIP] GEMINI_API_KEY 없음")
        return

    init_db()
    generated = 0

    # 1. 클러스터 기사 생성
    print("\n[클러스터링] 오늘 기사 분석 중...")
    all_articles = get_today_articles(limit=100)
    clusters = cluster_articles(all_articles)
    print(f"  → {len(all_articles)}건 중 {len(clusters)}개 클러스터 발견")

    for i, cluster in enumerate(clusters):
        titles = [a.get("title_ko") or a.get("title_en") or "" for a in cluster]
        country = cluster[0].get("country") or ""
        category = cluster[0].get("category") or ""
        cluster_id = f"cluster_{time.strftime('%Y%m%d')}_{i}"

        if already_generated_today(cluster_id):
            print(f"  [SKIP] 클러스터 {i+1} — 오늘 이미 생성됨")
            continue

        print(f"\n  [클러스터 {i+1}/{len(clusters)}] {country} / {category} — {len(cluster)}건")
        for t in titles:
            print(f"    - {t[:50]}")

        prompt = build_cluster_prompt(cluster)
        content = call_gemini(prompt, max_tokens=800)

        if not content:
            print(f"  ❌ 생성 실패")
            continue

        # 제목: 대표 기사 제목 기반
        rep_title = titles[0][:40]
        today_label = time.strftime("%Y.%m.%d")
        full_title = f"[종합] {rep_title} 외 {len(cluster)-1}건 — {today_label}"

        article_id = save_article(
            title_ko=full_title,
            summary_ko=content,
            article_type=cluster_id,
            category=category or "종합",
            region=cluster[0].get("region") or "global",
            country=country,
        )

        if article_id > 0:
            print(f"  ✅ 저장 완료 (id={article_id}): {full_title}")
            generated += 1
        else:
            print(f"  ⚠️ 저장 실패")

        time.sleep(CALL_INTERVAL)

    # 2. 브리핑 기사 생성
    print("\n[브리핑] 일별 브리핑 생성 중...")
    for art_type in BRIEFING_TYPES:
        print(f"\n  [{art_type['title']}] 시작...")

        if already_generated_today(art_type["type"]):
            print(f"  [SKIP] 오늘 이미 생성됨")
            continue

        articles = get_today_articles(
            regions=art_type["regions"],
            limit=art_type["max_src"]
        )

        if len(articles) < 3:
            print(f"  [SKIP] 기사 부족 ({len(articles)}건)")
            continue

        print(f"  → 참고 기사 {len(articles)}건으로 생성 중...")
        prompt = build_briefing_prompt(articles, art_type["title"])
        content = call_gemini(prompt, max_tokens=1000)

        if not content:
            print(f"  ❌ 생성 실패")
            continue

        today_label = time.strftime("%Y.%m.%d")
        full_title = f"{art_type['title']} — {today_label}"

        article_id = save_article(
            title_ko=full_title,
            summary_ko=content,
            article_type=art_type["type"],
            category=art_type["category"],
            region=art_type["region"],
        )

        if article_id > 0:
            print(f"  ✅ 저장 완료 (id={article_id}): {full_title}")
            generated += 1
        else:
            print(f"  ⚠️ 저장 실패")

        time.sleep(CALL_INTERVAL)

    print(f"\n✅ 자체 기사 생성 완료: {generated}건 (클러스터 + 브리핑)")


if __name__ == "__main__":
    run()
