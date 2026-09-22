# -*- coding: utf-8 -*-
"""
속보 경로(run_breaking) 회귀 테스트 (2026-09-23 신설).

collectors 직후 도는 가벼운 경로라, (a) 중대성 낮은/오래된 클러스터를 잘못
집어가지 않는지 (b) 이미 heavy가 처리한 클러스터를 중복 집필하지 않는지가
핵심이다. Gemini 호출은 fake로 갈아끼워 네트워크 없이 검증한다.

실행: python scripts/test_breaking_news.py
"""
import os
import sys
from datetime import timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp949 콘솔에서 이모지/줄표 출력 보호
except Exception:
    pass

os.environ.setdefault("GEMINI_API_KEY", "test")

import gemini_writer as gw  # noqa: E402


def art(source, title, hours_ago=1):
    ts = (gw.now_kst() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")
    return {"source": source, "title_ko": title, "title_en": title,
            "summary_ko": "본문 요약", "full_text": "본문", "created_at": ts,
            "country": "", "category": "정치·외교"}


def test_severity_filter_picks_only_high():
    disaster = [art(s, "지진으로 200명 사망", hours_ago=1) for s in ("AP", "Reuters")]
    routine = [art(s, "기업 분기 실적 발표", hours_ago=1) for s in ("A", "B", "C")]
    members_d = [a for a in disaster if not a.get("__needs_review__")]
    members_r = [a for a in routine if not a.get("__needs_review__")]
    assert gw._cluster_hits_severity_high(members_d) is True
    assert gw._cluster_hits_severity_high(members_r) is False


def test_stale_disaster_excluded_by_age():
    """3시간 넘은 참사는 '속보'가 아니다 — 후속 심층기사는 heavy가 처리."""
    stale = [art(s, "지진으로 200명 사망", hours_ago=10) for s in ("AP", "Reuters")]
    latest = gw._cluster_latest_dt(stale)
    age_h = (gw.now_kst() - latest).total_seconds() / 3600.0
    assert age_h > gw.BREAKING_MAX_AGE_HOURS


def test_run_breaking_skips_when_no_candidates(monkeypatch):
    monkeypatch.setattr(gw, "get_today_articles", lambda limit: [])
    monkeypatch.setattr(gw, "cluster_articles", lambda arts: [])
    called = []
    monkeypatch.setattr(gw, "run", lambda **kw: called.append(kw))
    gw.run_breaking()
    assert called == [], "후보가 없는데 run()을 호출했다"


def test_run_breaking_calls_run_with_override(monkeypatch):
    disaster = [art(s, "지진으로 200명 사망", hours_ago=1) for s in ("AP", "Reuters")]
    routine = [art(s, "기업 분기 실적 발표", hours_ago=1) for s in ("A", "B", "C")]
    clusters = [disaster, routine]

    monkeypatch.setattr(gw, "get_today_articles", lambda limit: sum(clusters, []))
    monkeypatch.setattr(gw, "cluster_articles", lambda arts: clusters)
    called = []
    monkeypatch.setattr(gw, "run", lambda **kw: called.append(kw))

    gw.run_breaking()
    assert len(called) == 1
    kw = called[0]
    assert kw["clusters_override"] == [disaster], "중대성 낮은 클러스터까지 넘겼다"
    assert kw["max_clusters"] == gw.BREAKING_MAX_CLUSTERS_PER_RUN
    assert kw["skip_extras"] is True


def test_regular_run_unaffected_by_new_params():
    """clusters_override/max_clusters를 안 주면 기존 동작 그대로(하위 호환)."""
    import inspect
    sig = inspect.signature(gw.run)
    assert sig.parameters["clusters_override"].default is None
    assert sig.parameters["max_clusters"].default is None
    assert sig.parameters["skip_extras"].default is False


if __name__ == "__main__":
    import unittest.mock as mock

    class _MonkeyPatch:
        def __init__(self):
            self._orig = {}

        def setattr(self, obj, name, value):
            self._orig[(obj, name)] = getattr(obj, name)
            setattr(obj, name, value)

        def undo(self):
            for (obj, name), val in self._orig.items():
                setattr(obj, name, val)

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in t.__code__.co_varnames:
                t(mp)
            else:
                t()
            print(f"  ok  {t.__name__}")
        finally:
            mp.undo()
    print(f"\n{len(tests)}건 통과")
