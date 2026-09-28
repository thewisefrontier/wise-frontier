# -*- coding: utf-8 -*-
"""전 세계 회화 후보 목록 수집 → scripts/data/art_weekly_global_artworks.json (art_weekly_writer가 ARTWORKS에 이어붙임).

2026-09-28 개편 — 사용자 지시: "그림만 다루자", "미술은 전 세계 기준(아프리카·동남아 등)", "메트뿐 아니라
루브르나 다른 박물관 건 없나?". 메트 API를 수만 번 호출하다 403 차단당해(57분 헛돎) 폐기, 두 원천으로 교체:

 1) 위키데이터(SPARQL): 회화(P31=Q3305213) + 이미지(P18) + 작가 사후 70년 경과 + 다른 언어 위키 6개 이상(유명도).
    루브르·프라도·에르미타주·국립박물관 등 소장처 무관, 위키백과 문서가 있는 작품만(근거자료 확보).
 2) 메트 공식 CSV(github.com/metmuseum/openaccess, 약 318MB): 동아시아·인도·이슬람 등 비서구 회화 보강.
    API 대신 CSV라 요청 0회. 이미지 URL은 CSV에 없어 발행 때 art_weekly_writer가 작품 1건만 API로 조회.

순서: 서구 / 동·남아시아 / 그 밖(아프리카·중동·중남미·오세아니아…)을 W A W X 패턴으로 섞어 한 지역에 쏠리지 않게.
작가당 PER_ARTIST점 상한. 저작권: 작가 사후 70년(1955 이전 사망) — 작가 불명 메트 항목은 highlight/위키데이터 등재만.

실행: python scripts/harvest_art_global.py [--met-csv MetObjects.csv]
"""
import csv
import json
import os
import random
import re
import sys
import time

import requests

OUT = os.path.join(os.path.dirname(__file__), "data", "art_weekly_global_artworks.json")
UA = {"User-Agent": "NewsFinalBot/1.0 (https://newsfinal.co.kr)"}
PER_ARTIST = 3
MIN_SITELINKS = 6
DEATH_MAX = 1955  # 사후 70년 경과(2026 - 71)

_SPARQL_T = """SELECT ?p ?pLabel ?enLabel ?sl ?c ?cLabel ?cb ?cd ?ctry ?ctryLabel ?collLabel ?inc ?img ?enwiki ?kowiki WHERE {
 %(pre)s
 ?p wdt:P31 wd:Q3305213; wikibase:sitelinks ?sl; wdt:P18 ?img; wdt:P170 ?c.
 FILTER(?sl>=%(sl)d)
 ?c wdt:P570 ?cd. FILTER(YEAR(?cd)<=%(death)d)
 %(ctry)s
 OPTIONAL { ?c wdt:P569 ?cb. }
 OPTIONAL { ?p wdt:P195 ?coll. }
 OPTIONAL { ?p wdt:P571 ?inc. }
 OPTIONAL { ?p rdfs:label ?enLabel. FILTER(LANG(?enLabel)="en") }
 OPTIONAL { ?en schema:about ?p; schema:isPartOf <https://en.wikipedia.org/>; schema:name ?enwiki. }
 OPTIONAL { ?ko schema:about ?p; schema:isPartOf <https://ko.wikipedia.org/>; schema:name ?kowiki. }
 SERVICE wikibase:label { bd:serviceParam wikibase:language "ko,en". }
} LIMIT 30000"""

# 비서구 국적 작가는 위키데이터 등재가 적어 유명도 기준을 낮춰(2개 언어판) 따로 조회 — "미술은 전 세계 기준" 반영.
NON_WEST_COUNTRIES = ("Q252 Q928 Q881 Q869 Q836 Q833 Q424 Q819 Q79 Q115 Q1033 Q258 Q117 Q1028 Q262 Q948 Q114 Q1041 "
                      "Q96 Q155 Q414 Q419 Q739 Q298 Q241 Q717 Q736 Q794 Q796 Q43 Q12560 Q668 Q843 Q854 Q837 Q884 Q17 "
                      "Q148 Q865 Q711 Q408 Q664 Q801 Q822 Q858 Q265 Q230 Q399 Q1044 Q1027 Q916 Q974 Q1005").split()


