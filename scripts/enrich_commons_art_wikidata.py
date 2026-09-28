# -*- coding: utf-8 -*-
"""scripts/enrich_commons_art_wikidata.py
-------------------------------------------
art_weekly_global_artworks.json 중 "Commons 직접 큐레이션" 배치(harvest_art_curated.py/
harvest_art_extra.py 산출물, 예: 네덜란드 문화유산청·핀란드 국립미술관 등)는
museum_grounding이 "작가: X(국적, 몰년). 분류: Paintings 출처: 위키미디어 커먼즈 분류..."
한 줄(중앙값 101자)뿐이다. 반면 Cleveland/AIC/Walters 오픈API 배치는 이미 제작연도·기법·
미술관 해설까지 담겨 중앙값 800~1000자다(2026-09-29 확인, id=293944 사족 사고 조사 중 발견).

이 스크립트는 그 "얇은" Commons 배치만 골라, 이미 쓰는 Wikimedia Commons 이미지 파일에
붙은 정보(extmetadata)에서 연결된 Wikidata 항목을 찾아(불안정한 SPARQL 엔드포인트가
아니라 단건 GET인 Special:EntityData/wbgetentities 사용) 제작연도·재질/기법·장르·크기·
소장처·인벤토리 번호 등 실제 사실을 museum_grounding에 덧붙인다. 항목을 못 찾으면
건드리지 않는다(없는 내용을 채우지 않는다는 이 프로젝트 원칙 유지).

실행: python scripts/enrich_commons_art_wikidata.py [--dry-run] [--limit N]
"""
import json
import os
import re
import sys
import time

import requests

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "art_weekly_global_artworks.json")
UA = {"User-Agent": "NewsFinalArtEnrich/1.0 (contact: overmandol@gmail.com)"}
THIN_GROUNDING_MAX = 300  # 이 길이 미만인 것만 대상(이미 풍부한 Cleveland/AIC/Walters는 건드리지 않음)
REQUEST_DELAY = 0.3

_FACT_PROPS = {
    "P571": "제작 시기", "P186": "재질/기법", "P136": "장르",
    "P195": "소장 기관", "P276": "소장 위치", "P217": "인벤토리 번호",
}


def _commons_title_from_url(url: str) -> str | None:
    m = re.search(r"Special:FilePath/([^?]+)", url or "")
    return f"File:{requests.utils.unquote(m.group(1))}" if m else None


def _extmetadata(title: str) -> dict:
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=20, params={
        "action": "query", "titles": title, "prop": "imageinfo", "iiprop": "extmetadata", "format": "json"})
    r.raise_for_status()
    page = next(iter(r.json().get("query", {}).get("pages", {}).values()), {})
    return page.get("imageinfo", [{}])[0].get("extmetadata", {})


def _find_qid(meta: dict) -> str | None:
    m = re.findall(r"wikidata\.org/wiki/(Q\d+)", json.dumps(meta))
    return m[0] if m else None


def _wd_entity(qid: str) -> dict:
    r = requests.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json", headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()["entities"][qid]


def _labels_batch(qids: list) -> dict:
    """참조된 항목 QID들의 영문 라벨을 한 번에 조회(단위·재질·장르 등 사람이 읽을 이름 필요)."""
    out = {}
    qids = [q for q in dict.fromkeys(qids) if q]
    for i in range(0, len(qids), 50):
        chunk = qids[i:i + 50]
        r = requests.get("https://www.wikidata.org/w/api.php", headers=UA, timeout=20, params={
            "action": "wbgetentities", "ids": "|".join(chunk), "props": "labels", "languages": "en", "format": "json"})
        r.raise_for_status()
        for qid, ent in r.json().get("entities", {}).items():
            out[qid] = ent.get("labels", {}).get("en", {}).get("value", qid)
    return out


