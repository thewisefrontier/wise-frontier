"""
writer_rules 프롬프트를 최신 버전으로 업데이트
"""
import os, requests
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

NEW_WRITER_RULES = """[주의사항]
- 본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
- 마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
- 매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 포함하지 마세요.
- 날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- 기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다" 같은 논평/칼럼 문체는 금지입니다.
아래 형식으로 출력:
제목: (핵심을 담은 제목)
국가: (기사의 핵심 주체가 되는 국가 1개. 어느 나라 기업/정부/기관이 주체인가 기준. 글로벌 기업·국제기구가 주체면 "없음")
관련국가: (기사에서 유의미하게 다뤄지는 국가들. 쉼표로 구분, 최대 4개. 없으면 "없음". 예: 인도, 나이지리아)
분야: (다음 중 가장 적합한 하나를 선택)
  - 경제: 거시경제, 무역, GDP, 통화정책, 인플레이션 등 국가 경제 전반
  - 금융: 은행, 증권, 투자, 환율, 핀테크
  - 자원·에너지: 광업, 채굴, 석유, 가스, 전력, 원자재, 재생에너지
  - 산업·기업: 제조업, 기업 실적, 산업 정책, 공급망
  - 정치·외교: 정부, 선거, 국제관계, 정책, 외교
  - 사회: 인프라, 교육, 보건, 노동, 인구
  - IT·과학: 기술, 통신, 연구개발, 우주산업
  - 글로벌: 특정 국가에 국한되지 않는 세계적 이슈 (단, 위 카테고리로 분류 가능하면 해당 카테고리 우선)
본문: (기사 본문)"""

def update():
    # 기존 버전 비활성화
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/prompts?name=eq.writer_rules",
        headers=_headers(),
        json={"is_active": False}
    )
    print(f"기존 비활성화: {res.status_code}")

    # 새 버전 저장
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/prompts",
        headers={**_headers(), "Prefer": "return=representation"},
        json={"name": "writer_rules", "content": NEW_WRITER_RULES, "version": 3, "is_active": True}
    )
    if res.status_code in (200, 201):
        print("✅ writer_rules v3 저장 완료")
    else:
        print(f"❌ 실패: {res.text[:100]}")

if __name__ == "__main__":
    update()
