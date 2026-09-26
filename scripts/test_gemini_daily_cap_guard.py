# -*- coding: utf-8 -*-
"""lite_daily_usage_near_cap() 회귀 테스트(2026-09-26 갱신 — 모델별 독립 확인 후
합산 검사를 (모델,키)별 개별 검사로 수정). 실행: python scripts/test_gemini_daily_cap_guard.py"""
import os
import sys

os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
sys.path.insert(0, os.path.dirname(__file__))
import gemini_client as gc  # noqa: E402


class R:
    def __init__(self, code, rows):
        self.status_code, self._rows = code, rows
    def json(self):
        return self._rows


KEYS = ["k1", "k2", "k3", "k4", "k5"]

# 3.5-lite 키1이 80% 넘으면 True(3.1-lite는 여유 있어도 무관)
gc.requests.get = lambda *a, **k: R(200, [
    {"model": "gemini-3.5-flash-lite", "key_index": 1, "success_calls": 400, "error_429": 5},  # 405/500=81%
    {"model": "gemini-3.1-flash-lite", "key_index": 1, "success_calls": 20, "error_429": 0},
])
assert gc.lite_daily_usage_near_cap(KEYS) is True

# 3.5-lite가 소진돼도(495/500) 3.1-lite는 15건뿐이면 "합산" 아니라 "개별" 검사이므로
# 3.5 쪽 하나만으로도 여전히 True(80% 조건은 모델 하나만 넘어도 걸림 — 부가호출은 보수적으로)
gc.requests.get = lambda *a, **k: R(200, [
    {"model": "gemini-3.5-flash-lite", "key_index": 1, "success_calls": 495, "error_429": 0},
    {"model": "gemini-3.1-flash-lite", "key_index": 1, "success_calls": 15, "error_429": 0},
])
assert gc.lite_daily_usage_near_cap(KEYS) is True

# 둘 다 여유 있으면 False
gc.requests.get = lambda *a, **k: R(200, [
    {"model": "gemini-3.5-flash-lite", "key_index": 1, "success_calls": 100, "error_429": 0},
    {"model": "gemini-3.1-flash-lite", "key_index": 1, "success_calls": 20, "error_429": 0},
])
assert gc.lite_daily_usage_near_cap(KEYS) is False

# 키 없음/조회 실패/행 없음 → 막지 않음(False)
assert gc.lite_daily_usage_near_cap([]) is False
gc.requests.get = lambda *a, **k: R(500, [])
assert gc.lite_daily_usage_near_cap(KEYS) is False
def boom(*a, **k): raise RuntimeError("net")
gc.requests.get = boom
assert gc.lite_daily_usage_near_cap(KEYS) is False
gc.requests.get = lambda *a, **k: R(200, [])
assert gc.lite_daily_usage_near_cap(KEYS) is False

print("ok")
