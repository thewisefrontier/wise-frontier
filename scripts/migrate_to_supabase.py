"""
migrate_to_supabase.py
----------------------
기존 SQLite(articles.db)의 데이터를 Supabase REST API로 마이그레이션
psycopg2 직접 연결 대신 HTTP API 사용 (IPv6 문제 우회)
"""

import os
import sqlite3
import requests
import time
import sys
from dotenv import load_dotenv

load_dotenv()

SQLITE_FILE = "data/articles.db"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

BATCH_SIZE = 50


def get_sqlite_articles():
    conn = sqlite3.connect(SQLITE_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM articles ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_existing_urls():
    existing = set()
    offset = 0
    while True:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/articles",
            headers={**HEADERS, "Range": f"{offset}-{offset+999}"},
            params={"select": "url"},
            timeout=30
        )
        if res.status_code not in (200, 206):
            print(f"  [경고] URL 조회 실패: {res.status_code} {res.text[:100]}")
            break
        data = res.json()
        if not data:
            break
        for row in data:
            existing.add(row["url"])
        if len(data) < 1000:
            break
        offset += 1000
    return existing


def insert_batch(batch):
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/articles",
        headers={**HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"},
        json=batch,
        timeout=30
    )
    if res.status_code not in (200, 201, 204):
        print(f"  [ERROR] {res.status_code}: {res.text[:200]}")
        return False
    return True


def migrate():
    if not os.path.exists(SQLITE_FILE):
        print(f"[ERROR] SQLite 파일 없음: {SQLITE_FILE}")
        return

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[ERROR] SUPABASE_URL 또는 SUPABASE_SERVICE_KEY 환경변수 없음")
        return

    print(f"[Supabase] URL: {SUPABASE_URL}")

    # 연결 테스트
    test = requests.get(
        f"{SUPABASE_URL}/rest/v1/articles",
        headers={**HEADERS, "Range": "0-0"},
        params={"select": "id"},
        timeout=10
    )
    if test.status_code == 404:
        print("""
[ERROR] articles 테이블이 없습니다!
Supabase 대시보드 → SQL Editor → New query → 아래 SQL 실행 후 다시 실행하세요:

CREATE TABLE IF NOT EXISTS articles (
    id            BIGSERIAL PRIMARY KEY,
    title_en      TEXT NOT NULL DEFAULT '',
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
);
        """)
        return
    elif test.status_code not in (200, 206):
        print(f"[ERROR] Supabase 연결 실패: {test.status_code} {test.text[:200]}")
        return

    print("✅ Supabase 연결 확인")

    rows = get_sqlite_articles()
    print(f"[SQLite] 총 {len(rows)}건 기사 발견")

    existing_urls = get_existing_urls()
    print(f"[Supabase] 기존 {len(existing_urls)}건 존재")

    new_rows = [r for r in rows if r.get("url") not in existing_urls]
    print(f"[마이그레이션] 신규 {len(new_rows)}건 삽입 예정")

    if not new_rows:
        print("✅ 이미 모두 마이그레이션됨")
        return

    success = 0
    error = 0

    for i in range(0, len(new_rows), BATCH_SIZE):
        batch = new_rows[i:i + BATCH_SIZE]
        batch_clean = [{k: v for k, v in r.items() if k != "id"} for r in batch]

        ok = insert_batch(batch_clean)
        if ok:
            success += len(batch)
            print(f"  → {success}/{len(new_rows)}건 완료...")
        else:
            error += len(batch)

        time.sleep(0.3)

    print(f"\n✅ 마이그레이션 완료 — 성공: {success}건 / 오류: {error}건")


if __name__ == "__main__":
    migrate()
