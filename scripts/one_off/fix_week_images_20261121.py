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
PERSON = {   # 기사 id → "이름|직업"
    325492: "Xi Jinping|politician", 325594: "Charles III|king", 325753: "Gibran Rakabuming Raka|politician",
    326445: "Martine Moïse|politician", 326574: "Pope Leo XIV|pope", 327373: "Dominique de Villepin|politician",
    327400: "Sergio Moro|politician",
}
STOCK = {    # 기사 id → Pixabay 검색어
    325348: "ukraine flag", 325398: "river barge", 325417: "water sanitation", 325607: "summit flags",
    325809: "courtroom gavel", 326121: "police car night", 326169: "coast guard boat", 326177: "church candles",
    326198: "handcuffs arrest", 326334: "elderly people", 326585: "court justice scales", 326624: "exam students",
    326677: "ambulance emergency", 326767: "cargo ship sea", 327044: "ballot box election", 327052: "measles vaccination",
    327078: "cyber fraud laptop", 327212: "earthquake damage", 327315: "semiconductor chip", 327339: "stock market chart",
    327371: "rhinoceros", 327526: "video game controller", 327591: "vaccination children", 327605: "military drone",
    327637: "police investigation", 327638: "ukraine flag", 327684: "hippopotamus", 327695: "european union flag",
    327766: "india rupee", 327913: "protest crowd",
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


def stock_image(aid, query):
    hits = a._pixabay_hits(query, frozenset())
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
