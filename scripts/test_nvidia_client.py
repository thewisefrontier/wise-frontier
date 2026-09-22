# -*- coding: utf-8 -*-
"""
NVIDIA 클라이언트 한도 관리 회귀 테스트 (2026-09-22 신설).

40RPM을 넘기거나 429를 그냥 삼키면 검증이 조용히 꺼진다(둘 다 로그를 봐야만
드러나는 종류의 사고라 테스트로 고정한다). 네트워크는 가짜 응답으로 대체한다.

실행: python scripts/test_nvidia_client.py
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp949 콘솔에서 이모지 출력 보호
except Exception:
    pass

os.environ["NVIDIA_API_KEY"] = "test-key"
os.environ["NVIDIA_MIN_INTERVAL"] = "0.05"     # 테스트가 느려지지 않게 축소

import nvidia_client as nc  # noqa: E402

nc._RATE_LIMIT_WAIT = 0.01
nc._SERVER_ERR_WAIT = 0.01


class FakeResp:
    def __init__(self, status, text="", content="응답"):
        self.status_code = status
        self.text = text
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def patch_post(responses):
    """호출마다 정해진 응답을 차례로 돌려주는 가짜 post. 호출 기록을 반환한다."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(time.monotonic())
        return responses[min(len(calls) - 1, len(responses) - 1)]

    nc.requests.post = fake_post
    nc._last_call_at = 0.0
    return calls


def test_success_returns_text():
    patch_post([FakeResp(200, content="  결과  ")])
    assert nc.call_nvidia("p") == "결과"


def test_rate_limit_is_retried_not_swallowed():
    """429를 그냥 None으로 삼키면 검증이 조용히 꺼진다 — 반드시 재시도해야 한다."""
    calls = patch_post([FakeResp(429, "rate limit"), FakeResp(200, content="ok")])
    assert nc.call_nvidia("p") == "ok"
    assert len(calls) == 2, "429인데 재시도하지 않았다"


def test_server_error_is_retried():
    """실측상 503이 약 11% 발생한다."""
    calls = patch_post([FakeResp(503, "overloaded"), FakeResp(200, content="ok")])
    assert nc.call_nvidia("p") == "ok"
    assert len(calls) == 2


def test_gives_up_after_max_attempts():
    calls = patch_post([FakeResp(503, "overloaded")])
    assert nc.call_nvidia("p") is None
    assert len(calls) == nc._MAX_ATTEMPTS


def test_client_error_not_retried():
    """400 같은 건 재시도해도 소용없다 — 한도만 태운다."""
    calls = patch_post([FakeResp(400, "bad request")])
    assert nc.call_nvidia("p") is None
    assert len(calls) == 1


def test_pacing_enforced_between_calls():
    """자기 자신이 버스트로 RPM을 태우지 않도록 최소 간격을 지켜야 한다."""
    calls = patch_post([FakeResp(200, content="ok")])
    nc.call_nvidia("p")
    nc.call_nvidia("p")
    gap = calls[1] - calls[0]
    assert gap >= nc.NVIDIA_MIN_INTERVAL * 0.8, f"간격 미준수: {gap:.3f}s"


def test_no_key_returns_none_without_calling():
    orig = nc.NVIDIA_API_KEY
    nc.NVIDIA_API_KEY = ""
    calls = patch_post([FakeResp(200)])
    try:
        assert nc.call_nvidia("p") is None
        assert calls == []
    finally:
        nc.NVIDIA_API_KEY = orig


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
