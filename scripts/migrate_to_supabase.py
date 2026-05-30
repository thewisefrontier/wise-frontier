"""
migrate_to_supabase.py
----------------------
기존 SQLite(articles.db)의 데이터를 Supabase PostgreSQL로 마이그레이션

실행: python scripts/migrate_to_supabase.py
"""

import os
import sqlite3
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

SQLITE_FILE = "data/articles.db"
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_DB_PASSWORD = os.getenv("SUPABASE_DB_PASSWORD", "")


def get_sqlite_conn():
    conn = sqlite3.connect(SQLITE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_conn():
    parsed = urlparse(SUPABASE_URL)
    project_ref = parsed.hostname.split(".")[0]
    db_url = f"postgresql://postgres.{project_ref}:{SUPABASE_DB_PASSWORD}@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_pg_table(pg_conn):
    """Supabase에 테이블 생성"""
    c = pg_conn.cursor()
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
            created_at    TEXT,
            sent_telegram INTEGER DEFAULT 0,
            posted_blog   INTEGER DEFAULT 0
        )
    """)
    pg_conn.commit()
    print("✅ Supabase 테이블 생성 완료")


def migrate():
    if not os.path.exists(SQLITE_FILE):
        print(f"[ERROR] SQLite 파일 없음: {SQLITE_FILE}")
        return

    if not SUPABASE_URL or not SUPABASE_DB_PASSWORD:
        print("[ERROR] SUPABASE_URL 또는 SUPABASE_DB_PASSWORD 환경변수 없음")
        return

    # SQLite 연결
    sqlite_conn = get_sqlite_conn()
    sqlite_c = sqlite_conn.cursor()

    # 전체 기사 조회
    sqlite_c.execute("SELECT * FROM articles ORDER BY id ASC")
    rows = sqlite_c.fetchall()
    print(f"[SQLite] 총 {len(rows)}건 기사 발견")

    # PostgreSQL 연결
    pg_conn = get_pg_conn()
    init_pg_table(pg_conn)
    pg_c = pg_conn.cursor()

    # 현재 Supabase에 있는 URL 목록
    pg_c.execute("SELECT url FROM articles")
    existing_urls = {r["url"] for r in pg_c.fetchall()}
    print(f"[Supabase] 기존 {len(existing_urls)}건 존재")

    success = 0
    skip = 0
    error = 0

    for i, row in enumerate(rows):
        row = dict(row)
        url = row.get("url", "")

        # 이미 있으면 스킵
        if url in existing_urls:
            skip += 1
            continue

        try:
            pg_c.execute("""
                INSERT INTO articles (
                    title_en, title_ko, summary_en, summary_ko,
                    url, source, category, subcategory, region,
                    country, country_flag, score, created_at,
                    sent_telegram, posted_blog
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
            """, (
                row.get("title_en"), row.get("title_ko"),
                row.get("summary_en"), row.get("summary_ko"),
                url, row.get("source"), row.get("category"),
                row.get("subcategory"), row.get("region"),
                row.get("country"), row.get("country_flag"),
                row.get("score", 0), row.get("created_at"),
                row.get("sent_telegram", 0), row.get("posted_blog", 0)
            ))
            success += 1

            # 100건마다 커밋
            if success % 100 == 0:
                pg_conn.commit()
                print(f"  → {success}건 마이그레이션 완료...")

        except Exception as e:
            error += 1
            print(f"  [ERROR] id={row.get('id')}: {e}")
            pg_conn.rollback()

    pg_conn.commit()
    sqlite_conn.close()
    pg_conn.close()

    print(f"\n✅ 마이그레이션 완료")
    print(f"  성공: {success}건 / 스킵: {skip}건 / 오류: {error}건")


if __name__ == "__main__":
    migrate()
