import sqlite3
import os
import time

DB_FILE = "data/articles.db"

def get_conn():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title_en      TEXT NOT NULL,
            title_ko      TEXT,
            summary_en    TEXT,
            summary_ko    TEXT,
            url           TEXT UNIQUE NOT NULL,
            source        TEXT,
            category      TEXT,
            region        TEXT,
            country       TEXT,
            country_flag  TEXT,
            score         INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (strftime('%Y-%m-%d %H:%M', 'now', 'localtime')),
            sent_telegram INTEGER DEFAULT 0,
            posted_blog   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def is_url_exists(url: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM articles WHERE url = ?", (url,))
    row = c.fetchone()
    conn.close()
    return row is not None

def insert_article(
    title_en, title_ko, summary_en, summary_ko,
    url, source, category, region, country, country_flag, score
) -> int:
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO articles (
                title_en, title_ko, summary_en, summary_ko,
                url, source, category, region, country, country_flag, score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            title_en, title_ko, summary_en, summary_ko,
            url, source, category, region, country, country_flag, score
        ))
        conn.commit()
        return c.lastrowid
    except sqlite3.IntegrityError:
        return -1
    finally:
        conn.close()

def mark_sent_telegram(article_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE articles SET sent_telegram = 1 WHERE id = ?", (article_id,))
    conn.commit()
    conn.close()

def mark_posted_blog(article_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE articles SET posted_blog = 1 WHERE id = ?", (article_id,))
    conn.commit()
    conn.close()

def get_top_articles(date: str = None, limit: int = 10, region: str = None) -> list:
    """브리핑용 Top 기사 조회 — 점수 높은 순, 오늘 날짜"""
    conn = get_conn()
    c = conn.cursor()

    if date is None:
        date = time.strftime("%Y-%m-%d")

    query = """
        SELECT * FROM articles
        WHERE sent_telegram = 1
        AND created_at LIKE ?
    """
    params = [f"{date}%"]

    if region:
        query += " AND region = ?"
        params.append(region)

    query += " ORDER BY score DESC LIMIT ?"
    params.append(limit)

    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_articles_by_country(country: str, limit: int = 5) -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM articles
        WHERE country = ? AND sent_telegram = 1
        ORDER BY created_at DESC LIMIT ?
    """, (country, limit))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_articles_by_region(region: str, limit: int = 10) -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM articles
        WHERE region = ? AND sent_telegram = 1
        ORDER BY created_at DESC LIMIT ?
    """, (region, limit))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_unposted_articles(limit: int = 10) -> list:
    """블로그에 아직 올리지 않은 기사"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM articles
        WHERE sent_telegram = 1 AND posted_blog = 0
        ORDER BY score DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

if __name__ == "__main__":
    init_db()
    print("✅ DB 초기화 완료")
