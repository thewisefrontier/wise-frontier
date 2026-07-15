import os
import json
import requests
from datetime import datetime, timedelta, timezone

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
OUTPUT_FILE = "docs/data/articles.json"
MARKET_FILE = "docs/data/market_data.json"

def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

def _export_from_sqlite(limit=9999):
    """SQLite에서 직접 내보내기 (폴백)"""
    import sqlite3
    DB_FILE = "data/articles.db"
    if not os.path.exists(DB_FILE):
        print("[EXPORT] SQLite 파일도 없음")
        return
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT * FROM articles
        WHERE is_published = 1 AND source = 'NewsFinal' AND created_at >= date('now', '-7 days')
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"[EXPORT] SQLite 폴백: {len(rows)}개 기사 → {OUTPUT_FILE}")


def export_articles(limit=9999):
    os.makedirs("docs/data", exist_ok=True)

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[EXPORT] Supabase 환경변수 없음 — SQLite 폴백")
        _export_from_sqlite(limit)
        return

    all_articles = []
    offset = 0
    batch = 1000

    while True:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/articles",
            headers={**_headers(), "Range": f"{offset}-{offset+batch-1}"},
            params={
                "select": "*",
                "is_published": "eq.true",
                "source": "eq.NewsFinal",
                "created_at": f"gte.{(datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))) - timedelta(days=7)).strftime('%Y-%m-%d')}",
                "order": "created_at.desc",
            },
            timeout=30
        )
        if res.status_code not in (200, 206):
            print(f"[EXPORT] 오류: {res.status_code} — {res.text[:300]}")
            print("[EXPORT] SQLite 폴백 시도...")
            _export_from_sqlite(limit)
            return

        data = res.json()
        if not data:
            break

        all_articles.extend(data)
        if len(data) < batch:
            break
        offset += batch

    # 최신순 정렬
    all_articles.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    final = all_articles

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final[:limit], f, ensure_ascii=False, indent=2)

    print(f"[EXPORT] {len(final)}개 기사 → {OUTPUT_FILE}")
    return final[:limit]


