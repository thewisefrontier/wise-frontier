"""
scripts/article_image.py
-------------------------
기사 이미지 자동 삽입 공용 모듈. 위키미디어 커먼즈(CC/PD 라이선스)를 먼저
찾고, 없으면 Pixabay 일반 스톡사진으로 대체한다.

원래 gemini_writer.py 안에 있던 로직을 다른 writer 스크립트(gemini_summarizer.py,
econ_writer.py 등)도 공용으로 쓰도록 분리했다(2026-09-01, script_leak.py·
gemini_client.py와 같은 이유 — "다른 기사들에도 사진 자동 삽입을 도입해볼까"
라는 사용자 요청).

call_gemini_fn 인자로 각 스크립트 자신의 call_gemini() 래퍼를 주입받는다 —
스크립트마다 GeminiClient 인스턴스·API 키 세트가 다르기 때문(gemini_client.py
참조). 시그니처는 call_gemini_fn(prompt, max_tokens=30, start_tier=3) 형태를
기대한다(모든 writer 스크립트의 call_gemini 래퍼가 이미 이 시그니처).

사용:
    from article_image import fetch_article_image
    image_url, image_credit = fetch_article_image(title, body, entity, call_gemini)
"""

import html
import os
import re

import requests

PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

# 위키미디어 커먼즈는 CC/PD 라이선스 파일만 호스팅하지만, 드물게 마이그레이션
# 잔재로 비자유 파일이 섞여 있을 수 있어 License 값과 Restrictions를 이중 확인한다.
_WIKI_LICENSE_ALLOW = {
    "cc0", "pd", "pd-old", "pd-us",
    "cc-by-1.0", "cc-by-2.0", "cc-by-2.5", "cc-by-3.0", "cc-by-4.0",
    "cc-by-sa-1.0", "cc-by-sa-2.0", "cc-by-sa-2.5", "cc-by-sa-3.0", "cc-by-sa-4.0",
}
_HTML_TAG_RE = re.compile(r'<[^>]+>')

# 위키미디어 커먼즈 검색이 가끔 실제 환자의 얼굴이 나온 노골적인 임상
# 사진을 골라오는 문제(2026-09-07, 사용자 재제보 — "이 기사 사진 전에
# 마음에 안든다고 얘기했던건데 또 올라왔네". "trend_콜레라" 기사가 4일
# 연속 업데이트되면서 같은 사진(Adult_cholera_patient.jpg, 실제 환자
# 임상사진)이 매번 다시 선택됐음 — fetch_wikimedia_image는 시드/회전이
# 없어 같은 검색어에 항상 같은 1등 결과를 반환하기 때문). 확실히 알려진
# 파일은 제목으로 직접 차단하고, "patient"가 파일 제목에 들어간 경우는
# 일반 병원·의료진 사진이 아니라 환자 본인을 찍은 사진일 가능성이 높아
# 통째로 배제한다(위생상 보수적으로 접근 — 놓치는 것보다 잘못 쓰는 게
# 더 나쁨).
_WIKI_TITLE_BLOCKLIST = {
    "file:adult cholera patient.jpg",
}
_WIKI_TITLE_AVOID_WORDS = ("patient", "autopsy", "cadaver", "corpse")


