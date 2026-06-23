"""
backfill_companies.py
---------------------
기존 NewsFinal 기사에서 기업 소급 등록.
실행: python scripts/one_off/backfill_companies.py
"""
import os, sys, time, requests
from dotenv import load_dotenv
load_dotenv()

# scripts/ 폴더를 path에 추가 (one_off에서 실행 시)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from gemini_writer import (
    detect_and_register_companies,
    _sb_headers, _sb_url, now_kst, GEMINI_API_KEYS, CALL_INTERVAL
)

MAX_TOTAL = 500

def get_own_articles(limit):
    articles = []
    offset = 0
    while len(articles) < limit:
        res = requests.get(
            _sb_url(),
            headers={**_sb_headers(), "Range": f"{offset}-{offset+199}"},
            params={
                "select": "id,title_ko,summary_ko,country,category",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
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
        if len(data) < 200:
            break
        offset += 200
    return articles[:limit]

def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return

    print(f"[기업 소급 등록] 최대 {MAX_TOTAL}건 처리 시작...")
    articles = get_own_articles(MAX_TOTAL)
    print(f"  대상: {len(articles)}건")

    for i, a in enumerate(articles):
        title   = a.get("title_ko") or ""
        body    = a.get("summary_ko") or ""
        country = a.get("country") or ""
        if not title:
            continue
        print(f"[{i+1}/{len(articles)}] {title[:50]}")
        try:
            detect_and_register_companies(title, body, country)
        except Exception as e:
            print(f"  ⚠️ {e}")
        if i < len(articles) - 1:
            time.sleep(CALL_INTERVAL)

    print("✅ 완료")

run()
