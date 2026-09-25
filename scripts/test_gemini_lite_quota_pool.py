# -*- coding: utf-8 -*-
"""3.5/3.1-flash-lite가 키(프로젝트) 하나당 한도를 공유한다는 가설에 따른
페이싱·429 처리 회귀 테스트(2026-09-25). 실행: python scripts/test_gemini_lite_quota_pool.py"""
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
gc.time.sleep = lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s))


class R:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)
    def json(self):
        return self._b


OK = R(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "hi"}]}}]})
RPM429 = R(429, {"error": {"message": "You exceeded your current quota, please check your plan and billing details."}})

assert gc._quota_pool("gemini-3.5-flash-lite") == gc._quota_pool("gemini-3.1-flash-lite") == "lite-pool"
assert gc._quota_pool("gemini-3.8-flash") == "gemini-3.8-flash"

# 1) 3.5-lite에서 키 5개 전부 429 → 3.1-lite는 같은 풀이라 재시도 없이 즉시 다음(실제 요청 0번)
calls = []
def post(url, json=None, timeout=None):
    calls.append(url.split("/models/")[1].split(":")[0])
    return RPM429
gc.requests.post = post
c = gc.GeminiClient(["k1", "k2", "k3", "k4", "k5"], models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
assert c.call("p", start_tier=0) is None
assert calls == ["gemini-3.5-flash-lite"] * 5, calls  # 3.1-lite는 풀이 이미 소진 상태라 실제 호출 자체가 없음

# 2) 3.5-lite로 쓴 키는 3.1-lite 차례에도 "방금 씀"으로 잡혀 간격(5초)이 적용됨
calls.clear(); slept.clear()
gc.requests.post = lambda url, json=None, timeout=None: (calls.append(url.split("key=")[1]), OK)[1]
c2 = gc.GeminiClient(["k1"], models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
c2.call("a", start_tier=0)  # 3.5-lite, k1 사용
c2.call("b", start_tier=1)  # 3.1-lite만 시도 — 같은 풀이라 k1 최근 사용 기록을 봄
assert calls == ["k1", "k1"]
assert slept and abs(slept[0] - 5) < 1e-6, slept

# 3) 프리미엄 모델은 풀 공유 없이 독립
assert gc._quota_pool("gemini-3.6-flash") != gc._quota_pool("gemini-3.5-flash")
print("ok")
