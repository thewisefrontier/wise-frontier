"""
scripts/export_global.py
-------------------------
다국어(글로벌) 채널용 정적 JSON export. `article_translations` +
`articles`(DomesticKR 원본 + 국내유가 데이터저널리즘)를 언어별로 묶어
`docs/data/global/<lang>.json`을 생성한다.

2026-09-08 도입. 기존 `export_articles.py`(한국어 메인 사이트, 운영 중)는
건드리지 않는다 — 별도 파일로 리스크 분리.

`docs/article.html`이 쓰는 "?id=로 클라이언트에서 JSON fetch" 패턴을
그대로 따른다: docs/global/index.html + docs/global/article.html이
?lang=<code>로 이 JSON들을 불러와 렌더링한다.
"""

import os
import json
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

from translate_guard import LANG_NAMES

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

LANGUAGES = ["en", "hi", "fr", "es"]
OUT_DIR = "docs/data/global"
SITE_DIR = "docs/global"
KST = timezone(timedelta(hours=9))


def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _fetch_all(table: str, params: dict) -> list:
    all_rows = []
    offset, batch = 0, 1000
    while True:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**_headers(), "Range": f"{offset}-{offset+batch-1}"},
            params=params,
            timeout=30,
        )
        if res.status_code not in (200, 206):
            print(f"[EXPORT_GLOBAL] {table} 조회 오류: {res.status_code} — {res.text[:300]}")
            return all_rows
        data = res.json()
        if not data:
            break
        all_rows.extend(data)
        if len(data) < batch:
            break
        offset += batch
    return all_rows


def export_global():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[EXPORT_GLOBAL] Supabase 환경변수 없음 — 스킵")
        return

    os.makedirs(OUT_DIR, exist_ok=True)

    translations = _fetch_all("article_translations", {
        "select": "article_id,lang,title,summary,created_at",
        "order": "created_at.desc",
    })
    if not translations:
        print("[EXPORT_GLOBAL] 번역 데이터 없음")
        return

    article_ids = sorted({t["article_id"] for t in translations})
    # PostgREST in.() 필터 — id 목록이 크면 여러 번에 나눠 조회
    articles_by_id = {}
    chunk = 500
    for i in range(0, len(article_ids), chunk):
        ids = article_ids[i:i + chunk]
        rows = _fetch_all("articles", {
            "select": "id,url,source,country,created_at,source_published_at,image_url,category,subcategory",
            "id": f"in.({','.join(str(x) for x in ids)})",
        })
        for r in rows:
            articles_by_id[r["id"]] = r

    by_lang = {lang: [] for lang in LANGUAGES}
    for t in translations:
        lang = t["lang"]
        if lang not in by_lang:
            continue
        art = articles_by_id.get(t["article_id"])
        if not art:
            continue
        source_tag = art.get("source") or ""
        source_domain = source_tag.split(":", 1)[1] if ":" in source_tag else source_tag
        by_lang[lang].append({
            "id": t["article_id"],
            "title": t["title"],
            "summary": t["summary"],
            "source_domain": source_domain,
            "source_url": art.get("url") or "",
            "country": art.get("country") or "",
            "created_at": t["created_at"],
            "image_url": art.get("image_url") or "",
            "category": art.get("category") or "",
        })

    for lang, items in by_lang.items():
        items.sort(key=lambda x: x["created_at"], reverse=True)
        out_path = os.path.join(OUT_DIR, f"{lang}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"lang": lang, "lang_name": LANG_NAMES.get(lang, lang),
                       "generated_at": datetime.now(KST).isoformat(), "articles": items},
                      f, ensure_ascii=False, indent=2)
        print(f"[EXPORT_GLOBAL] {lang}.json — {len(items)}건")

    _write_sitemap(by_lang)


def _write_sitemap(by_lang: dict):
    """언어별 article.html?lang=..&id=.. URL을 담은 최소 sitemap."""
    urls = []
    base = "https://global.newsfinal.co.kr"
    for lang, items in by_lang.items():
        for item in items:
            urls.append(f"{base}/article.html?lang={lang}&id={item['id']}")

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        lines.append(f"  <url><loc>{u}</loc></url>")
    lines.append("</urlset>")

    os.makedirs(SITE_DIR, exist_ok=True)
    with open(os.path.join(SITE_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[EXPORT_GLOBAL] sitemap.xml — {len(urls)}개 URL")


if __name__ == "__main__":
    export_global()
