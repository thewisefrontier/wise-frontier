"""
scripts/db_capacity_check.py
-------------------------------
Aiven 무료 플랜 한도 감시(2026-10-20 신설 — Supabase가 무료 이그레스 초과로 402
차단돼 사이트·파이프라인이 멈춘 뒤 Aiven으로 옮기면서, 같은 일이 예고 없이
반복되지 않게 함).

Aiven 무료 PostgreSQL(공식 문서 aiven.io/docs/products/postgresql/concepts/pg-free-tier,
2026-10-20 확인): 디스크 1GB, max_connections 20, 네트워크 전송량 과금 없음.
디스크가 차면 Aiven이 default_transaction_read_only=ON으로 바꿔 **모든 쓰기(DELETE
포함)가 막힌다**(aiven.io/docs/products/postgresql/howto/prevent-full-disk) — 그래서
차기 전에 알아야 한다.

pg_database_size는 WAL·시스템 파일을 빼고 잰다(WAL 크기는 무료 계정 권한으로 못 읽음).
실제 디스크 사용률은 이보다 높으니 임계값을 낮게 잡았다(경고 60%, 위험 75%).
정확한 디스크 %는 Aiven 콘솔 Metrics에서 확인.

경고 이상이면 관리자 텔레그램 알림, 위험이면 exit 1로 워크플로를 실패시켜 드러낸다.

실행: python scripts/db_capacity_check.py  (AIVEN_SERVICE_URI 필요)
"""

import os
import sys

import psycopg2
import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

DISK_LIMIT_MB = int(os.getenv("DB_DISK_LIMIT_MB", "1024"))
# 2026-11-08 실사고: DB 474MB일 때 콘솔 디스크 사용률이 이미 70%(약 717MB)였다 — WAL·시스템 파일이
# pg_database_size 밖에서 약 250MB를 상시 차지한다. DB 크기만 1GB에 견주던 탓에 실제 디스크는 85%를
# 넘어야 경고가 나는 구조였고, 그 상태에서 기사 4500행 일괄 UPDATE의 WAL 폭증(수백 MB)으로 100%에
# 닿아 읽기 전용이 됐다. 추정 디스크 = DB 크기 + 이 값으로 판정한다.
# ponytail: 고정 추정치 — Aiven API 토큰이 생기면 콘솔 Metrics의 실제 디스크 %로 바꿀 것.
DISK_OVERHEAD_MB = int(os.getenv("DB_DISK_OVERHEAD_MB", "250"))
WARN_RATIO, CRIT_RATIO = 0.70, 0.80
CONN_WARN_RATIO = 0.80
STUCK_TX_MINUTES = 10


def evaluate(size_mb: float, conns: int, conn_cap: int, stuck_tx: int) -> tuple:
    """(등급, 문제 목록) — 등급은 ok/warn/crit."""
    issues, level = [], "ok"
    ratio = (size_mb + DISK_OVERHEAD_MB) / DISK_LIMIT_MB
    if ratio >= CRIT_RATIO:
        level = "crit"
        issues.append(f"추정 디스크 {ratio:.0%} (DB {size_mb:.0f}MB + WAL 등 {DISK_OVERHEAD_MB}MB / {DISK_LIMIT_MB}MB) — 위험")
    elif ratio >= WARN_RATIO:
        level = "warn"
        issues.append(f"추정 디스크 {ratio:.0%} (DB {size_mb:.0f}MB + WAL 등 {DISK_OVERHEAD_MB}MB / {DISK_LIMIT_MB}MB) — 경고")
    if conns >= conn_cap * CONN_WARN_RATIO:
        level = "warn" if level == "ok" else level
        issues.append(f"연결 {conns}/{conn_cap}")
    if stuck_tx:
        level = "warn" if level == "ok" else level
        issues.append(f"{STUCK_TX_MINUTES}분 넘게 열린 트랜잭션(idle in transaction) {stuck_tx}개")
    return level, issues


