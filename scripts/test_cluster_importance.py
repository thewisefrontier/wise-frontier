# -*- coding: utf-8 -*-
"""
클러스터 중요도 정렬 회귀 테스트 (2026-09-22 신설).

한 실행에 MAX_CLUSTERS_PER_RUN(7)개만 기사화하므로, 정렬이 틀리면 중요한 사안이
조용히 뒤로 밀린다. 여기서 검증하는 건 "종전의 크기 정렬로는 못 잡던" 네 가지다.

실행: python scripts/test_cluster_importance.py
"""
import os
from datetime import timedelta

os.environ.setdefault("GEMINI_API_KEY", "test")

from gemini_writer import cluster_importance, now_kst  # noqa: E402


def art(source, title, summary="본문 요약이 실제로 들어 있는 경우입니다.", hours_ago=1):
    ts = (now_kst() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")
    return {"source": source, "title_ko": title, "title_en": title,
            "summary_ko": summary, "full_text": "본문", "created_at": ts}


def test_distinct_sources_beat_duplicate_count():
    """같은 매체가 3건 쓴 것보다 서로 다른 3개 매체가 다룬 사안이 중요하다."""
    one_source = [art("Reuters", f"의회 예산안 심의 {i}") for i in range(3)]
    three_sources = [art(s, "의회 예산안 심의") for s in ("Reuters", "AP", "AFP")]
    assert cluster_importance(three_sources) > cluster_importance(one_source)


def test_severity_outranks_size():
    """소스가 2곳뿐인 대형 참사가, 소스 4곳짜리 일상 기사보다 먼저 처리돼야 한다."""
    disaster = [art(s, "지진으로 200명 사망") for s in ("AP", "Reuters")]
    routine = [art(s, "기업 분기 실적 발표") for s in ("A", "B", "C", "D")]
    assert cluster_importance(disaster) > cluster_importance(routine)


def test_fresh_beats_stale():
    """같은 조건이면 최근 소식이 먼저다(오래된 클러스터가 상위에 눌러앉지 않게)."""
    fresh = [art(s, "중앙은행 금리 결정", hours_ago=1) for s in ("A", "B")]
    stale = [art(s, "중앙은행 금리 결정", hours_ago=40) for s in ("A", "B")]
    assert cluster_importance(fresh) > cluster_importance(stale)


def test_headline_only_ranks_lower():
    """제목뿐인 클러스터는 어차피 검증에서 걸리므로 슬롯을 먼저 쓰면 안 된다."""
    with_body = [art(s, "광산 노동자 파업 확산") for s in ("A", "B")]
    headline_only = [{"source": s, "title_ko": "광산 노동자 파업 확산",
                      "title_en": "", "summary_ko": "", "full_text": "",
                      "created_at": now_kst().strftime("%Y-%m-%d %H:%M")}
                     for s in ("A", "B")]
    assert cluster_importance(with_body) > cluster_importance(headline_only)


def test_needs_review_pushed_back():
    """미발행 확정(다국가 혼합 등) 클러스터는 맨 뒤로."""
    normal = [art(s, "홍수 피해 확산") for s in ("A", "B")]
    flagged = [art(s, "홍수 피해 확산") for s in ("A", "B")] + [{"__needs_review__": True}]
    assert cluster_importance(flagged) < cluster_importance(normal)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
