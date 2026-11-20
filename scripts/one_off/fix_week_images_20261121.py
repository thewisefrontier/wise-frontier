# -*- coding: utf-8 -*-
"""최근 일주일(11/14~) 사진 점검에서 찾은 부적절 사진 37건 교체(2026-11-21, 사용자 지시 "사진 점검해보자").
사람이 썸네일을 직접 보고 고른 대상과 대체 기준 — 인물은 위키백과 대표 사진, 그 외는 Pixabay 주제 사진.
실행: one_off_task.yml(script=one_off/fix_week_images_20261121.py). 기사 행 UPDATE는 wait_wal_drain 뒤에."""
import os
import sys

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import article_image as a  # noqa: E402
from db_write_guard import wait_wal_drain  # noqa: E402
from image_store import store_image  # noqa: E402

DISCLAIMER = "이미지 출처: Pixabay (기사 내용과 직접 관련 없는 예시 이미지)"
# 2차(교체 결과를 직접 보고 다시 고를 5건): 장난감 피규어·시신 윤곽선·다른 주제 시위·사진 없음
PERSON = {326198: "Gabito Ballesteros|singer"}
STOCK = {
    327637: ["police officers france", "police raid", "police officers"],
    327913: ["soldiers patrol", "army soldiers"],
    325607: ["international conference", "conference meeting", "diplomacy meeting"],
    327605: ["drone attack", "military drone", "war ruins"],
}


def person_image(spec):
    name, _, occ = spec.partition("|")
    lead = a.wikipedia_lead_file(name, occ)
    url, credit = a.fetch_wikimedia_image(name, file_title=lead) if lead else (None, None)
    if not url:
        url, credit = a.fetch_wikimedia_image(f"{name} {occ}")
    if not url:
        return None, None
    return store_image(url, key_hint=f"wiki_{name}_lead"), credit


def stock_image(aid, queries):
    hits = []
    for query in queries:
        hits = a._pixabay_hits(query, frozenset())
        if hits:
            break
    if not hits:
        return None, None
    hit = a.pick_safe_pixabay_hit(hits)
    raw = hit.get("webformatURL") or hit.get("largeImageURL")
    return store_image(raw, key_hint=f"article_pixabay_{hit.get('id')}"), DISCLAIMER


conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=15)
conn.autocommit = True
cur = conn.cursor()
for i, (aid, fn, arg) in enumerate([(k, person_image, v) for k, v in PERSON.items()] +
                                   [(k, stock_image, v) for k, v in STOCK.items()]):
    try:
        url, credit = fn(arg) if fn is person_image else fn(aid, arg)
    except Exception as e:
        print(f"[실패] {aid} {arg}: {str(e)[:80]}")
        continue
    if not url or "r2.dev" not in url:
        print(f"[없음] {aid} {arg}")
        continue
    if i % 20 == 0:
        wait_wal_drain(cur)
    cur.execute("update articles set image_url=%s, image_credit=%s where id=%s", (url, credit, aid))
    print(f"[교체] {aid} {arg} → {url.rsplit('/', 1)[-1]} | {credit}")
