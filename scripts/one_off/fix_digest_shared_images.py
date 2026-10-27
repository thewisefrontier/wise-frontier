# -*- coding: utf-8 -*-
"""다이제스트 사진 공유 사고 복구(2026-10-27, 1회용).

데일리 다이제스트·오늘의 AI/스타트업/신기술이 R2 파일 이름을 날짜만으로(digest_YYYYMMDD) 정해
같은 날 기사들이 한 파일을 덮어썼다 → 그날 마지막에 올린 기사 사진이 모두에 보였다.
같은 image_url을 공유하는 묶음에서 마지막 기사(=실제 파일 주인)만 두고 나머지는 사진을 새로 받는다.
실행: one_off_task.yml(script=one_off/fix_digest_shared_images.py) — Pixabay·R2 키가 거기 있다.
"""
import os
import sys

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from daily_digest import fetch_article_image  # noqa: E402

conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=10)
conn.autocommit = True
cur = conn.cursor()
cur.execute("""
    select id, subcategory, title_ko, summary_ko, image_url from articles
    where image_url in (
        select image_url from articles
        where image_url like '%%/articles/digest_2026%%'
        group by image_url having count(*) > 1)
    order by image_url, id
""")
groups = {}
for row in cur.fetchall():
    groups.setdefault(row[4], []).append(row)

fixed = failed = 0
for url, rows in groups.items():
    for aid, sub, title, body, _ in rows[:-1]:   # 마지막(id 최대)이 파일 주인
        new = fetch_article_image(title, body or "", key_hint=sub)
        if new and new != url:
            cur.execute("update articles set image_url=%s where id=%s", (new, aid))
            fixed += 1
            print(f"#{aid} {sub} → {new}")
        else:
            failed += 1
            print(f"#{aid} {sub} 사진 재수집 실패 — 그대로 둠")
print(f"완료: 교체 {fixed}건, 실패 {failed}건")
