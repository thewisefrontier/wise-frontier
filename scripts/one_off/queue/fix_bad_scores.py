"""
fix_bad_scores.py
-----------------
score >= 2인데 update_log가 1건 이하인 기사 — 잘못된 클러스터링 결과물
전부 미발행 처리.
"""
import os, sys, requests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# score >= 2, update_log 길이 <= 1, NewsFinal 자체 기사
res = requests.get(
    f"{SUPABASE_URL}/rest/v1/articles",
    headers=headers,
    params={
        "select": "id,title_ko,score,update_log,subcategory",
        "source": "eq.NewsFinal",
        "is_published": "eq.true",
        "score": "gte.2",
        "order": "score.desc",
        "limit": "200",
    },
    timeout=30
)

articles = res.json()
bad_ids = []
for a in articles:
    sub = a.get("subcategory") or ""
    if sub.startswith("digest_") or sub.endswith("briefing"):
        continue
    log = a.get("update_log") or []
    if len(log) <= 1:
        bad_ids.append(a["id"])
        print(f"  미발행: id={a['id']} score={a['score']} — {(a['title_ko'] or '')[:50]}")

print(f"\n총 {len(bad_ids)}건 미발행 처리 시작...")

success = 0
for id_ in bad_ids:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/articles?id=eq.{id_}",
        headers=headers,
        json={"is_published": False},
        timeout=15
    )
    if r.status_code in (200, 204):
        success += 1
    else:
        print(f"  ❌ id={id_} 실패: {r.status_code}")

print(f"\n✅ {success}/{len(bad_ids)}건 미발행 처리 완료")