def measure(cur) -> dict:
    cur.execute("select pg_database_size(current_database())")
    size_mb = cur.fetchone()[0] / 1024 / 1024
    cur.execute("select current_setting('max_connections')::int - current_setting('superuser_reserved_connections')::int")
    conn_cap = cur.fetchone()[0]
    cur.execute("select count(*) from pg_stat_activity where backend_type = 'client backend'")
    conns = cur.fetchone()[0]
    cur.execute(f"select count(*) from pg_stat_activity where state = 'idle in transaction'"
                f" and now() - state_change > interval '{STUCK_TX_MINUTES} minutes'")
    stuck_tx = cur.fetchone()[0]
    cur.execute("select relname, pg_total_relation_size(relid) / 1024 / 1024 from pg_stat_user_tables"
                " order by 2 desc limit 5")
    top = cur.fetchall()
    return dict(size_mb=size_mb, conns=conns, conn_cap=conn_cap, stuck_tx=stuck_tx, top=top)


def _alert(msg: str):
    token, chat = os.getenv("TELEGRAM_TOKEN", ""), os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
    if not token or not chat:
        print("[WARN] 텔레그램 설정 없음 — 알림 생략")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=15)
    except Exception as e:
        print(f"[WARN] 텔레그램 알림 실패: {e}")


BACKUP_MAX_AGE_DAYS = 8


def last_backup_age_days():
    """db-backup.yml에서 backup 작업이 실제로 성공한 마지막 실행이 며칠 전인지. 못 찾으면 None.
    2026-11-08: 요일 판정 오류로 6주간 매번 건너뛰었는데 실행은 초록색이라 아무도 몰랐다 — 실행 성공이 아니라
    backup 작업 성공을 본다. 공개 저장소라 토큰 없이도 읽히지만 있으면 쓴다."""
    from datetime import datetime, timezone
    repo = os.getenv("GITHUB_REPOSITORY", "thewisefrontier/wise-frontier")
    h = {"Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    api = f"https://api.github.com/repos/{repo}/actions"
    runs = requests.get(f"{api}/workflows/db-backup.yml/runs", headers=h, params={"status": "completed", "per_page": 15},
                        timeout=30).json().get("workflow_runs", [])
    for run in runs:
        jobs = requests.get(f"{api}/runs/{run['id']}/jobs", headers=h, timeout=30).json().get("jobs", [])
        if any(j["name"] == "backup" and j["conclusion"] == "success" for j in jobs):
            done = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - done).days
    return None


def main():
    try:
        age = last_backup_age_days()
        print(f"마지막 실제 DB 백업: {age}일 전" if age is not None else "마지막 실제 DB 백업: 최근 15회 중 없음")
        if age is None or age > BACKUP_MAX_AGE_DAYS:
            _alert(f"[뉴스파이널 DB 백업 경고] 마지막 실제 백업이 {age if age is not None else '15회 넘게'}일 전입니다"
                   f" — db-backup.yml 실행 로그(backup 작업) 확인 필요")
    except Exception as e:
        print(f"[WARN] 백업 점검 실패: {e}")

    conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=30)
    try:
        m = measure(conn.cursor())
    finally:
        conn.close()
    level, issues = evaluate(m["size_mb"], m["conns"], m["conn_cap"], m["stuck_tx"])
    top = ", ".join(f"{t} {mb}MB" for t, mb in m["top"])
    print(f"DB {m['size_mb']:.0f}MB/{DISK_LIMIT_MB}MB, 연결 {m['conns']}/{m['conn_cap']}, "
          f"멈춘 트랜잭션 {m['stuck_tx']} → {level}\n상위 테이블: {top}")
    if level != "ok":
        _alert(f"[뉴스파이널 Aiven 한도 {level.upper()}]\n" + "\n".join(issues)
               + f"\n상위 테이블: {top}\n(디스크가 차면 Aiven이 읽기 전용으로 전환 — 콘솔 Metrics 확인)")
    if level == "crit":
        sys.exit(1)


if __name__ == "__main__":
    main()
