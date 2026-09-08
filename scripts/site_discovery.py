"""
scripts/site_discovery.py
-------------------------
newspaper4k의 사이트 전체 기사 발견 기능(newspaper.build())을 이용해,
이미 신뢰하는(=rss_sources에 등록된) 매체 홈페이지를 통째로 훑어 RSS
피드에 안 걸린 기사를 찾아낸다.

2026-09-08 도입 배경: RSS 피드는 매체가 카테고리별로 부실하게 운영하거나
특정 섹션을 피드에 안 실어주는 경우가 흔하다(사용자 지시: "newspaper4k의
사이트 전체 기사 발견 기능... 진행해"). newspaper.build()는 홈페이지를
크롤링해 사이트 전체에서 기사로 보이는 링크를 찾아내므로, RSS의 사각지대를
메운다.

⚠️ newspaper.build()는 사이트 하나당 수백 건의 링크(카테고리/태그/작성자
페이지 포함)를 반환하고 크롤링 자체도 느리다 — 30분 주기 메인
파이프라인(run.yml)에 넣기엔 무겁다. 그래서 이 스크립트는 독립
workflow_dispatch로만 실행되고(하루 1회 등 낮은 빈도 권장), 소스 목록
중 일부만(SOURCES_PER_RUN) 순번대로 훑어 시간을 제어한다.

실행마다 처리할 다음 소스 목록은 data/site_discovery_state.json에
순번(cursor)으로 저장해, 실행할 때마다 다른 소스를 훑도록 한다(전체
소스를 며칠에 걸쳐 순환).
"""

import os
import re
import json
import time
from urllib.parse import urlparse

import requests
from deep_translator import GoogleTranslator
from rapidfuzz import fuzz
from dotenv import load_dotenv

load_dotenv()

import newspaper

from db import init_db, is_url_exists, insert_article
from geo_detect import detect_region, detect_countries, GLOBAL_COUNTRIES
from text_crawl import crawl_full_text, clean_text

try:
    from dedup_guard import normalize_tags
except Exception:
    def normalize_tags(tags, limit=10):
        return tags[:limit]

STATE_FILE = "data/site_discovery_state.json"
SOURCES_PER_RUN = 4          # 실행당 훑을 매체 수 (전체를 며칠에 걸쳐 순환)
MAX_CANDIDATES_PER_SOURCE = 10
SIMILARITY_THRESHOLD = 75

# 카테고리/태그/작성자 등 기사 아닌 페이지 — URL 패턴으로 걸러낸다
_NON_ARTICLE_PATH_RE = re.compile(
    r"/(tag|tags|category|categories|author|page|topic|topics|search|about|contact|"
    r"privacy|terms|subscribe|advertise|feed|rss|login|register|wp-json)(/|$)",
    re.IGNORECASE,
)


def _load_rss_sources() -> list:
    """rss_sources 테이블에서 활성 소스 name/url을 읽는다(rss_fetcher.py의
    load_rss()와 동일한 REST 호출을 독립적으로 재구현 — 그 파일은 import
    시점에 전체 파이프라인이 실행돼 직접 재사용할 수 없다)."""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    res = requests.get(
        f"{supabase_url}/rest/v1/rss_sources",
        headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
        params={"select": "name,url", "is_active": "eq.true", "limit": "1000"},
        timeout=15,
    )
    res.raise_for_status()
    return res.json() or []


# 뉴스 매체 자체가 아니라 검색/집계 게이트웨이인 도메인 — newspaper.build()로
# 홈페이지를 훑어봐야 그 매체 소유의 기사 링크가 아니라 검색 UI만 나온다.
# (실측 2026-09-08: rss_sources 418건 중 83건이 구글뉴스 국가별 피드로,
# 전부 news.google.com 한 홈페이지로 뭉쳐서 무의미한 크롤 1회를 낭비함)
AGGREGATOR_SKIP_DOMAINS = {"news.google.com"}


def _homepages_from_sources(sources: list) -> list:
    """RSS 소스 URL에서 홈페이지(scheme://netloc)만 뽑아 중복 제거."""
    seen = set()
    homepages = []
    for s in sources:
        try:
            p = urlparse(s["url"])
            home = f"{p.scheme}://{p.netloc}"
        except Exception:
            continue
        if home in seen or not p.netloc or p.netloc in AGGREGATOR_SKIP_DOMAINS:
            continue
        seen.add(home)
        homepages.append({"name": s["name"], "home": home})
    return homepages