def fetch_market_data():
    os.makedirs("docs/data", exist_ok=True)
    market_data = {
        "updated_at": datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
        "indices": [],   # 기존 호환용 — 프론티어 지수 전체를 평평하게 담음 (사이드바 티커용)
        "groups": {
            "us": [],       # 미국
            "kr": [],       # 한국
            "frontier": [], # 아프리카 · 프론티어 마켓
            "fx": [],       # 환율
        }
    }
    headers = {"User-Agent": "Mozilla/5.0"}

    def fetch_one(name, symbol):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d"
            res = requests.get(url, headers=headers, timeout=8)
            data = res.json()
            result = data["chart"]["result"][0]
            meta = result["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("chartPreviousClose", meta.get("previousClose", price))
            change_pct = ((price - prev) / prev * 100) if prev else 0
            entry = {
                "name": name, "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "up": change_pct >= 0,
            }
            print(f"[MARKET] {name}: {price} ({change_pct:+.2f}%)")
            return entry
        except Exception as e:
            print(f"[MARKET] {name} 실패: {e}")
            return None

    # 미국 주요 지수
    us_symbols = {
        "S&P 500": "^GSPC",
        "나스닥": "^IXIC",
        "다우존스": "^DJI",
    }
    # 한국 주요 지수
    kr_symbols = {
        "코스피": "^KS11",
        "코스닥": "^KQ11",
    }
    # 아프리카 · 프론티어 마켓 지수
    frontier_symbols = {
        "NGX (나이지리아)": "^NGSEINDX",
        "JSE (남아공)": "^J203.JO",
        "NSE (케냐)": "^NASI",
        "EGX30 (이집트)": "^CASE30",
        "SET (태국)": "^SET.BK",
        "IDX (인도네시아)": "^JKSE",
        "PSEi (필리핀)": "PSEI.PS",
    }
    # 환율 (프론티어 통화 vs USD)
    forex_pairs = {
        "USD/NGN": "USDNGN=X",
        "USD/KES": "USDKES=X",
        "USD/ZAR": "USDZAR=X",
    }

    for group_key, symbols in [("us", us_symbols), ("kr", kr_symbols), ("frontier", frontier_symbols)]:
        for name, symbol in symbols.items():
            entry = fetch_one(name, symbol)
            if entry:
                market_data["groups"][group_key].append(entry)
                market_data["indices"].append(entry)

    for name, symbol in forex_pairs.items():
        entry = fetch_one(name, symbol)
        if entry:
            market_data["groups"]["fx"].append(entry)
            market_data["indices"].append(entry)

    # 기업 주가 수집 (companies 테이블에서 ticker + exchange 있는 기업)
    try:
        comp_res = requests.get(
            f"{SUPABASE_URL}/rest/v1/companies",
            headers=_headers(),
            params={"select": "id,ticker,exchange,name", "is_published": "eq.true",
                    "ticker": "not.is.null", "order": "name.asc"},
            timeout=15
        )
        if comp_res.status_code in (200, 206):
            companies = comp_res.json()
            EXCHANGE_SUFFIX = {
                'NSE': '.NR', 'NGX': '.LG', 'JSE': '.JO',
                'IDX': '.JK', 'SET': '.BK', 'PSE': '.PS',
                'EGX': '.CA', 'HOSE': '.VN', 'KSE': '.KA',
            }
            company_prices = {}
            for comp in companies[:30]:  # 최대 30개
                ticker = comp.get("ticker", "")
                exchange = comp.get("exchange", "")
                if not ticker:
                    continue
                suffix = EXCHANGE_SUFFIX.get(exchange, "")
                symbol = ticker + suffix
                entry = fetch_one(comp["name"], symbol)
                if entry:
                    company_prices[ticker] = {
                        "name": comp["name"],
                        "symbol": symbol,
                        "price": entry["price"],
                        "change_pct": entry["change_pct"],
                        "up": entry["up"],
                    }
            market_data["company_prices"] = company_prices
            print(f"[MARKET] 기업 주가 {len(company_prices)}개 수집")
    except Exception as e:
        print(f"[MARKET] 기업 주가 수집 실패: {e}")
        market_data["company_prices"] = {}

    with open(MARKET_FILE, "w", encoding="utf-8") as f:
        json.dump(market_data, f, ensure_ascii=False, indent=2)
    print(f"[MARKET] {len(market_data['indices'])}개 시세 저장 완료")


def generate_sitemap(articles):
    """sitemap.xml 생성

    주의: URL은 확장자 없는 형태(article?id=..)로 생성한다.
    Cloudflare Pages가 *.html?query 요청을 자동으로 확장자 없는 주소로
    301 리디렉션하는 내장 동작이 있어(끌 수 없음), sitemap/canonical이
    .html 버전을 가리키면 구글이 "리디렉션 있는 페이지"로 색인 제외한다.
    """
    os.makedirs("docs", exist_ok=True)

    # 게시된 전체 기사 조회 (홈 피드 7일 의존에서 분리)
    sitemap_articles = []
    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        offset = 0
        batch = 1000
        fetch_ok = True
        while True:
            res = requests.get(
                f"{SUPABASE_URL}/rest/v1/articles",
                headers={**_headers(), "Range": f"{offset}-{offset+batch-1}"},
                params={
                    "select": "id,created_at",
                    "is_published": "eq.true",
                    "order": "created_at.desc",
                },
                timeout=30
            )
            if res.status_code not in (200, 206):
                print(f"[SITEMAP] 전체 조회 오류: {res.status_code} — 7일치로 폴백")
                fetch_ok = False
                break
            data = res.json()
            if not data:
                break
            sitemap_articles.extend(data)
            if len(data) < batch:
                break
            offset += batch
        if not fetch_ok:
            sitemap_articles = articles or []
    else:
        print("[SITEMAP] Supabase 환경변수 없음 — 7일치로 폴백")
        sitemap_articles = articles or []

    if not sitemap_articles:
        print("[SITEMAP] 기사 목록 없음 — 기존 sitemap 유지(스킵)")
        return

    urls = [
        '<url><loc>https://newsfinal.co.kr/</loc><changefreq>hourly</changefreq><priority>1.0</priority></url>',
        '<url><loc>https://newsfinal.co.kr/about.html</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>',
        '<url><loc>https://newsfinal.co.kr/privacy.html</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>',
        '<url><loc>https://newsfinal.co.kr/terms.html</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>',
        '<url><loc>https://newsfinal.co.kr/archive.html</loc><changefreq>daily</changefreq><priority>0.6</priority></url>',
        '<url><loc>https://newsfinal.co.kr/live.html</loc><changefreq>hourly</changefreq><priority>0.8</priority></url>',
        '<url><loc>https://newsfinal.co.kr/calendar.html</loc><changefreq>daily</changefreq><priority>0.6</priority></url>',
        '<url><loc>https://newsfinal.co.kr/markets.html</loc><changefreq>hourly</changefreq><priority>0.7</priority></url>',
        '<url><loc>https://newsfinal.co.kr/country.html</loc><changefreq>daily</changefreq><priority>0.7</priority></url>',
        '<url><loc>https://newsfinal.co.kr/company.html</loc><changefreq>daily</changefreq><priority>0.7</priority></url>',
    ]
    for a in sitemap_articles:
        if a.get('id'):
            created = (a.get('created_at') or '')[:10]
            lastmod = f'<lastmod>{created}</lastmod>' if len(created) == 10 else ''
            urls.append(f'<url><loc>https://newsfinal.co.kr/article?id={a["id"]}</loc>{lastmod}<changefreq>weekly</changefreq><priority>0.8</priority></url>')

    sitemap = f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
</urlset>'''

    with open("docs/sitemap.xml", "w", encoding="utf-8") as f:
        f.write(sitemap)
    print(f"[SITEMAP] {len(urls)}개 URL → docs/sitemap.xml")


def export_companies():
    """Supabase companies 테이블 → docs/data/companies.json"""
    os.makedirs("docs/data", exist_ok=True)
    all_companies = []
    offset = 0
    batch = 1000
    while True:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/companies",
            headers={**_headers(), "Range": f"{offset}-{offset+batch-1}"},
            params={
                "select": "id,name,name_ko,country,country_flag,exchange,ticker,sector,description,founded_year,headquarters,website,is_published,updated_at",
                "is_published": "eq.true",
                "order": "country.asc,name.asc",
            },
            timeout=30
        )
        if res.status_code not in (200, 206):
            print(f"[COMPANIES] 오류: {res.status_code}")
            break
        data = res.json()
        if not data:
            break
        all_companies.extend(data)
        if len(data) < batch:
            break
        offset += batch

    with open("docs/data/companies.json", "w", encoding="utf-8") as f:
        json.dump(all_companies, f, ensure_ascii=False, indent=2)
    print(f"[COMPANIES] {len(all_companies)}개 기업 → docs/data/companies.json")


if __name__ == "__main__":
    articles = export_articles()
    fetch_market_data()
    export_companies()
    generate_sitemap(articles or [])
