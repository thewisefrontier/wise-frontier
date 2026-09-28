# -*- coding: utf-8 -*-
"""
scripts/harvest_literature.py
--------------------------------
문학작품 소개 기사(literature_writer.py)가 쓸 작품 목록을 위키데이터로 확정해
scripts/data/literature_works.json에 저장한다.

2026-09-27 신설(사용자: "문학 작품 소개 기사도 쓸 수 있을까?" → "해외 작품은?").
소스 3종:
  1. 구텐베르크 인기 고전(gutendex.com) — 구텐베르크 ID(P2034)로 위키데이터 작품에 1:1 연결
  2. 문학상 수상작(부커·국제 부커·퓰리처·전미도서상·공쿠르·아쿠타가와·
     여성문학상·나오키·노벨) — 위키데이터 P166(수상)으로 확정
  3. 한국·동아시아 큐레이션 목록 — 한국어 위키백과 문서 → 위키데이터 작품으로 확정

모든 작품은 "위키데이터 작품 항목(P50 저자 보유) + 한국어/영어 위키백과 문서 제목"
까지 여기서 확정해 둔다 — 런타임에 검색하지 않으므로, 미술 기능에서 반복됐던
"검색어는 맞았는데 엉뚱한 문서(동명 드라마·동명 식물 등)가 잡히는" 사고 부류가
구조적으로 없다.

저작권 메모: 작품 "소개"는 작품을 복제하지 않으므로 퍼블릭도메인일 필요가 없다
(사용자 지적 — 미술은 작품 사진 자체가 콘텐츠라 PD가 필요했던 것과 다름).
그래서 구텐베르크의 "미국 기준 PD" 목록에 한국 기준 보호작이 섞여도 문제되지
않는다. 이미지만은 위키미디어 커먼즈 파일(자유 라이선스)만 쓴다.

실행: python scripts/harvest_literature.py   (수 분 소요, 위키데이터 호출 수백 회)
"""
import json
import os
import random
import re
import sys
import time

import requests

UA = {"User-Agent": "NewsFinal-LitWriter/1.0 (+https://newsfinal.co.kr)"}
WD = "https://www.wikidata.org/w/api.php"
OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "literature_works.json")

# 페이지당 32권. 고전은 5일 중 1일만 나가서(literature_writer.ROTATION) 연 70여 편이면
# 충분 — 한국·동아시아 큐레이션과 합쳐 1년 이상 분량이 되는 선에서 줄였다.
GUTENBERG_PAGES = 5
LIT_WORDS = ("fiction", "poetry", "poems", "drama", "plays", "novel", "short stories", "literature")
EXCLUDE_WORDS = ("erotic", "sex ", "sexual")

PRIZES = {
    "Q160082": "부커상", "Q2052291": "국제 부커상", "Q833633": "퓰리처상(소설)",
    "Q3873144": "전미도서상(소설)", "Q187300": "공쿠르상", "Q424160": "아쿠타가와상",
    "Q18884": "여성문학상", "Q224573": "나오키상", "Q37922": "노벨문학상",
    # 2026-09-27 장르 문학상 추가(깃허브 bookrank 참고 — 순수문학 상만으론 독자층이
    # 좁고, 장르 수상작이 대중성이 높다).
    "Q255032": "휴고상(장편)", "Q266012": "네뷸러상(장편)", "Q113436830": "에드거상(장편)",
    "Q708830": "아서 C. 클라크상",
}

