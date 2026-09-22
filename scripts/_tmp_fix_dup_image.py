"""
일회성 스크립트 2 — 이미 발행된 주간 시세 카드(id=213872)에 이미지가 없던 것을
weekly_market_card.py에 새로 넣은 로직(주간 최대 상승 항목=비트코인 기준)으로
백필한다. 로컬에 PIXABAY_API_KEY가 없어(CI 시크릿 전용) 임시 워크플로우로
실행. 실행 확인 후 이 파일과 대응 워크플로우는 삭제한다.

(1차: id=214175 이미지 교체는 이미 완료됨 — 이 파일은 그 작업 대신 재사용)
"""
import requests
from db import _url, _headers
from article_image import fetch_seeded_pixabay_image

ARTICLE_ID = 213872
WEEK_END_ORDINAL = __import__("datetime").date(2026, 9, 18).toordinal()
KEYWORDS = ["bitcoin cryptocurrency", "cryptocurrency coin"]

image_url = fetch_seeded_pixabay_image(KEYWORDS, seed=WEEK_END_ORDINAL, key_hint="weekly_market_2026-09-18")
print(f"이미지: {image_url}")
if not image_url:
    print("이미지 조회 실패 — 중단")
    raise SystemExit(1)

patch = requests.patch(
    f'{_url("articles")}?id=eq.{ARTICLE_ID}',
    headers={**_headers(), "Prefer": "return=representation"},
    json={"image_url": image_url},
    timeout=15,
)
print(patch.status_code, patch.text[:300])
