"""카테고리 정규화 가드.

Gemini가 반환한 `분야`/`category` 값을 사이트가 아는 정규 카테고리로 강제한다.
검증이 없으면 `글ローバル`(가타카나 혼입), `경제/금융`(프롬프트 구분자 오독),
`정치·외교, 보건, 경제`(복합 나열) 같은 값이 그대로 DB에 저장된다.

패턴 B(공통 유틸 분리) — 소비자 쪽은 try/except import 폴백으로 감싼다.
"""

import re
import unicodedata

# 사이트 네비게이션 8종(책·미술은 2026-10-04 문화·예술로 통합)
CANON = (
    "경제", "금융", "자원·에너지", "산업·기업",
    "정치·외교", "사회", "IT·과학",
    "문화·예술", "글로벌", "스포츠",
)

# 시스템이 직접 지정하는 카테고리 — 정규화 대상 아님
PASSTHROUGH = ("날씨", "다이제스트", "브리핑")

ALIASES = {
    "세계": "글로벌", "국제": "글로벌", "글로벌경제": "글로벌", "월드": "글로벌",
    "정치": "정치·외교", "외교": "정치·외교", "안보": "정치·외교",
    "국방": "정치·외교", "행정": "정치·외교", "법률": "정치·외교",
    "보건": "사회", "의료": "사회", "환경": "사회", "교육": "사회",
    "사건사고": "사회", "노동": "사회", "인권": "사회",
    "문화": "문화·예술", "예술": "문화·예술", "연예": "문화·예술", "관광": "문화·예술",
    "미술": "문화·예술", "책": "문화·예술", "도서": "문화·예술", "문학": "문화·예술", "영화": "문화·예술",
    "자원": "자원·에너지", "에너지": "자원·에너지", "원자재": "자원·에너지",
    "산업": "산업·기업", "기업": "산업·기업", "제조": "산업·기업",
    "IT": "IT·과학", "과학": "IT·과학", "기술": "IT·과학", "테크": "IT·과학",
    "증권": "금융", "은행": "금융", "투자": "금융",
    "체육": "스포츠", "축구": "스포츠", "올림픽": "스포츠",
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


# ── 스포츠 판별(2026-09-28) ──────────────────────────────────────────
# gemini_writer 클러스터 단계의 하루 상한·희소성 정렬용. 원제(title_en — 실제론 원어 제목)는
# 단어경계로, 구글 번역 제목(title_ko)은 스포츠 외 의미가 거의 없는 말만 본다.
# 실측 샘플(9/28 24시간 수집분)에서 뺀 오탐: nba(나이지리아 변호사협회), caf(중남미개발은행),
# copa(코파항공), marathon/마라톤(마라톤 협상), grand prix/그랑프리(칸 영화제), striker(단식 투쟁자),
# 경기(경기 침체)·감독(금융감독)·선수·대표팀(협상 대표팀)·사이클(경기 사이클)·육상(육상 풍력).
_SPORTS_EN = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in (
    "football", "soccer", "fútbol", "futebol", "cricket", "basketball", "rugby", "tennis", "golf",
    "athletics", "olympic", "olympics", "paralympics", "asian games", "world cup", "cup final",
    "premier league", "champions league", "nations league", "la liga", "serie a", "bundesliga", "ligue 1",
    "fifa", "uefa", "afcon", "concacaf", "conmebol", "nfl", "mlb", "nhl", "ipl", "t20", "odi",
    "formula 1", "formula one", "f1", "motogp", "boxing", "ufc", "midfielder", "goalkeeper", "hat-trick",
    "wicket", "volleyball", "handball", "hockey", "baseball", "cycling", "sumo",
    "semi-final", "semifinal", "quarter-final", "quarterfinal", "grand slam", "gold medal", "matchday",
    "super eagles", "bafana bafana", "black stars", "all blacks", "springboks", "wallabies",
)) + r")s?\b", re.IGNORECASE)
_SPORTS_KO = re.compile("|".join(re.escape(w) for w in (
    "축구", "크리켓", "럭비", "테니스", "골프", "농구", "야구", "배구", "핸드볼", "하키",
    "올림픽", "패럴림픽", "월드컵", "아시안게임", "네이션스리그", "챔피언스리그", "프리미어리그",
    "네이션스컵", "복싱", "해트트릭", "골키퍼", "미드필더", "결승골", "득점", "구단", "무승부",
    "승부차기", "준결승", "결승전", "8강", "16강", "금메달", "국가대표", "경기장",
)))


def is_sports_title(title_en: str = "", title_ko: str = "") -> bool:
    return bool(_SPORTS_EN.search(title_en or "") or _SPORTS_KO.search(title_ko or ""))


def is_sports_cluster(cluster) -> bool:
    """구성원(검토필요 표시 제외) 절반 이상이 스포츠 제목이면 스포츠 클러스터."""
    members = [a for a in cluster if not a.get("__needs_review__")]
    hits = sum(1 for a in members if is_sports_title(a.get("title_en"), a.get("title_ko")))
    return hits > 0 and hits * 2 >= len(members)


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