# 한국어 위키백과 문서 제목 기준. 틀린 제목·동음이의 문서는 resolve 단계에서
# 자동으로 걸러지고(작품 항목 + P50 저자 필수), " (소설)" 등 접미 변형도 자동 시도.
CURATED_KOWIKI = [
    # 한국 근현대
    "무정", "날개", "운수 좋은 날", "메밀꽃 필 무렵", "봄봄", "동백꽃", "감자", "배따라기",
    "삼대", "탁류", "태평천하", "레디메이드 인생", "진달래꽃", "님의 침묵",
    "하늘과 바람과 별과 시", "오감도", "광장", "무진기행", "소나기", "사랑손님과 어머니",
    "벙어리 삼룡이", "화수분", "표본실의 청개구리", "상록수", "흙", "백치 아다다", "사하촌",
    "모래톱 이야기", "수난이대", "카인의 후예", "꺼삐딴 리", "오발탄", "서울, 1964년 겨울",
    "난장이가 쏘아올린 작은 공", "토지", "태백산맥", "아리랑", "혼불", "객주", "장길산",
    "우리들의 일그러진 영웅", "사람의 아들", "관촌수필", "당신들의 천국", "서편제",
    "엄마를 부탁해", "채식주의자", "소년이 온다", "작별하지 않는다", "흰", "82년생 김지영",
    "칼의 노래", "남한산성", "살인자의 기억법", "아몬드", "완득이", "나목",
    "그 많던 싱아는 누가 다 먹었을까", "외딴방", "고래", "원미동 사람들", "삼포 가는 길",
    "객지", "7년의 밤", "종의 기원",
    # 한국 고전
    "춘향전", "심청전", "홍길동전", "구운몽", "사씨남정기", "흥부전", "토끼전", "금오신화",
    "열하일기", "관동별곡", "사미인곡", "한중록", "박씨전", "허생전", "양반전", "운영전",
    # 동아시아
    "삼국지연의", "수호전", "서유기", "홍루몽", "아Q정전", "광인일기", "나는 고양이로소이다",
    "마음", "라쇼몬", "인간 실격", "설국", "노르웨이의 숲", "금각사", "겐지 이야기", "도련님",
]
SUFFIXES = ("", " (소설)", " (시집)", " (시)", " (고전 소설)")


def get_json(url, params=None, retries=3, timeout=30):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def wd_search(query: str, limit: int = 500) -> list:
    out, offset = [], 0
    while offset < limit:
        d = get_json(WD, {"action": "query", "list": "search", "srsearch": query, "format": "json",
                          "srlimit": 50, "sroffset": offset})
        hits = (d or {}).get("query", {}).get("search", [])
        out += [h["title"] for h in hits]
        if len(hits) < 50:
            break
        offset += 50
        time.sleep(0.2)
    return out


def wd_entities(qids) -> dict:
    qids, out = list(dict.fromkeys(qids)), {}
    for i in range(0, len(qids), 50):
        d = get_json(WD, {"action": "wbgetentities", "ids": "|".join(qids[i:i + 50]),
                          "props": "claims|sitelinks|labels", "languages": "ko|en", "format": "json"})
        out.update((d or {}).get("entities", {}))
        time.sleep(0.2)
    return out


def claim_values(ent, pid) -> list:
    vals = []
    for c in ent.get("claims", {}).get(pid, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, dict) and "id" in v:
            vals.append(v["id"])
        elif isinstance(v, dict) and "time" in v:
            vals.append(v["time"])
        elif isinstance(v, dict) and "text" in v:
            vals.append(v["text"])
        elif v is not None:
            vals.append(v)
    return vals


def year_of(time_str) -> int | None:
    m = re.match(r"([+-])(\d+)-", time_str or "")
    if not m:
        return None
    y = int(m.group(2))
    return -y if m.group(1) == "-" else y


def label(ent, lang):
    return ent.get("labels", {}).get(lang, {}).get("value")


def sitelink(ent, site):
    return ent.get("sitelinks", {}).get(site, {}).get("title")


