# -*- coding: utf-8 -*-
"""외부 트렌드 경로(run_external_trend_articles)의 날짜 표기 하드 블록 회귀 테스트
(2026-09-30, id=294859/294908 — 원문 없이 지어낸 날짜가 검증도 안 되고 중복 판정도 무력화시킨 사고).
실행: python scripts/test_extrend_date_block.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import gemini_summarizer as gs  # noqa: E402

assert gs.extract_local_time_marks("4일(현지시간)부터 아프리카 전역에서 열렸다.")
assert gs.extract_local_time_marks("나이지리아는 9일(현지시간) 예선전을 치른다.")
assert gs.extract_local_time_marks("이번 예선전이 지역 경제에 관심을 모으고 있다.") == []
print("extrend 날짜 하드블록 테스트 ok")
