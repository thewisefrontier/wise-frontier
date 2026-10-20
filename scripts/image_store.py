# -*- coding: utf-8 -*-
"""
image_store.py — 외부 스톡 이미지의 R2 영구 저장 헬퍼

배경:
  Pixabay `largeImageURL`은 약 24시간만 유효한 임시 URL(핫링크 금지)이다.
  DB에 그대로 저장하면 하루 뒤 기사 이미지가 전부 깨진다.
  → 이미지 바이트를 받아 Cloudflare R2에 올리고, 영구 URL을 대신 저장한다.

사용:
    from image_store import store_image
    image_url = store_image(pixabay_url, key_hint="weather_나이지리아")

설계 원칙:
  - 실패해도 절대 예외를 밖으로 던지지 않는다. 실패 시 원본 URL을 그대로 반환해
    기존 동작으로 안전하게 후퇴한다 — 단 만료되는 임시 URL(Pixabay)은 빈 문자열로
    후퇴한다(2026-10-13, 임시 URL이 기사에 그대로 저장돼 하루 뒤 깨지던 문제).
  - key_hint를 정규화해 결정적(deterministic) 파일명을 쓴다.
    같은 대상은 같은 키로 덮어써져 R2에 중복 객체가 쌓이지 않는다.
  - Worker의 JSON(base64) 경로를 쓴다. 이 경로만 filename을 클라이언트가 지정할 수 있다.

필요 환경변수:
  IMAGE_UPLOAD_URL  예) https://newsfinal-image-upload.thewisefrontier.workers.dev/upload
  UPLOAD_SECRET     Worker의 Bearer 토큰
"""

import base64
import io
import os
import re
import unicodedata

import requests

# 2026-10-20 실사고: 같은 날 아침 국제유가·글로벌마켓 기사가 Pixabay 이미지 다운로드 429(요청 과다)로,
# 국내유가 기사가 R2 업로드 워커 SSL 연결 끊김(SSLEOFError)으로 사진 없이 발행됐다 — 둘 다 일시적
# 오류인데 재시도 없이 한 번에 포기했다(같은 실행에서 몇 초 뒤 다른 사진은 정상 저장됨). 공용 재시도
# 세션(429·5xx·연결 오류, 백오프·Retry-After 존중)으로 교체한다. 호출이 기사당 1~2회뿐이라
# rss_fetcher.py 때 같은 처리 시간 폭증 위험은 없다.
from http_retry import get_session
requests = get_session()

IMAGE_UPLOAD_URL = os.getenv("IMAGE_UPLOAD_URL", "")
UPLOAD_SECRET = os.getenv("UPLOAD_SECRET", "")

# 다운로드 상한(바이트). Worker의 base64 처리 부담을 막기 위한 안전장치.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 히어로 이미지는 CSS상 max-height:420px로만 표시된다. 호출부(Pixabay
# webformatURL/largeImageURL, 위키미디어 원본 등)가 이보다 큰 이미지를
# 넘겨도 여기서 한 번 더 강제로 줄인다(2026-09-04 "구조적으로 고쳐놔"
# 사용자 지시 — 호출부마다 URL 필드를 챙기는 방식은 새 스크립트가 추가될
# 때마다 다시 놓칠 수 있어서, 실제로 R2에 올라가는 지점 하나에서 강제).
MAX_IMAGE_WIDTH = 960
JPEG_QUALITY = 82

_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

# 이미 영구 저장소에 있는 URL은 재업로드하지 않는다.
_PERMANENT_HINTS = ("r2.dev", "newsfinal.co.kr")


