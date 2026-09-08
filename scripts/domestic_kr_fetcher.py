"""
scripts/domestic_kr_fetcher.py
-------------------------
GDELT DOC 2.0 API로 국내(한국) 뉴스만 수집 — 다국어(글로벌) 채널 전용
소스. 한국어 메인 사이트(newsfinal.co.kr)에는 절대 노출되지 않는다.

2026-09-08 도입 배경: 다국어 채널 콘텐츠 전략을 여러 차례 논의한 끝에
사용자가 최종 확정한 방향 — "해외쪽에는 오히려 한국기사만 번역해서
보여주는게 더 나을 것 같은데"(프론티어 뉴스를 재번역해봐야 현지 언론이
이미 더 잘 다루는 내용이라 차별화가 없고, 반대로 "한국 뉴스"를 외국어로
보여주는 건 그 나라 언론에 없는 진짜 새 콘텐츠). 또한 "사실상 지금
버려지고 있는 한국 기사들을 재활용하는 의미도 있다" — gdelt_fetcher.py가
메인 파이프라인용으로 sourcecountry=="South Korea"를 걸러내던(등재
2026-09-08, id=146386 무신사 기사 오탐 이후) 바로 그 소스를 여기서
거꾸로 채택한다.

⚠️ "전체 기사를 다 번역할 필요는 없다"(사용자) — 아래 쿼리 자체가 이미
선별적(한국 경제·기업 관련 검색어 매칭분만, 전체 한국 뉴스 파이어호스가
아님)이고, 다운스트림 multilang_translate.py도 사이클당 처리 상한을 둔다.

이 스크립트는 rss_fetcher.py/gdelt_fetcher.py와 완전히 독립적으로
실행되는 별도 스크립트다(rss_fetcher.py는 import 시점에 파이프라인
전체가 실행되는 구조라 재사용 불가 — geo_detect.py/text_crawl.py 사본을
그대로 재사용).

저장된 행은 source="DomesticKR:{domain}", is_published=False로 저장되며
gemini_writer.py/gemini_summarizer.py의 클러스터링 로직이 이 태그를
전혀 참조하지 않으므로 한국어 메인 사이트에 자동 발행될 위험이 없고,
export_articles.py도 source=eq.NewsFinal 필터라 애초에 안 잡힌다.
"""

import re
import time
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv

load_dotenv()

from db import init_db, is_url_exists, insert_article
from text_crawl import crawl_full_text, clean_text

try:
    from dedup_guard import normalize_tags
except Exception:
    def normalize_tags(tags, limit=10):
        return tags[:limit]

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# 한국 관련 검색어 — 전체 한국 뉴스가 아니라 해외 다국어 채널에 옮길
# 가치가 있는(경제·기업·산업 중심) 기사만 선별적으로 겨냥한다.
KR_QUERIES = [
    "South Korea economy",
    "Korea business investment",
    "Korean company overseas expansion",
    "한국 기업",
    "한국 경제",
    "삼성 OR 현대 OR LG OR SK",
    "한국 수출",
]

MAX_RECORDS_PER_QUERY = 10
TIMESPAN = "6h"
REQUEST_INTERVAL = 6.0   # GDELT 제한: 5초당 1회(2026-09-08 실측, gdelt_fetcher.py와 동일)
MAX_AGE_DAYS = 2

_TARGET_COUNTRY = "South Korea"


def _normalize_url(url):
    try:
        p = urlparse(url)
        return urlunparse(p._replace(query="", fragment=""))
    except Exception:
        return url


def fetch_gdelt(query: str, maxrecords: int = MAX_RECORDS_PER_QUERY, timespan: str = TIMESPAN) -> list:
    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": str(maxrecords),
        "format": "json",
        "sort": "datedesc",
        "timespan": timespan,
    }
    try:
        res = requests.get(GDELT_API, params=params, timeout=20,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)"})
        if res.status_code == 429:
            time.sleep(6.0)
            res = requests.get(GDELT_API, params=params, timeout=20,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)"})
        if res.status_code != 200:
            print(f"[GDELT 실패] {query!r} — HTTP {res.status_code}")
            return []
        data = res.json()
        return data.get("articles", []) or []
    except Exception as e:
        print(f"[GDELT 실패] {query!r} — {e}")
        return []


def _seendate_to_iso(seendate: str) -> str:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$", seendate or "")
    if not m:
        return ""
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"


def _age_days(iso_str: str):
    if not iso_str:
        return None
    try:
        import time as _t, calendar as _cal
        tt = _t.strptime(iso_str[:19], "%Y-%m-%dT%H:%M:%S")
        return (_t.time() - _cal.timegm(tt)) / 86400.0
    except Exception:
        return None


def run():
    init_db()
    seen_titles = []
    inserted = 0
    scanned = 0

    for query in KR_QUERIES:
        articles = fetch_gdelt(query)
        time.sleep(REQUEST_INTERVAL)
        if not articles:
            continue

        for art in articles:
            scanned += 1
            # 메인 gdelt_fetcher.py와 정반대 필터 — 국내 매체만 채택.
            if art.get("sourcecountry") != _TARGET_COUNTRY:
                continue

            title = clean_text(art.get("title", ""))
            link = _normalize_url(art.get("url", ""))
            if not title or not link:
                continue

            src_published = _seendate_to_iso(art.get("seendate", ""))
            age = _age_days(src_published)
            if age is not None and age > MAX_AGE_DAYS:
                continue

            if is_url_exists(link):
                continue
            if title in seen_titles:
                continue
            seen_titles.append(title)

            domain = (art.get("domain") or "unknown").strip()
            full_text = crawl_full_text(link, timeout=8)
            if not full_text or len(full_text) < 300:
                # 원문이 이미 한국어이므로 크롤링 실패 시 GDELT 제목만으론
                # multilang_translate.py의 번역 재료가 너무 빈약함 — 스킵.
                continue

            article_id = insert_article(
                title_en="", title_ko=title,
                summary_en="", summary_ko=full_text[:2000],
                url=link, source=f"DomesticKR:{domain}", category="글로벌",
                subcategory="", region="korea",
                country="한국", country_flag="🇰🇷",
                score=0, full_text=full_text,
                countries=["한국"],
                is_published=False,
                source_published_at=src_published or None,
                source_data={"tags": normalize_tags([query], limit=10)},
            )
            if article_id > 0:
                inserted += 1
                print(f"[DomesticKR 저장] {title[:60]}")

    print(f"\n✅ domestic_kr_fetcher 완료 — {scanned}건 조회, {inserted}건 신규 저장")


if __name__ == "__main__":
    run()
