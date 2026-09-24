# -*- coding: utf-8 -*-
"""
gemini_client 프로세스 간 공유 페이싱 회귀 테스트 (2026-09-24 신설).

collectors/breaking/heavy 3개 job이 동시에 돌며 서로 다른 프로세스가 같은
5개 Gemini 키를 동시에 써 무거운 실행마다 429가 14~22회 났다(프로세스
메모리 안에서만 유지되던 기존 페이싱이 서로를 몰랐음). Supabase RPC
claim_gemini_key로 공유하는 _wait_for_shared_slot()의 대기/포기/장애
무해통과를 검증한다(RPC는 모킹 — 실제 DB 호출 없음).

실행: python scripts/test_gemini_shared_pacing.py
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import gemini_client as gc  # noqa: E402


class Resp:
    def __init__(self, rows, code=200):
        self._rows, self.status_code = rows, code

    def json(self):
        return self._rows


def test_allowed_immediately_no_sleep():
    gc.requests.post = lambda *a, **kw: Resp([{"allowed": True, "wait_seconds": 0}])
    slept = []
    time.sleep_orig, time.sleep = time.sleep, lambda s: slept.append(s)
    try:
        gc._wait_for_shared_slot("m", 0, 13)
    finally:
        time.sleep = time.sleep_orig
    assert slept == []


def test_blocked_then_waits_reported_seconds():
    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return Resp([{"allowed": False, "wait_seconds": 2.5}])
        return Resp([{"allowed": True, "wait_seconds": 0}])

    gc.requests.post = fake_post
    slept = []
    time.sleep_orig, time.sleep = time.sleep, lambda s: slept.append(s)
    try:
        gc._wait_for_shared_slot("m", 0, 13)
    finally:
        time.sleep = time.sleep_orig
    assert slept == [2.5], slept
    assert calls["n"] == 2


def test_gives_up_after_cap_not_infinite_loop():
    gc.requests.post = lambda *a, **kw: Resp([{"allowed": False, "wait_seconds": 100}])
    slept = []
    time.sleep_orig, time.sleep = time.sleep, lambda s: slept.append(s)
    try:
        gc._wait_for_shared_slot("m", 0, 13)
    finally:
        time.sleep = time.sleep_orig
    assert sum(slept) <= gc._SHARED_WAIT_CAP + 0.01, slept
    assert len(slept) <= 4


def test_db_failure_does_not_block():
    def raise_err(*a, **kw):
        raise ConnectionError("no network")
    gc.requests.post = raise_err
    t0 = time.monotonic()
    gc._wait_for_shared_slot("m", 0, 13)
    assert time.monotonic() - t0 < 0.5


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
