"""
merge_duplicate_articles.py
-----------------------------
중복 기사를 실제로 병합하는 1회성 스크립트.
두 기사의 내용을 Gemini로 합쳐서 최신 기사에 반영하고,
구버전은 미발행 처리.

실행: python scripts/one_off/merge_duplicate_articles.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
] if k]
GEMINI_MODEL = "gemini-2.5-flash-lite"

def sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def get_articles_by_keyword(keyword, limit=10):
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/articles",
        headers=sb_headers(),
        params={
            "select": "id,title_ko,created_at,is_published,score,summary_ko",
            "source": "eq.NewsFinal",
            "title_ko": f"ilike.%{keyword}%",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=15
    )
    return res.json() if res.status_code in (200, 206) else []

def call_gemini(prompt):
    for key in GEMINI_API_KEYS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
            res = requests.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1000},
            }, timeout=30)
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif res.status_code == 429:
                continue
        except Exception as e:
            print(f"  ⚠️ Gemini 오류: {e}")
    return None

def update_article(article_id, title, summary):
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/articles?id=eq.{article_id}",
        headers=sb_headers(),
        json={"title_ko": title, "summary_ko": summary},
        timeout=15
    )
    return res.status_code in (200, 201, 204)

def unpublish(article_id):
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/articles?id=eq.{article_id}",
        headers=sb_headers(),
        json={"is_published": False},
        timeout=15
    )
    return res.status_code in (200, 201, 204)

def run():
    print("[검색] '갤럭시 위치' 관련 NewsFinal 기사...")
    articles = get_articles_by_keyword("울트라 2")

    if not articles:
        print("기사를 찾을 수 없습니다.")
        return

    # 발행된 기사만 필터
    published = [a for a in articles if a['is_published']]
    print(f"\n발행된 기사 {len(published)}건:")
    for a in published:
        print(f"  ID={a['id']} | {a['created_at']} | {a['title_ko']}")

    if len(published) < 2:
        print("\n중복 기사가 없습니다.")
        return

    # 최신순 정렬
    published_sorted = sorted(published, key=lambda x: x['created_at'], reverse=True)
    keep = published_sorted[0]   # 최신 기사 — 여기에 병합
    old = published_sorted[1]    # 구버전 기사 — 병합 후 미발행

    print(f"\n[병합] 두 기사를 합칩니다...")
    print(f"  최신: ID={keep['id']} | {keep['title_ko']}")
    print(f"  구버전: ID={old['id']} | {old['title_ko']}")

    # Gemini로 두 기사 병합
    prompt = f"""아래 두 기사를 하나로 병합하세요.
최신 정보를 우선하되, 구버전 기사의 추가 맥락도 자연스럽게 통합하세요.
마크다운 없이 평문으로, 5~8문장으로 작성하세요.

[최신 기사]
제목: {keep['title_ko']}
내용: {keep['summary_ko'] or ''}

[구버전 기사]
제목: {old['title_ko']}
내용: {old['summary_ko'] or ''}

병합된 본문만 출력하세요."""

    merged_body = call_gemini(prompt)
    if not merged_body:
        print("  ❌ Gemini 병합 실패 — 단순 미발행 처리만 진행합니다.")
        ok = unpublish(old['id'])
        print(f"  구버전 미발행: {'✅' if ok else '❌'}")
        return

    print(f"\n[병합 결과] {merged_body[:100]}...")

    # 최신 기사 업데이트
    ok1 = update_article(keep['id'], keep['title_ko'], merged_body)
    print(f"  최신 기사 업데이트: {'✅' if ok1 else '❌'}")

    # 구버전 미발행
    ok2 = unpublish(old['id'])
    print(f"  구버전 미발행 처리: {'✅' if ok2 else '❌'}")

    print("\n완료. 라이브 페이지에서 확인하세요.")

if __name__ == "__main__":
    run()
