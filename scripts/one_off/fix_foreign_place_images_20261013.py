"""
2026-10-13 일회성: 타국 명소 사진이 붙은 기사 2건의 이미지를 새 필터 로직으로 다시 골라 교체.
- id=308347(북미 날씨): 런던 웨스트민스터 궁 → weather_report.fetch_country_image("미국")
- id=308571(미 연준): 프랑크푸르트 ECB 건물 → article_image.fetch_article_image (타국 지명 필터 적용)
- Pixabay 임시 URL(pixabay.com/get, ~24시간 만료)이 그대로 저장된 기사 7건(공개 스냅샷 기준):
  아직 살아 있으면 R2로 옮기고, 만료됐으면 이미지·크레딧을 비운다(깨진 이미지 방지).
R2 저장(store_image)이 필요해 IMAGE_UPLOAD_URL/UPLOAD_SECRET이 있는 Actions(one_off_task.yml)에서 실행.
실행: one_off_task.yml → script = one_off/fix_foreign_place_images_20261013.py
"""
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from article_store import sb_headers, sb_url  # noqa: E402
from article_image import fetch_article_image  # noqa: E402
from weather_report import fetch_country_image, now_kst  # noqa: E402


def get(aid):
    r = requests.get(f"{sb_url()}?id=eq.{aid}&select=id,title_ko,summary_ko,image_url,update_log",
                     headers=sb_headers(), timeout=20)
    return r.json()[0]


def patch(a, fields, note):
    log = (a.get("update_log") or []) + [{"timestamp": now_kst().strftime("%Y-%m-%d %H:%M"), "note": note}]
    r = requests.patch(f"{sb_url()}?id=eq.{a['id']}", headers=sb_headers(), timeout=20,
                       json={**fields, "update_log": log})
    print(f"id={a['id']} PATCH {r.status_code}: {a.get('image_url')} → {fields.get('image_url')}")


a = get(308347)
url = fetch_country_image("미국")
if url and "pixabay.com" not in url:  # R2 저장 실패로 임시 URL이 오면 교체하지 않는다(하루 뒤 깨짐)
    patch(a, {"image_url": url}, "이미지 교체: 타국(런던) 사진 → 국가 일치 사진(타국 지명 필터 도입)")
else:
    print("id=308347 교체 안 함 — 사진 없음 또는 R2 저장 실패:", url)

a = get(308571)
# 키워드 추출용 Gemini 호출 대신 고정 키워드(인명·지명 제외 원칙 동일) — 일회성이라 호출 아낌
url, credit = fetch_article_image(a["title_ko"], a["summary_ko"], "", lambda *_, **__: "bank regulation finance")
if url and "pixabay.com" not in url:
    patch(a, {"image_url": url, "image_credit": credit}, "이미지 교체: 타국(ECB 프랑크푸르트) 사진 → 타국 지명 없는 사진")
else:
    print("id=308571 교체 안 함 — 사진 없음 또는 R2 저장 실패:", url)

from image_store import store_image  # noqa: E402

for aid in (307949, 304921, 302248, 302241, 302240, 300438, 300435):
    a = get(aid)
    src = a.get("image_url") or ""
    if "pixabay.com/get/" not in src:
        print(f"id={aid} 건너뜀 — 이미 임시 URL 아님: {src[:60]}")
        continue
    stored = store_image(src, key_hint=f"article_fix_{aid}")  # 만료됐으면 "" (image_store 참고)
    if stored:
        patch(a, {"image_url": stored}, "이미지 영구 저장: Pixabay 임시 URL → R2")
    else:
        patch(a, {"image_url": "", "image_credit": ""}, "이미지 제거: Pixabay 임시 URL 만료(깨진 이미지 방지)")
