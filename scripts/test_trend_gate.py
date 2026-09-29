# -*- coding: utf-8 -*-
"""trend_gate.run_trend_gates 회귀 테스트(2026-09-29 — 트렌드 기사에 일반 기사 검증 이식). 실행: python scripts/test_trend_gate.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import fabrication_guard as fg  # noqa: E402
import trend_gate as tg  # noqa: E402

fg.wikipedia_confirms = lambda n, threshold=70: False      # 위키 조회는 항상 미확인으로(외부 호출 차단)
fg.call_nvidia = None                                       # 원문 재확인 없음 → 미확인 이름 그대로 남김

body = ("엠폭스 환자가 12,500명으로 늘었다. 보건당국은 방역을 강화했다. " * 6 + "\n\n"
        "부상자는 트리부반 대학교 부속 병원으로 옮겨졌다. 당국은 수색을 이어가고 있다. " + "확산 경로 조사가 이어졌다. " * 30)
srcs = [{"title_en": "Mpox", "summary_en": "mpox cases rise"}]

# 1) 미번역 외국어 → 미발행 사유
llm = lambda p, max_tokens=0, start_tier=0: "kızamık" if "번역 안 된 외국어" in p else "없음"
b, ex, why = tg.run_trend_gates("t", body, srcs, llm, extras=("a", "b"))
assert why.startswith("번역 누락") and "kızamık" in why, why
assert "12,500" not in b and "1만2500" in b          # 숫자 콤마 제거는 항상 적용

# 2) 근거 없는 이름 문장만 삭제 후 통과 + 3줄요약/투자아이디어에 이름 남으면 비움
def llm2(p, max_tokens=0, start_tier=0):
    if "번역 안 된 외국어" in p:
        return "없음"
    if "이름 바꿔치기" in p:
        return "없음"
    return "트리부반 대학교 부속 병원"                   # 고유명사 추출
b, ex, why = tg.run_trend_gates("엠폭스 확산", body, srcs, llm2, extras=("요약", "트리부반 대학교 부속 병원이 언급됨"))
assert why == "" and "트리부반" not in b and ex == ("요약", ""), (why, ex)

# 3) check_names=False(소스 없는 외부 트렌드 경로)면 이름 대조를 건너뜀
b, ex, why = tg.run_trend_gates("t", body, [], llm2, extras=("a", "b"), check_names=False)
assert why == "" and "트리부반" in b
print("trend_gate 테스트 ok")
