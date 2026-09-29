# -*- coding: utf-8 -*-
"""scripts/cache_art_wiki.py
---------------------------
art_weekly_writer.py가 기사를 쓸 때마다 위키백과를 라이브로 조회하던 근거자료(작가 소개·
작품 문서)를 미리 받아 scripts/data/art_wiki_cache.json에 저장(깃에 커밋)한다 — 매일
외부 호출을 줄이고, 검색이 그날그날 엉뚱한 문서를 잡는 불안정성도 없앤다(같은 작가는
한 번만 조회). 또 라이브 경로는 REST summary(도입부 한 문단, 갈렌칼레라 280자)만 가져와
근거가 빈약했는데, 여기선 본문 앞부분(최대 1500자)까지 받아 작가 근거를 크게 늘린다.

키: "artist:<artist_en>" (전체 작품 대상, 작가당 1회) / "work:<title_en>|<artist_en>" (Commons
큐레이션 배치만 — 나머지는 미술관 해설이 이미 충분). 값은 텍스트(없으면 "" — 재시도 방지).
art_weekly_writer.py는 캐시에 키가 있으면 그 값을 쓰고, 없을 때만 라이브로 조회한다.

실행: python scripts/cache_art_wiki.py [--limit N]   (중단 후 재실행하면 이어서 진행)
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(__file__))
from art_weekly_writer import _wiki_search_title, _WIKI_UA  # noqa: E402

HERE = os.path.dirname(__file__)
DATA_PATH = os.path.join(HERE, "data", "art_weekly_global_artworks.json")
CACHE_PATH = os.path.join(HERE, "data", "art_wiki_cache.json")
MAX_CHARS = 1500  # fabrication_guard의 Gemini 대조창(source_prompt[:3000])을 넘기지 않도록 상한
DELAY = 0.5

_CUT_HEADINGS = re.compile(r"^==\s*(See also|References|External links|Notes|Further reading|Sources|Bibliography|"
                           r"Gallery|각주|외부 링크|같이 보기|참고 문헌|참고 자료|바깥 고리)\s*==\s*$", re.I)


def _rich_extract(title: str, lang: str) -> str:
    try:
        r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", headers=_WIKI_UA, timeout=20, params={
            "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1,
            "titles": title, "format": "json"})
        if r.status_code != 200:
            return ""
        page = next(iter(r.json().get("query", {}).get("pages", {}).values()), {})
        raw = page.get("extract") or ""
    except Exception:
        return ""
    lines = []
    for ln in raw.split("\n"):
        if _CUT_HEADINGS.match(ln.strip()):
            break
        if re.match(r"^=+\s*.*?\s*=+$", ln.strip()):
            continue  # 소제목 줄은 제거(본문만)
        if ln.strip():
            lines.append(ln.strip())
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        end = max(cut.rfind(". "), cut.rfind("다. "), cut.rfind("\n"))
        text = cut[:end + 1] if end > MAX_CHARS * 0.5 else cut
    return text.strip()


def fetch_rich(query_ko: str, query_en: str) -> str:
    """fetch_wikipedia_grounding과 같은 언어 우선순위(한국어 우선, 200자 미만이면 영어)."""
    t = _wiki_search_title(query_ko, "ko")
    text = _rich_extract(t, "ko") if t else ""
    if len(text) < 200:
        t2 = _wiki_search_title(query_en, "en")
        text_en = _rich_extract(t2, "en") if t2 else ""
        if len(text_en) > len(text):
            text = text_en
    return text


def main():
    data = json.load(open(DATA_PATH, encoding="utf-8"))
    cache = json.load(open(CACHE_PATH, encoding="utf-8")) if os.path.exists(CACHE_PATH) else {}

    jobs = {}
    for a in data:
        name = (a.get("artist_en") or "").strip()
        if len(name) >= 3 and not name.lower().startswith("unknown"):
            k = f"artist:{name}"
            jobs.setdefault(k, (a["artist_ko"], f"{name} painter"))
        if "commons.wikimedia.org" in (a.get("direct_image_url") or ""):
            jobs.setdefault(f"work:{a['title_en']}|{name}",
                            (f"{a['artist_ko']} {a['title_ko']}", a["wiki_query"]))
    todo = [(k, q) for k, q in jobs.items() if k not in cache]
    if "--limit" in sys.argv:
        todo = todo[:int(sys.argv[sys.argv.index("--limit") + 1])]
    print(f"전체 {len(jobs)}건 중 캐시 없음 {len(todo)}건")

    lock = threading.Lock()
    done = [0]

    def work(item):
        k, (qk, qe) = item
        time.sleep(DELAY)
        try:
            text = fetch_rich(qk, qe)
        except Exception as e:
            print(f"  ⚠️ {k[:50]} 실패: {str(e)[:60]}")
            return
        with lock:
            cache[k] = text
            done[0] += 1
            if done[0] % 50 == 0:
                json.dump(cache, open(CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
                print(f"  … {done[0]}/{len(todo)}건 (저장)", flush=True)

    with ThreadPoolExecutor(2) as ex:
        list(ex.map(work, todo))
    json.dump(cache, open(CACHE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    filled = sum(1 for v in cache.values() if v)
    print(f"완료: 캐시 {len(cache)}건(내용 있음 {filled}건, 없음 {len(cache) - filled}건)")


if __name__ == "__main__":
    main()
