# -*- coding: utf-8 -*-
"""scripts/config_store.py
--------------------------
`prompts`(writer_rules 등)·`rss_sources` 조회를 Cloudflare D1로 먼저 시도하고,
D1이 설정 안 됐거나 실패하면 기존처럼 Supabase로 폴백한다(2026-09-30,
Supabase 이그레스 초과 대응 — [newsfinal_supabase_egress_quota_incident.md]).

이 두 테이블은 작고(76+1375행) 쓰기가 드물고 읽기가 잦아 D1 무료 한도
(행 수 기준, 이그레스 바이트 과금 없음)에 잘 맞는다. 쓰기(관리자 도구
docs/admin.html, feed_discovery.py, rss_source_discovery.py, 소스 상태
갱신)는 그대로 Supabase가 원본이고, sync_config_to_d1.py가 주기적으로
D1에 반영한다 — 이 파일은 읽기 전용이다.

CLOUDFLARE_API_TOKEN(D1 Edit 권한)·CLOUDFLARE_ACCOUNT_ID·
NEWSFINAL_CONFIG_D1_ID 세 환경변수가 모두 있어야 D1 경로를 쓴다. 없으면
(마이그레이션 전이거나 시크릿 미등록 상태) 기존 동작 그대로 Supabase만
쓴다 — 새 인프라가 없다고 파이프라인이 죽으면 안 된다."""
import json
import os

from http_retry import get_session

requests = get_session()

_prompt_cache: dict = {}
_rss_cache: list | None = None


def _d1_ready() -> bool:
    return bool(os.getenv("CLOUDFLARE_API_TOKEN") and os.getenv("CLOUDFLARE_ACCOUNT_ID")
                and os.getenv("NEWSFINAL_CONFIG_D1_ID"))


def _d1_query(sql: str, params: list | None = None) -> list | None:
    """D1 REST API 호출. 실패 시 None(호출부가 Supabase로 폴백)."""
    token = os.getenv("CLOUDFLARE_API_TOKEN")
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    db_id = os.getenv("NEWSFINAL_CONFIG_D1_ID")
    try:
        res = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{db_id}/query",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"sql": sql, "params": params or []},
            timeout=10,
        )
        data = res.json()
        if not data.get("success"):
            print(f"[config_store] D1 쿼리 실패: {data.get('errors')}")
            return None
        return data["result"][0]["results"]
    except Exception as e:
        print(f"[config_store] D1 호출 예외, Supabase로 폴백: {e}")
        return None


def _supabase_headers():
    return {"apikey": os.getenv("SUPABASE_SERVICE_KEY", ""),
            "Authorization": f"Bearer {os.getenv('SUPABASE_SERVICE_KEY', '')}"}


def load_prompt(name: str, fallback: str = "") -> str:
    """활성 프롬프트(최신 버전) 본문 로드. D1 우선, 실패 시 Supabase, 그다음 fallback."""
    global _prompt_cache
    if name in _prompt_cache:
        return _prompt_cache[name]

    if _d1_ready():
        rows = _d1_query(
            "SELECT content FROM prompts WHERE name = ? AND is_active = 1 ORDER BY version DESC LIMIT 1",
            [name],
        )
        if rows:
            _prompt_cache[name] = rows[0]["content"]
            return _prompt_cache[name]

    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    try:
        res = requests.get(
            f"{sb_url}/rest/v1/prompts", headers=_supabase_headers(),
            params={"name": f"eq.{name}", "is_active": "eq.true", "order": "version.desc", "limit": "1"},
            timeout=10,
        )
        if res.status_code in (200, 206):
            data = res.json()
            if data:
                _prompt_cache[name] = data[0]["content"]
                return _prompt_cache[name]
    except Exception as e:
        print(f"[config_store] 프롬프트 로드 실패({name}): {e}")

    _prompt_cache[name] = fallback
    return fallback


def load_photo_gate(name: str, fallback: dict) -> dict:
    """JSON 본문을 담은 프롬프트 행(예: domestic_photo_credit_gate) 로드 후 파싱.
    blocked_domains는 항상 set으로 맞춘다(호출부가 in 연산으로 씀)."""
    raw = load_prompt(name, "")
    cfg = None
    if raw:
        try:
            cfg = json.loads(raw)
        except Exception as e:
            print(f"[config_store] {name} JSON 파싱 실패, 폴백 사용: {e}")
    cfg = cfg if cfg is not None else dict(fallback)
    cfg["blocked_domains"] = set(cfg.get("blocked_domains", []))
    return cfg


def load_rss_sources(fields: str = "name,category,subcategory,url") -> list:
    """활성 RSS 소스 목록. D1 우선(전량 한 번에), 실패 시 Supabase(페이지네이션)."""
    global _rss_cache
    if _rss_cache is not None:
        return _rss_cache

    if _d1_ready():
        cols = ", ".join(fields.split(","))
        rows = _d1_query(f"SELECT {cols} FROM rss_sources WHERE is_active = 1")
        if rows:
            _rss_cache = rows
            print(f"✅ RSS 소스 {len(rows)}개 로드 (D1)")
            return rows

    sb_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    sources, offset, page = [], 0, 1000
    while True:
        res = requests.get(
            f"{sb_url}/rest/v1/rss_sources", headers=_supabase_headers(),
            params={"select": fields, "is_active": "eq.true",
                    "order": "id.asc", "limit": str(page), "offset": str(offset)},
            timeout=15,
        )
        res.raise_for_status()
        batch = res.json()
        sources.extend(batch)
        if len(batch) < page:
            break
        offset += page
    _rss_cache = sources
    print(f"✅ RSS 소스 {len(sources)}개 로드 (Supabase)")
    return sources