def sparql_query(sl, values=""):
    # 비서구 조회는 VALUES(국적)를 맨 앞에 둬 그 나라 작가부터 출발하게 한다(뒤에 두면 504 시간 초과)
    ctry = "?c wdt:P27 ?ctry." if values else "OPTIONAL { ?c wdt:P27 ?ctry. }"
    return _SPARQL_T % {"sl": sl, "death": DEATH_MAX, "pre": values, "ctry": ctry}


# 메트 CSV 국가/문화 → (한국어, 국기, region)
GEO = {
    "japan": ("일본", "🇯🇵", "asia"), "china": ("중국", "🇨🇳", "asia"), "korea": ("한국", "🇰🇷", "asia"),
    "india": ("인도", "🇮🇳", "asia"), "nepal": ("네팔", "🇳🇵", "asia"), "tibet": ("티베트", "", "asia"),
    "thailand": ("태국", "🇹🇭", "asia"), "myanmar": ("미얀마", "🇲🇲", "asia"), "burma": ("미얀마", "🇲🇲", "asia"),
    "cambodia": ("캄보디아", "🇰🇭", "asia"), "vietnam": ("베트남", "🇻🇳", "asia"), "indonesia": ("인도네시아", "🇮🇩", "asia"),
    "java": ("인도네시아", "🇮🇩", "asia"), "sri lanka": ("스리랑카", "🇱🇰", "asia"), "pakistan": ("파키스탄", "🇵🇰", "asia"),
    "bangladesh": ("방글라데시", "🇧🇩", "asia"), "iran": ("이란", "🇮🇷", "asia"), "iraq": ("이라크", "🇮🇶", "asia"),
    "turkey": ("튀르키예", "🇹🇷", "asia"), "egypt": ("이집트", "🇪🇬", "africa"), "ethiopia": ("에티오피아", "🇪🇹", "africa"),
    "mexico": ("멕시코", "🇲🇽", "global"), "peru": ("페루", "🇵🇪", "global"), "bolivia": ("볼리비아", "🇧🇴", "global"),
}
GEO.update({
    "nigeria": ("나이지리아", "🇳🇬", "africa"), "ghana": ("가나", "🇬🇭", "africa"), "mali": ("말리", "🇲🇱", "africa"),
    "congo": ("콩고", "🇨🇩", "africa"), "cameroon": ("카메룬", "🇨🇲", "africa"), "ivory coast": ("코트디부아르", "🇨🇮", "africa"),
    "morocco": ("모로코", "🇲🇦", "africa"), "kenya": ("케냐", "🇰🇪", "africa"), "south africa": ("남아프리카공화국", "🇿🇦", "africa"),
    "sudan": ("수단", "🇸🇩", "africa"), "tanzania": ("탄자니아", "🇹🇿", "africa"), "africa": ("아프리카", "", "africa"),
    "syria": ("시리아", "🇸🇾", "asia"), "lebanon": ("레바논", "🇱🇧", "asia"), "afghanistan": ("아프가니스탄", "🇦🇫", "asia"),
    "uzbekistan": ("우즈베키스탄", "🇺🇿", "asia"), "philippines": ("필리핀", "🇵🇭", "asia"), "malaysia": ("말레이시아", "🇲🇾", "asia"),
    "laos": ("라오스", "🇱🇦", "asia"), "mongolia": ("몽골", "🇲🇳", "asia"), "taiwan": ("대만", "🇹🇼", "asia"),
    "brazil": ("브라질", "🇧🇷", "global"), "colombia": ("콜롬비아", "🇨🇴", "global"), "ecuador": ("에콰도르", "🇪🇨", "global"),
    "chile": ("칠레", "🇨🇱", "global"), "argentina": ("아르헨티나", "🇦🇷", "global"), "guatemala": ("과테말라", "🇬🇹", "global"),
    "cuba": ("쿠바", "🇨🇺", "global"), "venezuela": ("베네수엘라", "🇻🇪", "global"), "australia": ("오스트레일리아", "🇦🇺", "global"),
    "new zealand": ("뉴질랜드", "🇳🇿", "global"), "israel": ("이스라엘", "🇮🇱", "asia"), "armenia": ("아르메니아", "🇦🇲", "asia"),
    "georgia": ("조지아", "🇬🇪", "asia"), "ottoman": ("튀르키예", "🇹🇷", "asia"), "persia": ("이란", "🇮🇷", "asia"),
})
# 국적 형용사("Japanese, 1761–1828")도 인식 — 클리블랜드·시카고는 출신지 대신 작가 국적만 적힌 경우가 많다.
GEO.update({
    "japanese": GEO["japan"], "chinese": GEO["china"], "korean": GEO["korea"], "indian": GEO["india"],
    "persian": GEO["persia"], "iranian": GEO["iran"], "turkish": GEO["turkey"], "thai": GEO["thailand"],
    "burmese": GEO["burma"], "vietnamese": GEO["vietnam"], "indonesian": GEO["indonesia"], "javanese": GEO["java"],
    "tibetan": GEO["tibet"], "nepalese": GEO["nepal"], "mexican": GEO["mexico"], "peruvian": GEO["peru"],
    "brazilian": GEO["brazil"], "egyptian": GEO["egypt"], "ethiopian": GEO["ethiopia"], "nigerian": GEO["nigeria"],
    "cuzco": GEO["peru"], "cusco": GEO["peru"], "edo": GEO["japan"], "qing": GEO["china"], "ming": GEO["china"],
    "song dynasty": GEO["china"], "yuan dynasty": GEO["china"], "joseon": GEO["korea"], "mughal": GEO["india"],
})
EUROPE_EN = ("france", "french", "italy", "italian", "netherlands", "dutch", "flanders", "flemish", "belgium", "spain", "spanish",
             "england", "britain", "british", "scotland", "ireland", "germany", "german", "austria", "switzerland", "sweden", "norway",
             "denmark", "finland", "russia", "poland", "hungary", "bohemia", "czech", "portugal", "greece", "romania", "europe",
             "united states", "america", "canada", "venice", "florence", "rome", "naples", "lombardy", "tuscany", "paris")


