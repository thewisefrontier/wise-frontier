# -*- coding: utf-8 -*-
"""RSS 소스 교체 2차 점검(2026-11-08, 1회용, DB에 쓰지 않음).

probe_sources.py에서 "피드 없음"이던 매체의 피드 주소를 ① 알려진 공식 주소 → ② 흔한 경로(브라우저 UA로
직접 받기 — 봇 UA를 막는 사이트가 있다) → ③ 구글뉴스 사이트 검색 피드 순으로 찾고, 최근 기사 3건 본문
크롤링 성공 수를 잰다. 실행: one_off_task.yml(script=one_off/probe_sources2.py)
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

import feedparser
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from text_crawl import crawl_full_text  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36", "Accept": "application/rss+xml, application/xml, text/xml, */*"}
MIN_TEXT = 500
FRESH_DAYS = 3
COMMON = ["/feed", "/feed/", "/rss", "/rss.xml", "/rss/", "/feeds/rss", "/index.rss", "/feed/rss",
          "/arc/outboundfeeds/rss/?outputType=xml", "/rss/news", "/news/rss", "/rss/all"]

# (홈페이지, category, subcategory, 구글뉴스 언어·지역, [알려진 피드 주소])
SITES = [
    ("https://apnews.com", "세계", "북미", "en-US:US", ["https://apnews.com/index.rss"]),
    ("https://www.usatoday.com", "세계", "북미", "en-US:US",
     ["https://rssfeeds.usatoday.com/usatoday-NewsTopStories", "https://rssfeeds.usatoday.com/UsatodaycomWorld-TopStories"]),
    ("https://www.politico.com", "세계", "북미", "en-US:US",
     ["https://rss.politico.com/politics-news.xml", "https://www.politico.com/rss/politicopicks.xml"]),
    ("https://www.ctvnews.ca", "세계", "북미", "en-CA:CA",
     ["https://www.ctvnews.ca/rss/ctvnews-ca-top-stories-public-rss-1.822009"]),
    ("https://www.thestar.com", "세계", "북미", "en-CA:CA",
     ["https://www.thestar.com/search/?f=rss&t=article&c=news*&l=50&s=start_time&sd=desc"]),
    ("https://www.tagesspiegel.de", "세계", "유럽", "de:DE", ["https://www.tagesspiegel.de/contentexport/feed/home"]),
    ("https://www.lessentiel.lu", "세계", "유럽", "fr:LU", ["https://www.lessentiel.lu/fr/rss", "https://www.lessentiel.lu/rss/fr/"]),
    ("https://www.rtve.es", "세계", "유럽", "es:ES", ["https://api2.rtve.es/rss/temas_noticias.xml"]),
    ("https://www.lefigaro.fr", "세계", "유럽", "fr:FR", ["https://www.lefigaro.fr/rss/figaro_actualites.xml"]),
    ("https://www.independent.co.uk", "세계", "유럽", "en-GB:GB",
     ["https://www.independent.co.uk/news/world/rss", "https://www.independent.co.uk/news/uk/rss"]),
    ("https://www.1news.co.nz", "세계", "오세아니아", "en-NZ:NZ", ["https://www.1news.co.nz/arc/outboundfeeds/rss/?outputType=xml"]),
    ("https://www.animalpolitico.com", "세계", "라틴아메리카", "es-419:MX", ["https://www.animalpolitico.com/feed"]),
    ("https://www.proceso.com.mx", "세계", "라틴아메리카", "es-419:MX",
     ["https://www.proceso.com.mx/arc/outboundfeeds/rss/?outputType=xml"]),
    ("https://www.eluniversal.com.mx", "세계", "라틴아메리카", "es-419:MX",
     ["https://www.eluniversal.com.mx/arc/outboundfeeds/rss/?outputType=xml"]),
    ("https://www.milenio.com", "세계", "라틴아메리카", "es-419:MX", ["https://www.milenio.com/rss"]),
    ("https://ifpnews.com", "세계", "중동", "en-US:US", ["https://ifpnews.com/feed/"]),
    ("https://shafaq.com/en", "세계", "중동", "en-US:US", ["https://shafaq.com/en/rss"]),
    ("https://www.rudaw.net/english", "세계", "중동", "en-US:US", ["https://www.rudaw.net/english/rss"]),
    ("https://www.ina.iq", "세계", "중동", "ar:IQ", ["https://ina.iq/eng/rss.php", "https://www.ina.iq/rss.php"]),
    ("https://www.tap.info.tn", "세계", "아프리카", "fr:TN", ["https://www.tap.info.tn/fr/rss", "https://www.tap.info.tn/en/rss"]),
    ("https://nation.africa", "세계", "아프리카", "en-KE:KE", ["https://nation.africa/kenya/rss.xml"]),
    ("https://www.standardmedia.co.ke", "세계", "아프리카", "en-KE:KE", ["https://www.standardmedia.co.ke/rss/headlines.php"]),
    ("https://addisstandard.com", "세계", "아프리카", "en-US:US", ["https://addisstandard.com/feed/"]),
    ("https://sudantribune.com", "세계", "아프리카", "en-US:US", ["https://sudantribune.com/feed/"]),
    ("https://www.garoweonline.com", "세계", "아프리카", "en-US:US", ["https://www.garoweonline.com/en/rss"]),
    ("https://www.news24.com", "세계", "아프리카", "en-ZA:ZA", ["https://feeds.news24.com/articles/news24/TopStories/rss"]),
    ("https://www.graphic.com.gh", "세계", "아프리카", "en-GH:GH", ["https://www.graphic.com.gh/news.feed?type=rss"]),
    ("https://citinewsroom.com", "세계", "아프리카", "en-GH:GH", ["https://citinewsroom.com/feed/"]),
    ("https://www.vanguardngr.com", "세계", "아프리카", "en-NG:NG", ["https://www.vanguardngr.com/feed/"]),
    ("https://www.abidjan.net", "세계", "아프리카", "fr:CI", ["https://news.abidjan.net/rss"]),
    ("https://www.fratmat.info", "세계", "아프리카", "fr:CI", ["https://www.fratmat.info/feed"]),
    ("https://www.koaci.com", "세계", "아프리카", "fr:CI", ["https://www.koaci.com/rss"]),
    ("https://www.lexpress.mu", "세계", "아프리카", "fr:MU", ["https://lexpress.mu/rss.xml"]),
    ("https://jakartaglobe.id", "세계", "동남아시아", "en-ID:ID", ["https://jakartaglobe.id/rss"]),
    ("https://www.thejakartapost.com", "세계", "동남아시아", "en-ID:ID", ["https://www.thejakartapost.com/rss"]),
    ("https://www.malaymail.com", "세계", "동남아시아", "en-MY:MY", ["https://www.malaymail.com/feed/rss/malaysia"]),
    ("https://www.thestar.com.my", "세계", "동남아시아", "en-MY:MY", ["https://www.thestar.com.my/rss/News/Nation"]),
    ("https://www.bernama.com/en", "세계", "동남아시아", "en-MY:MY", ["https://www.bernama.com/en/rssfeed.php"]),
    ("https://www.sentinelassam.com", "세계", "남아시아", "en-IN:IN", ["https://www.sentinelassam.com/rss"]),
    ("https://www.livemint.com", "경제", "금융/증권", "en-IN:IN", ["https://www.livemint.com/rss/markets"]),
    ("https://www.financialexpress.com", "경제", "금융/증권", "en-IN:IN", ["https://www.financialexpress.com/market/feed/"]),
    ("https://www.thestreet.com", "경제", "금융/증권", "en-US:US", ["https://www.thestreet.com/.rss/full/"]),
    ("https://www.fxstreet.com", "경제", "금융/증권", "en-US:US", ["https://www.fxstreet.com/rss/news"]),
    ("https://www.kitco.com", "경제", "금융/증권", "en-US:US", ["https://www.kitco.com/feed/rss/news/"]),
    ("https://www.zacks.com", "경제", "금융/증권", "en-US:US", []),
    ("https://cryptoslate.com", "경제", "금융/증권", "en-US:US", ["https://cryptoslate.com/feed/"]),
    ("https://citizen.digital", "세계", "아프리카", "en-KE:KE", []),  # 참고용(이미 등록) — 구글뉴스 경로 대조군
]


def google_feed(home, locale):
    hl, gl = locale.split(":")
    host = urlparse(home).netloc.replace("www.", "")
    path = urlparse(home).path.strip("/")
    q = f"site:{host}/{path}" if path else f"site:{host}"
    return f"https://news.google.com/rss/search?q={quote(q)}+when:3d&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}"


def fetch_feed(url):
    try:
        r = requests.get(url, headers=UA, timeout=15)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        d = feedparser.parse(r.content)
    except Exception as e:
        return None, repr(e)[:80]
    if not d.entries:
        return None, "항목 없음"
    newest = max((time.mktime(e.published_parsed) for e in d.entries if e.get("published_parsed")), default=0)
    if newest and time.time() - newest > FRESH_DAYS * 86400:
        return None, "최근 기사 없음"
    return d, ""


def test_feed(url, d):
    lens = []
    for e in d.entries[:3]:
        try:
            lens.append(len(crawl_full_text(e.get("link") or "", timeout=10) or ""))
        except Exception:
            lens.append(0)
    return sum(l >= MIN_TEXT for l in lens), lens


def probe(site):
    home, cat, sub, locale, known = site
    base = f"{urlparse(home).scheme}://{urlparse(home).netloc}"
    tries = known + [base + p for p in COMMON if base + p not in known] + [google_feed(home, locale)]
    best = {"home": home, "category": cat, "subcategory": sub, "feed": "", "title": "", "ok": 0, "lens": [], "notes": []}
    for url in tries:
        d, why = fetch_feed(url)
        if not d:
            if url in known or "news.google.com" in url:
                best["notes"].append(f"{url[:70]}: {why}")
            continue
        ok, lens = test_feed(url, d)
        if ok > best["ok"]:
            best.update(feed=url, title=(d.feed.get("title") or "")[:80], ok=ok, lens=lens)
        if ok >= 2:
            break
    return best


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(probe, SITES))
    for r in sorted(results, key=lambda r: (-r["ok"], r["home"])):
        print(f"{r['ok']}/3 {r['home']} | {r['feed']} | {r['title']} | {r['lens']}")
        for n in r["notes"][:3]:
            print(f"      - {n}")
    print("RESULT_JSON " + json.dumps([r for r in results if r["ok"] >= 2], ensure_ascii=False))
