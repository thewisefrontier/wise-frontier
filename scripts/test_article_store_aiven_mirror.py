# -*- coding: utf-8 -*-
"""
article_store._mirror_final_article() 회귀 테스트 (2026-09-24 신설).

db.py의 insert_article()에만 Aiven 미러가 붙어 있었는데, 실제 발행 기사는
전부 article_store.insert_final_article()을 쓴다(db.insert_article을
is_published=True로 부르는 곳이 코드 전체에 없음을 확인) — 발행 기사가
Aiven에 하루 넘게 한 건도 안 들어가고 있었다. 화이트리스트 컬럼 필터링과
JSON 컬럼 래핑을 검증한다(psycopg2 연결은 모킹 — 실제 네트워크 없음).

실행: python scripts/test_article_store_aiven_mirror.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import article_store as astore  # noqa: E402


def test_unknown_columns_filtered_out():
    captured = {}
    astore._mirror = lambda sql, params: captured.update(sql=sql, params=params)
    astore._mirror_final_article({
        "id": 99, "title_ko": "t", "is_published": True,
        "__weird_internal_field__": "should not leak into SQL",
    })
    assert "__weird_internal_field__" not in captured["sql"]
    assert "id" in captured["sql"] and "title_ko" in captured["sql"]


def test_json_columns_wrapped():
    from psycopg2.extras import Json
    captured = {}
    astore._mirror = lambda sql, params: captured.update(sql=sql, params=params)
    astore._mirror_final_article({"id": 1, "source_data": {"tags": ["a"]}, "update_log": [{"note": "x"}]})
    idx_sd = captured["sql"].split("(")[1].split(")")[0].split(",").index("source_data")
    assert isinstance(captured["params"][idx_sd], Json)


def test_no_id_skips_mirror():
    called = []
    astore._mirror = lambda *a: called.append(a)
    astore._mirror_final_article({"title_ko": "no id here"})
    assert called == []


def test_only_published_triggers_mirror():
    orig = astore._mirror_final_article
    calls = []
    try:
        astore._mirror_final_article = lambda row: calls.append(row)
        astore.requests.post = lambda *a, **kw: type("R", (), {
            "status_code": 201, "json": lambda self: [{"id": 5, "is_published": False}]})()
        astore.insert_final_article({"title_ko": "t", "is_published": False})
        assert calls == []
        astore.requests.post = lambda *a, **kw: type("R", (), {
            "status_code": 201, "json": lambda self: [{"id": 6, "is_published": True}]})()
        astore.insert_final_article({"title_ko": "t", "is_published": True})
        assert len(calls) == 1 and calls[0]["id"] == 6
    finally:
        astore._mirror_final_article = orig


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
