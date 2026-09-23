# -*- coding: utf-8 -*-
"""
rss_processor.pick_for_telegram() 회귀 테스트 (2026-09-23 신설).

원자재 전량 발송 → 사이클당 선별 발송으로 바꾸면서 추가. 최신순·매체당 상한·
전체 상한을 검증한다.

실행: python scripts/test_telegram_pick.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
os.environ.setdefault("TELEGRAM_TOKEN", "test")

import rss_processor as rp  # noqa: E402


def c(src, pub):
    return {"source": src, "published": pub, "args": ()}


def test_newest_first_and_limit():
    cands = [c("A", "2026-09-23T01"), c("B", "2026-09-23T03"), c("C", "2026-09-23T02")]
    got = rp.pick_for_telegram(cands, 2)
    assert [x["source"] for x in got] == ["B", "C"]


def test_per_source_cap():
    cands = [c("A", f"2026-09-23T0{i}") for i in range(5)] + [c("B", "2026-09-22T00")]
    got = rp.pick_for_telegram(cands, 10)
    assert sum(1 for x in got if x["source"] == "A") == 2
    assert any(x["source"] == "B" for x in got)


def test_missing_date_goes_last():
    got = rp.pick_for_telegram([c("A", ""), c("B", "2026-09-23T00")], 1)
    assert got[0]["source"] == "B"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