def _slugify(text: str) -> str:
    """한글 등 비ASCII를 안전한 파일명 토큰으로 변환.

    주의: 한글은 ASCII 변환 시 통째로 사라진다. 그래서 "weather_나이지리아"와
    "weather_케냐"가 둘 다 "weather"가 되어 R2에서 서로 덮어쓰는 사고가 난다.
    → 비ASCII가 하나라도 있으면 원문 해시를 접미사로 붙여 충돌을 막는다.
    같은 입력은 항상 같은 값이므로 덮어쓰기(멱등) 성질은 유지된다.
    """
    text = (text or "").strip()
    ascii_part = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_part).strip("_").lower()

    if ascii_part != text:  # 비ASCII 문자가 있었다 → 정보 손실 발생
        import hashlib

        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[:40]}_{digest}" if slug else digest

    return slug[:60] or "img"


def _guess_ext(src_url: str, content_type: str) -> str:
    ext = _EXT_BY_MIME.get((content_type or "").split(";")[0].strip().lower())
    if ext:
        return ext
    m = re.search(r"\.(jpe?g|png|webp|gif)(?:\?|$)", (src_url or "").lower())
    return (m.group(1).replace("jpeg", "jpg") if m else "jpg")


# 시간이 지나면 만료되는 임시 URL — 실패해도 원본으로 후퇴하면 안 된다(store_image 참고).
_EXPIRING_HINTS = ("pixabay.com/get/",)


def _is_expiring(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _EXPIRING_HINTS)


def is_permanent(url: str) -> bool:
    """이미 R2 등 영구 저장소 URL인지 판정."""
    u = (url or "").lower()
    return bool(u) and any(h in u for h in _PERMANENT_HINTS)


def _optimize_image(data: bytes, ext: str) -> tuple[bytes, str]:
    """MAX_IMAGE_WIDTH보다 넓으면 축소하고 JPEG로 재압축해 용량을 최소화한다.
    GIF(움짤 가능성)는 프레임이 깨지므로 건드리지 않는다. 실패하면 원본
    바이트를 그대로 반환(최적화는 있으면 좋은 보강일 뿐, 업로드 자체를
    막을 이유가 아니다)."""
    if ext == "gif":
        return data, ext
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.width > MAX_IMAGE_WIDTH:
            new_height = round(img.height * MAX_IMAGE_WIDTH / img.width)
            img = img.resize((MAX_IMAGE_WIDTH, new_height), Image.LANCZOS)
        if img.mode not in ("RGB", "L"):
            # 알파 채널(RGBA/팔레트 등)은 JPEG가 지원 안 하니 흰 배경에 합성.
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        optimized = out.getvalue()
        # 이미 작은 이미지를 재압축이 오히려 키우는 경우(고압축 원본 등)엔
        # 원본을 그대로 쓴다.
        if len(optimized) < len(data):
            return optimized, "jpg"
        return data, ext
    except Exception as e:
        print(f"  ⚠️ 이미지 최적화 실패(원본 그대로 사용): {e}")
        return data, ext


def store_image_bytes(data: bytes, ext: str, key_hint: str = "", fallback_url: str = "") -> str:
    """이미지 바이트를 직접 R2에 저장하고 영구 URL을 반환.

    스크린샷 캡처나 자체 생성 이미지처럼 원본 URL 없이 바이트만 있는 경우에 쓴다.

    Args:
        data:         이미지 바이트
        ext:          확장자(jpg/png/webp/gif)
        key_hint:     R2 파일명에 쓸 힌트. 같은 힌트는 같은 키로 덮어쓴다.
        fallback_url: 실패 시 대신 반환할 값(보통 빈 문자열).

    Returns:
        성공 시 R2 영구 URL. 실패하거나 설정이 없으면 fallback_url.
    """
    if not data:
        return fallback_url

    if not IMAGE_UPLOAD_URL or not UPLOAD_SECRET:
        print("  ⚠️ R2 업로드 미설정(IMAGE_UPLOAD_URL/UPLOAD_SECRET)")
        return fallback_url

    try:
        if len(data) > MAX_IMAGE_BYTES:
            print(f"  ⚠️ 이미지 과대({len(data)}B)")
            return fallback_url

        mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
        filename = f"{_slugify(key_hint) or 'img'}.{ext}"

        up = requests.post(
            IMAGE_UPLOAD_URL,
            headers={
                "Authorization": f"Bearer {UPLOAD_SECRET}",
                "Content-Type": "application/json",
            },
            json={
                "base64": base64.b64encode(data).decode("ascii"),
                "mimeType": mime,
                "filename": filename,
            },
            timeout=60,
        )
        if up.status_code != 200:
            print(f"  ⚠️ R2 업로드 실패 {up.status_code}: {up.text[:120]}")
            return fallback_url

        url = (up.json() or {}).get("url", "")
        if not url:
            print("  ⚠️ R2 응답에 url 없음")
            return fallback_url

        print(f"  🗄️ R2 저장 완료: {filename} ({len(data):,}B)")
        return url

    except Exception as e:
        print(f"  ⚠️ R2 저장 예외: {e}")
        return fallback_url