def geo_of_en(text):
    """영어 국가/문화/출신지 문자열 → (한국어, 국기, region). 서구는 region 'europe'(버킷 W)."""
    t = (text or "").lower()
    for k, val in GEO.items():
        if re.search(r"\b" + re.escape(k) + r"\b", t):
            return val
    if any(w in t for w in EUROPE_EN):
        return ("", "", "europe")
    return ("", "", "global")


def _plain(html_text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text or "")).strip()


def _death_from_display(txt):
    m = re.findall(r"(\d{3,4})\s*[–-]\s*(\d{3,4})", txt or "")
    return int(m[-1][1]) if m else None


def from_cleveland(path):
    d = json.load(open(path, encoding="utf-8"))
    out = []
    for a in d:
        if a.get("type") != "Painting" or a.get("share_license_status") != "CC0":
            continue
        img = (a.get("images") or {})
        url = ((img.get("print") or img.get("web")) or {}).get("url")
        cr = next((c for c in (a.get("creators") or []) if c.get("role") == "artist"), None)
        if not url or not cr:
            continue
        m = re.match(r"^(.*?)\s*\(([^)]*)\)", cr.get("description") or "")
        name = (m.group(1) if m else (cr.get("description") or "")).strip()
        death = _death_from_display(cr.get("description"))
        text = _plain(a.get("description")) + " " + _plain(a.get("did_you_know"))
        if not name or "unknown" in name.lower() or death is None or death > DEATH_MAX or len(text.strip()) < 150:
            continue
        cult = (a.get("culture") or [""])[0] if isinstance(a.get("culture"), list) else (a.get("culture") or "")
        g = geo_of_en(f"{cult} {cr.get('description') or ''}")
        facts = [f"작가: {cr.get('description')}", f"제작 시기: {a.get('creation_date')}", f"재질/기법: {a.get('technique')}",
                 f"문화권: {cult}", "분류: Paintings", "현재 클리블랜드 미술관에 소장.", f"[미술관 해설] {text.strip()[:1800]}"]
        out.append({
            "title_ko": a["title"], "title_en": a["title"], "artist_ko": name, "artist_en": name,
            "year_label": a.get("creation_date") or "", "country": g[0], "country_flag": g[1], "region": g[2],
            "wiki_query": f"{a['title']} {name}", "direct_image_url": url,
            "image_credit": "이미지 출처: The Cleveland Museum of Art (CC0 퍼블릭 도메인)",
            "museum_grounding": " ".join(f for f in facts if not f.endswith(": None")),
            "_artist": name.lower(), "_sl": min(len(text), 3000) // 10 + 20,
        })
    print(f"클리블랜드 회화 후보 {len(out)}건", file=sys.stderr)
    return out


