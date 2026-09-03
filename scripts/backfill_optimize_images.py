"""
scripts/backfill_optimize_images.py
--------------------------------------
일회성 백필: image_store.py에 리사이즈+재압축 안전망(2026-09-04)을 넣기
전에 이미 R2에 올라간 기존 이미지들을 같은 기준(960px, JPEG quality=82)
으로 다시 최적화한다. 사용자 요청 — "이전에 업로드 된 파일들도 수정
가능한가?".

R2 객체 키는 URL 경로에서 그대로 뽑아 재사용한다(articles/{filename}) —
그러면 DB의 image_url을 하나도 안 바꾸고 같은 URL 자리에 최적화된
바이트로 덮어쓸 수 있다. 다운로드→최적화→같은 파일명으로 재업로드,
실패한 항목은 건너뛰고 계속 진행(개별 이미지 실패가 전체를 막지 않음).

대상 URL 목록은 이 파일과 같은 디렉터리의 _r2_image_urls.txt에서 읽는다
(Supabase에서 조회한 R2 호스팅 image_url 485건 — 2026-09-04 스냅샷).

실행: python scripts/backfill_optimize_images.py
필요 환경변수: IMAGE_UPLOAD_URL, UPLOAD_SECRET
"""

import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

from image_store import _optimize_image, _guess_ext

IMAGE_UPLOAD_URL = os.getenv("IMAGE_UPLOAD_URL", "")
UPLOAD_SECRET = os.getenv("UPLOAD_SECRET", "")

URL_LIST_FILE = os.path.join(os.path.dirname(__file__), "..", "_r2_image_urls.txt")


def reupload(filename: str, data: bytes, ext: str) -> bool:
    import base64
    mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
    res = requests.post(
        IMAGE_UPLOAD_URL,
        headers={"Authorization": f"Bearer {UPLOAD_SECRET}", "Content-Type": "application/json"},
        json={"base64": base64.b64encode(data).decode("ascii"), "mimeType": mime, "filename": filename},
        timeout=60,
    )
    return res.status_code == 200


def main():
    if not IMAGE_UPLOAD_URL or not UPLOAD_SECRET:
        print("[SKIP] IMAGE_UPLOAD_URL/UPLOAD_SECRET 없음")
        return
    if not os.path.exists(URL_LIST_FILE):
        print(f"[ERROR] URL 목록 파일 없음: {URL_LIST_FILE}")
        return

    with open(URL_LIST_FILE, encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    print(f"[backfill_optimize_images] 대상 {len(urls)}건")

    total_before = 0
    total_after = 0
    optimized_count = 0
    skipped_count = 0
    failed_count = 0

    for i, url in enumerate(urls, 1):
        # articles/{filename} 구조에서 filename만 뽑는다(worker.js의
        # `key = 'articles/' + fileName` 규칙과 정확히 맞춰야 같은 객체를
        # 덮어쓴다).
        filename = url.rsplit("/articles/", 1)[-1] if "/articles/" in url else url.rsplit("/", 1)[-1]
        try:
            res = requests.get(url, timeout=30)
            if res.status_code != 200 or not res.content:
                print(f"  [{i}/{len(urls)}] ⚠️ 다운로드 실패 {res.status_code}: {filename}")
                failed_count += 1
                continue

            data = res.content
            before_size = len(data)
            ext = _guess_ext(url, res.headers.get("Content-Type", ""))
            opt_data, opt_ext = _optimize_image(data, ext)

            if len(opt_data) >= before_size:
                skipped_count += 1
                total_before += before_size
                total_after += before_size
                continue

            # 확장자가 jpg로 바뀌었으면 파일명도 .jpg로 맞춰야 하는데,
            # 그러면 새 키가 되어 원본이 안 지워지고 남는다 — 원래 확장자
            # 그대로 파일명을 유지하고, 내용만 JPEG로 바꿔 올린다(콘텐츠
            # 타입만 image/jpeg로 정확히 표기하면 브라우저는 확장자 대신
            # 실제 바이트를 보고 렌더링하므로 문제없음).
            ok = reupload(filename, opt_data, opt_ext)
            if ok:
                optimized_count += 1
                total_before += before_size
                total_after += len(opt_data)
                pct = 100 * (1 - len(opt_data) / before_size)
                print(f"  [{i}/{len(urls)}] ✓ {filename}: {before_size:,}B → {len(opt_data):,}B (-{pct:.0f}%)")
            else:
                failed_count += 1
                total_before += before_size
                total_after += before_size
                print(f"  [{i}/{len(urls)}] ⚠️ 재업로드 실패: {filename}")

        except Exception as e:
            print(f"  [{i}/{len(urls)}] ⚠️ 예외: {filename} — {e}")
            failed_count += 1

        time.sleep(0.2)  # R2/Worker에 과부하 안 걸리게 살짝 페이싱

    saved = total_before - total_after
    pct = (100 * saved / total_before) if total_before else 0
    print(f"\n[backfill_optimize_images] 완료 — 최적화 {optimized_count}건, "
          f"이미 최적 {skipped_count}건, 실패 {failed_count}건")
    print(f"  총 용량: {total_before:,}B → {total_after:,}B (절약 {saved:,}B, {pct:.1f}%)")


if __name__ == "__main__":
    main()