def store_image(src_url: str, key_hint: str = "", timeout: int = 30) -> str:
    """임시 이미지 URL을 R2에 저장하고 영구 URL을 반환.

    Args:
        src_url:  원본 이미지 URL (Pixabay 등)
        key_hint: R2 파일명에 쓸 힌트(국가명, 토픽 등). 같은 힌트는 같은 키로 덮어쓴다.

    Returns:
        성공 시 R2 영구 URL. 실패하거나 설정이 없으면 src_url 원본 그대로 —
        단 Pixabay 임시 URL처럼 만료되는 원본이면 빈 문자열.
    """
    if not src_url:
        return ""

    # 이미 영구 URL이면 그대로 둔다(재실행 시 중복 업로드 방지).
    if is_permanent(src_url):
        return src_url

    # 2026-10-13 실측: 공개 기사 358건 중 7건이 Pixabay 임시 URL(pixabay.com/get/…, ~24시간 만료)
    # 그대로 저장돼 있었다 — 아래 실패 경로들이 "원본 URL 유지"로 후퇴했기 때문. 만료되는
    # 소스는 원본을 돌려주면 하루 뒤 깨진 이미지가 되므로 빈 문자열(=이미지 없이 발행)로 후퇴한다.
    # 위키미디어 원본처럼 만료 없는 URL은 기존대로 원본 유지.
    fallback = "" if _is_expiring(src_url) else src_url

    if not IMAGE_UPLOAD_URL or not UPLOAD_SECRET:
        print("  ⚠️ R2 업로드 미설정(IMAGE_UPLOAD_URL/UPLOAD_SECRET) — 원본 URL 유지")
        return fallback

    try:
        # 2026-09-07 실사고: 위키미디어는 User-Agent 없는 요청을 403으로
        # 차단한다(User-Agent 정책). Pixabay 등 다른 소스는 문제없었지만
        # 위키미디어 원본을 처음 이 함수에 태우면서 드러났다.
        res = requests.get(
            src_url, timeout=timeout, stream=True,
            headers={"User-Agent": "NewsFinalBot/1.0 (+https://newsfinal.co.kr)"},
        )
        if res.status_code != 200:
            print(f"  ⚠️ 이미지 다운로드 실패 {res.status_code} — 원본 URL 유지")
            return fallback

        data = res.content
        if not data:
            print("  ⚠️ 이미지 응답이 비어 있음 — 원본 URL 유지")
            return fallback
        if len(data) > MAX_IMAGE_BYTES:
            print(f"  ⚠️ 이미지 과대({len(data)}B) — 원본 URL 유지")
            return fallback

        content_type = res.headers.get("Content-Type", "")
        ext = _guess_ext(src_url, content_type)
        data, ext = _optimize_image(data, ext)
        url = store_image_bytes(data, ext, key_hint, fallback_url=fallback)
        return url

    except Exception as e:
        print(f"  ⚠️ R2 저장 예외: {e} — 원본 URL 유지")
        return fallback
