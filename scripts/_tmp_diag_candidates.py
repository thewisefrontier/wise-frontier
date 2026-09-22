"""
일회성 진단 스크립트 — gemini_writer.py의 get_today_articles()가 실제로는
15,528건이 매칭되는데도 40건만 반환한 이유를 CI 환경(진짜 서비스 키)에서
재현한다. 진단 후 이 파일과 워크플로우는 삭제한다.
"""
import os
import requests
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
def now_kst():
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def sb_url():
    return f"{SUPABASE_URL}/rest/v1/articles"

def sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

since = (now_kst() - timedelta(hours=96)).strftime("%Y-%m-%d %H:%M")
print("since:", since, "now_kst:", now_kst())

# gemini_writer.py와 똑같은 요청 재현
res = requests.get(
    sb_url(),
    headers={**sb_headers(), "Range": "0-499"},
    params={
        "select": "id,created_at,source",
        "sent_telegram": "eq.1",
        "source": "neq.NewsFinal",
        "created_at": f"gte.{since}",
        "order": "score.desc,created_at.desc",
    },
    timeout=30,
)
print("status:", res.status_code)
print("Content-Range:", res.headers.get("Content-Range"))
print("all response headers:", dict(res.headers))
data = res.json()
print("returned rows:", len(data))
if isinstance(data, list) and data:
    print("first:", data[0])
    print("last:", data[-1])
else:
    print("raw body[:500]:", res.text[:500])
