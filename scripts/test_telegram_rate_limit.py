# -*- coding: utf-8 -*-
"""
send_telegram() 레이트 리미팅 회귀 테스트 (2026-09-23 신설).

실사고: 병렬 워커 10개가 거의 동시에 send_telegram()을 호출해 텔레그램
자체 속도제한(429)에 대량으로 걸렸다. 여러 스레드에서 동시 호출해도
실제 발송(requests.post) 간격이 TELEGRAM_MIN_INTERVAL 이상인지 검증한다.

실행: python scripts/test_telegram_rate_limit.py
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("TELEGRAM_TOKEN", "test")
os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import rss_fetcher as rf  # noqa: E402
import http_retry  # noqa: E402


class FakeResp:
    def json(self):
        return {"ok": True}


def test_concurrent_calls_are_spaced_out():
    # 테스트가 오래 안 걸리게 간격을 축소
    rf.TELEGRAM_MIN_INTERVAL = 0.05
    rf._telegram_last_sent_at[0] = 0.0

    call_times = []

    def fake_post(url, data=None):
        call_times.append(time.monotonic())
        return FakeResp()

    # send_telegram()은 http_retry.get_session().post(...)를 호출한다
    # (rf.requests가 아님 — RSS 피드 fetch까지 재시도가 번지지 않도록
    # 전역 shadow를 되돌리고 텔레그램 발송에만 지역적으로 세션을 쓰게
    # 고친 뒤 패치 대상도 맞춰 바꿨다).
    http_retry.get_session().post = fake_post

    N = 8
    with ThreadPoolExecutor(max_workers=N) as ex:
        futs = [ex.submit(rf.send_telegram, f"제목{i}", "요약", "https://x", "src",
                           "정치", "sub", "region", "country") for i in range(N)]
        for f in futs:
            f.result()

    assert len(call_times) == N
    call_times.sort()
    gaps = [call_times[i + 1] - call_times[i] for i in range(N - 1)]
    min_gap = min(gaps)
    assert min_gap >= rf.TELEGRAM_MIN_INTERVAL * 0.9, (
        f"발송 간격이 {min_gap:.3f}s로 최소 간격({rf.TELEGRAM_MIN_INTERVAL}s)보다 좁다 — "
        f"여러 워커가 여전히 몰려서 나가고 있다"
    )


def test_single_call_no_unnecessary_wait():
    """호출이 하나뿐이면 불필요하게 기다리지 않는다."""
    rf.TELEGRAM_MIN_INTERVAL = 1.0
    rf._telegram_last_sent_at[0] = time.monotonic() - 10  # 충분히 오래 전

    def fake_post(url, data=None):
        return FakeResp()

    http_retry.get_session().post = fake_post

    t0 = time.monotonic()
    rf.send_telegram("제목", "요약", "https://x", "src", "정치", "sub", "region", "country")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2, f"불필요하게 {elapsed:.2f}s 대기했다"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
