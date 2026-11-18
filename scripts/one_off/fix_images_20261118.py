# -*- coding: utf-8 -*-
"""2026-11-18 사용자 제보 사진 5건 교체 + 새 Pixabay 태그 관련도 검사(tags_match_query)가 너무 엄격하지 않은지 측정.
실행: one_off_task.yml(script=one_off/fix_images_20261118.py) — Pixabay·R2 키가 거기 있다.
DB 쓰기는 기사 5행 UPDATE뿐(묶음마다 wait_wal_drain)."""
import importlib
import os
import sys

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import article_image as a  # noqa: E402
from db_write_guard import wait_wal_drain  # noqa: E402
from image_store import store_image  # noqa: E402

# 1) 측정: 각 정기 기사 키워드 풀에서 태그 검사 통과 장수(0이면 그 키워드는 사진을 못 고른다)
POOLS = {}
for mod, attr in (("gold_price_writer", "_IMAGE_KEYWORDS"), ("frontier_markets_writer", "_MARKET_IMAGE_KEYWORDS"),
                  ("oil_price_writer", None), ("stock_news_writer", None), ("crypto_news_writer", None),
                  ("weekly_crypto_writer", "_IMAGE_KEYWORDS"), ("literature_writer", "BOOK_IMAGE_KEYWORDS")):
    try:
        m = importlib.import_module(mod)
        names = [attr] if attr else [n for n in dir(m) if n.endswith("KEYWORDS") and isinstance(getattr(m, n), list)]
        for n in names:
            POOLS[f"{mod}.{n}"] = getattr(m, n)
    except Exception as e:
        print("import 실패", mod, e)
orig = a.tags_match_query
for name, kws in POOLS.items():
    for q in kws:
        a.tags_match_query = lambda t, qq: True
        total = len(a._pixabay_hits(q, frozenset()))
        a.tags_match_query = orig
        passed = len(a._pixabay_hits(q, frozenset()))
        print(f"[측정] {name} | {q!r}: 통과 {passed}/{total}")

# 2) 사진 교체
fixes = {}
for aid, person in ((326456, "Xi Jinping"), (326605, "Robert Pattinson"), (327152, "Crimean Bridge Attack")):
    url, credit = a.fetch_wikimedia_image(person)
    if url:
        fixes[aid] = (store_image(url, key_hint=f"wiki_{person}_20261118"), credit)
gold = importlib.import_module("gold_price_writer")
fm = importlib.import_module("frontier_markets_writer")
for aid, kws, key in ((327094, gold._IMAGE_KEYWORDS, "gold_fix_20261118"),
                      (327095, fm._MARKET_IMAGE_KEYWORDS, "frontier_markets_fix_20261118")):
    url = a.fetch_seeded_pixabay_image(kws, 739000 + aid, key)
    if url:
        fixes[aid] = (url, "이미지 출처: Pixabay (기사 내용과 직접 관련 없는 예시 이미지)")

conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=15)
conn.autocommit = True
cur = conn.cursor()
for aid, (url, credit) in fixes.items():
    if not url:
        continue
    wait_wal_drain(cur)
    cur.execute("update articles set image_url=%s, image_credit=%s where id=%s", (url, credit, aid))
    print(f"[교체] {aid} → {url} | {credit}")
print("미교체:", sorted({326456, 326605, 327152, 327094, 327095} - set(fixes)))
