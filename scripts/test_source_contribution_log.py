# -*- coding: utf-8 -*-
"""
gemini_writer.log_source_contribution() 회귀 테스트 (2026-09-24 신설).

원자재 유입량(하루 6만 건)에 비해 발행 전환율이 너무 낮아(약 0.06%),
"필요 없는 소스"를 추측이 아니라 실측으로 솎아내려고 추가. 클러스터가
발행/업데이트로 이어지면 구성원 소스를 source_contribution_log에 남긴다.
검토 필요(__needs_review__) 표시 행은 제외, 실패해도 예외를 던지지 않는지
검증한다(Supabase 호출은 모킹).

실행: python scripts/test_source_contribution_log.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import gemini_writer as gw  # noqa: E402


def test_logs_member_sources_excluding_review_marker():
    captured = {}
    gw.requests.post = lambda url, headers=None, json=None, timeout=None: captured.update(url=url, rows=json)
    cluster = [{"source": "BBC", "title_ko": "a"}, {"source": "CNN", "title_ko": "b"},
               {"__needs_review__": True}]
    gw.log_source_contribution(cluster, 123)
    assert "source_contribution_log" in captured["url"]
    assert captured["rows"] == [{"source": "BBC", "article_id": 123}, {"source": "CNN", "article_id": 123}]


def test_empty_cluster_skips_call():
    called = []
    gw.requests.post = lambda *a, **kw: called.append(1)
    gw.log_source_contribution([{"__needs_review__": True}], 1)
    assert called == []


def test_network_failure_does_not_raise():
    def raise_err(*a, **kw):
        raise ConnectionError("no network")
    gw.requests.post = raise_err
    gw.log_source_contribution([{"source": "BBC"}], 1)  # 예외 안 나면 통과


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