def from_artic(root):
    import glob
    out = []
    for fp in glob.glob(os.path.join(root, "*.json")):
        try:
            a = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        if not a.get("is_public_domain") or (a.get("classification_title") or "").lower() != "painting" or not a.get("image_id"):
            continue
        disp = a.get("artist_display") or ""
        name = (a.get("artist_title") or disp.split(chr(10))[0]).strip()
        death = _death_from_display(disp)
        text = _plain(a.get("description")) or _plain(a.get("short_description"))
        if not name or death is None or death > DEATH_MAX or len(text) < 150:
            continue
        g = geo_of_en(f"{a.get('place_of_origin') or ''} {disp}")
        facts = [f"작가: {disp.replace(chr(10), ', ')}", f"제작 시기: {a.get('date_display')}", f"재질/기법: {a.get('medium_display')}",
                 f"출신/제작지: {a.get('place_of_origin')}", "분류: Paintings", "현재 시카고 미술관에 소장.", f"[미술관 해설] {text[:1800]}"]
        out.append({
            "title_ko": a["title"], "title_en": a["title"], "artist_ko": name, "artist_en": name,
            "year_label": a.get("date_display") or "", "country": g[0], "country_flag": g[1], "region": g[2],
            "wiki_query": f"{a['title']} {name}",
            "direct_image_url": f"https://www.artic.edu/iiif/2/{a['image_id']}/full/1686,/0/default.jpg",
            "image_credit": "이미지 출처: The Art Institute of Chicago (CC0 퍼블릭 도메인)",
            "museum_grounding": " ".join(f for f in facts if not f.endswith(": None")),
            "_artist": name.lower(), "_sl": min(len(text), 3000) // 10 + 20 + (30 if a.get("is_boosted") else 0),
        })
    print(f"시카고 회화 후보 {len(out)}건", file=sys.stderr)
    return out


NON_WEST_DEPTS = {"Asian Art", "Islamic Art", "Arts of Africa, Oceania, and the Americas", "Egyptian Art",
                  "Ancient Near Eastern Art", "The Michael C. Rockefeller Wing"}


def sparql(query):
    for attempt in range(3):
        try:
            r = requests.get("https://query.wikidata.org/sparql", params={"query": query}, timeout=170,
                             headers={**UA, "Accept": "application/sparql-results+json"})
            if r.ok:
                return r.json()["results"]["bindings"]
            print(f"  SPARQL {r.status_code}: {r.text[:120]}", file=sys.stderr)
        except Exception as e:
            print(f"  SPARQL 예외: {e}", file=sys.stderr)
        time.sleep(30)
    print("  ⚠️ SPARQL 실패 — 이 조회분은 비어 있음", file=sys.stderr)
    return []


def v(b, k):
    return (b.get(k) or {}).get("value", "")


def year_of(s):
    m = re.match(r"^(-?\d{1,4})", s or "")
    return int(m.group(1)) if m else None


def commons_url(img_url):
    """P18 값(http://commons.wikimedia.org/wiki/Special:FilePath/파일명) → 1600px 썸네일 URL."""
    name = img_url.rsplit("/", 1)[-1]
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{name}?width=1600"


EUROPE_WORDS = ("네덜란드", "베네치아", "피렌체", "교황령", "스위스", "벨기에", "로렌", "바이에른", "밀라노", "핀란드", "신성 로마",
                "브라반트", "나폴리", "부르고뉴", "라트비아", "우르비노", "시스라이타니아", "합스부르크", "프로이센", "작센", "토스카나",
                "제노바", "사보이", "헝가리", "폴란드", "보헤미아", "덴마크", "노르웨이", "스웨덴", "Netherlands", "만토바", "페라라",
                "에스토니아", "리투아니아", "체코", "슬로바키아", "크로아티아", "루마니아", "불가리아", "세르비아", "아일랜드", "스코틀랜드",
                "카탈루냐", "아라곤", "카스티야", "포르투갈", "오스트리아", "루카", "시에나", "파르마", "모데나", "리보르노", "뷔르템베르크")


def region_of(ctry_ko):
    if any(w in (ctry_ko or "") for w in EUROPE_WORDS):
        return (ctry_ko, "", "europe")
    if ctry_ko in ("미국", "캐나다"):
        return (ctry_ko, "🇺🇸" if ctry_ko == "미국" else "🇨🇦", "europe")  # 서구 버킷(W)에 포함
    sys.path.insert(0, os.path.dirname(__file__))
    os.environ.setdefault("SUPABASE_URL", "http://fake")
    os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
    from literature_writer import country_info
    return country_info(ctry_ko)


