# -*- coding: utf-8 -*-
"""sync_config_to_d1.py의 _esc()/_sync_table() SQL 조립이 안전한지 검증하는 회귀 테스트
(2026-11-17 보안 점검 후속).

배경: D1 HTTP API는 (a) 한 sql에 여러 statement를 넣으면 params 바인딩을 거부하고
(code 7400, "params with multiple statements is not supported"), (b) query 엔드포인트는
배치용 배열 바디도 받지 않는다(실제 테스트 DB에 붙여 확인함) — 그래서 "DELETE 전체 +
INSERT 전체"를 한 번의 원자적 HTTP 요청으로 보내려면 ?바인딩 대신 _esc()로 직접 문자열을
조립하는 지금 방식이 불가피하다. 대신 (1) table/컬럼명은 항상 하드코딩 상수이고(외부 입력
아님) (2) 값(cell)만 Supabase에서 오는데 _esc()가 SQLite 표준 방식(따옴표를 ''로 두 배)으로
이스케이프하므로 주입이 안 되는지를 여기서 고정해 둔다. sqlite3는 D1과 같은 SQLite 문법이라
executescript()로 실제 D1에 보내는 것과 동일한 멀티스테이트먼트 문자열을 그대로 실행해
검증할 수 있다.

실행: python scripts/test_sync_config_to_d1.py
"""
import os
import sqlite3
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("SUPABASE_URL", "http://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")
os.environ.setdefault("CLOUDFLARE_API_TOKEN", "test")
os.environ.setdefault("CLOUDFLARE_ACCOUNT_ID", "test")
os.environ.setdefault("NEWSFINAL_CONFIG_D1_ID", "test")

import sync_config_to_d1 as mod  # noqa: E402

FAILS = []


def check(name, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        FAILS.append(name)


def build_stmts(table, cols, rows):
    """_sync_table()과 동일한 조립 로직(네트워크 호출 부분만 제외)."""
    stmts = [f"DELETE FROM {table};"]
    for r in rows:
        vals = ",".join(mod._esc(r.get(c)) for c in cols)
        stmts.append(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({vals});")
    return " ".join(stmts)


def run_against_sqlite(table, cols, coltypes, rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {table} ({','.join(f'{c} {t}' for c, t in zip(cols, coltypes))})")
    sql = build_stmts(table, cols, rows)
    conn.executescript(sql)  # D1 HTTP API에 그대로 보내는 멀티스테이트먼트 문자열과 동일
    out = conn.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY rowid").fetchall()
    conn.close()
    return out


def main():
    # 1) 정상 값(한글 포함)이 그대로 왕복되는지
    cols, coltypes = ["id", "name", "active"], ["INTEGER", "TEXT", "INTEGER"]
    rows = [{"id": 1, "name": "속보: 테스트", "active": True},
            {"id": 2, "name": None, "active": False}]
    out = run_against_sqlite("t1", cols, coltypes, rows)
    check("정상 값 왕복(한글/None/bool)", out == [(1, "속보: 테스트", 1), (2, None, 0)])

    # 2) 값 안의 홑따옴표가 문자열 리터럴을 탈출하지 못하는지
    evil = "O'Brien"
    out = run_against_sqlite("t2", ["id", "name"], ["INTEGER", "TEXT"],
                              [{"id": 1, "name": evil}])
    check("홑따옴표가 포함된 값이 안전하게 저장됨", out == [(1, evil)])

    # 3) 전형적 SQLi 페이로드를 "값"으로 넣었을 때 별도 statement로 실행되지 않는지
    payload = "x'); DROP TABLE t3; --"
    out = run_against_sqlite("t3", ["id", "name"], ["INTEGER", "TEXT"],
                              [{"id": 1, "name": payload}])
    check("SQLi 페이로드가 값으로만 저장되고 DROP이 실행되지 않음",
          out == [(1, payload)])

    # 4) DELETE+INSERT 전체 재작성이 기존 행을 지우고 새 행으로만 남기는지
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t4 (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO t4 VALUES (99, 'old')")
    sql = build_stmts("t4", ["id", "name"], [{"id": 1, "name": "new"}])
    conn.executescript(sql)
    out = conn.execute("SELECT id, name FROM t4").fetchall()
    conn.close()
    check("DELETE+INSERT 재작성 후 기존 행이 남지 않음", out == [(1, "new")])

    # 5) _esc() 자체의 기본 동작
    check("_esc(None) == 'NULL'", mod._esc(None) == "NULL")
    check("_esc(True) == '1'", mod._esc(True) == "1")
    check("_esc(False) == '0'", mod._esc(False) == "0")
    check("_esc(3) == '3'", mod._esc(3) == "3")
    check("_esc(\"a'b\") 이스케이프", mod._esc("a'b") == "'a''b'")

    if FAILS:
        print(f"\n{len(FAILS)}건 실패:", FAILS)
        sys.exit(1)
    print("\n전체 통과")


if __name__ == "__main__":
    main()
