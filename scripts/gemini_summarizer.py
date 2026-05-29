"""
gemini_summarizer.py
--------------------
DB에 저장된 기사 중 summary_ko가 빈약한 기사를 골라
Gemini Flash로 고품질 한국어 요약을 재생성합니다.

실행: python scripts/gemini_summarizer.py
"""

import os
import time
import sqlite3
import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-3.1-flash-lite"
DB_FILE = "data/articles.db"

# API 키 폴백 체인 — 한도 소진 시 다음 키로 자동 전환
GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
] if k]

_current_key_idx = 0  # 현재 사용 중인 키 인덱스 (전역)

# 한 번 실행당 최대 처리 기사 수
MAX_ARTICLES = 30
# API 호출 간격 (초)
CALL_INTERVAL = 5


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_articles_to_summarize(limit: int) -> list:
    """
    요약 고도화 대상 기사:
    - summary_ko가 없거나 100자 미만 (단순 번역 수준)
    - 최근 24시간 이내
    """
    conn = get_conn()
    c = conn.cursor()
    since = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() - 86400))
    c.execute("""
        SELECT id, title_en, title_ko, summary_en, summary_ko,
               source, category, subcategory, region, country
        FROM articles
        WHERE created_at >= ?
          AND (summary_ko IS NULL OR LENGTH(summary_ko) < 100)
        ORDER BY created_at DESC
        LIMIT ?
    """, (since, limit))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_summary(article_id: int, summary_ko: str, summary_en: str = None):
    conn = get_conn()
    c = conn.cursor()
    if summary_en:
        c.execute(
            "UPDATE articles SET summary_ko = ?, summary_en = ? WHERE id = ?",
            (summary_ko, summary_en, article_id)
        )
    else:
        c.execute(
            "UPDATE articles SET summary_ko = ? WHERE id = ?",
            (summary_ko, article_id)
        )
    conn.commit()
    conn.close()


def build_prompt(article: dict) -> str:
    title = article.get("title_en") or article.get("title_ko") or ""
    summary = article.get("summary_en") or ""
    source = article.get("source") or ""
    category = article.get("category") or ""
    country = article.get("country") or ""
    region = article.get("region") or ""

    return f"""당신은 프론티어 마켓(아프리카, 동남아시아, 동유럽, 중동, 중앙아시아 등 신흥·개척 시장) 전문 미디어 The Wise Frontier의 에디터입니다.

아래 기사를 바탕으로 한국어 요약문을 작성하세요.

[기사 정보]
- 제목(영문): {title}
- 출처: {source}
- 분야: {category}
- 국가/지역: {country} ({region})
- 원문 요약(영문): {summary}

[요약 작성 규칙]
1. 3~4문장, 150~200자 분량
2. 단순 번역이 아닌 핵심 의미와 맥락을 담아 작성
3. 프론티어 마켓 투자자/분석가 관점에서 중요한 포인트 강조
4. 한국어로만 작성 (영어 단어는 꼭 필요한 경우만 사용)
5. 요약문만 출력 (제목, 설명 등 다른 텍스트 없이)

요약문:"""


def call_gemini(prompt: str, retry: int = 2) -> str | None:
    global _current_key_idx
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 300,
        }
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


def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음 — gemini_summarizer 건너뜀")
        return

    # API 연결 테스트
    print(f"[체크] Gemini API 연결 테스트... (키 {len(GEMINI_API_KEYS)}개)")
    test = call_gemini("ping", retry=1)
    if test is None:
        print("[SKIP] Gemini API 응답 없음 — 건너뜀")
        return
    print("[체크] ✅ API 연결 확인")

    articles = get_articles_to_summarize(MAX_ARTICLES)
    print(f"[요약 고도화] 대상 기사 {len(articles)}건")

    success = 0
    for i, article in enumerate(articles):
        prompt = build_prompt(article)
        summary_ko = call_gemini(prompt)

        if summary_ko:
            update_summary(article["id"], summary_ko)
            print(f"[{i+1}/{len(articles)}] ✅ {article['title_ko'] or article['title_en'][:50]}")
            success += 1
        else:
            print(f"[{i+1}/{len(articles)}] ❌ 실패 — {article['title_en'][:50]}")

        # API 한도 준수
        if i < len(articles) - 1:
            time.sleep(CALL_INTERVAL)

    print(f"\n✅ 요약 고도화 완료: {success}/{len(articles)}건 성공")


if __name__ == "__main__":
    run()
