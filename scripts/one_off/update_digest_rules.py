"""
digest_rules 프롬프트 업데이트 (1회 실행용)
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

NEW_RULES = """[작성 규칙]
- 지난 24시간 동안 NewsFinal이 다룬 프론티어 마켓 기사들을 종합해 오늘의 핵심 테마를 정리하는 일일 다이제스트를 작성하세요.
- 개별 기사를 단순 나열하지 말고, 여러 국가/기사에 걸쳐 공통적으로 나타나는 패턴, 테마, 트렌드를 중심으로 통찰을 제공하세요.
- 예: "이번 주 여러 아프리카 국가에서 통화 평가절하 압력이 동시에 나타남", "동남아 국가들의 외국인직접투자 유치 경쟁 심화" 같은 교차 비교형 분석을 우선하세요.
- 지역별/테마별로 섹션을 나누고, 각 섹션은 불릿(- 로 시작)으로 핵심을 정리하세요.
- 마크다운 문법(**굵게**, ##제목)을 쓰지 말고 일반 텍스트와 줄바꿈, "- " 불릿만 사용하세요.
- 전문 형식 헤더([도시=출처] 등)나 매체 홍보 문구를 넣지 마세요.
- 다룬 기사가 적으면 무리하게 늘리지 말고 있는 그대로 간결하게 작성하세요.
- 한국어로만 작성하세요."""

def update():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[ERROR] 환경변수 없음")
        return
    # 기존 비활성화
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/prompts?name=eq.digest_rules",
        headers=_headers(),
        json={"is_active": False}
    )
    print(f"기존 비활성화: {res.status_code}")

    # 새 버전 저장
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/prompts",
        headers={**_headers(), "Prefer": "return=representation"},
        json={"name": "digest_rules", "content": NEW_RULES, "version": 2, "is_active": True},
    )
    print(f"✅ digest_rules v2 저장" if res.status_code in (200,201,204) else f"❌ {res.text[:150]}")

if __name__ == "__main__":
    update()
