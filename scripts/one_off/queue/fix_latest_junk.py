import os, sys, requests
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
headers = {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "Content-Type": "application/json"}

# 오늘 생성된 글로벌 묶음 엉터리 기사
for id_ in [7300]:
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/articles?id=eq.{id_}", headers=headers, json={"is_published": False}, timeout=15)
    print(f"✅ id={id_} 미발행" if r.status_code in (200,204) else f"❌ {r.status_code}")
