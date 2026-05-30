"""
fix_titles.py
-------------
기존 The Wise Frontier 자체 기사 제목에서
'[이슈]', '외 N건', '— 2026.MM.DD' 등 불필요한 부분 제거

실행: python scripts/fix_titles.py
"""

import sqlite3
import re
import os

DB_FILE = "data/articles.db"

def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def clean_title(title):
    if not title:
        return title
    # [이슈], [종합] 제거
    title = re.sub(r'^\[이슈\]\s*', '', title)
    title = re.sub(r'^\[종합\]\s*', '', title)
    # ' 외 N건' 제거
    title = re.sub(r'\s*외\s*\d+건', '', title)
    # ' — 2026.MM.DD' 날짜 제거
    title = re.sub(r'\s*—\s*\d{4}\.\d{2}\.\d{2}', '', title)
    # 앞뒤 공백 정리
    return title.strip()

def run():
    if not os.path.exists(DB_FILE):
        print("[ERROR] DB 파일 없음")
        return

    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        SELECT id, title_ko FROM articles
        WHERE source = 'The Wise Frontier'
        ORDER BY created_at DESC
    """)
    rows = c.fetchall()
    print(f"자체 기사 {len(rows)}건 확인")

    updated = 0
    for row in rows:
        old_title = row["title_ko"] or ""
        new_title = clean_title(old_title)
        if new_title != old_title:
            c.execute("UPDATE articles SET title_en = ?, title_ko = ? WHERE id = ?",
                      (new_title, new_title, row["id"]))
            print(f"  수정: {old_title[:60]}")
            print(f"    → {new_title[:60]}")
            updated += 1

    conn.commit()
    conn.close()
    print(f"\n✅ 완료 — {updated}건 제목 수정")

if __name__ == "__main__":
    run()
