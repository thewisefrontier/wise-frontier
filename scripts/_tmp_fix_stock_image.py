"""
일회성 스크립트 — id=220558(종목동향, 2026-09-22)이 id=220557(글로벌마켓동향)과
같은 사진을 쓰고 있던 걸, 겹치지 않는 다른 사진으로 교체한다. 새 dedup 로직은
기존 파일명(photo id 미포함)을 인식 못 해 그대로 재실행하면 같은 사진이 다시
나올 수 있어, seed 인덱스에서 의도적으로 한 칸 옮겨 다른 사진을 강제로 고른다.
로컬에 PIXABAY_API_KEY가 없어 임시 워크플로우로 실행. 확인 후 이 파일과
워크플로우는 삭제한다.
"""
import requests
from db import _url, _headers
from image_store import store_image

ARTICLE_ID = 220558
KEYWORDS = ["stock market chart", "trading floor", "financial district",
            "stock exchange board", "business technology"]
SEED = __import__("datetime").date(2026, 9, 22).toordinal()

import os
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
query = KEYWORDS[SEED % len(KEYWORDS)]
res = requests.get(
    "https://pixabay.com/api/",
    params={"key": PIXABAY_API_KEY, "q": query, "image_type": "photo",
            "safesearch": "true", "per_page": 10},
    timeout=15,
)
res.raise_for_status()
hits = res.json().get("hits", [])
print(f"검색어: {query} / 후보 {len(hits)}건")
if not hits:
    raise SystemExit(1)

# 기존 frontier_markets 기사가 이미 seed%len(hits) 인덱스를 쓰고 있으므로,
# 여기선 의도적으로 한 칸 옮겨 다른 사진을 고른다.
pick = hits[(SEED + 1) % len(hits)]
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
