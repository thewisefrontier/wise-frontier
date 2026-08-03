"""카테고리 정규화 가드.

Gemini가 반환한 `분야`/`category` 값을 사이트가 아는 정규 카테고리로 강제한다.
검증이 없으면 `글ローバル`(가타카나 혼입), `경제/금융`(프롬프트 구분자 오독),
`정치·외교, 보건, 경제`(복합 나열) 같은 값이 그대로 DB에 저장된다.

패턴 B(공통 유틸 분리) — 소비자 쪽은 try/except import 폴백으로 감싼다.
"""

import re
import unicodedata

# 사이트 네비게이션 7종 + writer 프롬프트가 추가로 허용하는 2종
CANON = (
    "경제", "금융", "자원·에너지", "산업·기업",
    "정치·외교", "사회", "IT·과학",
    "문화·예술", "글로벌",
)

# 시스템이 직접 지정하는 카테고리 — 정규화 대상 아님
PASSTHROUGH = ("날씨", "다이제스트", "브리핑")

ALIASES = {
    "세계": "글로벌", "국제": "글로벌", "글로벌경제": "글로벌", "월드": "글로벌",
    "정치": "정치·외교", "외교": "정치·외교", "안보": "정치·외교",
    "국방": "정치·외교", "행정": "정치·외교", "법률": "정치·외교",
    "보건": "사회", "의료": "사회", "환경": "사회", "교육": "사회",
    "사건사고": "사회", "노동": "사회", "인권": "사회", "스포츠": "사회",
    "문화": "문화·예술", "예술": "문화·예술", "연예": "문화·예술", "관광": "문화·예술",
    "자원": "자원·에너지", "에너지": "자원·에너지", "원자재": "자원·에너지",
    "산업": "산업·기업", "기업": "산업·기업", "제조": "산업·기업",
    "IT": "IT·과학", "과학": "IT·과학", "기술": "IT·과학", "테크": "IT·과학",
    "증권": "금융", "은행": "금융", "투자": "금융",
    "무역": "경제", "통상": "경제",
}

_SPLIT = re.compile(r"[,/·|;：:＋+&()\[\]]+")
_STRIP = " \t\r\n*#-—–\"'`（）()[]"


def _resolve(token: str) -> str:
    """단일 토큰을 정규 카테고리로. 못 찾으면 빈 문자열."""
    if not token:
        return ""
    if token in CANON:
        return token
    if token in ALIASES:
        return ALIASES[token]

    # 비한글·비영문 문자(가타카나 등) 제거 후 재시도
    han = re.sub(r"[^가-힣A-Za-z]", "", token)
    if not han:
        return ""
    if han in CANON:
        return han
    if han in ALIASES:
        return ALIASES[han]

    # 접두 매칭 — '글'→글로벌, '글로벌경제'→글로벌
    for c in CANON:
        flat = c.replace("·", "")
        if flat.startswith(han) or han.startswith(flat):
            return c
    for a, c in ALIASES.items():
        if a.startswith(han) or han.startswith(a):
            return c
    return ""


def normalize_category(raw, default: str = "글로벌") -> str:
    """Gemini 출력 카테고리를 정규 카테고리로 강제한다.

    빈 값이 들어오면 빈 문자열을 그대로 돌려준다(호출부의 기존 폴백 유지).
    해석 불가한 값만 `default`로 떨어진다.
    """
    if raw is None:
        return ""
    v = unicodedata.normalize("NFC", str(raw)).strip(_STRIP).strip()
    if not v:
        return ""
    if v in PASSTHROUGH or v in CANON:
        return v

    hit = _resolve(v)
    if hit:
        return hit

    # 복합 나열 — 앞쪽 토큰 우선 ("정치·외교, 보건, 경제" → 정치·외교)
    for part in _SPLIT.split(v):
        part = part.strip(_STRIP).strip()
        if not part:
            continue
        hit = _resolve(part)
        if hit:
            return hit

    return default
