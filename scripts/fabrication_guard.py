"""
scripts/fabrication_guard.py
--------------------------------
생성된 기사 본문에 원본 자료에 없는 고유명사(작품명·인명·지명·기관명)나
수식어가 새로 지어내졌는지 확인하는 공용 로직.

2026-09-08 감사("공용 모듈이 필요한 시스템이 더 있는지 점검해줘")로 발견:
gemini_writer.py/econ_writer.py/frontier_markets_writer.py/daily_digest.py/
oil_price_writer.py/opinet_price_writer.py/opinet_weekly_writer.py 7개
파일에 이름만 같은 verify_no_fabricated_names()가 각자 복붙돼 있었고,
실제로 서로 다른 버전으로 갈라져 있었다 — gemini_writer.py만 2026-08-25
id=98010 실사고(수식어 날조) 이후 "[2. 수식어 날조]" 탐지가 추가됐는데
나머지 6개는 2026-08-16 id=79327(작품명 오역) 시점 버전에 멈춰 있었다.
article_image.py·style_guard.py와 같은 이유로 가장 발전된(gemini_writer.py)
버전을 기준으로 공용화한다.

call_gemini_fn 인자로 각 스크립트 자신의 call_gemini(prompt, max_tokens=...,
start_tier=...) 래퍼를 주입받는다(article_image.py·style_guard.py와 동일
패턴 — 스크립트마다 GeminiClient 인스턴스·API 키 세트가 다르기 때문).
wikipedia_confirms()는 순수 HTTP 조회라 주입이 필요 없다.

사용:
    from fabrication_guard import verify_no_fabricated_names
    suspect = verify_no_fabricated_names(source_prompt, body, call_gemini)
"""

import requests

try:
    from rapidfuzz import fuzz
except Exception:
    fuzz = None


def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
    """이름과 충분히 비슷한 위키 문서 제목이 하나라도 있으면 True (결정론적 조회)."""
    name = (name or "").strip()
    if not name or fuzz is None:
        return False
    titles = []
    for lang in ("ko", "en"):
        try:
            res = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": name, "limit": 3, "namespace": 0, "format": "json"},
                headers={"User-Agent": "NewsFinal-EntityCheck/1.0 (+https://newsfinal.co.kr)"},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if len(data) >= 2 and isinstance(data[1], list):
                    titles.extend(data[1])
        except Exception:
            continue
    return any(fuzz.token_sort_ratio(name, t) >= threshold for t in titles)


def extract_candidate_names(body: str, call_gemini_fn) -> list:
    """판단이 아니라 단순 추출 — LLM 위험도가 낮은 작업이라 위키 조회 대상을 뽑는 데만 쓴다."""
    if not body:
        return []
    prompt = f"""아래 기사 본문에서 실제 존재 여부를 확인해볼 만한 구체적 고유명사를 추출하세요.
영화·도서·게임 등 작품명, 특정 인물 실명, 특정 기관·단체·기업명만 대상으로 합니다.
국가명·일반 지명(도시·나라)이나 흔한 일반명사·직함은 제외하세요.
쉼표로 구분해 나열만 하세요(설명 금지). 대상이 없으면 "없음"이라고만 답하세요.

본문:
{body[:2000]}

답변:"""
    result = call_gemini_fn(prompt, max_tokens=150, start_tier=3)
    if not result:
        return []
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return []
    return [n.strip() for n in result.split(",") if n.strip() and len(n.strip()) >= 2][:15]


def verify_no_fabricated_names(source_prompt: str, body: str, call_gemini_fn) -> str:
    """생성된 본문에 원문 자료에 없는 고유명사(작품명·인명·지명·기관명)가 새로 등장했는지 확인.
    두 신호를 같이 쓴다: ① 원본 자료 대조(Gemini 판단, 기존 방식) ② 위키피디아 독립 조회
    (판단이 아닌 단순 추출 + 결정론적 HTTP 조회 — Gemini가 오판해도 이 신호는 별개로 남는다.
    "LLM 혼자만의 판단은 위험하다" 2026-08-16 피드백 반영).
    실사고(2026-08-16, id=79327): 영화 "Brand New Day"를 "유니온 오브 어 뉴 데이"로
    완전히 잘못 옮김 — 작품 제목은 인명·지명과 달리 음차 규칙이 커버하지 않던 영역이라
    Gemini가 자기 사전지식으로 그럴듯한 제목을 지어냈다. 문제 없으면 빈 문자열, 의심되면
    이름 목록 반환."""
    if not body:
        return ""
    check_prompt = f"""아래는 기사 작성에 쓰인 원본 자료와, 그걸 바탕으로 생성된 한국어 기사 본문입니다.
다음 두 가지 유형의 오류를 확인하세요.

[1. 이름 바꿔치기] 기사 본문에 나오는 고유명사(영화·도서·게임 등 작품명, 인명, 지명,
기관명)가 원본 자료에 실제로 근거하는지 확인하세요. 정상적인 한글 음차나 공식 번역명은
문제가 아닙니다 — 원본 자료에 등장하는 대상을 다른 이름으로 완전히 잘못 지어낸 경우만
찾으세요.

[2. 수식어 날조] 실존하는 일반명사·집단명·직함(예: "아디바시", "원주민", "노동자") 앞에
원본 자료에 없는 수식어나 설명을 새로 만들어 붙인 경우를 찾으세요(예: 원문에 그냥
"Adivasis"라고만 나오는데 본문에 "PreferredSource Adivasis"처럼 없는 수식어가 붙은
경우 — 2026-08-25 id=98010 실사고). 명사 자체는 실존해도 그 앞에 붙은 꾸밈말이
원본에 없으면 이 유형입니다.

두 유형 다 있으면 "[이름] 지어낸표기 → 원본표기" 또는 "[수식어] 지어낸표기 → 원본표기
(수식어 삭제 후 표기)" 형식으로 쉼표 구분해 나열하세요. 없으면 "없음"이라고만 답하세요.

[원본 자료]
{source_prompt[:3000]}

[생성된 기사 본문]
{body[:2000]}

답변:"""
    result = call_gemini_fn(check_prompt, max_tokens=150, start_tier=3)
    suspect = ""
    if result:
        result = result.strip()
        if result and not ("없음" in result and len(result) <= 12):
            suspect = result

    unconfirmed = [n for n in extract_candidate_names(body, call_gemini_fn) if not wikipedia_confirms(n)]
    if unconfirmed:
        note = "[위키 미확인] " + ", ".join(unconfirmed)
        suspect = (suspect + "\n" + note) if suspect else note

    return suspect
