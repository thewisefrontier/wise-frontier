# -*- coding: utf-8 -*-
"""
db.mirror_article_update() 회귀 테스트 (2026-09-24 신설).

최초 발행(insert_final_article)만 Aiven에 미러링되고, 그 뒤 수정(클러스터에
새 소스가 붙어 본문이 갱신되는 update_article, 트렌드 기사 전개 병합
merge_trend_article 등)은 안 따라가고 있었다(사용자 지적: "지금 정확히
백업하는 게 어떤거야?"). 컬럼 화이트리스트 필터링과 JSON 래핑을 검증한다
(psycopg2 연결은 모킹).

실행: python scripts/test_mirror_article_update.py
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


def test_unknown_field_filtered_and_id_excluded_from_set():
    captured = {}
    db._mirror = lambda sql, params: captured.update(sql=sql, params=params)
    db.mirror_article_update(42, {"title_ko": "t", "id": 999, "__weird__": "x"})
    assert "__weird__" not in captured["sql"]
    assert "id=%s" not in captured["sql"].split("WHERE")[0]  # SET절엔 id가 없어야 함
    assert captured["params"][-1] == 42  # WHERE id의 값


def test_json_column_wrapped():
    from psycopg2.extras import Json
    captured = {}
    db._mirror = lambda sql, params: captured.update(sql=sql, params=params)
    db.mirror_article_update(1, {"update_log": [{"note": "x"}]})
    assert isinstance(captured["params"][0], Json)


def test_no_fields_or_no_id_skips():
    calls = []
    db._mirror = lambda *a: calls.append(a)
    db.mirror_article_update(0, {"title_ko": "t"})
    db.mirror_article_update(5, {"__weird__": "x"})
    assert calls == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
