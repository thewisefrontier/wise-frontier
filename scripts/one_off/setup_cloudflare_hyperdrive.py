"""
scripts/one_off/setup_cloudflare_hyperdrive.py  (로컬 PC에서 1회 실행)
------------------------------------------------------------------
2026-10-20 Supabase 이탈 — 사이트(Cloudflare Pages Functions)가 Aiven을 Hyperdrive로 직접 조회하도록
① Hyperdrive 설정 생성(있으면 재사용) ② Pages 프로젝트 production·preview에 HYPERDRIVE 바인딩과
nodejs_compat 플래그(postgres.js가 필요로 함)를 붙인다. 기존 프로젝트 설정은 읽어서 병합한다.

필요 .env: AIVEN_SERVICE_URI, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_DEPLOY_TOKEN(Hyperdrive Edit + Pages Edit)
출력엔 비밀값을 찍지 않는다.
"""

import os
import sys
from urllib.parse import urlparse, unquote

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ACCT = os.environ["CLOUDFLARE_ACCOUNT_ID"]
H = {"Authorization": f"Bearer {os.environ['CLOUDFLARE_DEPLOY_TOKEN']}", "Content-Type": "application/json"}
API = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
NAME = "newsfinal-aiven"
SITE_DOMAIN = "newsfinal.co.kr"
ORIGIN_CONNECTIONS = 5  # Aiven 무료 max_connections 20 — 파이프라인 PostgREST와 나눠 쓴다
MIN_COMPAT_DATE = "2024-09-23"  # nodejs_compat v2(현행 Node API) 기준일


def call(method, path, **kw):
    r = requests.request(method, API + path, headers=H, timeout=30, **kw)
    j = r.json()
    if not j.get("success"):
        raise SystemExit(f"{method} {path} 실패: {r.status_code} {[e.get('message') for e in j.get('errors', [])]}")
    return j["result"]


def hyperdrive_id():
    for cfg in call("GET", "/hyperdrive/configs"):
        if cfg["name"] == NAME:
            print(f"Hyperdrive 재사용: {NAME}")
            return cfg["id"]
    u = urlparse(os.environ["AIVEN_SERVICE_URI"])
    cfg = call("POST", "/hyperdrive/configs", json={
        "name": NAME,
        "origin": {"scheme": "postgres", "host": u.hostname, "port": u.port, "database": u.path.lstrip("/"),
                   "user": unquote(u.username), "password": unquote(u.password)},
        "origin_connection_limit": ORIGIN_CONNECTIONS,
    })
    print(f"Hyperdrive 생성: {NAME}")
    return cfg["id"]


def pages_project():
    projects = call("GET", "/pages/projects")
    for p in projects:
        if SITE_DOMAIN in (p.get("domains") or []):
            return p
    raise SystemExit(f"{SITE_DOMAIN} 도메인을 가진 Pages 프로젝트를 못 찾음: {[p['name'] for p in projects]}")


def main():
    hd = hyperdrive_id()
    proj = call("GET", f"/pages/projects/{pages_project()['name']}")
    name = proj["name"]
    dc = proj.get("deployment_configs") or {}
    patch = {}
    for env in ("production", "preview"):
        cur = dc.get(env) or {}
        flags = sorted(set(cur.get("compatibility_flags") or []) | {"nodejs_compat"})
        date = cur.get("compatibility_date") or ""
        patch[env] = {
            "compatibility_flags": flags,
            "compatibility_date": date if date >= MIN_COMPAT_DATE else "2026-10-01",
            "hyperdrive_bindings": {**(cur.get("hyperdrive_bindings") or {}), "HYPERDRIVE": {"id": hd}},
        }
        print(f"{env}: flags={flags} date {date or '(없음)'} → {patch[env]['compatibility_date']}")
    call("PATCH", f"/pages/projects/{name}", json={"deployment_configs": patch})
    print(f"Pages 프로젝트 '{name}' 설정 완료 — 다음 배포부터 적용(빌드: {proj.get('build_config')})")


if __name__ == "__main__":
    main()
