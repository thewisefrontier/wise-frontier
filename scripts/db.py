"""
db.py — Supabase REST API 버전
기존 SQLite 인터페이스를 그대로 유지하면서 내부를 Supabase REST API로 교체
IPv6 문제 없이 HTTP로 동작
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _url(table="articles"):
    return f"{SUPABASE_URL}/rest/v1/{table}"


def init_db():
    """하위 호환용 — Supabase 전환 완료로 실제 동작 없음"""
    pass



def is_url_exists(url: str) -> bool:
    res = requests.get(
        _url(),
        headers=_headers(),
        params={"select": "id", "url": f"eq.{url}", "limit": "1"},
        timeout=10
    )
    if res.status_code in (200, 206):
        return len(res.json()) > 0
    return False


def existing_urls(urls: list, chunk: int = 50) -> set:
    """urls 중 articles에 이미 있는 것만 집합으로 반환 — 항목마다 is_url_exists()를
    따로 부르면(왕복 1회씩) 소스당 최대 5개, 1200+소스에서 실측 초당 1건 수준으로
    병목이 됐다(2026-09-22, rss_collector.py 최초 실전 시험: 13분에 689건). 대신 배치
    전체를 IN 쿼리로 묶되, GET 쿼리스트링 길이 상한(게이트웨이 통상 8~16KB)을 피하려
    chunk개씩 나눠 보낸다."""
    found = set()
    for i in range(0, len(urls), chunk):
        part = urls[i:i + chunk]
        quoted = ",".join(f'"{u}"' for u in part)
        res = requests.get(
            _url(), headers=_headers(),
            params={"select": "url", "url": f"in.({quoted})"}, timeout=10,
        )
        if res.status_code in (200, 206):
            found.update(r["url"] for r in res.json())
    return found


def insert_article(
    title_en, title_ko, summary_en, summary_ko,
    url, source, category, subcategory, region, country, country_flag, score,
    full_text="", countries=None, is_published=False, source_published_at=None,
    source_data=None, image_url=None, image_credit=None,
) -> int:
    payload = {
        "title_en": title_en or "",
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": summary_ko,
        "url": url,
        "source": source,
        "category": category,
        "subcategory": subcategory,
        "region": region,
        "country": country,
        "country_flag": country_flag,
        "score": score,
        "full_text": full_text or None,
        "countries": ([country] + [c for c in (countries or []) if c and c != country]) if country else (countries or None),
        "is_published": is_published,
        "created_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        "sent_telegram": 0,
        "posted_blog": 0,
    }
    # 원문(RSS) 발행일 — 값이 있을 때만 실어 기존 동작에 영향 없게 한다
    if source_published_at:
        payload["source_published_at"] = source_published_at
    # RSS 원문 태그(<category> 등) — 트렌드 중복판정에서 country 대신/보조로
    # 쓸 수 있게 원문 그대로 보존한다(2026-09-08, gemini_summarizer.py 참고).
    if source_data:
        payload["source_data"] = source_data
    # 대표 이미지 — 수집 단계에서 이미 이미지를 구한 수집기(domestic_kr_fetcher.py
    # 등)만 채운다. 값이 없을 때만 실어 기존 동작(발행 단계에서 gemini_writer.py가
    # 채우는 흐름)에 영향 없게 한다.
    if image_url:
        payload["image_url"] = image_url
    if image_credit:
        payload["image_credit"] = image_credit
    headers = {**_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        if data:
            return data[0].get("id", -1)
    elif res.status_code == 409:
        return -1  # 중복
    return -1


def mark_sent_telegram(article_id: int):
    res = requests.patch(
        f"{_url()}?id=eq.{article_id}",
        headers=_headers(),
        json={"sent_telegram": 1},
        timeout=10
    )
    return res.status_code in (200, 204)


def mark_posted_blog(article_id: int):
    res = requests.patch(
        f"{_url()}?id=eq.{article_id}",
        headers=_headers(),
        json={"posted_blog": 1},
        timeout=10
    )
    return res.status_code in (200, 204)


def get_top_articles(date: str = None, limit: int = 10, region: str = None) -> list:
    if date is None:
        date = now_kst().strftime("%Y-%m-%d")
    params = {
        "select": "*",
        "sent_telegram": "eq.1",
        "created_at": f"like.{date}%",
        "order": "score.desc",
        "limit": str(limit),
    }
    if region:
        params["region"] = f"eq.{region}"
    res = requests.get(_url(), headers=_headers(), params=params, timeout=15)
    if res.status_code in (200, 206):
        return res.json()
    return []


def get_articles_by_country(country: str, limit: int = 5) -> list:
    res = requests.get(
        _url(),
        headers=_headers(),
        params={
            "select": "*",
            "country": f"eq.{country}",
            "sent_telegram": "eq.1",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


def get_articles_by_region(region: str, limit: int = 10) -> list:
    res = requests.get(
        _url(),
        headers=_headers(),
        params={
            "select": "*",
            "region": f"eq.{region}",
            "sent_telegram": "eq.1",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


def get_unposted_articles(limit: int = 10) -> list:
    res = requests.get(
        _url(),
        headers=_headers(),
        params={
            "select": "*",
            "sent_telegram": "eq.1",
            "posted_blog": "eq.0",
            "order": "score.desc",
            "limit": str(limit),
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


# ── RSS 수집/처리 분리 큐 (2026-09-22) ──────────────────────────────
# rss_collector.py가 채우고 rss_processor.py가 비운다. articles 테이블과 완전히
# 별개라 다른 writer의 소비 로직에 영향이 없다(newsfinal_db_backup_architecture와
# 같은 원칙: 새 테이블은 GRANT도 RLS와 별도로 확인 — 이미 반영됨, apply_migration
# rss_raw_queue_grants 참고).

def queue_link_exists(link: str) -> bool:
    res = requests.get(
        _url("rss_raw_queue"), headers=_headers(),
        params={"select": "id", "link": f"eq.{link}", "limit": "1"}, timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def queue_insert(link, title, source_name, category, subcategory,
                  summary_en="", source_published_at=None, raw_tags=None) -> int:
    payload = {
        "link": link, "title": title, "source_name": source_name,
        "category": category, "subcategory": subcategory, "summary_en": summary_en or "",
    }
    if source_published_at:
        payload["source_published_at"] = source_published_at
    if raw_tags:
        payload["raw_tags"] = raw_tags
    headers = {**_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_url("rss_raw_queue"), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0]["id"] if data else -1
    return -1


def queue_claim_batch(limit: int = 150) -> list:
    """처리 대기 중(processed=false)인 항목을 오래된 순으로 가져온다.

    ⚠️ 여러 처리기 실행이 동시에 돌면 같은 행을 중복으로 집어갈 수 있다 — 하지만
    run.yml이 concurrency 그룹(cancel-in-progress: false)으로 겹침 자체를 막고 있어
    (newsfinal-auto-run) 지금 구조에서는 발생하지 않는다. 나중에 별도 워크플로에서
    이 큐를 처리하게 되면 SELECT ... FOR UPDATE SKIP LOCKED 같은 잠금이 필요해진다."""
    res = requests.get(
        _url("rss_raw_queue"), headers=_headers(),
        params={"select": "*", "processed": "eq.false", "order": "fetched_at.asc", "limit": str(limit)},
        timeout=20,
    )
    return res.json() if res.status_code in (200, 206) else []


def queue_delete(row_id: int):
    requests.delete(f'{_url("rss_raw_queue")}?id=eq.{row_id}', headers=_headers(), timeout=10)


def queue_mark_failed(row_id: int, error: str, max_attempts: int = 3):
    """실패 시 attempts를 늘리고, 상한을 넘으면 영구 실패로 보고 큐에서 지운다
    (깨진 링크 하나가 매 실행 재시도되며 배치를 계속 잡아먹는 걸 방지)."""
    res = requests.get(_url("rss_raw_queue"), headers=_headers(),
                        params={"select": "attempts", "id": f"eq.{row_id}"}, timeout=10)
    attempts = (res.json()[0]["attempts"] + 1) if res.status_code in (200, 206) and res.json() else 1
    if attempts >= max_attempts:
        queue_delete(row_id)
        return
    requests.patch(f'{_url("rss_raw_queue")}?id=eq.{row_id}', headers=_headers(),
                    json={"attempts": attempts, "last_error": str(error)[:500]}, timeout=10)


if __name__ == "__main__":
    init_db()
