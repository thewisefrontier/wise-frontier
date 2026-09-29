# -*- coding: utf-8 -*-
"""script_leak.detect_script_leak의 '편집메모' 유출 검사 회귀 테스트(2026-09-29, id=294288).
실행: python scripts/test_script_leak_meta_note.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from script_leak import detect_script_leak  # noqa: E402

bad = "엠폭스와 kızamık(크자믝·국문 표기: kızamık는 튀르키예어로 홍역을 의미하므로 문맥상 홍역) 등을 포함한"
hits = detect_script_leak("", bad)
assert hits and hits[0][0] == "편집메모", hits
assert detect_script_leak("", "홍역과 엠폭스 등을 포함한 바이러스 단백질 구조가 등재됐다.") == []
assert detect_script_leak("", "레옹 14세(Léon XIV)가 28일(현지시간) 메츠(Metz)를 방문했다.") == []
assert detect_script_leak("", "그는 이 단어를 스페인어로 '길'이라는 뜻으로 쓴다고 설명했다.") == []  # 괄호 밖 정상 문장
print("script_leak 편집메모 테스트 ok")
