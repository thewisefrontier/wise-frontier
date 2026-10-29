# -*- coding: utf-8 -*-
"""수동 교정한 미발행 기사를 사진 붙여 발행(1회용). 미발행 기사는 저장 때 사진을 안 받아서
DB에서 is_published만 켜면 사진 없는 기사가 된다. IDS를 고친 뒤
one_off_task.yml(script=one_off/publish_with_image.py)로 실행 — Pixabay·R2 키가 거기 있다.
2026-10-29: #320093 연준 금리 동결(이름 검사기 오탐·복수 토픽으로 보류됐던 것을 교정)."""
import os
import sys

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gemini_writer import fetch_article_image  # noqa: E402

IDS = [320093]

conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=10)
conn.autocommit = True
cur = conn.cursor()
for aid in IDS:
    cur.execute("select title_ko, summary_ko, is_published from articles where id=%s", (aid,))
    title, body, pub = cur.fetchone()
    if pub:
        print(aid, "이미 발행됨 — 건너뜀")
        continue
    url, credit = fetch_article_image(title, body)
    if not url:
        print(aid, "사진 못 받음 — 발행 안 함")
        continue
    cur.execute("update articles set image_url=%s, image_credit=%s, is_published=true where id=%s",
                (url, credit, aid))
    print(aid, "발행:", url, credit)
