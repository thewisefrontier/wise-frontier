"""
일회성 스크립트 2차 — 1차 시도(offset+1)가 "trading floor" 태그였지만 실제론
금융과 무관한 로프트 인테리어 사진을 골라버렸다(발행 직후 자체 발견). 여러
후보를 로그로 찍어 직접 확인하고, 명확히 금융/증시 관련 사진만 고른다.
"""
import os
import requests
from db import _url, _headers
from image_store import store_image

ARTICLE_ID = 220558
QUERY = "stock exchange board"  # 종목 시세판 — 가장 명확하게 금융 연상되는 검색어로 직접 지정

PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
res = requests.get(
    "https://pixabay.com/api/",
    params={"key": PIXABAY_API_KEY, "q": QUERY, "image_type": "photo",
            "safesearch": "true", "per_page": 10},
    timeout=15,
)
res.raise_for_status()
hits = res.json().get("hits", [])
print(f"검색어: {QUERY} / 후보 {len(hits)}건")
for h in hits:
    print(" ", h.get("id"), h.get("tags"))

if not hits:
    raise SystemExit(1)

pick = hits[0]
print(f"선택: id={pick.get('id')} tags={pick.get('tags')}")

raw_url = pick.get("webformatURL") or pick.get("largeImageURL")
key_hint = f"stock_news_2026-09-22_pid{pick.get('id')}"
new_url = store_image(raw_url, key_hint=key_hint)
print(f"새 이미지: {new_url}")

patch = requests.patch(
    f'{_url("articles")}?id=eq.{ARTICLE_ID}',
    headers={**_headers(), "Prefer": "return=representation"},
    json={"image_url": new_url},
    timeout=15,
)
print(patch.status_code, patch.text[:200])
