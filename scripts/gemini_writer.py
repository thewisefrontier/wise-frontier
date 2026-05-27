"""
gemini_writer.py
----------------
당일 수집된 기사들을 Gemini Flash로 분석해
The Wise Frontier 자체 기사(브리핑/분석)를 생성하고 DB에 저장합니다.

실행: python scripts/gemini_writer.py
하루 1회 실행 권장 (GitHub Actions 별도 스케줄)
"""

import os
import sys
import time
import sqlite3
import requests
import json
from dotenv import load_dotenv
from db import get_conn as db_get_conn, init_db

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_FILE = "data/articles.db"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-lite-preview-06-17:generateContent"
    f"?key={GEMINI_API_KEY}"
)

# 생성할 기사 유형
ARTICLE_TYPES = [
    {
        "type":     "daily_briefing",
        "title":    "오늘의 프론티어 마켓 브리핑",
        "category": "브리핑",
        "region":   "global",
        "regions":  None,   # 전체 지역
        "max_src":  15,
    },
    {
        "type":     "africa_briefing",
        "title":    "아프리카 경제 동향",
        "category": "브리핑",
        "region":   "africa",
        "regions":  ["africa"],
        "max_src":  8,
    },
    {
        "type":     "asia_briefing",
        "title":    "동남아·중앙아시아 동향",
        "category": "브리핑",
        "region":   "southeast_asia",
        "regions":  ["southeast_asia", "central_asia", "south_asia"],
        "max_src":  8,
    },
]

CALL_INTERVAL = 5


def get_today_articles(regions: list = None, limit: int = 15) -> list:
    """오늘 수집된 기사 조회"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    today = time.strftime("%Y-%m-%d")
    if regions:
        placeholders = ",".join("?" * len(regions))
        c.execute(f"""
            SELECT title_ko, title_en, summary_ko, summary_en,
                   source, category, subcategory, country, region, url
            FROM articles
            WHERE created_at LIKE ?
              AND sent_telegram = 1
              AND region IN ({placeholders})
            ORDER BY score DESC, created_at DESC
            LIMIT ?
        """, [f"{today}%"] + regions + [limit])
    else:
        c.execute("""
            SELECT title_ko, title_en, summary_ko, summary_en,
                   source, category, subcategory, country, region, url
            FROM articles
            WHERE created_at LIKE ?
              AND sent_telegram = 1
            ORDER BY score DESC, created_at DESC
            LIMIT ?
        """, (f"{today}%", limit))

    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def already_generated_today(article_type: str) -> bool:
    """오늘 이미 해당 타입 기사를 생성했는지 확인"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = time.strftime("%Y-%m-%d")
    c.execute("""
        SELECT id FROM articles
        WHERE source = 'The Wise Frontier'
          AND subcategory = ?
          AND created_at LIKE ?
        LIMIT 1
    """, (article_type, f"{today}%"))
    row = c.fetchone()
    conn.close()
    return row is not None


def save_generated_article(title_ko: str, summary_ko: str,
                            article_type: str, category: str, region: str) -> int:
    """생성된 자체 기사를 DB에 저장"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = time.strftime("%Y-%m-%d %H:%M")
    # url은 고유해야 하므로 타입+날짜로 생성
    url = f"internal://{article_type}/{time.strftime('%Y%m%d')}"
    try:
        c.execute("""
            INSERT INTO articles (
                title_en, title_ko, summary_en, summary_ko,
                url, source, category, subcategory, region,
                country, country_flag, score, sent_telegram, posted_blog
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            title_ko,       # title_en 자리에 한국어 제목 (자체 기사)
            title_ko,
            "",
            summary_ko,
            url,
            "The Wise Frontier",
            category,
            article_type,
            region,
            "",
            "",
            100,            # 자체 기사는 score 높게
            1,
            0,
        ))
        conn.commit()
        return c.lastrowid
    except sqlite3.IntegrityError:
        return -1
    finally:
        conn.close()


def build_briefing_prompt(articles: list, title: str) -> str:
    # 기사 목록 텍스트 구성
    article_list = ""
    for i, a in enumerate(articles, 1):
        t = a.get("title_ko") or a.get("title_en") or ""
        s = a.get("summary_ko") or a.get("summary_en") or ""
        country = a.get("country") or ""
        category = a.get("category") or ""
        article_list += f"{i}. [{category}] {country} — {t}\n"
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
6. 기사 본문만 출력 (제목 제외, 설명 텍스트 없이)
7. 문단 구분은 빈 줄로

기사 본문:"""


def call_gemini(prompt: str) -> str | None:
    if not GEMINI_API_KEY:
        print("[ERROR] GEMINI_API_KEY가 설정되지 않았습니다.")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.6,
            "maxOutputTokens": 1000,
        }
    }

    try:
        res = requests.post(GEMINI_URL, json=payload, timeout=30)
        if res.status_code != 200:
            print(f"[ERROR] Gemini API {res.status_code}: {res.text[:200]}")
            return None
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text
    except Exception as e:
        print(f"[ERROR] Gemini 호출 실패: {e}")
        return None


def run():
    if not GEMINI_API_KEY:
        print("[SKIP] GEMINI_API_KEY 없음 — gemini_writer 건너뜀")
        return

    init_db()
    generated = 0

    for art_type in ARTICLE_TYPES:
        print(f"\n[생성] {art_type['title']} 시작...")

        # 오늘 이미 생성했으면 스킵
        if already_generated_today(art_type["type"]):
            print(f"[SKIP] 오늘 이미 생성됨 — {art_type['type']}")
            continue

        # 기사 수집
        articles = get_today_articles(
            regions=art_type["regions"],
            limit=art_type["max_src"]
        )

        if len(articles) < 3:
            print(f"[SKIP] 기사 부족 ({len(articles)}건) — {art_type['type']}")
            continue

        print(f"  → 참고 기사 {len(articles)}건으로 생성 중...")

        # Gemini로 기사 생성
        prompt = build_briefing_prompt(articles, art_type["title"])
        content = call_gemini(prompt)

        if not content:
            print(f"  ❌ 생성 실패")
            continue

        # 제목 구성: "오늘의 프론티어 마켓 브리핑 — 2026.05.27"
        today_label = time.strftime("%Y.%m.%d")
        full_title = f"{art_type['title']} — {today_label}"

        # DB 저장
        article_id = save_generated_article(
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
            print(f"  ⚠️ 저장 실패 (중복 가능성)")

        if generated < len(ARTICLE_TYPES):
            time.sleep(CALL_INTERVAL)

    print(f"\n✅ 자체 기사 생성 완료: {generated}건")


if __name__ == "__main__":
    run()