def from_wikidata():
    rows = sparql(sparql_query(MIN_SITELINKS))
    vals = "VALUES ?ctry { " + " ".join(f"wd:{q}" for q in NON_WEST_COUNTRIES) + " }"
    rows += sparql(sparql_query(2, vals))
    print(f"위키데이터 {len(rows)}행", file=sys.stderr)
    best = {}
    for b in rows:
        qid = v(b, "p").rsplit("/", 1)[-1]
        if not v(b, "enwiki") and not v(b, "kowiki"):
            continue  # 위키백과 문서가 없으면 근거자료가 없어 기사가 빈약해진다
        best.setdefault(qid, b)  # 같은 작품의 중복 행은 첫 행만
    out = []
    for qid, b in best.items():
        title = v(b, "pLabel")
        if re.fullmatch(r"Q\d+", title):  # 라벨 없는 항목
            continue
        en = v(b, "enLabel") or v(b, "enwiki") or title
        artist = v(b, "cLabel")
        ctry_ko = v(b, "ctryLabel")
        country, flag, region = region_of(ctry_ko)
        yr = year_of(v(b, "inc"))
        by, dy = year_of(v(b, "cb")), year_of(v(b, "cd"))
        facts = [f"작가: {artist}({by or '?'}~{dy or '?'}{', ' + ctry_ko if ctry_ko else ''})"]
        if v(b, "collLabel"):
            facts.append(f"소장처: {v(b, 'collLabel')}")
        if yr:
            facts.append(f"제작 시기: {yr}년경")
        out.append({
            "title_ko": title, "title_en": en, "artist_ko": artist, "artist_en": artist,
            "year_label": f"{yr}년" if yr else "", "country": country, "country_flag": flag, "region": region,
            "wiki_query": v(b, "enwiki") or en, "direct_image_url": commons_url(v(b, "img")),
            "image_credit": "이미지 출처: Wikimedia Commons (퍼블릭 도메인)",
            "museum_grounding": " ".join(facts), "wikidata": qid,
            "_artist": v(b, "c"), "_sl": int(v(b, "sl") or 0),
        })
    return out


def _latin_part(s):
    parts = [x.strip() for x in (s or "").split("|") if x.strip()]
    lat = [x for x in parts if re.search(r"[A-Za-z]{3}", x)]
    return (lat[-1] if lat else (parts[0] if parts else "")).strip()


def from_met_csv(path):
    csv.field_size_limit(10**8)
    cand = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["Is Public Domain"] != "True" or r["Classification"] != "Paintings":
                continue
            if r["Department"] not in NON_WEST_DEPTS:
                continue  # 서구 회화는 위키데이터 쪽이 유명도 순으로 더 잘 걸러 줌
            hl = r["Is Highlight"] == "True"
            if not r["Artist Wikidata URL"].strip():
                continue  # CSV엔 해설문이 없어 미술관 기록이 250자 안팎 — 작가가 위키데이터에 있어야 위키백과 근거를 붙일 수 있음
            end = year_of(r["Artist End Date"].strip())
            if not r["Artist Display Name"].strip() or end is None or end > DEATH_MAX:
                continue  # 작가가 특정되고 사후 70년 경과한 작품만
            geo_key = (r["Country"] or r["Culture"] or r["Artist Nationality"]).lower()
            g = next((GEO[k] for k in GEO if k in geo_key), ("", "", "global"))
            artist = ""
            facts = []
            artist = _latin_part(r["Artist Display Name"])
            if r["Artist Display Bio"].strip():
                facts.append(f"작가: {artist}({_latin_part(r['Artist Display Bio'])})")
            for label, key in (("문화권", "Culture"), ("시대", "Period"), ("제작 시기", "Object Date"),
                               ("재질/기법", "Medium"), ("소장 경위", "Credit Line")):
                if r[key].strip():
                    facts.append(f"{label}: {r[key].strip()}")
            facts.append(f"현재 뉴욕 메트로폴리탄 미술관({r['Department']})에 소장.")
            title = _latin_part(r["Title"])
            artist = _latin_part(r["Artist Display Name"]) or artist
            if len(title) < 2 or not artist:
                continue  # 작자 미상은 근거가 미술관 기록뿐이라 기사가 빈약해진다(2026-09-28 사고) — 제외
            cand.append({
                "title_ko": title, "title_en": title, "artist_ko": artist or "작자 미상", "artist_en": artist or "Unknown artist",
                "year_label": r["Object Date"], "country": g[0], "country_flag": g[1], "region": g[2],
                "wiki_query": f"{title} {artist}".strip(), "met_object_id": int(r["Object ID"]),
                "met_department": r["Department"], "museum_grounding": " ".join(facts),
                "image_credit": "이미지 출처: The Metropolitan Museum of Art (CC0 퍼블릭 도메인)",
                "_artist": r["Artist Wikidata URL"].strip() or artist or f"anon-{r['Object ID']}",
                "_sl": 100 if hl else (50 if r["Object Wikidata URL"].strip() else 10),
            })
    print(f"메트 CSV 비서구 회화 후보 {len(cand)}건", file=sys.stderr)
    # 국가별 상한(일본·중국·인도가 압도적) — 유명도 순으로 자름
    caps = {"일본": 120, "중국": 120, "인도": 100}
    cnt, out = {}, []
    for a in sorted(cand, key=lambda x: -x["_sl"]):
        k = a["country"]
        if cnt.get(k, 0) >= caps.get(k, 10**6):
            continue
        cnt[k] = cnt.get(k, 0) + 1
        out.append(a)
    return out


