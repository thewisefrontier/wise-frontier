# -*- coding: utf-8 -*-
"""auto_dedup.py 회귀 테스트(2026-10-26): 유사도 50~70% 구간은 같은 사건이면 통합, 별개면 둘 다 유지.
실행: python scripts/test_auto_dedup.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import auto_dedup as d  # noqa: E402

assert d._is_distinct("별개기사") and d._is_distinct(" 별개기사\n") and not d._is_distinct("제목: x\n본문: 별개기사 아님")

calls = {"merge": [], "reviewed": [], "unpub": []}
results = {}


def fake_merge(keep, remove, judge=False):
    calls["merge"].append((keep, remove, judge))
    return results.get(remove, True)


d.time.sleep = lambda *_: None
d.merge_pair = fake_merge
d.mark_dedup_reviewed = lambda ids: calls["reviewed"].extend(ids)
d.unpublish_articles = lambda ids, scores=None: calls["unpub"].extend(ids) or True


def pair(a, b, score):
    return {"id_a": a, "id_b": b, "score": score, "created_at_a": f"2026-10-25 {a % 24:02d}:00", "created_at_b": f"2026-10-25 {b % 24:02d}:00"}


# 1(원본, 발행) ← 2(같은 사건) / 3(별개 사건) / 4(통합 실패) / 5(이미 미발행) / 6(원본 아님: 7이 미발행 원본)
d.fetch_duplicate_pairs = lambda **k: [pair(1, 2, .6), pair(1, 3, .55), pair(1, 4, .52), pair(1, 5, .6), pair(7, 8, .6)]
d.fetch_categories = lambda ids: {i: ("정치·외교", "cluster_x", i != 5 and i != 7) for i in ids}
results.update({3: d.DISTINCT, 4: False})
d.run()
assert [m[:2] for m in calls["merge"]] == [(1, 2), (1, 3), (1, 4)], calls          # 5(이미 미발행)·8(원본 미발행)은 호출 안 함
assert all(m[2] for m in calls["merge"])                                            # 50~70%는 판정 모드
assert sorted(calls["reviewed"]) == [3, 5], calls                                    # 별개·기미발행은 재검출만 차단, 발행 유지
assert calls["unpub"] == [4], calls                                                  # 통합 실패만 예전처럼 미발행

# 상한: 쌍이 많아도 한 번에 MAX_JUDGED_MERGES_PER_RUN까지만
calls.update({"merge": [], "reviewed": [], "unpub": []}); results.clear()
d.fetch_duplicate_pairs = lambda **k: [pair(1, 100 + i, .6) for i in range(15)]
d.fetch_categories = lambda ids: {i: ("정치·외교", "cluster_x", True) for i in ids}
d.run()
assert len(calls["merge"]) == d.MAX_JUDGED_MERGES_PER_RUN, len(calls["merge"])

# 2026-10-26: 모델이 라벨 형식 대신 JSON(코드블록 포함)으로 답해도 본문·제목을 꺼낸다 — JSON 문자열이 본문이 되면 안 된다.
j = '{"title": "새 제목", "country": "프랑스", "body": "첫 문단.\\n\\n둘째 문단."}'
want = ("새 제목", "첫 문단.\n\n둘째 문단.")
assert d._parse_merge_output(j, "옛 제목")[:2] == want
assert d._parse_merge_output("```json\n" + j + "\n```", "옛 제목")[:2] == want
assert d._parse_merge_output("제목: 라벨 제목\n본문: 라벨 본문\n업데이트요약: 요약", "옛 제목") == ("라벨 제목", "라벨 본문", "요약")
assert d._parse_merge_output('{"title": "x", "body": "깨진', "옛 제목")[1] == ""   # 복구 불가 JSON → 빈 본문 → 통합 실패 처리

print("ok")

