"""
일회성 스크립트 — id=214175(베를린 선거 기사) 이미지를 213833(러시아 총선
기사)과 겹치지 않는 다른 사진으로 교체한다. 로컬에 PIXABAY_API_KEY가 없어
CI(secrets)에서만 실행 가능. 실행 후 이 파일과 대응 워크플로우는 삭제한다.
"""
import os
import requests
from db import image_used_recently, _url, _headers
from image_store import store_image

PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
ARTICLE_ID = 214175
QUERY = "political rally campaign speech crowd"

res = requests.get(
    "https://pixabay.com/api/",
    params={"key": PIXABAY_API_KEY, "q": QUERY, "image_type": "photo",
            "safesearch": "true", "per_page": 10},
    timeout=15,
)
res.raise_for_status()
hits = res.json().get("hits", [])
print(f"후보 {len(hits)}건")

pick = None
for h in hits:
    frag = f"pixabay_{h.get('id')}"
    if not image_used_recently(frag, hours=48):
        pick = h
        break
if not pick and hits:
    pick = hits[0]

if not pick:
    print("후보 없음 — 중단")
    raise SystemExit(1)

raw_url = pick.get("webformatURL") or pick.get("largeImageURL")
key_hint = f"article_pixabay_{pick.get('id')}"
new_url = store_image(raw_url, key_hint=key_hint)
print(f"새 이미지: id={pick.get('id')} tags={pick.get('tags')} url={new_url}")

patch = requests.patch(
    f'{_url("articles")}?id=eq.{ARTICLE_ID}',
    headers={**_headers(), "Prefer": "return=representation"},
    json={"image_url": new_url, "image_credit": "이미지 출처: Pixabay (기사 내용과 직접 관련 없는 예시 이미지)"},
    timeout=15,
)
print(patch.status_code, patch.text[:300])
