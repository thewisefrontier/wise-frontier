"""
db.py — Supabase PostgreSQL 버전
기존 SQLite 인터페이스를 그대로 유지하면서 내부를 Supabase로 교체
"""

import os
import time
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse

# Supabase DB 연결 정보
# Supabase 프로젝트 URL에서 PostgreSQL 연결 문자열 구성
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _get_db_url():
    """Supabase URL에서 PostgreSQL 연결 문자열 생성"""
    if not SUPABASE_URL:
        raise ValueError("SUPABASE_URL 환경변수가 설정되지 않았습니다.")
    # Supabase PostgreSQL 직접 연결
    # URL: https://xxxx.supabase.co → host: db.xxxx.supabase.co
    parsed = urlparse(SUPABASE_URL)
    project_ref = parsed.hostname.split(".")[0]
    db_password = os.getenv("SUPABASE_DB_PASSWORD", "")
    return f"postgresql://postgres.{project_ref}:{db_password}@aws-0-ap-northeast-2.pooler.supabase.com:6543/postgres"

def get_conn():
    """PostgreSQL 연결 반환"""
    db_url = _get_db_url()
    conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    """테이블 초기화"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id            SERIAL PRIMARY KEY,
            title_en      TEXT NOT NULL,
            title_ko      TEXT,
            summary_en    TEXT,
            summary_ko    TEXT,
            url           TEXT UNIQUE NOT NULL,
            source        TEXT,
            category      TEXT,
            subcategory   TEXT,
            region        TEXT,
            country       TEXT,
            country_flag  TEXT,
            score         INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT to_char(NOW() AT TIME ZONE 'Asia/Seoul', 'YYYY-MM-DD HH24:MI'),
            sent_telegram INTEGER DEFAULT 0,
            posted_blog   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    print("✅ DB 초기화 완료 (Supabase PostgreSQL)")

def is_url_exists(url: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM articles WHERE url = %s", (url,))
    row = c.fetchone()
    conn.close()
    return row is not None

def insert_article(
    title_en, title_ko, summary_en, summary_ko,
    url, source, category, subcategory, region, country, country_flag, score
) -> int:
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO articles (
                title_en, title_ko, summary_en, summary_ko,
                url, source, category, subcategory, region, country, country_flag, score
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            title_en, title_ko, summary_en, summary_ko,
            url, source, category, subcategory, region, country, country_flag, score
        ))
        row = c.fetchone()
        conn.commit()
        return row["id"]
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return -1
    finally:
        conn.close()

def mark_sent_telegram(article_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE articles SET sent_telegram = 1 WHERE id = %s", (article_id,))
    conn.commit()
    conn.close()

def mark_posted_blog(article_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE articles SET posted_blog = 1 WHERE id = %s", (article_id,))
    conn.commit()
    conn.close()

def get_top_articles(date: str = None, limit: int = 10, region: str = None) -> list:
    conn = get_conn()
    c = conn.cursor()

    if date is None:
        date = time.strftime("%Y-%m-%d")

    query = """
        SELECT * FROM articles
        WHERE sent_telegram = 1
        AND created_at LIKE %s
    """
    params = [f"{date}%"]

    if region:
        query += " AND region = %s"
        params.append(region)

    query += " ORDER BY score DESC LIMIT %s"
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
        WHERE country = %s AND sent_telegram = 1
        ORDER BY created_at DESC LIMIT %s
    """, (country, limit))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_articles_by_region(region: str, limit: int = 10) -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM articles
        WHERE region = %s AND sent_telegram = 1
        ORDER BY created_at DESC LIMIT %s
    """, (region, limit))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_unposted_articles(limit: int = 10) -> list:
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM articles
        WHERE sent_telegram = 1 AND posted_blog = 0
        ORDER BY score DESC LIMIT %s
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

if __name__ == "__main__":
    init_db()
