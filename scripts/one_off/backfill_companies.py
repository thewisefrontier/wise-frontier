"""
backfill_companies.py
---------------------
기존 NewsFinal 기사에서 기업 소급 등록.
실행: python scripts/one_off/backfill_companies.py
1회 실행당 50건만 처리 (8초 간격 = 약 7분)
"""
import os, sys, time, requests
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from gemini_writer import (
    detect_and_register_companies,
    _sb_headers, _sb_url, GEMINI_API_KEYS, CALL_INTERVAL
)

MAX_TOTAL = 50  # 1회 50건 — 약 7분

def get_own_articles(limit):
    res = requests.get(
        _sb_url(),
        headers={**_sb_headers(), "Range": f"0-{limit-1}"},
        params={
            "select": "id,title_ko,summary_ko,country,category",
            "source": "eq.NewsFinal",
            "is_published": "eq.true",
            "or": "(category.eq.산업·기업,category.eq.경제,category.eq.금융)",
            "order": "created_at.desc",
        },
        timeout=20
    )
    if res.status_code in (200, 206):
        return res.json()
    return []

def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return

    articles = get_own_articles(MAX_TOTAL)
    print(f"[기업 소급 등록] {len(articles)}건 처리 시작...")

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
