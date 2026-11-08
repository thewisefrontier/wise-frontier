# -*- coding: utf-8 -*-
"""RSS 소스 교체 사전 점검(2026-11-08, 1회용, DB에 쓰지 않음).

사용자 지시: "크롤링을 막고 있는 사이트는 없애고, 다른 사이트를 추가해서 소스를 늘려."
본문 크롤링은 Actions 서버에서 돌기 때문에(데이터센터 IP는 내 PC와 차단 여부가 다르다) 후보 매체도
여기서 시험한다. ① 후보 홈페이지에서 피드 탐색 → 최근 기사 3건 본문 크롤링 성공 수, ② 구글뉴스
리다이렉트 해독이 Actions에서 왜 100% 실패하는지(로컬에선 성공).
실행: one_off_task.yml(script=one_off/probe_sources.py)
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import feedparser
import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from feed_discovery import HEADERS, discover_feeds  # noqa: E402
from text_crawl import crawl_full_text  # noqa: E402

# (홈페이지, category, subcategory)
CANDIDATES = [
    # 북미·유럽(The Hill·NYT·CBC·Sky·FT·Zeit·Telegraaf·Libération·Ouest-France·Irish Independent·HuffPost ES·Virgule 대체)
    ("https://apnews.com", "세계", "북미"), ("https://www.usatoday.com", "세계", "북미"),
    ("https://www.pbs.org/newshour", "세계", "북미"), ("https://www.politico.com", "세계", "북미"),
    ("https://www.ctvnews.ca", "세계", "북미"), ("https://www.thestar.com", "세계", "북미"),
    ("https://www.bbc.co.uk/news", "세계", "유럽"), ("https://www.independent.co.uk", "세계", "유럽"),
    ("https://www.breakingnews.ie", "세계", "유럽"), ("https://www.irishexaminer.com", "세계", "유럽"),
    ("https://www.tagesspiegel.de", "세계", "유럽"), ("https://www.n-tv.de", "세계", "유럽"),
    ("https://www.ad.nl", "세계", "유럽"), ("https://www.rtl.nl", "세계", "유럽"),
    ("https://www.lessentiel.lu", "세계", "유럽"), ("https://www.luxtimes.lu", "세계", "유럽"),
    ("https://www.rtve.es", "세계", "유럽"), ("https://www.eldiario.es", "세계", "유럽"),
    ("https://www.elmundo.es", "세계", "유럽"), ("https://www.lefigaro.fr", "세계", "유럽"),
    ("https://www.huffingtonpost.fr", "세계", "유럽"), ("https://www.1news.co.nz", "세계", "오세아니아"),
    # 라틴아메리카·카리브해(O Antagonista·24 Horas·Havana Times 대체)
    ("https://www.poder360.com.br", "세계", "라틴아메리카"), ("https://www.cnnbrasil.com.br", "세계", "라틴아메리카"),
    ("https://www.animalpolitico.com", "세계", "라틴아메리카"), ("https://www.proceso.com.mx", "세계", "라틴아메리카"),
    ("https://www.eluniversal.com.mx", "세계", "라틴아메리카"), ("https://www.milenio.com", "세계", "라틴아메리카"),
    ("https://www.cibercuba.com", "세계", "카리브해"), ("https://diariodecuba.com", "세계", "카리브해"),
    # 중동·북아프리카(IRNA·ISNA·바그다드투데이·Mosaique FM 대체)
    ("https://www.iranintl.com/en", "세계", "중동"), ("https://ifpnews.com", "세계", "중동"),
    ("https://shafaq.com/en", "세계", "중동"), ("https://www.rudaw.net/english", "세계", "중동"),
    ("https://www.ina.iq", "세계", "중동"), ("https://www.tap.info.tn", "세계", "아프리카"),
    ("https://kapitalis.com", "세계", "아프리카"),
    # 아프리카(AllAfrica 8개국·Linfodrome·IONNEWS 대체)
    ("https://nation.africa", "세계", "아프리카"), ("https://www.standardmedia.co.ke", "세계", "아프리카"),
    ("https://www.theeastafrican.co.ke", "세계", "아프리카"), ("https://addisstandard.com", "세계", "아프리카"),
    ("https://sudantribune.com", "세계", "아프리카"), ("https://www.dabangasudan.org/en", "세계", "아프리카"),
    ("https://www.garoweonline.com", "세계", "아프리카"), ("https://www.hiiraan.com", "세계", "아프리카"),
    ("https://www.news24.com", "세계", "아프리카"), ("https://iol.co.za", "세계", "아프리카"),
    ("https://www.graphic.com.gh", "세계", "아프리카"), ("https://citinewsroom.com", "세계", "아프리카"),
    ("https://punchng.com", "세계", "아프리카"), ("https://www.vanguardngr.com", "세계", "아프리카"),
    ("https://dailynews.co.tz", "세계", "아프리카"), ("https://www.abidjan.net", "세계", "아프리카"),
    ("https://www.fratmat.info", "세계", "아프리카"), ("https://www.koaci.com", "세계", "아프리카"),
    ("https://www.lexpress.mu", "세계", "아프리카"), ("https://defimedia.info", "세계", "아프리카"),
    ("https://www.lemauricien.com", "세계", "아프리카"),
    # 아시아(Tempo·Berita Harian·Northeast Now 대체)
    ("https://jakartaglobe.id", "세계", "동남아시아"), ("https://www.thejakartapost.com", "세계", "동남아시아"),
    ("https://www.malaymail.com", "세계", "동남아시아"), ("https://www.thestar.com.my", "세계", "동남아시아"),
    ("https://www.bernama.com/en", "세계", "동남아시아"), ("https://www.eastmojo.com", "세계", "남아시아"),
    ("https://www.sentinelassam.com", "세계", "남아시아"),
    # 금융·코인(Nasdaq·MarketWatch·Investing·Forbes·Seeking Alpha·Business Standard·BeInCrypto 대체)
    ("https://www.livemint.com", "경제", "금융/증권"), ("https://www.financialexpress.com", "경제", "금융/증권"),
    ("https://www.businesstoday.in", "경제", "금융/증권"), ("https://www.thestreet.com", "경제", "금융/증권"),
    ("https://www.fxstreet.com", "경제", "금융/증권"), ("https://www.kitco.com", "경제", "금융/증권"),
    ("https://www.zacks.com", "경제", "금융/증권"), ("https://www.theblock.co", "경제", "금융/증권"),
    ("https://cryptoslate.com", "경제", "금융/증권"), ("https://www.cryptopolitan.com", "경제", "금융/증권"),
]

MIN_TEXT = 500   # 이 길이 이상 본문이 나와야 성공으로 친다


def probe(cand):
    home, cat, sub = cand
    out = {"home": home, "category": cat, "subcategory": sub, "feed": "", "title": "", "ok": 0, "tried": 0, "lens": []}
    try:
        feeds = discover_feeds(home)
    except Exception as e:
        out["error"] = f"discover: {e}"
        return out
    if not feeds:
        out["error"] = "피드 없음"
        return out
    feed = feeds[0]
    out["feed"] = feed.get("url") if isinstance(feed, dict) else str(feed)
    parsed = feedparser.parse(out["feed"], request_headers=HEADERS)
    out["title"] = (parsed.feed.get("title") or "")[:80]
    links = [e.get("link") for e in parsed.entries[:3] if e.get("link")]
    for link in links:
        out["tried"] += 1
        try:
            text = crawl_full_text(link, timeout=10) or ""
        except Exception:
            text = ""
        out["lens"].append(len(text))
        out["ok"] += len(text) >= MIN_TEXT
    return out


def probe_google_news():
    from googlenewsdecoder import gnewsdecoder
    conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=10)
    cur = conn.cursor()
    cur.execute("select url from raw_candidates where url like '%%news.google.com%%' order by id desc limit 3")
    for (u,) in cur.fetchall():
        try:
            print("  [구글뉴스 해독]", json.dumps(gnewsdecoder(u, interval=1), ensure_ascii=False)[:300])
        except Exception as e:
            print("  [구글뉴스 해독] 예외:", repr(e)[:300])


if __name__ == "__main__":
    probe_google_news()
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(probe, CANDIDATES))
    for r in sorted(results, key=lambda r: (-r["ok"], r["home"])):
        print(f"{r['ok']}/{r['tried']} {r['home']} | {r['feed']} | {r['title']} | {r['lens']} {r.get('error', '')}")
    print("RESULT_JSON " + json.dumps([r for r in results if r["ok"] >= 2], ensure_ascii=False))
