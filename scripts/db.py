"""
db.py — Supabase REST API 버전
기존 SQLite 인터페이스를 그대로 유지하면서 내부를 Supabase REST API로 교체
IPv6 문제 없이 HTTP로 동작
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

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
    """Supabase는 대시보드에서 테이블 생성 — 여기서는 연결 확인만"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[WARNING] SUPABASE_URL 또는 SUPABASE_SERVICE_KEY 없음 — SQLite 폴백")
        return
    res = requests.get(_url(), headers=_headers(), params={"select": "id", "limit": "1"}, timeout=10)
    if res.status_code in (200, 206):
        print("✅ Supabase DB 연결 확인")
    else:
        print(f"[WARNING] Supabase 연결 실패: {res.status_code}")


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


def insert_article(
    title_en, title_ko, summary_en, summary_ko,
    url, source, category, subcategory, region, country, country_flag, score,
    full_text="", countries=None
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
        "countries": countries or ([country] if country else None),
        "created_at": time.strftime("%Y-%m-%d %H:%M"),
        "sent_telegram": 0,
        "posted_blog": 0,
    }
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
        date = time.strftime("%Y-%m-%d")
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


if __name__ == "__main__":
    init_db()
