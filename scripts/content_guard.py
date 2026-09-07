"""
content_guard.py — Gemini가 실제 기사 대신 "작성 불가" 응답을 낸 경우 감지

실사고(2026-09-07, id=142060): _has_enough_material()가 full_text 없이도
summary_en만으로 단독기사화 자격을 주는 소스가 있는데, 정작 프롬프트엔
항상 full_text만 넣다 보니 [원문]이 빈 채로 Gemini에 전달된 경우가 있었다.
Gemini는 "원문 정보 부재에 따른 기사 작성 대기"라는 정직한 거부 응답을
냈지만, 그 어떤 검수 단계(verify_single_topic/find_similar_article/
check_date_hallucination/detect_foreign_leftover)도 "이게 애초에 기사가
아니다"는 걸 걸러내지 못해 그대로 발행됐다.

프롬프트/전달 데이터 쪽 근본 원인은 고쳤지만(gemini_writer.py의 solo 경로),
같은 실패 양상(소스가 얇거나 검색 그라운딩이 실패했을 때 Gemini가 거부
응답을 내는 것)은 다른 경로에서도 재발할 수 있어 마지막 방어선으로 둔다.
"""

import re

_PLACEHOLDER_PATTERNS = [
    r"원문.{0,10}(제공되지|주어지지|없어|부재)",
    r"(정보|자료|본문|소스).{0,10}(부족|부재|확인할 수 없)",
    r"작성.{0,5}(대기|보류|어렵)",
    r"추가 정보.{0,10}확인되는 대로",
    r"source (material|text|content).{0,10}(not|isn't|wasn't) (provided|available)",
    r"cannot be (verified|confirmed) as",
    r"unable to (write|draft|generate) (the|an) article",
]

_PLACEHOLDER_RE = re.compile("|".join(_PLACEHOLDER_PATTERNS), re.IGNORECASE)


def is_placeholder_response(title: str, body: str) -> bool:
    """제목·본문이 실제 기사가 아니라 "작성 불가" 거부 응답인지 판정.

    짧은 응답(200자 미만)에서만 판정한다 — 정상 기사 본문 안에 우연히
    "정보가 부족하다"류 문구가 인용구로 들어갈 가능성을 감안해, 진짜
    거부 응답 특유의 짧은 길이를 함께 요구한다."""
    text = f"{title or ''}\n{body or ''}"
    if len(body or "") >= 200:
        return False
    return bool(_PLACEHOLDER_RE.search(text))
