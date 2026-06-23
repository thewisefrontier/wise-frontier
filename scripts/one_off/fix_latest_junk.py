import os, sys, requests
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

# 오늘 생성된 엉터리 기사들
# 7300: 미국-이란 평화 기류 속 국제 유가 하락세와 글로벌 기업들의 AI 전환 가속화
# + 제목으로 추가 검색
res = requests.get(
    f"{SUPABASE_URL}/rest/v1/articles",
    headers=headers,
    params={
        "select": "id,title_ko,created_at",
        "source": "eq.NewsFinal",
        "is_published": "eq.true",
        "created_at": "gte.2026-06-24 00:00",
        "or": "(title_ko.ilike.*글로벌*속*,title_ko.ilike.*공급망 재편*인도주의*,title_ko.ilike.*각국*행보*)",
        "order": "created_at.desc",
        "limit": "20",
    },
    timeout=15
)

junk_ids = [7300]  # 확인된 것
if res.status_code in (200, 206):
    for a in res.json():
        print(f"  발견: id={a['id']} {a['title_ko'][:60]}")
        if a['id'] not in junk_ids:
            junk_ids.append(a['id'])

print(f"\n미발행 처리: {junk_ids}")
success = 0
for id_ in junk_ids:
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

print(f"\n✅ {success}건 완료")
