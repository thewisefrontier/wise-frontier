import sqlite3
import json
import os
import sys
import requests
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

DB_FILE = "data/articles.db"
OUTPUT_FILE = "docs/data/articles.json"
MARKET_FILE = "docs/data/market_data.json"

def export_articles(limit=9999):
    if not os.path.exists(DB_FILE):
        print("[EXPORT] DB 파일 없음")
        return

    os.makedirs("docs/data", exist_ok=True)

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT * FROM articles
        WHERE sent_telegram = 1
        ORDER BY
            CASE
                WHEN source = 'The Wise Frontier'
                     AND DATE(created_at) = DATE('now') THEN 0
                WHEN source != 'The Wise Frontier' THEN 1
                ELSE 2
            END ASC,
            created_at DESC
        LIMIT ?
    """, (limit,))

    rows = c.fetchall()
    conn.close()

    articles = [dict(row) for row in rows]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"[EXPORT] {len(articles)}개 기사 → {OUTPUT_FILE}")

def fetch_market_data():
    """아프리카/프론티어 시장 시세 수집"""
    os.makedirs("docs/data", exist_ok=True)

    market_data = {
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "indices": []
    }

    # Yahoo Finance API로 주요 지수 수집
    symbols = {
        "NGX": "^NGSEINDX",      # 나이지리아
        "JSE": "^J203.JO",       # 남아공
        "NSE KE": "^NASI",       # 케냐
        "EGX30": "^CASE30",      # 이집트
        "SET": "^SET.BK",        # 태국
        "IDX": "^JKSE",          # 인도네시아
        "PSEi": "PSEI.PS",       # 필리핀
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
                "name": name,
                "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "up": change_pct >= 0
            })
            print(f"[MARKET] {name}: {price} ({change_pct:+.2f}%)")

        except Exception as e:
            print(f"[MARKET] {name} 실패: {e}")

    # 환율 추가 (USD/NGN, USD/KES)
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
                "name": name,
                "symbol": symbol,
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
