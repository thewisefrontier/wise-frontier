"""
scripts/rss_source_discovery.py
--------------------------------
RSS 소스 자동 발굴 — GitHub의 사람이 직접 검증해 관리하는 국가별/주제별 피드 목록에서
새 후보를 받아와, 살아있고 최근 갱신되는 것만 걸러 rss_sources에 등록한다.

2026-09-22 신설(사용자 지시: "RSS 소스를 수집해서 추가하고 검수하고 제외하는 시스템으로
확장"). feed_discovery.py(홈페이지 1건씩 피드 탐색)와 역할이 다르다 — 이쪽은 이미
피드 URL까지 정리돼 있는 대형 목록(196개국 커버)에서 대량으로 받아온다.

소스:
  - mhus/hrafnagud-catalog-rss (Apache-2.0) — 196개국, 나라당 2~5개 "그 나라
    사람이 실제로 읽는 매체" 큐레이션. 한국(south-korea)은 국내 매체 파이프라인
    (domestic_kr_fetcher.py)이 따로 담당하므로 제외.
  - plenaryapp/awesome-rss-feeds — 국가별(25개국)·주제별 큐레이션. 국가는 위
    hrafnagud가 더 넓어(196개국) 겹치는 나라는 자동으로 새 후보가 안 남고,
    주제(News/Business/Tech/Science 등)만 실질적으로 보탠다.

⚠️ feed_discovery.py와 동일한 안전장치: 검증을 통과해도 is_active=false로만 등록한다
("검수 대기"). 사람이 어드민(RSS 소스 관리)에서 확인 후 켜야 실제 수집에 들어간다 —
대량으로 들여오는 만큼 품질 관리를 더 엄격히 지킨다.

실행: python scripts/rss_source_discovery.py            (검증 통과분 등록)
      python scripts/rss_source_discovery.py --dry-run   (등록 없이 개수만 출력)
권장 주기: 주 1회 이하(외부 GitHub 목록이 그렇게 자주 안 바뀜, 전체 스캔이 몇 분 걸림).
"""

import os
import re
import sys
import html
import time
import requests
import feedparser
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

from db import _url, _headers  # noqa: E402 — 프로젝트 관례(db.py가 유일한 REST 헤더 출처)

UA = {"User-Agent": "Mozilla/5.0 (compatible; NewsFinalSourceDiscovery/1.0)"}
GH_RAW = "https://raw.githubusercontent.com"

# 나라 슬러그 → REGION_KEYWORDS/geo_detect.py가 쓰는 것과 같은 지역명. 국내(한국)는
# domestic_kr_fetcher.py 전담이라 제외.
HRAF_REGION = {}
for _r, _names in {
    "중동": "bahrain iran iraq israel jordan kuwait lebanon oman palestine qatar saudi-arabia syria united-arab-emirates yemen turkey",
    "중앙아시아": "kazakhstan kyrgyzstan tajikistan turkmenistan uzbekistan armenia azerbaijan georgia",
    "남아시아": "afghanistan bangladesh bhutan india maldives nepal pakistan sri-lanka",
    "동남아시아": "brunei cambodia indonesia laos malaysia myanmar philippines singapore thailand timor-leste vietnam",
    "동아시아": "china hong-kong japan mongolia taiwan",
    "동유럽": "albania belarus bosnia-and-herzegovina bulgaria croatia czechia estonia hungary kosovo latvia lithuania moldova montenegro north-macedonia poland romania russia serbia slovakia slovenia ukraine",
    "북미": "canada united-states",
    "카리브해": "antigua-and-barbuda bahamas barbados cuba dominica dominican-republic grenada haiti jamaica saint-kitts-and-nevis saint-lucia saint-vincent-and-the-grenadines trinidad-and-tobago",
    "라틴아메리카": "belize costa-rica el-salvador guatemala honduras mexico nicaragua panama",
}.items():
    for _n in _names.split():
        HRAF_REGION[_n] = _r
CONTINENT_DEFAULT = {"africa": "아프리카", "europe": "유럽", "south-america": "라틴아메리카", "oceania": "오세아니아"}

