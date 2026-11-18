# -*- coding: utf-8 -*-
"""지난 발행 기사 사진 점검·교체(2026-11-18 사용자 지시 "예전에 나간 기사들 중 사진 이상한 것도 찾아서 고쳐").

① 정기 기사 Pixabay(파일명 _pid): Pixabay 태그가 그 기사 주제 키워드 풀과 안 맞으면(tags_match_query) 다시 고른다.
② 위키 사진: 같은 검색을 다시 돌려, 지금 사진(출처 표기로 식별)이 새 기준(여러 인물·우표·포스터 등)에서 탈락하는
   사진이면 새 선택으로 교체. 순위만 달라진 정상 사진은 둔다.
③ 일반 기사 Pixabay 예시 사진: keyword_en이 사람 이름 꼴이면 위키 인물 사진(파일명에 이름·성 모두 포함)으로 교체.
LLM 호출 없음. DRY=1이면 대상만 출력. DB 쓰기는 기사 행 UPDATE — 50행마다 wait_wal_drain(1GB 디스크 규칙).
실행: one_off_task.yml(script=one_off/fix_past_images_20261118.py) — Pixabay 키가 거기 있다. DRY는 아래 상수.
"""
import html
import importlib
import os
import re
import sys
import time
from urllib.parse import unquote

import psycopg2
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import article_image as a  # noqa: E402
from db_write_guard import wait_wal_drain  # noqa: E402
from image_store import store_image  # noqa: E402

DRY = True
UA = {"User-Agent": "NewsFinalBot/1.0 (+https://newsfinal.co.kr)"}
DISCLAIMER = "이미지 출처: Pixabay (기사 내용과 직접 관련 없는 예시 이미지)"
POOL_BY_SUB = {  # subcategory 접두 → (모듈, 키워드 풀)
    "국제금값": ("gold_price_writer", "_IMAGE_KEYWORDS"),
    "글로벌마켓동향": ("frontier_markets_writer", "_MARKET_IMAGE_KEYWORDS"),
    "종목동향": ("stock_news_writer", "_STOCK_IMAGE_KEYWORDS"),
    "장중특징주": ("stock_news_writer", "_STOCK_IMAGE_KEYWORDS"),
    "국제유가": ("oil_price_writer", "_OIL_IMAGE_KEYWORDS"),
    "가상자산동향": ("crypto_news_writer", "_CRYPTO_IMAGE_KEYWORDS"),
    "주간시세": ("weekly_crypto_writer", "_IMAGE_KEYWORDS"),
    "주간가상자산시황": ("weekly_crypto_writer", "_IMAGE_KEYWORDS"),
}
NOT_PERSON = re.compile(r"\b(Summit|Bank|Party|Group|Company|Corp|Airlines?|University|Ministry|Council|League|Cup|Festival|"
                        r"Crisis|Election|War|Attack|Agreement|Deal|Market|Exchange|Fund|Project|Bridge|Port|Airport|"
                        r"Stadium|Award|Prize|Federation|Union|Association|Committee|Court|Army|Force|Police|Bureau|"
                        r"Agency|Institute|Foundation|Museum|Hospital|Mine|Mining|Oil|Gas|Energy|Motors?|Holdings)\b")

conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=15)
conn.autocommit = True
cur = conn.cursor()
updates = []   # (id, image_url, image_credit, 사유)


def pixabay_tags(pid):
    r = requests.get("https://pixabay.com/api/", params={"key": a.PIXABAY_API_KEY, "id": pid}, timeout=15)
    time.sleep(0.7)   # 분당 100회 한도
    hits = r.json().get("hits", []) if r.ok else []
    return hits[0].get("tags", "") if hits else None


# ① 정기 기사
cur.execute("""select id, subcategory, image_url from articles where is_published and source='NewsFinal'
               and image_url ~ '_pid[0-9]+' and image_url not like '%%article_pixabay_%%'""")
seeded = cur.fetchall()
pools = {}
checked = bad = 0
for aid, sub, url in seeded:
    key = next((k for k in POOL_BY_SUB if (sub or "").startswith(k)), None)
    if not key:
        continue
    mod, attr = POOL_BY_SUB[key]
    pool = pools.setdefault(key, getattr(importlib.import_module(mod), attr))
    pid = re.search(r"_pid(\d+)", url).group(1)
    tags = pixabay_tags(pid)
    checked += 1
    if tags is None or any(a.tags_match_query(tags, q) for q in pool):
        continue
    bad += 1
    print(f"[정기] {aid} {sub} pid{pid} 태그 {tags!r} — 주제 불일치")
    if not DRY:
        new = a.fetch_seeded_pixabay_image(pool, aid, f"{key}_fix_{aid}")
        if new:
            updates.append((aid, new, DISCLAIMER, "정기 태그 불일치"))
print(f"① 정기 기사 {checked}건 점검, 불일치 {bad}건")


