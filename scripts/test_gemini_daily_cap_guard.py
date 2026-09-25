# -*- coding: utf-8 -*-
"""lite_daily_usage_near_cap() 회귀 테스트(2026-09-25). 실행: python scripts/test_gemini_daily_cap_guard.py"""
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

# 키 하나(1번)라도 합산 80% 넘으면 True
gc.requests.get = lambda *a, **k: R(200, [
    {"key_index": 1, "success_calls": 380, "error_429": 20},  # 3.5-lite: 400
    {"key_index": 1, "success_calls": 5, "error_429": 0},       # 3.1-lite 합쳐 405/500=81%
    {"key_index": 2, "success_calls": 100, "error_429": 0},
])
assert gc.lite_daily_usage_near_cap(KEYS) is True

# 전부 여유 있으면 False
gc.requests.get = lambda *a, **k: R(200, [
    {"key_index": 1, "success_calls": 100, "error_429": 0},
    {"key_index": 2, "success_calls": 200, "error_429": 10},
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
