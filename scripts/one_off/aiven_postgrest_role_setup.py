"""일회성: migrations/aiven_add_postgrest_role.sql을 Aiven에 적용한다.
저장소가 public이라 민감정보(비밀번호·토큰)는 절대 로그에 찍지 않는다 —
이 역할은 NOLOGIN이라 찍을 비밀번호 자체가 없다.
실행: .github/workflows/one_off_task.yml 수동 dispatch,
      script=one_off/aiven_postgrest_role_setup.py
"""
import os
import sys

import psycopg2

AIVEN_SERVICE_URI = os.getenv("AIVEN_SERVICE_URI", "")
SQL_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "migrations", "aiven_add_postgrest_role.sql")


def main():
    if not AIVEN_SERVICE_URI:
        print("❌ AIVEN_SERVICE_URI 미설정 — 중단")
        sys.exit(1)

    with open(SQL_PATH, encoding="utf-8") as f:
        sql = f.read()

    conn = psycopg2.connect(AIVEN_SERVICE_URI, connect_timeout=10)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "SELECT grantee, table_name, privilege_type FROM information_schema.table_privileges "
            "WHERE grantee = 'postgrest_svc' ORDER BY table_name, privilege_type"
        )
        rows = cur.fetchall()
    conn.close()

    print("✅ postgrest_svc 역할/권한 적용 완료")
    for grantee, table, priv in rows:
        print(f"  - {table}: {priv}")


if __name__ == "__main__":
    main()
