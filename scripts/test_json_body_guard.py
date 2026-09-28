# -*- coding: utf-8 -*-
"""json_body_guard.unwrap_json_body 및 gemini_writer.py/gemini_summarizer.py의 import-실패
폴백 회귀 테스트(2026-09-29, id=293932 raw JSON 본문 leak 실사고).
실행: python scripts/test_json_body_guard.py"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import json_body_guard as g

# 정상 본문(JSON 아님) -> None(변경 불필요)
assert g.unwrap_json_body("보통 기사 본문입니다.") is None
assert g.unwrap_json_body("") is None
assert g.unwrap_json_body(None) is None

# 정상 top-level JSON -> body만 추출
leaked = ('{"title": "제목", "country": "한국", "body": "실제 본문 첫 문장.\\n\\n둘째 문단.", '
          '"summary_3lines": "a"}')
assert g.unwrap_json_body(leaked) == "실제 본문 첫 문장.\n\n둘째 문단."

# body 필드 안에 JSON이 중첩(2026-08-04 실사고 패턴) -> 재귀적으로 벗김
nested = '{"title": "t", "body": "{\\"title\\": \\"t2\\", \\"body\\": \\"진짜 본문\\"}"}'
assert g.unwrap_json_body(nested) == "진짜 본문"

# body 필드가 비어있으면 복구 불가 -> ""(저장 차단)
assert g.unwrap_json_body('{"title": "t", "body": ""}') == ""
print("json_body_guard 단위 테스트 ok")


# import 실패 시 폴백 스텁 동작(gemini_writer.py/gemini_summarizer.py에 복붙된 패턴) —
# 원본 모듈 없이도 뻔한 raw JSON 본문은 fail-closed로 막아야 한다(그냥 통과시키면 안 됨).
def _fallback(text, _depth=0):
    s = str(text or "").strip()
    return "" if s.startswith("{") and ('"body"' in s[:800] or '"본문"' in s[:800]) else None


assert _fallback(leaked) == ""            # raw JSON은 모듈 없이도 차단
assert _fallback("보통 기사 본문입니다.") is None  # 정상 본문은 안 건드림
assert _fallback("{안 닫힌 JSON") is None      # body 키가 없으면 모듈 없이는 판단 불가 -> 통과(모듈 재도입 전까지의 한계)
print("import 실패 폴백(fail-closed) 단위 테스트 ok")
