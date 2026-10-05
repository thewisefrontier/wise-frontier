# -*- coding: utf-8 -*-
"""scripts/sync_config_to_d1.py
--------------------------------
`prompts`·`rss_sources`는 Supabase가 원본(도구 admin.html의 CRUD, feed_discovery.py,
rss_source_discovery.py, db.py의 소스 상태 갱신이 전부 Supabase에 씀)이고, D1은
config_store.py가 읽는 캐시다. 이 스크립트가 30분마다(run.yml collectors job) Supabase→D1
동기화를 시도한다.

CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID/NEWSFINAL_CONFIG_D1_ID 셋 중 하나라도
없으면 조용히 스킵한다(시크릿 등록 전까지는 read-path도 Supabase만 쓰므로 동기화가
없어도 무해하다).

2026-10-05: rss_sources가 D1 무료 쓰기 한도(10만행/일)의 87%를 하루에 소진하는
사고가 났다. 원인은 (a) config_store.load_rss_sources()가 실제로 읽는 건
name/category/subcategory/url/is_active뿐인데 last_checked_at 등 RSS 수집기가
매 사이클 갱신하는 헬스 컬럼까지 동기화해 "거의 매번 뭔가 바뀜"이 되어 있었고,
(b) 바뀐 게 없어도 매번 DELETE+INSERT 전체 재작성이라 쓰기행이 테이블 크기만큼
고정 소모됐던 것. 헬스 컬럼을 동기화 대상에서 빼고(아래 RSS_COLS), 신규
fetch의 서명이 바뀌었을 때만 쓰도록 고쳤다 — 이제 실제로 소스가
추가/수정/비활성화될 때만 쓰기가 발생한다.

서명 저장은 "변경 없음"을 확인하려고 매번 본 테이블(rss_sources 전체)을
다시 읽으면 이번엔 읽기 쿼터를 불필요하게 태운다(1450행×48회/일, 한도
500만행 대비는 작지만 공짜로 없앨 수 있는 낭비). 그래서 `sync_meta`라는
1행짜리 서명 캐시 테이블을 따로 두고, 거기 1행만 읽어 비교한다.

실행: python scripts/sync_config_to_d1.py"""
import hashlib
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


def _d1_query(sql: str) -> list:
    res = requests.post(
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{D1_ID}/query",
        headers={"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"},
        json={"sql": sql}, timeout=30,
    )
    data = res.json()
    if not data.get("success"):
        print(f"[sync_config_to_d1] 조회 실패: {data.get('errors')}")
        return []
    return data["result"][0]["results"]


def _signature(rows: list, cols: list) -> str:
    # _esc()로 양쪽(Supabase JSON / D1 응답)의 타입 표기 차이(True vs 1 등)를
    # 똑같은 문자열로 맞춰야 "변경 없음" 비교가 성립한다.
    body = "\n".join(",".join(_esc(r.get(c)) for c in cols) for r in rows)
    return hashlib.sha256(body.encode()).hexdigest()


PROMPTS_COLS = ["id", "name", "content", "version", "is_active", "created_at"]
# config_store.load_rss_sources()가 실제로 select하는 건 name/category/subcategory/url뿐
# (+ WHERE is_active=1) — consecutive_fails/total_ok/total_fail/last_ok_at/last_checked_at/
# deactivated_reason은 D1에서 아무도 안 읽는다. RSS 수집기가 매 사이클 건드리는 컬럼이라
# 동기화 대상에 넣으면 "거의 매번 바뀜"이 되어 서명 비교가 무력화된다.
RSS_COLS = ["id", "name", "category", "subcategory", "url", "is_active"]


def _get_meta_sig(table: str) -> str:
    rows = _d1_query(f"SELECT signature FROM sync_meta WHERE table_name = {_esc(table)}")
    return rows[0]["signature"] if rows else ""


def _set_meta_sig(table: str, sig: str) -> None:
    _d1_exec(
        f"INSERT INTO sync_meta (table_name, signature) VALUES ({_esc(table)}, {_esc(sig)}) "
        "ON CONFLICT(table_name) DO UPDATE SET signature = excluded.signature;"
    )


def _sync_table(table: str, cols: list) -> int:
    rows = _fetch_all(table, ",".join(cols))
    if not rows:
        return 0
    new_sig = _signature(rows, cols)
    if _get_meta_sig(table) == new_sig:
        return 0  # 변경 없음 — sync_meta 1행만 읽고 본 테이블은 안 건드림
    stmts = [f"DELETE FROM {table};"]
    for r in rows:
        vals = ",".join(_esc(r.get(c)) for c in cols)
        stmts.append(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({vals});")
    ok = _d1_exec(" ".join(stmts))
    if ok:
        _set_meta_sig(table, new_sig)
    return len(rows) if ok else 0


def main():
    if not (CF_TOKEN and CF_ACCOUNT and D1_ID):
        print("[sync_config_to_d1] Cloudflare D1 시크릿 미설정 — 스킵")
        return
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        print("[sync_config_to_d1] Supabase 환경변수 없음 — 스킵")
        return
    _d1_exec("CREATE TABLE IF NOT EXISTS sync_meta (table_name TEXT PRIMARY KEY, signature TEXT);")
    n1 = _sync_table("prompts", PROMPTS_COLS)
    n2 = _sync_table("rss_sources", RSS_COLS)
    print(f"[sync_config_to_d1] prompts {n1}건, rss_sources {n2}건 동기화")


if __name__ == "__main__":
    main()
