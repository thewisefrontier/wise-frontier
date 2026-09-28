# -*- coding: utf-8 -*-
"""미술 후보 추가 수집원 2종(harvest_art_curated.py가 호출). 2026-09-28.

1) 월터스 미술관(GitHub WaltersArtMuseum/api-thewalters-org 의 art.csv·media.csv·creators.csv, CC0):
   에티오피아 성화·러시아 이콘·일본/중국 회화. 해설문(Description) 250자 이상만.
2) 위키백과 작품 문서: 사용자 지시("저작권 상관없다, 언론은 써도 된다")로 사후 70년이 안 지난 근현대 작가도 포함.
   영어 위키백과 'Category:Paintings by {작가}' 하위의 작품 문서(=유명 작품)와 그 문서의 대표 이미지(비자유 저해상도
   이미지 포함)를 쓴다. wiki_query를 문서 제목 그대로 줘서 근거자료가 정확히 그 문서로 잡힌다.
"""
import csv
import re
import sys
import time

import requests

UA = {"User-Agent": "NewsFinalBot/1.0 (https://newsfinal.co.kr)"}
WIKI = "https://en.wikipedia.org/w/api.php"


def _plain(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t or "")).strip()


def from_walters(art_csv, media_csv, creators_csv, geo_of_en):
    csv.field_size_limit(10**8)
    media = {}
    for r in csv.DictReader(open(media_csv, encoding="utf-8-sig")):
        if r["MediaType"] == "Image" and (r["IsPrimary"] == "1" or r["ObjectID"] not in media):
            media[r["ObjectID"]] = r["ImageURL"]
    creators = {}
    for r in csv.DictReader(open(creators_csv, encoding="utf-8-sig")):
        creators[r.get("id", "")] = ((r.get("name") or "").strip(), (r.get("date") or "").strip())
    out = []
    for r in csv.DictReader(open(art_csv, encoding="utf-8-sig")):
        if r["Classification"] != "Painting & Drawing" or r["ObjectID"] not in media:
            continue
        title = r["Title"].strip()
        if re.search(r"leaf|folio|manuscript|album|page|binding|gospels|psalter|sketch|study|drawing|ostracon", title, re.I):
            continue
        if not re.search(r"paint|tempera|oil|ink|opaque|watercolor|gouache|pigment|panel|gold", r["Medium"], re.I):
            continue
        text = _plain(r["Description"])
        if len(text) < 250:
            continue
        cid = (r["Creators"] or "").split("|")[0].strip()
        name, cdate = creators.get(cid, ("", ""))
        culture = r["Culture"].strip()
        if not name or name.lower() in ("unknown", "anonymous") or re.search(r"unknown|anonymous", name, re.I):
            if not culture:
                continue
            name = f"{culture} tradition"  # 작자 미상 이콘 등은 문화권을 작가 자리에 — 해설문이 풍부한 것만 통과시킴
        g = geo_of_en(f"{culture} {cdate}")  # 제목은 제외("Flight into Egypt" 같은 서구 성화가 아프리카로 분류됨)
        facts = [f"작가/전통: {name}", f"제작 시기: {r['DateText']}", f"재질/기법: {r['Medium']}", f"문화권: {culture}",
                 "분류: Paintings", "현재 월터스 미술관(볼티모어)에 소장.", f"[미술관 해설] {text[:1800]}"]
        out.append({
            "title_ko": title, "title_en": title, "artist_ko": name, "artist_en": name, "year_label": r["DateText"],
            "country": g[0], "country_flag": g[1], "region": g[2], "wiki_query": f"{title} {name}",
            "direct_image_url": media[r["ObjectID"]],
            "image_credit": "이미지 출처: The Walters Art Museum (CC0 퍼블릭 도메인)",
            "museum_grounding": " ".join(f for f in facts if not f.endswith(": ")),
            "_len": len(text),
        })
    # 작가당 3점(문화권 표기뿐인 작자 미상은 문화권당 6점) — 해설이 긴 것 우선
    cnt, kept = {}, []
    for a in sorted(out, key=lambda x: -x["_len"]):
        cap = 6 if a["artist_en"].endswith(" tradition") else 3
        if cnt.get(a["artist_en"], 0) < cap:
            cnt[a["artist_en"]] = cnt.get(a["artist_en"], 0) + 1
            kept.append(a)
    for a in kept:
        a.pop("_len", None)
    print(f"월터스 회화 후보 {len(kept)}건(상한 적용 전 {len(out)})", file=sys.stderr)
    return kept


