"""
scripts/one_off/verify_rest_backend.py
--------------------------------------
2026-10-20 Supabase → Aiven 전환 직후 점검: 스크립트들이 쓰는 REST 경로({SUPABASE_URL}/rest/v1)가
조회·삽입(충돌 무시)·수정·삭제·RPC까지 Supabase 때와 같은 모양으로 동작하는지 확인한다.
settings 테이블에 임시 키 하나를 썼다 지운다(다른 데이터는 건드리지 않음). 로그엔 상태코드만.
"""

import os
import sys

import requests

BASE = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
KEY = os.environ["SUPABASE_SERVICE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
TEST_KEY = "_rest_backend_smoke_test"

checks = []


def check(name, ok, detail=""):
    checks.append(ok)
    print(f"{'OK ' if ok else 'FAIL'} {name} {detail}")


r = requests.get(f"{BASE}/articles", headers=H, params={"select": "id,created_at", "source": "eq.NewsFinal",
                 "is_published": "eq.true", "order": "created_at.desc", "limit": "1"}, timeout=30)
check("GET articles", r.ok and len(r.json()) == 1, f"{r.status_code}")

r = requests.get(f"{BASE}/articles", headers={**H, "Prefer": "count=exact", "Range": "0-0"},
                 params={"select": "id", "is_published": "eq.true"}, timeout=30)
check("count=exact", r.ok and "/" in r.headers.get("content-range", ""), r.headers.get("content-range", ""))

r = requests.post(f"{BASE}/settings", headers={**H, "Prefer": "resolution=ignore-duplicates,return=representation"},
                  json={"key": TEST_KEY, "value": "1"}, timeout=30)
check("POST ignore-duplicates", r.status_code in (200, 201), f"{r.status_code}")

r = requests.patch(f"{BASE}/settings", headers={**H, "Prefer": "return=minimal"},
                   params={"key": f"eq.{TEST_KEY}"}, json={"value": "2"}, timeout=30)
check("PATCH", r.status_code in (200, 204), f"{r.status_code}")

r = requests.get(f"{BASE}/settings", headers=H, params={"key": f"eq.{TEST_KEY}", "select": "value"}, timeout=30)
check("PATCH 반영", r.ok and r.json() == [{"value": "2"}], f"{r.status_code}")

r = requests.delete(f"{BASE}/settings", headers=H, params={"key": f"eq.{TEST_KEY}"}, timeout=30)
check("DELETE", r.status_code in (200, 204), f"{r.status_code}")

r = requests.post(f"{BASE}/rpc/find_duplicate_title", headers={**H, "Prefer": "return=representation"},
                  json={"p_title": "프랑스 시위", "p_hours": 720, "p_threshold": 0.3}, timeout=30)
check("RPC find_duplicate_title", r.ok and isinstance(r.json(), list), f"{r.status_code}")

r = requests.post(f"{BASE}/rpc/claim_gemini_key", headers=H,
                  json={"p_model": "_smoke", "p_key_index": 0, "p_min_interval": 0}, timeout=30)
check("RPC claim_gemini_key", r.ok, f"{r.status_code}")
requests.delete(f"{BASE}/gemini_key_pacing", headers=H, params={"model": "eq._smoke"}, timeout=30)

print(f"{sum(checks)}/{len(checks)} 통과")
sys.exit(0 if all(checks) else 1)
