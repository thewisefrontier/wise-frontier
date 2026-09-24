# -*- coding: utf-8 -*-
"""
db.insert_article() Aiven 미러 범위 회귀 테스트 (2026-09-24 신설).

미러를 처음엔 발행 여부와 무관하게 매 호출 실행했는데, 이 함수는 원자재
수집기(rss_processor.py 등)가 is_published=False로 하루 6만 건 넘게 부르는
함수라 Aiven 무료 1GB를 며칠 안에 채울 위험이 있었다. 발행된 것만
미러링하는지 검증한다(Supabase REST 호출은 모킹 — 실제 네트워크 없음).

실행: python scripts/test_aiven_mirror_scope.py
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


class Resp:
    def __init__(self, data, code=201):
        self._d, self.status_code = data, code

    def json(self):
        return self._d


def _call(is_published):
    mirrored = []
    db.requests.post = lambda *a, **kw: Resp([{"id": 42}])
    db._mirror = lambda sql, params=(): mirrored.append(params)
    aid = db.insert_article(
        title_en="t", title_ko="t", summary_en="s", summary_ko="s",
        url="https://x", source="BBC", category="정치", subcategory="정치",
        region="global", country="", country_flag="", score=0,
        is_published=is_published,
    )
    return aid, mirrored


def test_unpublished_raw_material_not_mirrored():
    aid, mirrored = _call(is_published=False)
    assert aid == 42
    assert mirrored == [], "미발행 원자재가 Aiven에 미러링됨 — 무료 용량 소진 위험"


def test_published_article_is_mirrored():
    aid, mirrored = _call(is_published=True)
    assert aid == 42
    assert len(mirrored) == 1
    assert mirrored[0][0] == 42  # id가 첫 파라미터


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
