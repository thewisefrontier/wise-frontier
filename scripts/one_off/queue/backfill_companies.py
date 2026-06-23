"""
backfill_companies.py
---------------------
기존 NewsFinal 기사에서 기업 소급 등록.
detect_and_register_companies()를 기존 기사에 적용.
"""
import os, sys, time

# GitHub Actions에서 scripts/ 폴더를 path에 추가
_scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
sys.path.insert(0, _scripts_dir)

import requests
from dotenv import load_dotenv
load_dotenv()

# gemini_writer에서 필요한 함수들 임포트
from gemini_writer import (
    detect_and_register_companies,
    _sb_headers, _sb_url, now_kst, GEMINI_API_KEYS, CALL_INTERVAL
)

BATCH = 100       # 1회 처리 기사 수
MAX_TOTAL = 500   # 전체 최대 처리 수 (Gemini 한도 고려)

def get_own_articles(limit: int) -> list:
    """NewsFinal 자체 기사 중 기업 관련 카테고리 우선"""
    articles = []
    offset = 0
    batch = 200
    while len(articles) < limit:
        res = requests.get(
            _sb_url(),
            headers={**_sb_headers(), "Range": f"{offset}-{offset+batch-1}"},
            params={
                "select": "id,title_ko,summary_ko,country,category",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                # 기업 관련 카테고리 우선 — 산업·기업, 경제, 금융
                "or": "(category.eq.산업·기업,category.eq.경제,category.eq.금융)",
                "order": "created_at.desc",
            },
            timeout=20
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

    # 나머지 카테고리 기사도 추가
    if len(articles) < limit:
        res2 = requests.get(
            _sb_url(),
            headers={**_sb_headers(), "Range": f"0-{limit-len(articles)-1}"},
            params={
                "select": "id,title_ko,summary_ko,country,category",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "not.or": "(category.eq.산업·기업,category.eq.경제,category.eq.금융)",
                "order": "created_at.desc",
            },
            timeout=20
        )
        if res2.status_code in (200, 206):
            existing_ids = {a["id"] for a in articles}
            articles.extend(a for a in res2.json() if a["id"] not in existing_ids)

    return articles[:limit]


def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return

    print(f"[기업 소급 등록] 최대 {MAX_TOTAL}건 기사 처리 시작...")
    articles = get_own_articles(MAX_TOTAL)
    print(f"  대상 기사: {len(articles)}건")

    registered = 0
    for i, a in enumerate(articles):
        title = a.get("title_ko") or ""
        body  = a.get("summary_ko") or ""
        country = a.get("country") or ""

        if not title:
            continue

        print(f"[{i+1}/{len(articles)}] {title[:50]}")
        try:
            detect_and_register_companies(title, body, country)
        except Exception as e:
            print(f"  ⚠️ 오류: {e}")

        # API 한도 준수
        if i < len(articles) - 1:
            time.sleep(CALL_INTERVAL)

    print(f"\n✅ 기업 소급 등록 완료")


if __name__ == "__main__":
    run()
