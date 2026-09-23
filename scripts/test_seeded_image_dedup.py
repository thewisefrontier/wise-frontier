# -*- coding: utf-8 -*-
"""
fetch_seeded_pixabay_image() 크로스스크립트 중복방지 회귀 테스트 (2026-09-23 신설).

실사고: frontier_markets_writer.py와 stock_news_writer.py가 같은 날 비슷한
키워드로 검색해 seed%len(hits) 고정 인덱스가 우연히 같은 사진을 가리켰다.
네트워크(Pixabay/R2)는 가짜로 갈아끼워 검증한다.

실행: python scripts/test_seeded_image_dedup.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("PIXABAY_API_KEY", "test")

import article_image as ai  # noqa: E402


class FakeResp:
    def __init__(self, hits):
        self.status_code = 200
        self._hits = hits

    def json(self):
        return {"hits": self._hits}


HITS = [{"id": i, "webformatURL": f"https://pixabay.example/{i}.jpg"} for i in range(10)]


def run(keywords, seed, key_hint, used_ids, requests_get, store_calls):
    ai.requests.get = requests_get
    import db
    db.image_used_recently = lambda frag, hours=24: any(f"seeded_pixabay_{uid}" in frag for uid in used_ids)

    def fake_store_image(src_url, key_hint=""):
        store_calls.append(key_hint)
        return f"https://r2.example/{key_hint}.jpg"

    import image_store
    image_store.store_image = fake_store_image
    return ai.fetch_seeded_pixabay_image(keywords, seed, key_hint)


def test_no_conflict_uses_original_index():
    """아무도 최근에 안 썼으면 기존과 동일하게 seed%len(hits) 그대로."""
    calls = []
    url = run(["finance"], seed=3, key_hint="frontier_markets_2026_09_22",
               used_ids=set(), requests_get=lambda *a, **k: FakeResp(HITS), store_calls=calls)
    assert calls == ["frontier_markets_2026_09_22_pid3"], calls
    assert "pid3" in url


def test_conflict_skips_to_next_unused():
    """seed 인덱스(3)가 이미 다른 스크립트에 최근 쓰였으면 다음 미사용 후보로."""
    calls = []
    url = run(["finance"], seed=3, key_hint="stock_news_2026_09_22",
              used_ids={3}, requests_get=lambda *a, **k: FakeResp(HITS), store_calls=calls)
    assert calls == ["stock_news_2026_09_22_pid4"], calls  # 3 건너뛰고 4


def test_all_used_falls_back_to_original_index():
    """후보 10개 전부 최근 사용이면 어쩔 수 없이 원래 인덱스로 후퇴."""
    calls = []
    all_ids = {h["id"] for h in HITS}
    run(["finance"], seed=3, key_hint="jp_market_2026_09_22",
        used_ids=all_ids, requests_get=lambda *a, **k: FakeResp(HITS), store_calls=calls)
    assert calls == ["jp_market_2026_09_22_pid3"], calls


def test_deterministic_same_seed_same_history_same_result():
    """같은 seed + 같은 사용 이력이면 항상 같은 결과(멱등성 유지)."""
    calls1, calls2 = [], []
    run(["finance"], seed=5, key_hint="oil_2026_09_22",
        used_ids={5}, requests_get=lambda *a, **k: FakeResp(HITS), store_calls=calls1)
    run(["finance"], seed=5, key_hint="oil_2026_09_22",
        used_ids={5}, requests_get=lambda *a, **k: FakeResp(HITS), store_calls=calls2)
    assert calls1 == calls2


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
