# -*- coding: utf-8 -*-
"""date_guard.check_date_hallucination 회귀 테스트.

id=298369 실사고(2026-10-05): 소스(TechCrunch 팟캐스트 기사)에 날짜 근거가
전혀 없는데("this week"/"this weekend"뿐) Gemini가 "5일(현지시간)"을 써버렸고,
base_date(KST 생성일)와 day가 우연히 일치해 gap=0으로 통과됐다(실제 미국
현지는 생성 시점 기준 아직 전날). 소스에 날짜 근거가 하나도 없으면 gap<=3
관용을 적용하지 않도록 고친 수정의 회귀 테스트.
"""

from datetime import date

from date_guard import check_date_hallucination

# 200자 미달이면 "판정 불가 → 통과" 분기로 빠지므로, 날짜 근거 없는 긴 텍스트
_NO_DATE_SOURCE_TEXT = (
    "President Donald Trump hosted many of the biggest names in artificial "
    "intelligence this week to announce a new initiative. The effort "
    "continued this weekend with further details. Analysts discussed the "
    "motivation behind the meeting and what effect it might have on the "
    "industry going forward, noting the rebranding effort was significant "
    "even without firm commitments attached to it."
)


def test_fabricated_date_with_zero_source_evidence_blocked():
    """소스에 날짜 근거가 전혀 없으면, base_date와 우연히 같은 날짜도 차단."""
    body = "도널드 트럼프 미국 대통령은 5일(현지시간) 새 기구를 창설했다고 밝혔다."
    sources = [{"full_text": _NO_DATE_SOURCE_TEXT, "title_en": "", "summary_en": ""}]
    bad, reason = check_date_hallucination(body, sources, base_date=date(2026, 10, 5))
    assert bad, "날짜 근거가 전혀 없는데도 통과됐다"
    assert "5일" in reason


def test_date_with_source_evidence_still_passes():
    """발행일 등 근거가 있으면 기존 관용(과거 3일 이내)은 그대로 유지."""
    body = "도널드 트럼프 미국 대통령은 5일(현지시간) 새 기구를 창설했다고 밝혔다."
    sources = [{"full_text": _NO_DATE_SOURCE_TEXT, "source_published_at": "2026-10-04T10:00:00+00:00"}]
    bad, reason = check_date_hallucination(body, sources, base_date=date(2026, 10, 5))
    assert not bad, reason


def test_past_marker_still_passes_without_evidence():
    """'지난' 같은 과거명시어가 붙으면 근거 없어도 기존처럼 통과(가설적 표현이 아니라 명시적 헤지)."""
    body = "도널드 트럼프 미국 대통령은 지난 5일(현지시간) 새 기구를 창설했다고 밝혔다."
    sources = [{"full_text": _NO_DATE_SOURCE_TEXT}]
    bad, reason = check_date_hallucination(body, sources, base_date=date(2026, 10, 5))
    assert not bad, reason


if __name__ == "__main__":
    test_fabricated_date_with_zero_source_evidence_blocked()
    test_date_with_source_evidence_still_passes()
    test_past_marker_still_passes_without_evidence()
    print("OK")