def build_facts(entity: dict) -> str:
    claims = entity.get("claims", {})
    qids_to_resolve = []
    raw = {}
    for prop in _FACT_PROPS:
        c = claims.get(prop)
        if not c:
            continue
        vals = []
        for x in c[:4]:
            v = x.get("mainsnak", {}).get("datavalue", {}).get("value")
            if v is None:
                continue
            if isinstance(v, dict) and "id" in v:
                qids_to_resolve.append(v["id"])
                vals.append(("qid", v["id"]))
            elif isinstance(v, dict) and "time" in v:
                m = re.match(r"[+-](\d{1,4})-(\d{2})-(\d{2})", v["time"])
                vals.append(("text", m.group(1) if m else v["time"]))
            else:
                vals.append(("text", str(v)))
        if vals:
            raw[prop] = vals

    height = claims.get("P2048", [{}])[0].get("mainsnak", {}).get("datavalue", {}).get("value")
    width = claims.get("P2049", [{}])[0].get("mainsnak", {}).get("datavalue", {}).get("value")
    size_unit_qid = None
    if height and isinstance(height, dict):
        size_unit_qid = (height.get("unit") or "").rsplit("/", 1)[-1] or None
        qids_to_resolve.append(size_unit_qid)

    labels = _labels_batch(qids_to_resolve)

    parts = []
    for prop, label in _FACT_PROPS.items():
        vals = raw.get(prop)
        if not vals:
            continue
        shown = [labels.get(v, v) if t == "qid" else v for t, v in vals]
        parts.append(f"{label}: {', '.join(dict.fromkeys(shown))}")
    if height and width and isinstance(height, dict) and isinstance(width, dict):
        unit = labels.get(size_unit_qid, "") if size_unit_qid else ""
        parts.append(f"크기: {height['amount'].lstrip('+')} x {width['amount'].lstrip('+')} {unit}".strip())

    title = entity.get("labels", {}).get("en", {}).get("value")
    if title:
        parts.insert(0, f"원제(영문): {title}")
    return " / ".join(parts)


def enrich(dry_run: bool = False, limit: int | None = None):
    data = json.load(open(DATA_PATH, encoding="utf-8"))
    targets = [a for a in data if "commons.wikimedia.org" in (a.get("direct_image_url") or "")
               and len(a.get("museum_grounding") or "") < THIN_GROUNDING_MAX]
    print(f"대상(Commons 큐레이션 + 근거자료 {THIN_GROUNDING_MAX}자 미만): {len(targets)}건")
    if limit:
        targets = targets[:limit]

    done = skipped = failed = 0
    for i, a in enumerate(targets):
        title = _commons_title_from_url(a["direct_image_url"])
        if not title:
            skipped += 1
            continue
        try:
            meta = _extmetadata(title)
            time.sleep(REQUEST_DELAY)
            qid = _find_qid(meta)
            if not qid:
                skipped += 1
                continue
            entity = _wd_entity(qid)
            time.sleep(REQUEST_DELAY)
            facts = build_facts(entity)
            if not facts:
                skipped += 1
                continue
        except Exception as e:
            print(f"  ⚠️ [{i+1}/{len(targets)}] {a.get('title_ko','')[:40]} 실패: {str(e)[:80]}")
            failed += 1
            continue

        a["museum_grounding"] = (a.get("museum_grounding") or "") + f" [위키데이터 보강] {facts}"
        done += 1
        print(f"  ✓ [{i+1}/{len(targets)}] {a.get('title_ko','')[:40]} → {facts[:90]}")

    print(f"\n완료: 보강 {done}건, 근거 못 찾음 {skipped}건, 실패 {failed}건")
    if not dry_run and done:
        json.dump(data, open(DATA_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"저장 완료: {DATA_PATH}")
    elif dry_run:
        print("[dry-run] 저장 안 함")


if __name__ == "__main__":
    enrich(dry_run="--dry-run" in sys.argv,
           limit=(int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None))
