import os
import json
import requests
import time
from datetime import datetime

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
    today = time.strftime("%Y-%m-%d")
    c.execute("""
        SELECT * FROM articles WHERE sent_telegram = 1
        ORDER BY
            CASE WHEN source = 'The Wise Frontier' AND DATE(created_at) = DATE('now') THEN 0
                 WHEN source != 'The Wise Frontier' THEN 1
                 ELSE 2 END ASC,
            created_at DESC
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
                "sent_telegram": "eq.1",
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

    # 정렬: 오늘 자체기사 → 일반기사 → 이전 자체기사
    today = time.strftime("%Y-%m-%d")
    def sort_key(a):
        is_own = a.get("source") == "The Wise Frontier"
        is_today = (a.get("created_at") or "").startswith(today)
        if is_own and is_today:
            return (0, a.get("created_at", ""))
        elif not is_own:
            return (1, a.get("created_at", ""))
        else:
            return (2, a.get("created_at", ""))

    all_articles.sort(key=sort_key, reverse=False)
    # created_at 내림차순 유지 (같은 그룹 내)
    own_today = [a for a in all_articles if a.get("source") == "The Wise Frontier" and (a.get("created_at") or "").startswith(today)]
    normal = [a for a in all_articles if a.get("source") != "The Wise Frontier"]
    own_old = [a for a in all_articles if a.get("source") == "The Wise Frontier" and not (a.get("created_at") or "").startswith(today)]

    own_today.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    normal.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    own_old.sort(key=lambda a: a.get("created_at", ""), reverse=True)

    final = own_today + normal + own_old

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final[:limit], f, ensure_ascii=False, indent=2)

    print(f"[EXPORT] {len(final)}개 기사 → {OUTPUT_FILE}")


def fetch_market_data():
    os.makedirs("docs/data", exist_ok=True)
    market_data = {
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "indices": []
    }
    symbols = {
        "NGX": "^NGSEINDX",
        "JSE": "^J203.JO",
        "NSE KE": "^NASI",
        "EGX30": "^CASE30",
        "SET": "^SET.BK",
        "IDX": "^JKSE",
        "PSEi": "PSEI.PS",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    for name, symbol in symbols.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d"
            res = requests.get(url, headers=headers, timeout=8)
            data = res.json()
            result = data["chart"]["result"][0]
            meta = result["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("chartPreviousClose", meta.get("previousClose", price))
            change_pct = ((price - prev) / prev * 100) if prev else 0
            market_data["indices"].append({
                "name": name, "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "up": change_pct >= 0
            })
            print(f"[MARKET] {name}: {price} ({change_pct:+.2f}%)")
        except Exception as e:
            print(f"[MARKET] {name} 실패: {e}")

    forex_pairs = {
        "USD/NGN": "USDNGN=X",
        "USD/KES": "USDKES=X",
        "USD/ZAR": "USDZAR=X",
    }
    for name, symbol in forex_pairs.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d"
            res = requests.get(url, headers=headers, timeout=8)
            data = res.json()
            result = data["chart"]["result"][0]
            meta = result["meta"]
            price = meta.get("regularMarketPrice", 0)
            prev = meta.get("chartPreviousClose", price)
            change_pct = ((price - prev) / prev * 100) if prev else 0
            market_data["indices"].append({
                "name": name, "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "up": change_pct >= 0
            })
            print(f"[MARKET] {name}: {price} ({change_pct:+.2f}%)")
        except Exception as e:
            print(f"[MARKET] {name} 실패: {e}")

    with open(MARKET_FILE, "w", encoding="utf-8") as f:
        json.dump(market_data, f, ensure_ascii=False, indent=2)
    print(f"[MARKET] {len(market_data['indices'])}개 시세 저장 완료")


if __name__ == "__main__":
    export_articles()
    fetch_market_data()
