# -*- coding: utf-8 -*-
"""
title_cache 증분 캐시 회귀 테스트 (2026-09-24 신설).

트렌드 감지가 매 실행 9일치 원자재 전량을 DB에서 받던 걸 캐시+증분 조회로
바꾸면서 추가. 캐시 없음→전체 조회, 캐시 있음→마지막 시각-15분 이후만 조회,
오래된 행 정리, id 중복 병합, 같은 프로세스 1회 갱신을 검증한다.

실행: python scripts/test_title_cache.py
"""
import os
import sys
import tempfile
from datetime import timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import title_cache as tc  # noqa: E402

F = "%Y-%m-%d %H:%M"


def ago(**kw):
    return (tc.now_kst() - timedelta(**kw)).strftime(F)


def _setup(new_rows):
    calls = []
    tc._mem = None
    tc.CACHE_PATH = os.path.join(tempfile.mkdtemp(), "c.json.gz")
    tc._fetch_since = lambda since: calls.append(since) or list(new_rows)
    return calls


def test_no_cache_fetches_whole_window_and_saves():
    calls = _setup([{"id": 1, "created_at": ago(hours=1)}])
    rows = tc.load_recent(2)
    assert [r["id"] for r in rows] == [1]
    assert calls[0] <= ago(days=tc.KEEP_DAYS, minutes=-1)
    assert os.path.exists(tc.CACHE_PATH)


def test_incremental_since_last_minus_overlap_and_merge():
    calls = _setup([])
    last = ago(hours=3)
    tc._save_file([{"id": 1, "created_at": last, "title_en": "old"},
                   {"id": 2, "created_at": ago(days=12)}])  # 9일 초과 → 정리 대상
    tc._fetch_since = lambda since: calls.append(since) or [
        {"id": 1, "created_at": last, "title_en": "new"},
        {"id": 3, "created_at": ago(minutes=5)}]
    rows = tc.load_recent(9)
    from datetime import datetime
    assert calls[0] == (datetime.strptime(last, F) - timedelta(minutes=tc.OVERLAP_MIN)).strftime(F)
    assert [r["id"] for r in rows] == [3, 1]
    assert rows[1]["title_en"] == "new"


def test_days_filter_and_single_refresh_per_process():
    calls = _setup([{"id": 1, "created_at": ago(hours=1)}, {"id": 2, "created_at": ago(days=5)}])
    assert [r["id"] for r in tc.load_recent(2)] == [1]
    assert [r["id"] for r in tc.load_recent(9)] == [1, 2]
    assert len(calls) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
