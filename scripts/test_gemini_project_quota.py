# -*- coding: utf-8 -*-
"""Gemini 한도가 프로젝트 단위(5키 공통)라는 전제의 페이싱·429 처리 회귀 테스트.
실행: python scripts/test_gemini_project_quota.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "")
sys.path.insert(0, os.path.dirname(__file__))
import gemini_client as gc  # noqa: E402

gc._log_usage = lambda *a, **k: None
clock = {"t": 1000.0}
slept = []
gc.time.time = lambda: clock["t"]
def fake_sleep(s):
    slept.append(s); clock["t"] += s
gc.time.sleep = fake_sleep


class R:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)
    def json(self):
        return self._b


OK = R(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hi"}]}}]})
RPM429 = R(429, {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel", "quotaValue": "15"}]}]}})

# 1) 분당 429 → 같은 모델의 다른 키로 헛시도하지 않고 다음 모델로
calls = []
def post(url, json=None, timeout=None):
    model = url.split("/models/")[1].split(":")[0]
    calls.append(model)
    return RPM429 if model == "m1" else OK
gc.requests.post = post
c = gc.GeminiClient(["k1", "k2", "k3", "k4", "k5"], models=["m1", "m2"])
gc.MIN_KEY_INTERVAL_SECONDS.update({"m1": 5, "m2": 5})
assert c.call("p", start_tier=0) == "hi"
assert calls == ["m1", "m2"], calls
assert all(c._exhausted_keys["m1"][i] > clock["t"] for i in range(5))  # 모든 키 쿨다운
assert gc._quota_value(RPM429) == "15"

# 2) 키가 달라도 같은 모델이면 간격(5초) 유지
calls.clear(); slept.clear()
gc.requests.post = lambda url, json=None, timeout=None: (calls.append(url.split("key=")[1]), OK)[1]
c2 = gc.GeminiClient(["k1", "k2"], models=["m2"])
c2.call("a", start_tier=0); c2.call("b", start_tier=0)
assert calls == ["k1", "k2"], calls          # 키는 번갈아 쓰지만
assert slept and abs(slept[0] - 5) < 1e-6, slept   # 두 번째 호출 전 모델 간격 5초 대기
print("ok")
