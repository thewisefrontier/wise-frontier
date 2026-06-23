"""
fix_multitopic_v2.py
--------------------
확실한 복수주제 혼합 기사 미발행 처리.
국가 수 기준이 아닌 제목 패턴 + 수동 확인된 ID 기반.
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

# 확실한 복수주제 혼합 기사 (수동 확인)
JUNK_IDS = [
    7118,  # 태국 투자 73% 급증 속 아프리카 전기차 시장 투자 확대
    7033,  # 글로벌 기술 기업 인력 감축 속 태국·필리핀 등 아시아 시장
    7078,  # 미·중 무역 갈등 속 유럽 디지털 주권 강화
    7079,  # 베트남-프랑스 ODA 협력 강화 및 아시아 금융·투자 시장 동향
    7035,  # 글로벌 자원 및 모빌리티 시장 요동
    7026,  # 미얀마 국경 무역 재개와 인접국 인프라 확충
    7115,  # 에티오피아 감벨라 난민 캠프 인도적 위기 심화와 케냐 항공 확장
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
