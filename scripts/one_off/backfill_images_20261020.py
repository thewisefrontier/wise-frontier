"""
scripts/one_off/backfill_images_20261020.py
-------------------------------------------
2026-10-20 아침 국제유가·국내유가·글로벌마켓 기사 3건이 사진 없이 발행됐다(Pixabay 다운로드 429,
R2 업로드 SSL 끊김 — 일시 오류를 재시도 없이 포기; image_store.py에서 재시도 세션으로 수정).
각 작성기의 사진 함수와 같은 키워드·시드·파일명 규칙으로 다시 받아 붙인다.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from article_image import fetch_seeded_pixabay_image  # noqa: E402
from oil_price_writer import _OIL_IMAGE_KEYWORDS as OIL_KW  # noqa: E402
from opinet_price_writer import _OIL_IMAGE_KEYWORDS as OPINET_KW  # noqa: E402
from frontier_markets_writer import _MARKET_IMAGE_KEYWORDS as MARKET_KW  # noqa: E402
from http_retry import get_session  # noqa: E402

requests = get_session()
BASE = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
KEY = os.environ["SUPABASE_SERVICE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# (기사 id, 키워드, 기준일, 파일명 접두) — 작성기 규칙 그대로(유가·마켓은 전일 시장일, 국내유가는 당일)
JOBS = [
    (317211, OIL_KW, date(2026, 10, 19), "oil"),
    (317206, OPINET_KW, date(2026, 10, 20), "opinet"),
    (317215, MARKET_KW, date(2026, 10, 19), "frontier_markets"),
]

bad = 0
for aid, kws, d, prefix in JOBS:
    cur = requests.get(f"{BASE}/articles", headers=H, params={"id": f"eq.{aid}", "select": "image_url"}, timeout=30).json()
    if cur and cur[0].get("image_url"):
        print(f"#{aid}: 이미 사진 있음 — 건너뜀")
        continue
    url = fetch_seeded_pixabay_image(kws, d.toordinal(), f"{prefix}_{d.isoformat()}")
    if not url:
        print(f"#{aid}: 사진 확보 실패")
        bad += 1
        continue
    r = requests.patch(f"{BASE}/articles", headers={**H, "Prefer": "return=minimal"}, params={"id": f"eq.{aid}"},
                       json={"image_url": url}, timeout=30)
    print(f"#{aid}: {'붙임' if r.status_code in (200, 204) else f'갱신 실패 {r.status_code}'} → {url}")
    bad += r.status_code not in (200, 204)
sys.exit(1 if bad else 0)
