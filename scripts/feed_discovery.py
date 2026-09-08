"""
scripts/feed_discovery.py
-------------------------
후보 매체 홈페이지에서 RSS/Atom 피드 URL을 자동 탐색해 rss_sources
테이블에 "검수 대기"(is_active=false) 상태로 등록하는 도구.

2026-09-08 도입 배경: 프론티어 시장 매체를 새로 추가할 때마다 사람이
직접 그 사이트의 피드 URL을 찾아야 했다(사용자 지시: "RSS 피드 자동
탐색 도구도 진행해"). 이 스크립트는 ①홈페이지 HTML의
<link rel="alternate" type="application/rss+xml|atom+xml"> 태그, ②흔한
경로(/feed, /rss, /rss.xml, /feed.xml, /atom.xml 등)를 순서대로 시도해
유효한 피드를 찾고, feedparser로 실제 파싱 가능한지·최근 항목이 있는지
검증한다.

⚠️ 자동으로 is_active=true를 주지 않는다 — 미검증 소스가 그대로 발행
파이프라인에 흘러들면 품질 관리가 무너진다. is_active=false로 등록해
사람이 rss_sources 테이블에서 검토 후 켜도록 한다(되돌리기 쉬운 안전한
기본값).

사용법:
    python scripts/feed_discovery.py https://example.com https://other.com
    (인자 없이 실행하면 CANDIDATE_HOMEPAGES 기본 목록을 사용)
"""

import os
import re
import sys
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from dotenv import load_dotenv

load_dotenv()

# 인자 없이 실행할 때 훑을 기본 후보 목록 — 아직 RSS 소스로 등록 안 된
# 프론티어 시장 매체를 사용자가 채워 넣을 자리(빈 상태로 둠, 필요시 CLI 인자로 전달).
CANDIDATE_HOMEPAGES = []

FEED_LINK_RE = re.compile(
    r'<link[^>]+type=["\'](?:application/rss\+xml|application/atom\+xml)["\'][^>]*>',
    re.IGNORECASE,
)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)

COMMON_FEED_PATHS = ["/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/feeds/posts/default"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)"}


def _discover_via_link_tags(home: str) -> list:
    try:
        res = requests.get(home, headers=HEADERS, timeout=15)
        if res.status_code != 200:
            return []
    except Exception:
        return []

    found = []
    for tag in FEED_LINK_RE.findall(res.text):
        m = HREF_RE.search(tag)
        if m:
            found.append(urljoin(home, m.group(1)))
    return found


def _discover_via_common_paths(home: str) -> list:
    found = []
    for path in COMMON_FEED_PATHS:
        url = urljoin(home, path)
        try:
            res = requests.get(url, headers=HEADERS, timeout=10)
            if res.status_code == 200 and len(res.content) > 200:
                found.append(url)
        except Exception:
            continue
    return found


def validate_feed(url: str) -> dict:
    """feedparser로 실제 파싱 가능한지, 최근 항목이 있는지 확인."""
    try:
        d = feedparser.parse(url, request_headers=HEADERS)
    except Exception:
        return {"valid": False}
    entries = d.entries or []
    if not entries:
        return {"valid": False}
    return {
        "valid": True,
        "entry_count": len(entries),
        "latest_title": entries[0].get("title", ""),
        "feed_title": (d.feed or {}).get("title", ""),
    }


def discover_feeds(home: str) -> list:
    candidates = _discover_via_link_tags(home)
    candidates += [u for u in _discover_via_common_paths(home) if u not in candidates]

    results = []
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        info = validate_feed(url)
        if info.get("valid"):
            results.append({"url": url, **info})
    return results


def register_pending(name: str, feed_url: str, category: str = "글로벌", subcategory: str = "") -> bool:
    """검증된 피드를 rss_sources에 is_active=false로 등록(검수 대기)."""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    res = requests.post(
        f"{supabase_url}/rest/v1/rss_sources",
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=ignore-duplicates,return=representation",
        },
        json={
            "name": name, "url": feed_url, "category": category,
            "subcategory": subcategory, "is_active": False,
        },
        timeout=15,
    )
    return res.status_code in (200, 201)


def run(homepages: list):
    if not homepages:
        print("훑을 홈페이지가 없습니다. CLI 인자로 URL을 전달하세요.")
        return

    for home in homepages:
        netloc = urlparse(home).netloc
        print(f"\n[탐색] {home}")
        feeds = discover_feeds(home)
        if not feeds:
            print("  피드를 찾지 못함")
            continue
        for f in feeds:
            print(f"  ✅ {f['url']} — {f['entry_count']}건, 최신: {f['latest_title'][:50]!r}")
            ok = register_pending(name=netloc, feed_url=f["url"])
            print(f"     → rss_sources 등록(검수 대기, is_active=false): {'성공' if ok else '실패'}")


if __name__ == "__main__":
    args = sys.argv[1:] or CANDIDATE_HOMEPAGES
    run(args)
