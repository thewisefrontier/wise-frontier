# -*- coding: utf-8 -*-
"""fabrication_guard.proofread_fix 회귀 테스트(2026-10-12). 실행: python scripts/test_proofread.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import fabrication_guard as fg  # noqa: E402

body = "그는 보푸로스 전쟁을 그렸다. 2026년 10월 8일 별세했다. 모니카 바르바로나 미라 역을 맡았다. 영화 아티피셜이 개봉했다."
text = "제목\n" + body

# 기계 필터: 숫자 중간에서 잘린 구간·원어 되돌리기·없던 숫자 주입은 버린다(2026-10-12 시험의 오탐 유형).
assert fg._safe_fix("보푸로스 전쟁", "보불전쟁", text)
assert not fg._safe_fix("년 10월 8일", "2025년 10월 8일", text), "숫자 중간 구간"
assert not fg._safe_fix("아티피셜", "Artificial", text), "한글 음차→원어"
assert not fg._safe_fix("별세했다", "2025년 별세했다", text), "없던 숫자"

# 두 모델이 동의한 항목만 치환. NVIDIA가 3건 지적 → 1건은 기계 필터, 1건은 Gemini 거부, 1건만 반영.
fg.call_nvidia = lambda p, max_tokens=500: ("보푸로스 전쟁 => 보불전쟁 | 음차\n"
                                            "년 10월 8일 => 2025년 10월 8일 | 연도\n"
                                            "모니카 바르바로나 => 모니카 바르바로가 | 조사")
gem = lambda p, max_tokens, start_tier: "1: Y\n2: N"
t, b, applied = fg.proofread_fix("제목", body, gem)
assert applied == [("보푸로스 전쟁", "보불전쟁")], applied
assert "보불전쟁" in b and "바르바로나" in b and "2026년 10월 8일" in b

# 지적이 본문에 없는 글자면(검수기가 지어낸 지적) 버린다.
fg.call_nvidia = lambda p, max_tokens=500: "없는표현 => 다른표현 | ?"
assert fg.proofread_issues("제목", body) == []

# NVIDIA·Gemini 실패 시 원문 그대로(fail-open).
fg.call_nvidia = lambda p, max_tokens=500: None
assert fg.proofread_fix("제목", body, gem) == ("제목", body, [])
fg.call_nvidia = lambda p, max_tokens=500: "보푸로스 전쟁 => 보불전쟁 | 음차"
assert fg.proofread_fix("제목", body, lambda *a, **k: None) == ("제목", body, [])

print("test_proofread OK")
