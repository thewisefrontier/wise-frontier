# -*- coding: utf-8 -*-
"""#327095 마켓 동향 사진 재선택(새 키워드) + 새 키워드별 태그 통과 장수 측정. 실행: one_off_task.yml."""
import os
import sys

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import article_image as a  # noqa: E402
import frontier_markets_writer as fm  # noqa: E402
from db_write_guard import wait_wal_drain  # noqa: E402

for q in fm._MARKET_IMAGE_KEYWORDS + ["stock market trading screen"]:
    print(f"[측정] {q!r}: 통과 {len(a._pixabay_hits(q, frozenset()))}")
url = a.fetch_seeded_pixabay_image(fm._MARKET_IMAGE_KEYWORDS, 739000 + 327095, "frontier_markets_fix3_20261118")
print("[선택]", url)
if url:
    conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=15)
    conn.autocommit = True
    cur = conn.cursor()
    wait_wal_drain(cur)
    cur.execute("update articles set image_url=%s where id=327095", (url,))
    print("[교체] 327095")