PLENARY_COUNTRY_REGION = {
    "Australia": "오세아니아", "Bangladesh": "남아시아", "Brazil": "라틴아메리카", "Canada": "북미",
    "France": "유럽", "Germany": "유럽", "Hong Kong SAR China": "동아시아", "India": "남아시아",
    "Indonesia": "동남아시아", "Iran": "중동", "Ireland": "유럽", "Italy": "유럽", "Japan": "동아시아",
    "Mexico": "라틴아메리카", "Myanmar (Burma)": "동남아시아", "Nigeria": "아프리카", "Pakistan": "남아시아",
    "Philippines": "동남아시아", "Poland": "동유럽", "Russia": "동유럽", "South Africa": "아프리카",
    "Spain": "유럽", "Ukraine": "동유럽", "United Kingdom": "유럽", "United States": "북미",
}
PLENARY_TOPIC = {
    "News": ("세계", "세계뉴스"), "Business & Economy": ("경제", "경제일반"), "Tech": ("IT·과학", "IT일반"),
    "Science": ("IT·과학", "과학"), "Startups": ("경제", "산업/기업"), "Cryptocurrency": ("경제", "금융/증권"),
    "Personal finance": ("경제", "금융/증권"), "Cyber security": ("IT·과학", "IT일반"), "Environment": ("사회", "사회일반"),
}
SKIP_FOLDER_RE = re.compile(
    r"sport|entertain|lifestyle|fashion|beauty|food|travel|game|gaming|music|movie|film|celebr|"
    r"humou?r|fun|meme|cricket|football|soccer|tennis|cars?|auto|photo|design|diy|home|"
    r"kids|women|health & fit|wellness|horoscope|astrolog|religion", re.I,
)
POD_RE = re.compile(
    r"acast\.com|megaphone\.fm|art19\.com|simplecast\.com|anchor\.fm|relay\.fm|transistor\.fm|"
    r"libsyn\.com|atp\.fm|podcasts\.files|buzzsprout|podbean|spreaker|omnycontent|feeds\.npr\.org|"
    r"soundcloud\.com|podtrac|redcircle\.com|audioboom\.com|harvardbusiness\.org", re.I,
)

FRESH_MAX_AGE_DAYS = 14
MIN_ENTRIES = 3
FETCH_WORKERS = 24
INSERT_CHUNK = 100


