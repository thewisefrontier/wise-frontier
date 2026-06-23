"""
중복 라이브 기사 미발행 처리
"""
import os
import requests
from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

# 중복 확인된 구 버전 기사 ID — 최신 버전만 남기고 나머지 미발행
DUPLICATE_IDS = [6491, 6492, 6421, 6355, 6128, 6129]

for id_ in DUPLICATE_IDS:
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/articles?id=eq.{id_}",
        headers=headers,
        json={"is_published": False},
        timeout=15
    )
    if res.status_code in (200, 204):
        print(f"✅ id={id_} 미발행 처리 완료")
    else:
        print(f"❌ id={id_} 실패: {res.status_code} {res.text[:100]}")

print("완료")
