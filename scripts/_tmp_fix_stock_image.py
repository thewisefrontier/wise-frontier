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
QUERY = "financial district skyline"  # 3차 시도 — "stock exchange board"는 상위 10건 전부 암호화폐 사진이었음(2차 실패 원인)

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

# 2차 시도가 "stock exchange board" 상위 10건 전부 비트코인/암호화폐 사진이던
# 걸 겪고 나서 추가 — "비자 주가 하락" 같은 전통 주식 기사에 암호화폐 이미지가
# 나오면 안 되므로 태그로 걸러낸다.
BAD_TAGS = ("bitcoin", "cryptocurrency", "crypto")
candidates = [h for h in hits if not any(t in (h.get("tags") or "").lower() for t in BAD_TAGS)]
if not candidates:
    print("⚠️ 전부 암호화폐 태그 — 중단, 검색어를 바꿔야 함")
    raise SystemExit(1)
pick = candidates[0]
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
