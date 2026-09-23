# -*- coding: utf-8 -*-
"""
rss_collector.py 내부 데드라인 회귀 테스트 (2026-09-23 신설).

실사고: 소스 일부가 requests timeout=10을 우회할 만큼 오래 붙잡아(청크
단위 타임아웃이라 트리클 응답엔 안 먹힘) 매 사이클 외부 15분(900s)
타임아웃에 강제 종료됐다(##[error] ... has timed out after 15 minutes,
2026-09-23 실측 로그로 확인). continue-on-error가 이를 "success"로
가려서 안 보였을 뿐, 루프 끝의 update_source_health()(죽은 소스 자동
제외)가 단 한 번도 실행되지 못하고 있었다. 내부 데드라인으로 항상
정상 종료하는지 검증한다.

실행: python scripts/test_collector_deadline.py
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

import rss_collector as rc  # noqa: E402


def _fake_source(id_, name, hang=False):
    return {"id": id_, "name": name, "category": "정치", "subcategory": "정치", "url": f"http://x/{id_}"}


def test_hanging_sources_dont_block_completion():
    """일부 소스가 데드라인보다 오래 걸려도 collect()는 데드라인 근처에서 끝나야 한다."""
    rc.COLLECT_DEADLINE_SEC = 0.3

    sources = [_fake_source(i, f"src{i}") for i in range(5)]
    # 3개는 즉시 끝나고, 2개는 데드라인을 훨씬 넘겨서까지 응답 안 함(스레드만 붙잡음)
    def fake_fetch_source(s):
        if s["id"] in (3, 4):
            time.sleep(5)  # 데드라인(0.3s)보다 훨씬 김 — 실제 트리클 소스 흉내
            return [], s["name"], "ok"
        return [], s["name"], "ok"

    health_calls = []

    rc.load_rss_with_health = lambda: sources
    rc.fetch_source = fake_fetch_source
    rc.queue_insert_bulk = lambda rows: len(rows)
    rc.save_state = lambda: None
    rc.update_source_health = lambda outcomes, threshold: (
        health_calls.append(dict(outcomes)) or {"updated": len(outcomes), "deactivated": []}
    )

    t0 = time.monotonic()
    rc.collect()
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0, f"데드라인을 넘겨 완료까지 {elapsed:.1f}s 걸렸다 — 여전히 hang에 막힘"
    assert len(health_calls) == 1, "update_source_health()가 실행되지 않았다(예전 버그가 재발)"
    # 즉시 끝난 3개는 반영, hang 중인 2개는 이번 사이클엔 빠져야 정상
    assert set(health_calls[0].keys()) == {0, 1, 2}, health_calls[0]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
