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

# 7074 (이전 버전) 미발행, 7106 (최신, 더 풍부한 내용) 유지
r = requests.patch(
    f"{SUPABASE_URL}/rest/v1/articles?id=eq.7074",
    headers=headers,
    json={"is_published": False},
    timeout=15
)
print(f"✅ id=7074 미발행" if r.status_code in (200,204) else f"❌ {r.status_code}")
