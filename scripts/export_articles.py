import os
import json
import re
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


# ── update_log 공개 필터 ──────────────────────────────────────────────
# update_log에는 내부 운영 기록이 쌓인다(트렌드 감지 경로·이미지 처리·표기/문체 교정 사유 등).
# articles.json은 공개 배포되므로 원문을 담으면 누구나 열어볼 수 있다.
# → 첫 항목은 "최초 게시", 이후는 화이트리스트 통과분만 "내용 업데이트"로 일반화해 내보낸다.
#   원문은 DB에 그대로 남으며 admin.html 기사 편집 화면에서 확인한다.
# 공개 JSON에서 제외할 내부 전용 컬럼.
# articles는 select=* 로 조회하므로 컬럼을 새로 만들면 자동으로 공개 배포된다.
# 프론트가 쓰지 않는 내부 필드는 여기에 등록해 유출을 막을 것.
# ⚠️ full_text는 타 매체 원문 전문이 들어갈 수 있어 반드시 제외한다(저작권·용량).
EXPORT_EXCLUDE_FIELDS = (
    "full_text",
    "summary_en",
    "sent_telegram",
    "posted_blog",
    "company_scanned",
    "dedup_reviewed",
    # 2026-09-04 추가: index.html/live.html/archive.html/country.html 전부
    # grep으로 확인 — 이 두 필드를 쓰는 목록 페이지가 하나도 없다(둘 다
    # article.html이 Supabase에서 라이브로 따로 조회해서 보여줌). 목록용
    # JSON에 넣을 이유가 없는 순수 낭비(사용자 지적: "첫페이지 로딩에 데이터가
    # 너무 많이 쓰이면 모바일로 들어오는 사람이 없어져").
    "investment_idea",
    "summary_3lines",
)

# 목록용 카드 미리보기(120~180자)와 로컬 인스턴트 검색에 쓸 길이. 500자에서
# 300자로 더 줄임(2026-09-04, 모바일 로딩 부담 추가 지적) — 로컬 인스턴트
# 검색은 어차피 400ms 디바운스 후 Supabase 라이브 쿼리(summary_ko 전문)로
# 넘어가므로(index.html searchSupabase), 여기 더 잘려도 최종 검색 정확도엔
# 영향 없음.
SUMMARY_PREVIEW_LEN = 300

# 실사고(2026-08-09): 예전엔 화이트리스트 방식이었는데, 실제 업데이트 경로들이 쓰는 노트
# 문구("후속 정보 추가", generate_update_note()의 자유 문장 등)와 화이트리스트가 하나도
# 안 맞아 진짜 업데이트가 있어도 로그에 항상 안 보였다. 블랙리스트로 뒤집음.
# ⚠️ Python이라 docs/js/update-log-filter.js를 import할 수 없어 이 정규식은 그 파일과
#    별도로 유지된다 — 규칙을 고치면 반드시 두 곳(여기 + docs/js/update-log-filter.js)을
#    같이 고칠 것. docs/article.html·docs/live.html·functions/article.js 3곳은 그 JS
#    파일 하나를 공용 import한다(2026-09-01 통합, 그 전엔 이 3곳도 각자 복붙돼 있다가
#    8/9 수정이 일부만 반영돼 재발한 적 있음 — 메모리 newsfinal_update_log_display_drift 참조).
INTERNAL_ONLY_NOTE_RE = re.compile(r"실시간 트렌드 감지|자동 중복정리|음역 자동 교정|문자셋 이탈|복수주제 분리 파킹|수동 정리")


def sanitize_update_log(log):
    if not isinstance(log, list):
        return []
    out = []
    for i, item in enumerate(log):
        if not isinstance(item, dict):
            continue
        if i == 0:
            label = "최초 게시"
        elif not INTERNAL_ONLY_NOTE_RE.search(str(item.get("note") or "")):
            # 2026-09-02: headline(gemini_writer.py/gemini_summarizer.py의
            # _generate_update_headline()이 생성한 25자 내외 한 줄 요약)이 있으면
            # 그걸 노출한다 — 매번 "내용 업데이트"만 반복 표시하지 말고 실제로
            # 뭐가 바뀌었는지 보여달라는 요청. docs/js/update-log-filter.js와
            # 동일 로직 유지할 것.
            headline = str(item.get("headline") or "").strip()
            label = headline or "내용 업데이트"
        else:
            continue
        out.append({"timestamp": item.get("timestamp"), "note": label})
    return out


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

    # 내부 운영 기록·내부 전용 컬럼이 공개 JSON으로 새어나가지 않도록 정화
    for a in all_articles:
        if "update_log" in a:
            a["update_log"] = sanitize_update_log(a.get("update_log"))
        for k in EXPORT_EXCLUDE_FIELDS:
            a.pop(k, None)
        # 실사고(2026-09-04, 사용자 지적 "로딩이 좀 느린 느낌"): index.html/
        # live.html/archive.html/country.html/article.html의 관련기사 목록은
        # 전부 카드 미리보기(120~180자)나 제목만 쓰는데, 8/9 분량 하한을
        # 700자→2000자로 올린 뒤로 summary_ko가 기사당 평균 4.7KB까지 커져서
        # (344건 기준 articles.json 1.7MB) 목록 페이지마다 안 쓰는 본문
        # 전체를 매번 통째로 내려받고 있었다. 본문 전문은 article.html이
        # Supabase에서 라이브로 따로 조회하므로(article.html 상단 주석 참조)
        # 목록용 JSON에는 미리보기+로컬 검색에 충분한 길이만 남긴다.
        if isinstance(a.get("summary_ko"), str) and len(a["summary_ko"]) > SUMMARY_PREVIEW_LEN:
            a["summary_ko"] = a["summary_ko"][:SUMMARY_PREVIEW_LEN]

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
        '<url><loc>https://newsfinal.co.kr/about</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>',
        '<url><loc>https://newsfinal.co.kr/privacy</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>',
        '<url><loc>https://newsfinal.co.kr/terms</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>',
        '<url><loc>https://newsfinal.co.kr/archive</loc><changefreq>daily</changefreq><priority>0.6</priority></url>',
        '<url><loc>https://newsfinal.co.kr/live</loc><changefreq>hourly</changefreq><priority>0.8</priority></url>',
        '<url><loc>https://newsfinal.co.kr/calendar</loc><changefreq>daily</changefreq><priority>0.6</priority></url>',
        '<url><loc>https://newsfinal.co.kr/markets</loc><changefreq>hourly</changefreq><priority>0.7</priority></url>',
        '<url><loc>https://newsfinal.co.kr/country</loc><changefreq>daily</changefreq><priority>0.7</priority></url>',
        '<url><loc>https://newsfinal.co.kr/company</loc><changefreq>daily</changefreq><priority>0.7</priority></url>',
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
