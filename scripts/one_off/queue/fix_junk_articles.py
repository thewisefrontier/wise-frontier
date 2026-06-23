"""
fix_junk_articles.py
--------------------
엉터리 복수주제 기사 미발행 처리.
fix_bad_scores.py에서 처리 안 된 추가 대상.
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
}

# 확실한 엉터리 기사 ID (글로벌 종합 제목, 복수주제 나열)
JUNK_IDS = [
    7179, 7171, 7170, 7143, 7142, 7140, 7139, 7115, 7113, 7112,
    7111, 7110, 7109, 7081, 7079, 7078, 7076, 7075, 7034, 7033,
    7032, 7031, 7030, 7029, 6979, 6852, 6748, 6741, 6693, 6681,
    6541, 6274, 6273, 6247, 5983, 6012,
    # 방금 확인된 추가분
    7177, 7178, 7172,
]

success = 0
for id_ in JUNK_IDS:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/articles?id=eq.{id_}",
        headers=headers,
        json={"is_published": False},
        timeout=15
    )
    if r.status_code in (200, 204):
        print(f"✅ id={id_} 미발행")
        success += 1
    else:
        print(f"❌ id={id_} 실패: {r.status_code}")

print(f"\n✅ {success}/{len(JUNK_IDS)}건 완료")
