# -*- coding: utf-8 -*-
"""
queue_insert_bulk() on_conflict 회귀 테스트 (2026-09-23 신설).

실사고: resolution=ignore-duplicates만 걸고 on_conflict=link를 안 주면
PostgREST가 PK(id) 기준으로 ON CONFLICT를 잡아 link UNIQUE 제약 충돌은
그대로 409(23505)로 터진다. 큐 백로그가 쌓여 재수집 중복이 늘수록 매
배치마다 이분 재시도가 수십 번씩 터져 "RSS 수집" 스텝이 매 사이클
외부 타임아웃까지 걸리던 진짜 원인이었다(실전 로그로 확인). URL에
on_conflict=link가 실제로 붙는지만 검증한다(과한 목킹은 불필요).

실행: python scripts/test_queue_insert_bulk_conflict.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import db  # noqa: E402


class FakeResp:
    status_code = 201
    def json(self):
        return [{"id": 1}]


def test_on_conflict_link_in_url():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        return FakeResp()

    db.requests.post = fake_post
    n = db.queue_insert_bulk([{"link": "https://x/1", "title": "t", "source_name": "s"}])
    assert n == 1
    assert "on_conflict=link" in captured["url"], captured["url"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
