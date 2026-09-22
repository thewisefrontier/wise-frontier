"""
일회성 스크립트 3 — id=213872 이미지가 실제로는 스팀(Steem) 코인이었던 걸
진짜 비트코인 사진으로 재교체한다. key_hint가 이전과 동일(weekly_market_
2026-09-18)이라 같은 R2 파일을 덮어써서 DB image_url은 그대로 둬도 된다.
로컬에 PIXABAY_API_KEY가 없어(CI 시크릿 전용) 임시 워크플로우로 실행.
실행 확인 후 이 파일과 대응 워크플로우는 삭제한다.
"""
from article_image import fetch_seeded_pixabay_image

KEYWORDS = ["bitcoin coin", "bitcoin cryptocurrency logo"]
WEEK_END_ORDINAL = __import__("datetime").date(2026, 9, 18).toordinal()

image_url = fetch_seeded_pixabay_image(KEYWORDS, seed=WEEK_END_ORDINAL, key_hint="weekly_market_2026-09-18")
print(f"이미지: {image_url}")
if not image_url:
    print("이미지 조회 실패")
    raise SystemExit(1)