def _load_cursor(total: int) -> int:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("cursor", 0) % max(total, 1)
        except Exception:
            pass
    return 0


def _save_cursor(cursor: int, total: int):
    os.makedirs("data", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"cursor": cursor % max(total, 1)}, f)


def _is_bad_translation(t) -> bool:
    marks = ("That's an error", "Server Error", "Error 500", "Error 502", "Error 503",
              "unusual traffic from your computer network")
    if not t or not isinstance(t, str):
        return True
    return any(m in t for m in marks)


def discover_candidates(home: str, netloc: str) -> list:
    """홈페이지를 크롤링해 기사로 보이는 URL 후보를 반환."""
    try:
        paper = newspaper.build(home, memoize_articles=False, language="en")
    except Exception as e:
        print(f"[site_discovery 실패] {home} — {e}")
        return []

    candidates = []
    for art in paper.articles:
        url = art.url
        p = urlparse(url)
        if p.netloc != netloc:
            continue
        if _NON_ARTICLE_PATH_RE.search(p.path):
            continue
        if len(p.path.strip("/")) < 8:
            continue
        candidates.append(url)
        if len(candidates) >= MAX_CANDIDATES_PER_SOURCE:
            break
    return candidates


def run():
    init_db()
    sources = _load_rss_sources()
    homepages = _homepages_from_sources(sources)
    if not homepages:
        print("소스 없음 — 종료")
        return

    cursor = _load_cursor(len(homepages))
    batch = [homepages[(cursor + i) % len(homepages)] for i in range(min(SOURCES_PER_RUN, len(homepages)))]
    _save_cursor(cursor + SOURCES_PER_RUN, len(homepages))

    seen_titles = []
    inserted = 0
    scanned = 0

    for entry in batch:
        name, home = entry["name"], entry["home"]
        netloc = urlparse(home).netloc
        print(f"[site_discovery] {name} ({home}) 훑는 중...")
        candidates = discover_candidates(home, netloc)
        print(f"  후보 {len(candidates)}건")

        for url in candidates:
            scanned += 1
            if is_url_exists(url):
                continue

            try:
                art = newspaper.Article(url, language="en")
                art.download()
                art.parse()
            except Exception:
                continue

            title = clean_text(art.title or "")
            if not title or len(title) < 10:
                continue
            if any(fuzz.token_sort_ratio(title.lower(), t.lower()) >= SIMILARITY_THRESHOLD for t in seen_titles):
                continue
            seen_titles.append(title)

            full_text = crawl_full_text(url, timeout=8)
            if not full_text or len(full_text) < 300:
                continue

            src_published = art.publish_date.isoformat() if art.publish_date else None

            content_countries = detect_countries(title, source=name)
            country_names = [n for _, n in content_countries]
            if country_names:
                frontier = [n for n in country_names if n not in GLOBAL_COUNTRIES]
                country_name = frontier[0] if frontier else country_names[0]
                country_flag = next((f for f, n in content_countries if n == country_name), "")
            else:
                country_name, country_flag = "", ""
                country_names = []

            try:
                title_ko = clean_text(GoogleTranslator(source="auto", target="ko").translate(title[:500]))
                if _is_bad_translation(title_ko):
                    title_ko = title
            except Exception:
                title_ko = title

            summary_en = clean_text(full_text[:500])
            summary_ko = ""
            try:
                summary_ko = clean_text(GoogleTranslator(source="auto", target="ko").translate(summary_en[:4500]))
                if _is_bad_translation(summary_ko):
                    summary_ko = ""
            except Exception:
                summary_ko = ""

            article_id = insert_article(
                title_en=title, title_ko=title_ko,
                summary_en=summary_en, summary_ko=summary_ko,
                url=url, source=f"SiteDiscovery:{name}", category="글로벌",
                subcategory="", region=detect_region(name),
                country=country_name, country_flag=country_flag,
                score=0, full_text=full_text,
                countries=country_names,
                is_published=False,
                source_published_at=src_published,
                source_data={"tags": normalize_tags([name], limit=10)},
            )
            if article_id > 0:
                inserted += 1
                print(f"  [저장] [{country_name}] {title[:60]}")

    print(f"\n✅ site_discovery 완료 — {scanned}건 후보 확인, {inserted}건 신규 저장")


if __name__ == "__main__":
    run()
