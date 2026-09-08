"""
scripts/gdelt_fetcher.py
-------------------------
GDELT DOC 2.0 API(https://api.gdeltproject.org/api/v2/doc/doc) 기반
글로벌 뉴스 검색 수집기. API 키 불필요.

2026-09-08 도입 배경: 우리 파이프라인은 RSS만 소스로 쓰다 보니
① RSS 피드가 없거나 죽은 매체, ② RSS에 안 걸리는 특정 이슈 기사를
놓치는 구조적 한계가 있었다(사용자 지시: "GDELT 전역 뉴스 검색 API...
진행해"). GDELT는 전세계 뉴스를 거의 실시간으로 색인해 자유 텍스트
검색으로 찾아준다 — RSS의 보완재로 쓴다.

이 스크립트는 rss_fetcher.py와 별개의 독립 실행 스크립트다(그 파일은
import 시점에 파이프라인 전체가 실행되는 구조라 재사용이 안 됨).
크롤링·국가감지 로직은 geo_detect.py/text_crawl.py로 뗀 사본을 쓴다.

수집된 기사는 RSS 기사와 동일하게 is_published=False로 저장 —
텔레그램 발송은 하지 않는다(RSS와 검색 결과가 겹치면 중복 알림이 되므로).
downstream(gemini_summarizer.py/gemini_writer.py)이 그대로 원재료로 픽업한다.
"""

import os
import re
import time
import hashlib
from urllib.parse import quote, urlparse, urlunparse

import requests
from deep_translator import GoogleTranslator
from rapidfuzz import fuzz
from dotenv import load_dotenv

load_dotenv()

from db import init_db, is_url_exists, insert_article, now_kst
from geo_detect import detect_region, detect_countries, detect_country, GLOBAL_COUNTRIES
from text_crawl import crawl_full_text, clean_text

try:
    from dedup_guard import normalize_tags
except Exception:
    def normalize_tags(tags, limit=10):
        return tags[:limit]

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"

# 프론티어 시장 위주 검색 쿼리 — RSS 소스 커버리지가 얕은 지역/주제를 겨냥한다.
# GDELT free-text 검색은 국가명만으로도 그 국가 매체 기사가 잘 걸린다(실측 확인).
GDELT_QUERIES = [
    "Nigeria (economy OR business OR politics)",
    "Kenya (economy OR business OR politics)",
    "Ghana (economy OR business)",
    "Ethiopia (economy OR politics)",
    "Egypt (economy OR business)",
    "Vietnam (economy OR business)",
    "Indonesia (economy OR business)",
    "Philippines (economy OR business)",
    "Pakistan (economy OR politics)",
    "Bangladesh (economy OR business)",
    "Kazakhstan OR Uzbekistan (economy OR business)",
    "Ukraine (economy OR reconstruction)",
    "Iraq OR Jordan OR Lebanon (economy OR politics)",
    "Ivory Coast OR Senegal OR Cote d'Ivoire (economy OR business)",
    "frontier markets investment",
    "emerging markets Africa business",
]

MAX_RECORDS_PER_QUERY = 10
TIMESPAN = "3h"          # 30분 간격 실행이라도 놓친 구간을 흡수하도록 여유
# GDELT 공식 제한: 5초당 1회("Please limit requests to one every 5 seconds" —
# 2026-09-08 실측, 1.2초로 뒀다가 429 확인). 여유를 두어 6초로 설정.
REQUEST_INTERVAL = 6.0
MAX_AGE_DAYS = 2

_SIMILARITY_THRESHOLD = 75


def _normalize_url(url):
    try:
        p = urlparse(url)
        return urlunparse(p._replace(query="", fragment=""))
    except Exception:
        return url


def _is_duplicate_title(title, seen_titles):
    for seen in seen_titles:
        if fuzz.token_sort_ratio(title.lower(), seen.lower()) >= _SIMILARITY_THRESHOLD:
            return True
    return False


def _is_bad_translation(t) -> bool:
    marks = ("That's an error", "Server Error", "Error 500", "Error 502", "Error 503",
              "unusual traffic from your computer network")
    if not t or not isinstance(t, str):
        return True
    return any(m in t for m in marks)


def fetch_gdelt(query: str, maxrecords: int = MAX_RECORDS_PER_QUERY, timespan: str = TIMESPAN) -> list:
    """GDELT DOC 2.0 검색. 실패해도 예외를 삼키고 빈 리스트 반환."""
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
            # 공유 IP(GitHub Actions 러너 등)에서 다른 트래픽과 겹칠 수 있어 1회 재시도.
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
    """GDELT seendate는 'YYYYMMDDTHHMMSSZ' 형식."""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$", seendate or "")
    if not m:
        return ""
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"


def _age_days(iso_str: str):
    if not iso_str:
        return None
    try:
        import calendar as _cal
        tt = time.strptime(iso_str[:19], "%Y-%m-%dT%H:%M:%S")
        return (time.time() - _cal.timegm(tt)) / 86400.0
    except Exception:
        return None


def run():
    init_db()
    seen_titles = []
    inserted = 0
    scanned = 0

    for query in GDELT_QUERIES:
        articles = fetch_gdelt(query)
        time.sleep(REQUEST_INTERVAL)
        if not articles:
            continue

        for art in articles:
            scanned += 1
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
            if _is_duplicate_title(title, seen_titles):
                continue
            seen_titles.append(title)

            source_name = (art.get("domain") or "GDELT").strip()
            full_text = crawl_full_text(link, timeout=8)

            content_countries = detect_countries(title, source=source_name)
            country_names = [n for _, n in content_countries]
            if country_names:
                frontier = [n for n in country_names if n not in GLOBAL_COUNTRIES]
                if frontier:
                    country_name = frontier[0]
                    country_flag = next((f for f, n in content_countries if n == country_name), "")
                    category, region = "글로벌", detect_region(source_name)
                else:
                    country_name = country_names[0]
                    country_flag = next((f for f, n in content_countries if n == country_name), "")
                    category, region = "글로벌", "global"
            else:
                country_name, country_flag = "", ""
                category, region = "글로벌", "global"
                country_names = []

            try:
                title_ko = clean_text(GoogleTranslator(source="auto", target="ko").translate(title[:500]))
                if _is_bad_translation(title_ko):
                    title_ko = title
            except Exception:
                title_ko = title

            summary_en = clean_text((full_text or "")[:500])
            summary_ko = ""
            if summary_en:
                try:
                    summary_ko = clean_text(GoogleTranslator(source="auto", target="ko").translate(summary_en[:4500]))
                    if _is_bad_translation(summary_ko):
                        summary_ko = ""
                except Exception:
                    summary_ko = ""

            article_id = insert_article(
                title_en=title, title_ko=title_ko,
                summary_en=summary_en, summary_ko=summary_ko,
                url=link, source=f"GDELT:{source_name}", category=category,
                subcategory="", region=region,
                country=country_name, country_flag=country_flag,
                score=0, full_text=full_text,
                countries=country_names,
                is_published=False,
                source_published_at=src_published or None,
                source_data={"tags": normalize_tags([query], limit=10)},
            )
            if article_id > 0:
                inserted += 1
                print(f"[GDELT 저장] [{country_name}] {title[:60]}")

    print(f"\n✅ GDELT 완료 — {scanned}건 조회, {inserted}건 신규 저장")


if __name__ == "__main__":
    run()