def main():
    items = []
    if "--wikidata" in sys.argv:  # 위키데이터 SPARQL은 자주 504라 기본 제외(가끔 성공하면 서구 보강용)
        items += from_wikidata()
    for flag, fn in (("--cleveland", from_cleveland), ("--artic", from_artic)):
        if flag in sys.argv:
            items += fn(sys.argv[sys.argv.index(flag) + 1])
    if "--met-csv" in sys.argv:
        items += from_met_csv(sys.argv[sys.argv.index("--met-csv") + 1])
    if len(items) < 300:
        sys.exit(f"후보가 {len(items)}건뿐 — 입력 누락으로 판단해 저장하지 않음")
    # 작가당 PER_ARTIST점(유명도 순)
    by, final = {}, []
    for a in sorted(items, key=lambda x: -x["_sl"]):
        n = by.get(a["_artist"], 0)
        if n < PER_ARTIST:
            by[a["_artist"]] = n + 1
            final.append(a)
    # 한국어 작가명(위키데이터 라벨은 이미 ko 우선 — 메트 항목만 추가 조회)
    need = {a["_artist"].rsplit("/", 1)[-1]: a for a in final if a["_artist"].startswith("http") and "wikidata.org" in a["_artist"] and a["artist_ko"] == a["artist_en"]}
    ids = list(need)
    for i in range(0, len(ids), 50):
        try:
            r = requests.get("https://www.wikidata.org/w/api.php", headers=UA, timeout=40, params={
                "action": "wbgetentities", "format": "json", "props": "labels", "languages": "ko", "ids": "|".join(ids[i:i + 50])}).json()
            for q, e in (r.get("entities") or {}).items():
                lab = (e.get("labels") or {}).get("ko", {}).get("value")
                if lab:
                    need[q]["artist_ko"] = lab
        except Exception as e:
            print(f"  라벨 조회 실패: {e}", file=sys.stderr)
    for a in final:
        a.pop("_artist", None)
        a.pop("_sl", None)
    # W A X 섞기: 서구 / 동·남아시아 / 그 밖
    def bucket(a):
        return "W" if a["region"] == "europe" else ("A" if a["region"] == "asia" else "X")
    pools = {k: [a for a in final if bucket(a) == k] for k in "WAX"}
    rnd = random.Random(20260928)
    for p in pools.values():
        rnd.shuffle(p)
    order, pat = [], "WAWXWAWX"
    while any(pools.values()):
        for k in pat:
            if pools[k]:
                order.append(pools[k].pop())
        if not any(pools[k] for k in "WAX"):
            break
        if not pools["W"] and not pools["A"] and not pools["X"]:
            break
        # 특정 버킷이 소진되면 남은 버킷만 이어서
    print(f"최종 {len(order)}건 — " + ", ".join(f"{k}:{sum(1 for a in order if bucket(a) == k)}" for k in "WAX"), file=sys.stderr)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(order, f, ensure_ascii=False, indent=1)
    print(f"저장: {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