def _gh_tree(repo: str, branch: str) -> list:
    r = requests.get(f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1", headers=UA, timeout=30)
    r.raise_for_status()
    return [t["path"] for t in r.json()["tree"] if t["type"] == "blob"]


def _gh_raw(repo: str, branch: str, path: str) -> bytes:
    r = requests.get(f"{GH_RAW}/{repo}/{branch}/{path}", headers=UA, timeout=30)
    r.raise_for_status()
    return r.content


def _parse_opml(raw: bytes) -> list:
    """OPML을 태그 단위 정규식으로 훑는다 — 실전에서 깨진 XML(&, 따옴표 미이스케이프)이
    흔해 xml.etree가 자주 실패했다(2026-09-22 실측, 66개 중 상당수)."""
    txt = raw.decode("utf-8", errors="replace")
    out, stack = [], []
    for m in re.finditer(r"<outline\s([^>]*?)(/?)>|</outline>", txt, re.S):
        if m.group(0).startswith("</"):
            if stack:
                stack.pop()
            continue
        attrs = dict((k, html.unescape(v)) for k, v in re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if attrs.get("xmlUrl"):
            out.append({"title": attrs.get("text") or attrs.get("title") or "", "url": attrs["xmlUrl"].strip(),
                        "folder": stack[-1] if stack else ""})
        elif m.group(2) != "/":
            stack.append(attrs.get("text") or attrs.get("title") or "")
    return out


def gather_candidates() -> list:
    """[(name, category, subcategory, url), ...] — 아직 dedupe·검증 전."""
    out = []

    # ① hrafnagud-catalog-rss: 국가별 (196개국, 나라당 2~5개)
    for path in _gh_tree("mhus/hrafnagud-catalog-rss", "main"):
        if not path.endswith(".opml"):
            continue
        cont, slug = path.rsplit("/", 1)
        slug = slug[:-5]
        if slug == "south-korea":
            continue  # domestic_kr_fetcher.py 전담
        region = HRAF_REGION.get(slug) or CONTINENT_DEFAULT.get(cont)
        if not region:
            continue
        country = slug.replace("-", " ").title()
        try:
            feeds = _parse_opml(_gh_raw("mhus/hrafnagud-catalog-rss", "main", path))
        except Exception:
            continue
        for f in feeds:
            out.append((f"{country} - {f['title']}"[:120], "세계", region, f["url"]))

    # ② awesome-rss-feeds: 국가별(겹치는 나라는 ①에 이미 있어 자연히 걸러짐) + 주제별
    for path in _gh_tree("plenaryapp/awesome-rss-feeds", "master"):
        if "/with_category/" not in path or not path.endswith(".opml"):
            continue
        kind, _, name = path.replace(".opml", "").split("/")[-3:]
        try:
            feeds = _parse_opml(_gh_raw("plenaryapp/awesome-rss-feeds", "master", path))
        except Exception:
            continue
        if kind == "countries":
            region = PLENARY_COUNTRY_REGION.get(name)
            if not region:
                continue
            for f in feeds:
                if SKIP_FOLDER_RE.search(f["folder"] or ""):
                    continue
                out.append((f["title"] or f["url"], "세계", region, f["url"]))
        elif kind == "recommended" and name in PLENARY_TOPIC:
            cat, sub = PLENARY_TOPIC[name]
            for f in feeds:
                out.append((f["title"] or f["url"], cat, sub, f["url"]))

    return out


def _host(url: str) -> str:
    m = re.match(r"^https?://(www\.)?([^/]+)", url.strip().lower())
    return m.group(2) if m else url.strip().lower()


def existing_hosts() -> set:
    hosts, offset, page = set(), 0, 1000
    while True:
        res = requests.get(_url("rss_sources"), headers=_headers(),
                            params={"select": "url", "limit": str(page), "offset": str(offset)}, timeout=15)
        res.raise_for_status()
        batch = res.json()
        hosts.update(_host(r["url"]) for r in batch)
        if len(batch) < page:
            break
        offset += page
    return hosts


def verify(cand: tuple) -> dict | None:
    name, category, subcategory, url = cand
    try:
        r = requests.get(url, headers=UA, timeout=(8, 15))
        if r.status_code != 200:
            return None
        d = feedparser.parse(r.content)
        entries = d.entries or []
        if len(entries) < MIN_ENTRIES:
            return None
        newest = None
        for e in entries:
            tt = e.get("published_parsed") or e.get("updated_parsed")
            if tt:
                dt = datetime(*tt[:6], tzinfo=timezone.utc)
                newest = dt if newest is None or dt > newest else newest
        if newest is None or (datetime.now(timezone.utc) - newest).days > FRESH_MAX_AGE_DAYS:
            return None
        return {"name": name[:120], "category": category, "subcategory": subcategory, "url": r.url}
    except Exception:
        return None


def insert_pending(rows: list) -> int:
    """검수 대기(is_active=false)로 등록. 배치 하나에서 실패 원인 행이 전체를 막지
    않도록 이분 재시도(rss_raw_queue의 queue_insert_bulk와 같은 패턴)."""
    if not rows:
        return 0
    headers = {**_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    payload = [{**r, "is_active": False} for r in rows]
    try:
        res = requests.post(_url("rss_sources"), headers=headers, json=payload, timeout=30)
    except requests.RequestException as e:
        print(f"  [WARN] 등록 네트워크 오류({len(rows)}건): {e}")
        res = None
    if res is not None and res.status_code in (200, 201):
        return len(res.json())
    if res is not None:
        print(f"  [WARN] 등록 실패 status={res.status_code} 건수={len(rows)} body={res.text[:200]}")
    if len(rows) == 1:
        print(f"  [DROP] {rows[0]['url']}")
        return 0
    mid = len(rows) // 2
    return insert_pending(rows[:mid]) + insert_pending(rows[mid:])


def main():
    dry = "--dry-run" in sys.argv
    print("[발굴] GitHub 큐레이션 목록 수집 중...")
    candidates = gather_candidates()
    print(f"  후보 {len(candidates)}건")

    known = existing_hosts()
    seen_host, uniq = set(), []
    for c in candidates:
        u = c[3]
        if POD_RE.search(u):
            continue
        h = _host(u)
        if h in known or h in seen_host:
            continue
        seen_host.add(h)
        uniq.append(c)
    print(f"  신규 후보(호스트 기준 중복 제거) {len(uniq)}건")

    verified = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futs = [ex.submit(verify, c) for c in uniq]
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r:
                verified.append(r)
            if i % 200 == 0:
                print(f"  검증 {i}/{len(uniq)}")
    print(f"[발굴 완료] 생존·신선 검증 통과 {len(verified)}/{len(uniq)}")

    if dry:
        for v in verified[:20]:
            print(f"  · [{v['category']}/{v['subcategory']}] {v['name']} — {v['url']}")
        return

    inserted = 0
    for i in range(0, len(verified), INSERT_CHUNK):
        inserted += insert_pending(verified[i:i + INSERT_CHUNK])
    print(f"[등록] 검수 대기(is_active=false)로 {inserted}건 등록 — 어드민 RSS 소스 관리에서 확인 후 활성화할 것")


if __name__ == "__main__":
    main()
