"""
merge_duplicate_articles.py
-----------------------------
중복 기사를 수동으로 병합하는 1회성 스크립트.
오래된 기사(구버전)를 미발행 처리하고, 최신 기사에 내용을 통합.

실행: python scripts/one_off/merge_duplicate_articles.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
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

def unpublish(article_id):
    """기사를 미발행 처리"""
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/articles?id=eq.{article_id}",
        headers=sb_headers(),
        json={"is_published": False},
        timeout=15
    )
    return res.status_code in (200, 201, 204)

def run():
    # 삼성전자 갤럭시 언팩 중복 기사 찾기
    print("[검색] '갤럭시 언팩' 관련 NewsFinal 기사...")
    articles = get_articles_by_keyword("갤럭시 위치")

    if not articles:
        print("기사를 찾을 수 없습니다.")
        return

    print(f"\n{len(articles)}건 발견:")
    for a in articles:
        print(f"  ID={a['id']} | {a['created_at']} | published={a['is_published']} | score={a['score']} | {a['title_ko']}")

    # 중복 쌍 확인 — 최신(높은 ID)은 유지, 구버전(낮은 ID)은 미발행
    if len(articles) < 2:
        print("\n중복 기사가 없습니다.")
        return

    # created_at 기준으로 정렬 (최신 = 0번)
    articles_sorted = sorted(articles, key=lambda x: x['created_at'], reverse=True)
    keep = articles_sorted[0]   # 최신 기사 유지
    dups = articles_sorted[1:]  # 나머지는 미발행

    print(f"\n[유지] ID={keep['id']}: {keep['title_ko']}")
    for dup in dups:
        print(f"[미발행 처리] ID={dup['id']}: {dup['title_ko']}")
        ok = unpublish(dup['id'])
        print(f"  → {'✅ 완료' if ok else '❌ 실패'}")

    print("\n완료. 라이브 페이지에서 확인하세요.")

if __name__ == "__main__":
    run()