def _lead_image(title):
    """문서 본문에 실린 이미지 중 첫 jpg/png(=보통 인포박스 작품 이미지, 비자유 포함). (url, w, h) 또는 None."""
    try:
        imgs = requests.get(WIKI, headers=UA, timeout=30, params={
            "action": "parse", "format": "json", "page": title, "prop": "images", "redirects": 1}).json()["parse"]["images"]
        for f in imgs:
            if not re.search(r"\.(jpe?g|png)$", f, re.I) or re.search(r"logo|icon|flag|commons|wikiquote|signature|map|portrait_of_the_artist", f, re.I):
                continue
            ii = requests.get(WIKI, headers=UA, timeout=30, params={
                "action": "query", "format": "json", "prop": "imageinfo", "iiprop": "url|size", "iiurlwidth": 1600,
                "titles": "File:" + f}).json()["query"]["pages"]
            info = (list(ii.values())[0].get("imageinfo") or [{}])[0]
            if info.get("thumburl") and min(info.get("width", 0), info.get("height", 0)) >= 450:
                return info["thumburl"], info["width"], info["height"]
    except Exception:
        return None
    return None


def wiki_paintings(name_en, n=3):
    """영어 위키백과 'Category:Paintings by {name}'의 작품 문서 최대 n개 + 본문 이미지. 없으면 []."""
    try:
        cat = requests.get(WIKI, headers=UA, timeout=30, params={
            "action": "query", "format": "json", "list": "categorymembers", "cmtitle": f"Category:Paintings by {name_en}",
            "cmlimit": 60, "cmtype": "page"}).json()["query"]["categorymembers"]
        titles = [c["title"] for c in cat if not re.match(r"^(List of|Category:)", c["title"])][:40]
        if not titles:
            return []
        pages = requests.get(WIKI, headers=UA, timeout=40, params={
            "action": "query", "format": "json", "prop": "extracts", "exintro": 1, "explaintext": 1, "exlimit": "max",
            "titles": "|".join(titles)}).json()["query"]["pages"].values()
    except Exception as e:
        print(f"  ⚠️ {name_en} 위키 조회 실패: {e}", file=sys.stderr)
        return []
    out = []
    for p in sorted(pages, key=lambda x: -len(x.get("extract") or "")):
        ext = (p.get("extract") or "").strip()
        if len(ext) < 300 or re.search(r"mural|fresco|series|list of", p["title"], re.I) and False:
            continue
        li = _lead_image(p["title"])
        if not li:
            continue
        out.append({"title": p["title"], "image": li[0], "extract": ext[:1800]})
        if len(out) >= n:
            break
    return out


def modern_records(artists):
    """artists: (영문명, 한글명, 국가, 국기, region). 반환: 기록 목록."""
    recs = []
    for en, ko, country, flag, region in artists:
        got = wiki_paintings(en)
        time.sleep(0.4)
        print(f"  {en}: {len(got)}점", file=sys.stderr)
        for g in got:
            recs.append({
                "title_ko": g["title"], "title_en": g["title"], "artist_ko": ko, "artist_en": en, "year_label": "",
                "country": country, "country_flag": flag, "region": region, "wiki_query": g["title"],
                "direct_image_url": g["image"],
                "image_credit": "이미지 출처: 위키백과(작품 소개용 이미지)",
                "museum_grounding": f"작가: {en}({country}). 분류: Paintings. [위키백과 작품 문서 요약] {g['extract']}",
            })
    return recs
