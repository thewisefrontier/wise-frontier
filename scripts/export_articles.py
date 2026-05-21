import sqlite3
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

DB_FILE = "data/articles.db"
OUTPUT_FILE = "docs/data/articles.json"

def export_articles(limit=50):
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
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))

    rows = c.fetchall()
    conn.close()

    articles = [dict(row) for row in rows]

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    print(f"[EXPORT] {len(articles)}개 기사 → {OUTPUT_FILE}")

if __name__ == "__main__":
    export_articles()