# ② 위키 사진
def commons_candidates(query):
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=20, params={
        "action": "query", "generator": "search", "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 8,
        "prop": "imageinfo", "iiprop": "extmetadata", "format": "json"}).json()
    out = []
    for p in ((r.get("query") or {}).get("pages") or {}).values():
        m = ((p.get("imageinfo") or [{}])[0]).get("extmetadata") or {}
        artist = html.unescape(re.sub(r"<[^>]+>", "", m.get("Artist", {}).get("value", ""))).strip()[:80]
        lic = html.unescape(m.get("LicenseShortName", {}).get("value", ""))
        if (m.get("AttributionRequired", {}).get("value") or "").lower() == "true":
            credit = f"사진: {artist} ({lic}, Wikimedia Commons)" if artist else f"사진: Wikimedia Commons ({lic})"
        else:
            credit = "이미지 출처: Wikimedia Commons"
        title = (p.get("title") or "").lower().replace("_", " ")
        cats = (m.get("Categories", {}).get("value") or "").lower()
        q = query.lower()
        multi = any(w in title for w in a._WIKI_MULTI_PERSON_WORDS if w.strip() not in q)
        avoid = a._has_avoid_word(title, a._WIKI_TITLE_AVOID_WORDS) or a._has_avoid_word(cats, a._WIKI_TITLE_AVOID_WORDS) \
            or a._has_avoid_word(cats, a._WIKI_CATEGORY_AVOID_WORDS)
        out.append((credit, multi or avoid, p.get("title")))
    time.sleep(0.3)
    return out


cur.execute("""select a.id, a.image_url, a.image_credit, coalesce(k.keyword_en, '') from articles a
               left join article_keywords k on k.article_id = a.id
               where a.is_published and a.source='NewsFinal' and a.image_url like '%%/wiki_%%'""")
by_url = {}
for aid, url, credit, kw in cur.fetchall():
    by_url.setdefault(url, []).append((aid, credit or "", kw))
wiki_bad = 0
for url, rows in by_url.items():
    slug = unquote(url.rsplit("/wiki_", 1)[-1].rsplit(".", 1)[0])
    if re.search(r"_\d{8}$", slug):          # 오늘 고친 파일(…_20261118)은 건너뜀
        continue
    query = next((kw for _, _, kw in rows if kw), "") or slug.replace("_", " ")
    old_credit = rows[0][1]
    try:
        cands = commons_candidates(query)
    except Exception as e:
        print("  commons 실패", query, e)
        continue
    matches = [c for c in cands if c[0] == old_credit]
    if not matches or not all(rej for _, rej, _ in matches):
        continue
    wiki_bad += 1
    print(f"[위키] {query!r} 기사 {len(rows)}건 — 지금 사진 후보 {[t for _, _, t in matches]} 탈락 기준")
    if not DRY:
        new_url, new_credit = a.fetch_wikimedia_image(query)
        stored = store_image(new_url, key_hint=f"wiki_{query}_fix") if new_url else ""
        for aid, _, _ in rows:
            updates.append((aid, stored, new_credit or "", "위키 탈락 사진") if stored
                           else (aid, "", "", "위키 대체 사진 없음 — 사진 제거"))
print(f"② 위키 파일 {len(by_url)}개 점검, 교체 대상 {wiki_bad}개")

# ③ 일반 기사 Pixabay 예시 사진 → 인물 위키 사진
cur.execute("""select a.id, k.keyword_en from articles a join article_keywords k on k.article_id = a.id
               where a.is_published and a.source='NewsFinal' and a.image_url like '%%article_pixabay_%%'""")
person_rows = [(aid, kw.strip()) for aid, kw in cur.fetchall()
               if re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z'\-]+){1,3}", (kw or "").strip()) and not NOT_PERSON.search(kw)]
found = 0
cache = {}
for aid, kw in person_rows:
    if kw not in cache:
        url, credit = a.fetch_wikimedia_image(kw)
        parts = kw.lower().split()
        fname = unquote(url or "").lower().replace("_", " ")
        ok = bool(url) and parts[0] in fname and parts[-1] in fname
        cache[kw] = (url, credit) if ok else (None, None)
        time.sleep(0.3)
    url, credit = cache[kw]
    if not url:
        continue
    found += 1
    print(f"[인물] {aid} {kw!r} → 위키 인물 사진")
    if not DRY:
        stored = store_image(url, key_hint=f"wiki_{kw}")
        if stored:
            updates.append((aid, stored, credit or "", "예시 사진 → 인물 사진"))
print(f"③ 예시 사진 기사 중 인물명 키워드 {len(person_rows)}건, 인물 사진 찾음 {found}건")

for i, (aid, url, credit, why) in enumerate(updates):
    if i % 50 == 0:
        wait_wal_drain(cur)
    cur.execute("update articles set image_url=%s, image_credit=%s where id=%s", (url or None, credit or None, aid))
print(f"교체 완료 {len(updates)}건 (DRY={DRY})")
