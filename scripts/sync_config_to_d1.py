# -*- coding: utf-8 -*-
"""scripts/sync_config_to_d1.py
--------------------------------
`prompts`·`rss_sources`는 Supabase가 원본(도구 admin.html의 CRUD, feed_discovery.py,
rss_source_discovery.py, db.py의 소스 상태 갱신이 전부 Supabase에 씀)이고, D1은
config_store.py가 읽는 캐시다. 이 스크립트가 주기적으로 Supabase→D1 전량 동기화한다
(두 테이블 합쳐 1500행 안팎이라 증분 대신 매번 통째로 교체 — 단순함이 이득).

CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID/NEWSFINAL_CONFIG_D1_ID 셋 중 하나라도
없으면 조용히 스킵한다(시크릿 등록 전까지는 read-path도 Supabase만 쓰므로 동기화가
없어도 무해하다).

실행: python scripts/sync_config_to_d1.py"""
import os

from dotenv import load_dotenv

load_dotenv()
from http_retry import get_session  # noqa: E402

requests = get_session()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
CF_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
CF_ACCOUNT = os.getenv("CLOUDFLARE_ACCOUNT_ID")
D1_ID = os.getenv("NEWSFINAL_CONFIG_D1_ID")


def _fetch_all(table: str, select: str) -> list:
    rows, offset, page = [], 0, 1000
    headers = {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"}
    while True:
        res = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=headers,
                            params={"select": select, "order": "id.asc", "limit": page, "offset": offset},
                            timeout=30)
        res.raise_for_status()
        batch = res.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def _esc(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _d1_exec(sql: str) -> bool:
    res = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_ID}/query",
        headers={"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"},
        json={"sql": sql}, timeout=30,
    )
    data = res.json()
    if not data.get("success"):
        print(f"[sync_config_to_d1] 실패: {data.get('errors')}")
        return False
    return True


PROMPTS_COLS = ["id", "name", "content", "version", "is_active", "created_at"]
RSS_COLS = ["id", "name", "category", "subcategory", "url", "is_active", "created_at",
            "consecutive_fails", "total_ok", "total_fail", "last_ok_at", "last_checked_at",
            "deactivated_reason"]


def _sync_table(table: str, cols: list) -> int:
    rows = _fetch_all(table, ",".join(cols))
    if not rows:
        return 0
    stmts = [f"DELETE FROM {table};"]
    for r in rows:
        vals = ",".join(_esc(r.get(c)) for c in cols)
        stmts.append(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({vals});")
    ok = _d1_exec(" ".join(stmts))
    return len(rows) if ok else 0


def main():
    if not (CF_TOKEN and CF_ACCOUNT and D1_ID):
        print("[sync_config_to_d1] Cloudflare D1 시크릿 미설정 — 스킵")
        return
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        print("[sync_config_to_d1] Supabase 환경변수 없음 — 스킵")
        return
    n1 = _sync_table("prompts", PROMPTS_COLS)
    n2 = _sync_table("rss_sources", RSS_COLS)
    print(f"[sync_config_to_d1] prompts {n1}건, rss_sources {n2}건 동기화")


if __name__ == "__main__":
    main()
