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


# 사용량 집계 배치: 호출마다 POST 하지 않고 모아서 보낸다(2026-09-28)
posts = []
gc.requests.post = lambda url, **k: posts.append((url, k["json"])) or R(200, [])
gc._usage_buf.clear(); gc._usage_events = 0; gc._usage_first_ts = 0.0
for i in range(14):
    gc._log_usage("m", 1, "success", 10, 5, 15)
assert posts == []                                     # 15건 미만이고 20초 안 지남 → 아직 안 보냄
gc._log_usage("m", 1, "429")                           # 15번째 → flush
assert len(posts) == 1 and posts[0][0].endswith("increment_gemini_usage_batch")
row = posts[0][1]["p_rows"][0]
assert (row["model"], row["key_index"], row["success"], row["e429"], row["total"]) == ("m", 1, 14, 1, 210), row
gc._log_usage("m", 2, "503"); gc._flush_usage()        # 종료 시 flush 대상도 같은 경로
assert len(posts) == 2 and posts[1][1]["p_rows"][0]["e503"] == 1
gc._flush_usage(); assert len(posts) == 2              # 빈 버퍼는 안 보냄
print("usage batch ok")
