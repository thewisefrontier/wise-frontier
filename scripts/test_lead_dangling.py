# -*- coding: utf-8 -*-
"""리드 누락(인용/직함 단독으로 시작) 회귀 테스트(2026-10-04, id=297527: 리드 주어였던 이름이
문장삭제 보정으로 지워져 "장관은 이번 사태가…"로 시작하는 리드 없는 기사가 발행됨).
실행: python scripts/test_lead_dangling.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import fabrication_guard as fg  # noqa: E402
import script_leak as sl  # noqa: E402
import style_guard as sg  # noqa: E402
import trend_gate as tg  # noqa: E402

assert sg.lead_is_dangling("장관은 이번 사태가 과거와 같은 광범위한 성격을 띠지 않고") is True
assert sg.lead_is_dangling("그는 정점이 지났다고 강조했다.") is True
assert sg.lead_is_dangling('콩고민주공화국 보건장관이 2일 기자회견에서 밝혔다.') is False
assert sg.lead_is_dangling("이 대통령은 7일 초대 중수청장 후보로 지명했다.") is False  # 의도적 제외(오탐 방지)
print("lead_is_dangling ok")

hits = sl.detect_script_leak("콩고민주공화국 에bola 바이러스 확산 진정세", "")
assert any(h[0] == "표기혼입" for h in hits), hits
assert sl.detect_script_leak("오픈AI가 챗GPT를 공개했다", "iPhone을 샀다") == []
print("script_leak 표기혼입 ok")

# 리드 주어 이름이 걸리면 보정 불가(None), 둘째 문장 이름만 걸리면 보정 성공
body_lead_name = ("캄바 장관은 2일 기자회견에서 신규 감염이 크게 줄었다고 밝혔다. 그는 정점이 지났다고 강조했다.\n\n"
                   + "당국은 180일간 12억달러를 투입하는 대응 계획을 가동한다. " * 15)
assert fg.drop_flagged_sentences(body_lead_name, "제목", "[위키 미확인] 캄바", min_len=100, max_drop=5) is None

body_second_sentence = ("네팔 북부에서 폭우로 25명이 숨졌다. 부상자는 트리부반 대학교 부속 병원으로 옮겨졌다. "
                         "당국은 수색을 이어가고 있다.\n" + "당국은 도로 복구에 나섰다. " * 30)
fixed = fg.drop_flagged_sentences(body_second_sentence, "네팔 폭우 참사",
                                   "[위키 미확인] 트리부반 대학교 부속 병원", min_len=100)
assert fixed and "트리부반" not in fixed and "25명이 숨졌다." in fixed
print("drop_flagged_sentences 리드보호 ok")

b, ex, why = tg.run_trend_gates("t", "장관은 발표했다. 세부 내용이 이어진다.", [],
                                 lambda p, max_tokens=0, start_tier=0: "없음")
assert why.startswith("리드 누락"), why
print("trend_gate 리드 누락 게이트 ok")