def fetch_wikimedia_image(query: str):
    """위키미디어 커먼즈에서 CC/PD 라이선스 이미지를 검색한다.

    영화 포스터·앨범 커버 등 저작권이 있는 홍보물은 커먼즈 정책상 애초에
    거의 없다(비자유 콘텐츠는 위키백과 개별 문서에서만 "그 문서 안에서만"
    허용되는 fair use이지 재배포 가능한 자유 라이선스가 아니다) — 인물
    사진·공식 행사 사진·랜드마크 등에서 주로 성과가 난다.

    반환: (image_url, image_credit). CC-BY 계열처럼 저작자 표기 의무가
    있는 라이선스만 image_credit을 채우고, 퍼블릭도메인 등 표기 의무가
    없으면 빈 문자열.
    """
    if not query:
        return None, None
    try:
        res = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query", "generator": "search",
                "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 8,
                "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime",
                # 원본을 그대로 링크하면 히어로 이미지(max-height:420px)에
                # 비해 과도하게 큰 파일(커먼즈 원본은 수 MB인 경우도 흔함)이
                # 그대로 로드된다 — iiurlwidth로 960px 스케일 썸네일 URL을
                # 같이 받아온다(2026-09-04 "최적화는 해둬야지" 사용자 지시,
                # 공식 MediaWiki API 문서로 thumburl 필드 확인 후 반영).
                "iiurlwidth": 960,
                "format": "json",
            },
            headers={"User-Agent": "NewsFinalBot/1.0 (+https://newsfinal.co.kr)"},
            timeout=15,
        )
        if res.status_code != 200:
            return None, None
        # 검색 관련도가 부정확할 때가 있다(실측: "Central African Republic conflict"
        # 검색이 무관한 "Armed conflict zones in Myanmar.png"를 반환) — 쿼리의
        # 핵심 단어(보통 국가·질병명, 장르 접미사 앞부분)가 파일 제목에 실제로
        # 있는지 최소한으로 확인한다.
        _GENRE_SUFFIXES = {"conflict", "war", "violence", "disease", "civil", "coup", "crisis"}
        _q_words = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]+", query) if w.lower() not in _GENRE_SUFFIXES]
        anchor = _q_words[0].lower() if _q_words else ""

        pages = ((res.json().get("query") or {}).get("pages")) or {}
        for page in pages.values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            if info.get("mime") not in ("image/jpeg", "image/png", "image/webp", "image/svg+xml"):
                continue
            title_lower = (page.get("title") or "").lower()
            if title_lower in _WIKI_TITLE_BLOCKLIST:
                continue
            if any(w in title_lower for w in _WIKI_TITLE_AVOID_WORDS):
                continue
            if anchor and anchor not in title_lower:
                continue
            if (info.get("width") or 0) < 300 or (info.get("height") or 0) < 200:
                continue
            meta = info.get("extmetadata") or {}
            license_key = (meta.get("License", {}).get("value") or "").lower()
            restrictions = (meta.get("Restrictions", {}).get("value") or "").strip()
            if restrictions or license_key not in _WIKI_LICENSE_ALLOW:
                continue
            url = info.get("thumburl") or info.get("url", "")
            if not url:
                continue
            attribution_required = (meta.get("AttributionRequired", {}).get("value") or "").lower() == "true"
            credit = ""
            if attribution_required:
                artist = html.unescape(_HTML_TAG_RE.sub("", meta.get("Artist", {}).get("value", ""))).strip()[:80]
                license_name = html.unescape(meta.get("LicenseShortName", {}).get("value", ""))
                credit = f"사진: {artist} ({license_name}, Wikimedia Commons)" if artist \
                    else f"사진: Wikimedia Commons ({license_name})"
            return url, credit
    except Exception as e:
        print(f"  ⚠️ 위키미디어 검색 실패: {e}")
    return None, None


def fetch_seeded_pixabay_image(keywords: list, seed: int, key_hint: str) -> str:
    """고정 키워드 풀에서 seed로 결정론적으로 하나를 골라 Pixabay에서 검색하고
    R2에 영구 저장한다. 실패 시 빈 문자열.

    Gemini 호출 없이(=API 비용 없이) 매번 다른, 그러나 재현 가능한 사진을
    쓰고 싶은 데일리 템플릿 기사(유가·환율·글로벌 마켓 동향 등, 주제가
    매일 같아 키워드를 그때그때 새로 뽑을 이유가 없는 경우)용이다.
    fetch_article_image()와 달리 Gemini 호출이 없어 call_gemini_fn 주입이
    필요 없다.

    원래 oil_price_writer.py/opinet_price_writer.py/opinet_weekly_writer.py/
    frontier_markets_writer.py 4곳에 거의 동일한 코드로 복붙돼 있었다
    (2026-09-02 감사로 확인). seed는 보통 date.toordinal() — 키워드 선택과
    같은 날짜 안의 Pixabay 검색결과 중 선택 둘 다에 재사용해 동일 입력에
    항상 같은 사진이 나오게 한다. key_hint는 R2 저장 키 접두사(호출부마다
    달라 그대로 파라미터로 받는다, 예: f"oil_{price_date.isoformat()}").
    """
    if not PIXABAY_API_KEY or not keywords:
        return ""
    query = keywords[seed % len(keywords)]
    try:
        res = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "safesearch": "true",
                "per_page": 10,
            },
            timeout=15,
        )
        if res.status_code != 200:
            print(f"  ⚠️ Pixabay {res.status_code}: {res.text[:100]}")
            return ""
        hits = res.json().get("hits", [])
        if not hits:
            print(f"  ⚠️ Pixabay 결과 없음: {query}")
            return ""
        hit = hits[seed % len(hits)]
        # largeImageURL(최대 1280px)이 아니라 webformatURL(최대 640px)을 쓴다
        # — 기사 히어로 이미지는 CSS상 max-height:420px라 1280px가 과잉이고,
        # R2 저장 용량만 몇 배로 먹는다(2026-09-04 사용자 지적 — "사진이
        # DB에 용량을 제법 차지할텐데, 최대한 아끼는 법을 찾아봐". DB 자체엔
        # URL 문자열만 들어가 영향 없지만 R2 저장 용량은 실제로 아낄 수 있음).
        raw_url = hit.get("webformatURL", "") or hit.get("largeImageURL", "")
        if not raw_url:
            return ""
        from image_store import store_image
        url = store_image(raw_url, key_hint=key_hint)
        print(f"  🖼️ 이미지: {query} → {url[:70]}")
        return url or ""
    except Exception as e:
        print(f"  ⚠️ Pixabay 실패: {e}")
    return ""