def award_year(ent, prize_qid):
    for c in ent.get("claims", {}).get("P166", []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value", {})
        if isinstance(v, dict) and v.get("id") == prize_qid:
            for q in c.get("qualifiers", {}).get("P585", []):
                y = year_of(q.get("datavalue", {}).get("value", {}).get("time"))
                if y:
                    return y
    return None


# ── 소스 1: 구텐베르크 ─────────────────────────────────────
def gutenberg_works() -> dict:
    """{work_qid: 출처설명}"""
    books, url = [], "https://gutendex.com/books/?sort=popular"
    for _ in range(GUTENBERG_PAGES):
        d = get_json(url, timeout=60)
        if not d:
            break
        for b in d.get("results", []):
            tags = " ".join(b.get("subjects", []) + b.get("bookshelves", [])).lower()
            if any(w in tags for w in LIT_WORDS) and not any(w in tags for w in EXCLUDE_WORDS):
                books.append(b)
        url = d.get("next")
        if not url:
            break
    print(f"  구텐베르크 문학 후보 {len(books)}권", file=sys.stderr)

    item_of_gid = {}
    for b in books:
        hits = wd_search(f"haswbstatement:P2034={b['id']}", limit=1)
        if hits:
            item_of_gid[b["id"]] = hits[0]
        time.sleep(0.15)
    ents = wd_entities(item_of_gid.values())
    works = {}
    for gid, q in item_of_gid.items():
        ent = ents.get(q, {})
        edition_of = claim_values(ent, "P629")  # 판본 항목이면 원작품으로 올라감
        works[edition_of[0] if edition_of else q] = "구텐베르크 인기 고전"
    print(f"  구텐베르크 → 위키데이터 작품 {len(works)}건", file=sys.stderr)
    return works


# ── 소스 2: 문학상 ─────────────────────────────────────────
def prize_works() -> dict:
    """{work_qid: [수상 설명, ...]}"""
    works = {}
    for prize, name in PRIZES.items():
        qids = wd_search(f"haswbstatement:P166={prize}", limit=400)
        ents = wd_entities(qids)
        n_before = len(works)
        people_works = []
        for q, ent in ents.items():
            y = award_year(ent, prize)
            note = f"{name}" + (f"({y})" if y else "")
            if "Q5" in claim_values(ent, "P31"):  # 작가에게 준 상 → 대표작(P800) 1편
                nw = claim_values(ent, "P800")
                if nw:
                    people_works.append((nw[0], f"작가 {note} 수상"))
            elif claim_values(ent, "P50"):
                works.setdefault(q, []).append(f"{note} 수상작")
        for q, note in people_works:
            works.setdefault(q, []).append(note)
        print(f"  {name}: +{len(works) - n_before}건", file=sys.stderr)
    return works


# ── 소스 3: 한국·동아시아 큐레이션 ────────────────────────────
def curated_works() -> dict:
    """{work_qid: 출처설명}. 한국어 위키백과 문서 → 위키데이터 항목(pageprops)."""
    works = {}
    for base in CURATED_KOWIKI:
        for suf in SUFFIXES:
            title = base + suf
            d = get_json("https://ko.wikipedia.org/w/api.php",
                         {"action": "query", "titles": title, "prop": "pageprops",
                          "redirects": 1, "format": "json"})
            pages = (d or {}).get("query", {}).get("pages", {})
            page = next(iter(pages.values()), {}) if pages else {}
            props = page.get("pageprops", {})
            if "missing" in page or "disambiguation" in props or not props.get("wikibase_item"):
                continue
            q = props["wikibase_item"]
            ent = wd_entities([q]).get(q, {})
            if claim_values(ent, "P50"):  # 저자가 있는 작품 항목만(인물·지명·영화 문서 배제)
                works[q] = "한국·동아시아 문학 큐레이션"
                break
        time.sleep(0.1)
    print(f"  큐레이션 {len(CURATED_KOWIKI)}건 중 확정 {len(works)}건", file=sys.stderr)
    return works


# ── 해외 신간(실시간) ─────────────────────────────────────
# 2026-09-27 사용자 요청("구작보다는 신간, 해외 작품 위주로") — 키 없이 신간을 잡는
# 방법으로 영어 위키백과 연도별 소설 분류("Category:2025 novels" 및 하위 국가별 분류)를
# 쓴다. 위키백과 문서가 생길 만큼 주목받은 작품이라 품질이 걸러져 있고 근거자료도 있다.
# 이 목록은 JSON에 굳히지 않고 literature_writer.py가 실행 때마다 새로 가져간다.
EXCLUDED_SUBCAT_WORDS = ("graphic novel", "comic", "manga", "light novel")


def _cat_members(cat: str, cmtype: str) -> list:
    out, cont = [], {}
    while True:
        d = get_json("https://en.wikipedia.org/w/api.php", {
            "action": "query", "list": "categorymembers", "cmtitle": cat, "cmtype": cmtype,
            "cmlimit": 500, "format": "json", **cont})
        if not d:
            return out
        out += [m["title"] for m in d.get("query", {}).get("categorymembers", [])]
        if "continue" not in d:
            return out
        cont = d["continue"]


def recent_works(years) -> list:
    """해당 연도들의 영어 위키백과 소설 분류 → 작품 레코드. 여러 언어 위키에 실린
    작품(sitelinks 많음 = 국제적으로 주목받음)이 앞에 오도록 정렬."""
    titles = set()
    for y in years:
        cat = f"Category:{y} novels"
        titles |= set(_cat_members(cat, "page"))
        for sub in _cat_members(cat, "subcat"):
            if not any(w in sub.lower() for w in EXCLUDED_SUBCAT_WORDS):
                titles |= set(_cat_members(sub, "page"))
    titles, qids = sorted(titles), []
    for i in range(0, len(titles), 50):
        d = get_json("https://en.wikipedia.org/w/api.php", {
            "action": "query", "titles": "|".join(titles[i:i + 50]), "prop": "pageprops",
            "ppprop": "wikibase_item|disambiguation", "redirects": 1, "format": "json"})
        for p in (d or {}).get("query", {}).get("pages", {}).values():
            props = p.get("pageprops", {})
            if props.get("wikibase_item") and "disambiguation" not in props:
                qids.append(props["wikibase_item"])
    recs = build_records({q: {"src": "recent", "notes": []} for q in qids})
    oldest = min(years)
    recs = [r for r in recs if r.get("pub_year") is None or r["pub_year"] >= oldest]
    recs.sort(key=lambda r: (-(r.get("sitelinks") or 0), -(r.get("pub_year") or 0)))
    return recs


def build_records(sources: dict) -> list:
    """sources: {qid: {"src": 출처종류, "notes": [...]}}"""
    work_ents = wd_entities(sources.keys())
    author_ids = []
    for ent in work_ents.values():
        author_ids += claim_values(ent, "P50")[:1]
    author_ents = wd_entities(author_ids)
    country_ids = []
    for ent in author_ents.values():
        country_ids += claim_values(ent, "P27")[-1:]  # 가장 나중(최근) 국적
    country_ents = wd_entities(country_ids)

    records = []
    for q, meta in sources.items():
        ent = work_ents.get(q)
        if not ent:
            continue
        excluded = {"Q11424", "Q5398426", "Q7889", "Q24862"}  # 영화·TV시리즈·비디오게임·단편영화
        if excluded & set(claim_values(ent, "P31")):
            continue
        kowiki, enwiki = sitelink(ent, "kowiki"), sitelink(ent, "enwiki")
        authors = claim_values(ent, "P50")
        if not authors or not (kowiki or enwiki):
            continue
        a = author_ents.get(authors[0], {})
        country = country_ents.get((claim_values(a, "P27") or [None])[-1], {})
        title_ko = label(ent, "ko") or (re.sub(r"\s*\(.*\)$", "", kowiki) if kowiki else None)
        img_work = (claim_values(ent, "P18") or [None])[0]
        img_author = (claim_values(a, "P18") or [None])[0]
        records.append({
            "qid": q,
            "title_ko": title_ko,
            "title_en": label(ent, "en") or enwiki,
            "original_title": (claim_values(ent, "P1476") or [None])[0],
            "pub_year": year_of((claim_values(ent, "P577") or [None])[0]),
            "kowiki": kowiki, "enwiki": enwiki,
            "author_qid": authors[0],
            "author_ko": label(a, "ko") or (re.sub(r"\s*\(.*\)$", "", sitelink(a, "kowiki")) if sitelink(a, "kowiki") else None),
            "author_en": label(a, "en"),
            "author_birth": year_of((claim_values(a, "P569") or [None])[0]),
            "author_death": year_of((claim_values(a, "P570") or [None])[0]),
            "author_country_ko": label(country, "ko"),
            "author_kowiki": sitelink(a, "kowiki"), "author_enwiki": sitelink(a, "enwiki"),
            "image_file": img_work or img_author,
            "image_kind": "work" if img_work else ("author" if img_author else None),
            "sitelinks": len(ent.get("sitelinks", {})),
            "source": meta["src"],
            "notes": meta["notes"],
        })
    return records


def main():
    sources = {}

    def add(qid, src, note=None):
        s = sources.setdefault(qid, {"src": src, "notes": []})
        if note and note not in s["notes"]:
            s["notes"].append(note)

    for q, note in curated_works().items():
        add(q, "curated")
    for q, note in gutenberg_works().items():
        add(q, "gutenberg")
    for q, notes in prize_works().items():
        for n in notes:
            add(q, sources.get(q, {}).get("src", "prize"), n)

    records = build_records(sources)
    print(f"최종 {len(records)}건 (위키백과 문서 있는 작품만)", file=sys.stderr)

    # 발행 순서: 출처별로 고정 시드 셔플 후 번갈아 섞는다 — 고전 수백 편이 연달아
    # 나오지 않게. literature_writer는 이 순서대로 "아직 안 쓴 첫 작품"을 고른다.
    rng = random.Random(20260927)
    buckets = {}
    for r in records:
        buckets.setdefault(r["source"], []).append(r)
    for b in buckets.values():
        rng.shuffle(b)
    ordered, keys = [], sorted(buckets)
    while any(buckets.values()):
        for k in keys:
            if buckets[k]:
                ordered.append(buckets[k].pop())

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=1)
    from collections import Counter
    print(f"저장: {OUT_PATH} — 출처별 {dict(Counter(r['source'] for r in ordered))}", file=sys.stderr)


if __name__ == "__main__":
    main()
