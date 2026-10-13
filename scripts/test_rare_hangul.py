"""비표준 한글 음절 검사 회귀 테스트 (2026-10-13). python scripts/test_rare_hangul.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from script_leak import detect_rare_hangul, _KS_HANGUL  # noqa: E402

assert len(_KS_HANGUL) == 2350
assert [h[0] for h in detect_rare_hangul("", "17세 승마 선수 팕스 워커(Tinks Walker)")] == ["팕"]
assert detect_rare_hangul("", "바이에른주 바트퇼츠-볼프라츠하우젠") == []  # 허용 목록(정상 표기)
assert detect_rare_hangul("브라질 대선 결선", "룰라 대통령과 보우소나루 의원이 초접전") == []
assert detect_rare_hangul("", "") == []
print("test_rare_hangul ok")