def fetch_article_image(title: str, body: str, entity: str, call_gemini_fn) -> tuple:
    """기사 이미지를 찾는다. 반환: (image_url, image_credit).

    entity(기사의 핵심 고유명사, 보통 keyword_en이나 트렌드 그룹의 대표
    영문 키워드)가 있으면 위키미디어 커먼즈에서 먼저 찾고, 없으면 Pixabay
    일반 스톡사진으로 대체한다. call_gemini_fn은 호출 스크립트 자신의
    call_gemini(prompt, max_tokens=..., start_tier=...) 래퍼.
    """
    if entity:
        wiki_url, wiki_credit = fetch_wikimedia_image(entity)
        if wiki_url:
            # 2026-09-07 실사고(id=140160): 위키미디어 경로만 store_image()를
            # 거치지 않고 upload.wikimedia.org 원본 URL을 그대로 반환하고
            # 있었다 — Pixabay 경로(아래)는 이미 R2 저장+재압축을 거치는데
            # 위키미디어만 이 구조적 최적화에서 빠져 있었다.
            try:
                from image_store import store_image
                stored_url = store_image(wiki_url, key_hint=f"wiki_{entity}")
            except Exception as e:
                print(f"  ⚠️ 위키미디어 이미지 R2 저장 실패, 원본 URL 사용: {e}")
                stored_url = wiki_url
            print(f"  🖼️ 위키미디어 커먼즈 이미지 사용: {entity}")
            return (stored_url or wiki_url), (wiki_credit or "")

    if not PIXABAY_API_KEY:
        return "", ""
    prompt = f"""아래 뉴스 기사의 이미지 검색용 영문 키워드를 2~3개 추출하세요.
일반적인 시각 소재 위주로 (예: oil refinery, stock market, container port, farmland).
인명·기업명·구체적 지명은 제외. 쉼표 구분, 키워드만 출력.

제목: {title}
본문 앞부분: {(body or "")[:300]}"""
    kw = call_gemini_fn(prompt, max_tokens=30, start_tier=3)
    if not kw:
        return "", ""
    query = kw.strip().replace(",", " ").split("\n")[0][:100]
    if not query:
        return "", ""
    try:
        res = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "safesearch": "true",
                "per_page": 3,
            },
            timeout=15
        )
        if res.status_code == 200:
            hits = res.json().get("hits", [])
            if hits:
                # webformatURL(최대 640px) 사용 — largeImageURL(1280px)은 히어로
                # 이미지 표시 크기(max-height:420px)에 과잉이라 R2 용량만 낭비.
                raw_url = hits[0].get("webformatURL", "") or hits[0].get("largeImageURL", "")
                # Pixabay 이미지 URL은 ~24시간 후 만료되는 임시 URL이라
                # R2에 영구 저장해서 링크가 안 깨지게 한다(image_store.py 참조).
                try:
                    from image_store import store_image
                    return store_image(raw_url, key_hint=f"article_{query}"), ""
                except Exception as e:
                    print(f"  ⚠️ R2 저장 실패, 원본 URL 사용: {e}")
                    return raw_url, ""
        else:
            print(f"  ⚠️ Pixabay {res.status_code}: {res.text[:100]}")
    except Exception as e:
        print(f"  ⚠️ Pixabay 실패: {e}")
    return "", ""
