"""
gemini_summarizer.py
--------------------
DB에 저장된 기사 중 summary_ko가 빈약한 기사를 골라
Gemini Flash로 고품질 한국어 요약을 재생성합니다.

실행: python scripts/gemini_summarizer.py
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_MODEL = "gemini-3.1-flash-lite"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
] if k]

_current_key_idx = 0

MAX_ARTICLES = 30
CALL_INTERVAL = 5


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
    since = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() - 86400))
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_en,title_ko,summary_en,summary_ko,source,category,subcategory,region,country",
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
            "select": "id,title_en,title_ko,summary_en,summary_ko,source,category,subcategory,region,country",
            "created_at": f"gte.{since}",
            "summary_ko": f"lt.{'x'*100}",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30
    )
    articles = []
    if res.status_code in (200, 206):
        articles.extend(res.json())
    # 100자 미만 필터는 클라이언트에서 처리
    if short_res.status_code in (200, 206):
        for a in short_res.json():
            sk = a.get("summary_ko") or ""
            if len(sk) < 100 and a not in articles:
                articles.append(a)
    return articles[:limit]


def update_summary(article_id: int, summary_ko: str, summary_en: str = None):
    payload = {"summary_ko": summary_ko}
    if summary_en:
        payload["summary_en"] = summary_en
    requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json=payload,
        timeout=15
    )


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
