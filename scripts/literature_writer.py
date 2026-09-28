"""
scripts/literature_writer.py
------------------------------
하루 한 편, 문학작품(고전·문학상 수상작·한국/동아시아 문학)을 소개하는 "읽을거리" 기사.

2026-09-27 신설(사용자: "문학 작품 소개 기사도 쓸 수 있을까?" → "고전만 하면 신간 소개가
안 되잖아?" → "해외 작품은?"). art_weekly_writer.py(고전명화이야기)와 같은 구조지만
두 가지가 다르다:

- 작품 목록(scripts/data/literature_works.json)은 harvest_literature.py가 위키데이터로
  확정해 둔 것이라, 작품마다 한국어/영어 위키백과 "문서 제목"까지 박혀 있다. 런타임에
  검색을 하지 않으므로 미술 기능에서 겪은 "검색 결과가 엉뚱한 문서(동명 드라마 등)"
  사고 부류가 구조적으로 없다.
- 작품 소개는 작품 복제가 아니라 퍼블릭도메인일 필요가 없다(사용자 지적). 대신
  (1) 근거자료(위키백과) 문장을 그대로 베끼지 않게 연속 동일 구간을 검사하고,
  (2) 작품 본문 인용은 한 문장 이내로 제한하고, (3) 이미지는 위키미디어 커먼즈(자유
  라이선스, 저작자·라이선스 표기) 또는 Pixabay만 쓴다 — 출판사 표지를 긁어오지 않는다.

선택 규칙(2026-09-27 사용자: "구작보다는 신간, 해외 작품 위주로"): 5일 주기로
신간·신간·수상작·신간·고전 버킷을 돌린다(ROTATION). 버킷마다 "아직 안 쓴 첫 작품"
(url=internal://literature_{qid})을 고르고, 그날 버킷이 비었거나 실패하면 다른 버킷으로
넘어간다. 신간 버킷은 JSON이 아니라 영어 위키백과 연도별 소설 분류를 실행 때마다
실시간으로 가져온다(harvest_literature.recent_works) — 수집 파일을 다시 만들지 않아도
항상 최신. 하루 1편 게이트는 오늘(KST) 발행분이 이미 있으면 건너뛰는 것으로 건다.

실행: python scripts/literature_writer.py [--dry-run] [--qid Q12345] [--bucket recent|prize|classic]
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

try:
    from content_guard import is_placeholder_response
except Exception:
    def is_placeholder_response(title, body):
        return False

try:
    from json_body_guard import unwrap_json_body
except Exception:
    def unwrap_json_body(text, _depth=0):
        return None

from style_guard import has_column_style, has_polite_ending, to_plain_style, parse_article_output, ensure_paragraphs
from article_store import insert_final_article, sb_headers as _sb_headers, sb_url as _sb_url
from fabrication_guard import verify_no_fabricated_names as _fg_verify_no_fabricated_names
from gemini_client import GeminiClient
# 2026-09-27 dry-run 실사고: 근거엔 "Alabama School of Fine Arts"인데 본문에 "앤더슨 스쿨 오브
# 파인 아트"라고 썼고, Gemini 날조 검사(같은 계열 모델)는 못 잡았다. 계열이 다른 모델(NVIDIA)의
# "자료에 근거 없는 문장" 대조를 재사용한다(weekly_recap_writer.py와 같은 방식).
from explainer_writer import unsupported_claims

KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "NewsFinal-LitWriter/1.0 (+https://newsfinal.co.kr)"}
DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "literature_works.json")
SUBCATEGORY = "문학작품이야기"
URL_PREFIX = "internal://literature_"
MIN_BODY_LEN = 600
MIN_GROUNDING_LEN = 300
# 근거자료와 이만큼 연속으로 똑같은 구간이 있으면 "베꼈다"로 본다.
COPY_SPAN = 40
MAX_CANDIDATES_PER_RUN = 3
# 신간 3 : 수상작 1 : 고전(구텐베르크·한국/동아시아 큐레이션) 1
ROTATION = ["recent", "recent", "prize", "recent", "classic"]
RECENT_YEARS_BACK = 2  # 올해 + 작년 + 재작년 소설 분류
BOOK_IMAGE_KEYWORDS = ["old books library", "open book reading", "books stack vintage", "library bookshelf"]

GEMINI_MODELS = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
]
GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_2"), os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"), os.getenv("GEMINI_API_KEY_5"),
] if k]
_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


# 기사 본문은 상위 모델부터(start_tier=0) — 2026-09-27 dry-run에서 lite 모델이 "Alabama School of
# Fine Arts"를 "앨범 스쿨 오브 파이네 아츠"로 음차하는 등 품질이 들쭉날쭉했다. 하루 1편이라 비용 부담
# 없음. fabrication_guard는 자기 호출에 start_tier=4를 명시해서 넘기므로 검증 호출은 계속 가벼운 모델.
# max_tokens 8192: 3.x 비-lite 모델은 thinking 토큰도 maxOutputTokens에서 깎여 3000이면 MAX_TOKENS로
# 잘리고 모델을 옮겨 다니며 7분 넘게 걸렸다([[newsfinal_gemini_thinking_token_issue]], 2026-09-27 실측).
def call_gemini(prompt: str, max_tokens: int = 8192, start_tier: int = 0) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                               temperature=0.4, timeout=(10, 60))


def load_works() -> list:
    """수상작·고전 목록. 파일이 없거나 깨져도 신간 버킷(실시간)은 돌 수 있게 빈 목록."""
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠️ {os.path.basename(DATA_PATH)} 로드 실패({e}) — 신간 버킷만 사용")
        return []


def _award_year(w: dict) -> int:
    years = [int(y) for y in re.findall(r"\((\d{4})\)", " ".join(w.get("notes") or []))]
    return max(years) if years else 0


def bucket_works(name: str, works: list) -> list:
    """버킷별 후보 목록(우선순위 순). recent는 실시간 조회라 필요할 때만 부른다."""
    if name == "recent":
        try:
            from harvest_literature import recent_works
            y = now_kst().year
            return recent_works(range(y - RECENT_YEARS_BACK, y + 1))
        except Exception as e:
            print(f"  ⚠️ 신간 목록 조회 실패({e}) → 다른 버킷으로")
            return []
    if name == "prize":
        return sorted((w for w in works if w.get("notes")), key=_award_year, reverse=True)
    return [w for w in works if not w.get("notes")]


def pick_candidates(works: list, done: set, day_ordinal: int, only: str | None = None) -> list:
    first = only or ROTATION[day_ordinal % len(ROTATION)]
    order = [first] if only else [first] + [b for b in ("recent", "prize", "classic") if b != first]
    picked, seen = [], set()
    for name in order:
        for w in bucket_works(name, works):
            key = f"{URL_PREFIX}{w['qid']}"
            if key not in done and w["qid"] not in seen:
                picked.append(w)
                seen.add(w["qid"])
                break  # 버킷당 1편씩만 후보로 — 첫 버킷이 실패하면 다음 버킷 것으로
        if len(picked) >= MAX_CANDIDATES_PER_RUN:
            break
    return picked


def published_literature() -> list | None:
    """이미 발행한 문학 기사 [(url, created_at)]. 조회 실패 시 None(중복 위험 → 호출부가 중단)."""
    try:
        res = requests.get(_sb_url(), headers=_sb_headers(), timeout=15,
                           params={"select": "url,created_at", "url": f"like.{URL_PREFIX}*", "limit": "5000"})
        if res.status_code not in (200, 206):
            return None
        return [(r["url"], str(r.get("created_at") or "")) for r in res.json()]
    except Exception:
        return None


# ── 근거자료 ────────────────────────────────────────────────
_TAIL_SECTIONS = re.compile(
    r"\n=+\s*(각주|참고 문헌|참고문헌|외부 링크|같이 보기|더 읽을거리|References|Notes|"
    r"External links|See also|Further reading|Bibliography|Sources)\s*=+.*", re.S)


def wiki_extract(title: str, lang: str, max_chars: int) -> str:
    """정확한 문서 제목으로 본문 평문을 가져온다(검색 아님)."""
    if not title:
        return ""
    try:
        r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", headers=UA, timeout=20, params={
            "action": "query", "prop": "extracts", "explaintext": 1, "titles": title,
            "redirects": 1, "format": "json"})
        page = next(iter(r.json()["query"]["pages"].values()))
        text = _TAIL_SECTIONS.sub("", page.get("extract") or "").strip()
        return text[:max_chars]
    except Exception:
        return ""


def build_grounding(w: dict) -> tuple[str, str]:
    """(프롬프트용 전체 근거자료, 복붙 검사용 한국어 근거자료)"""
    work_ko = wiki_extract(w.get("kowiki"), "ko", 3500)
    work_en = wiki_extract(w.get("enwiki"), "en", 3500) if len(work_ko) < 1500 else ""
    author_ko = wiki_extract(w.get("author_kowiki"), "ko", 1200)
    author_en = wiki_extract(w.get("author_enwiki"), "en", 1200) if len(author_ko) < 400 else ""
    parts = []
    if work_ko:
        parts.append(f"[작품 — 한국어 위키백과]\n{work_ko}")
    if work_en:
        parts.append(f"[작품 — 영어 위키백과]\n{work_en}")
    if author_ko:
        parts.append(f"[작가 — 한국어 위키백과]\n{author_ko}")
    if author_en:
        parts.append(f"[작가 — 영어 위키백과]\n{author_en}")
    return "\n\n".join(parts), f"{work_ko}\n{author_ko}"


def copied_span(body: str, source: str, n: int = COPY_SPAN) -> str:
    """body가 source와 n자 이상 연속으로 똑같은 구간을 가지면 그 구간을 반환.
    ponytail: 10자 간격으로만 창을 대보므로 n+9자 이상 복사는 반드시 잡히고 그보다
    짧은 건 놓칠 수 있다 — 더 촘촘히 잡을 필요가 생기면 간격을 1로."""
    if not body or not source:
        return ""
    b = re.sub(r"\s+", " ", body)
    s = re.sub(r"\s+", " ", source)
    for i in range(0, max(0, len(b) - n + 1), 10):
        chunk = b[i:i + n]
        if chunk in s:
            return chunk
    return ""


# ── 이미지 ─────────────────────────────────────────────────
def commons_image(filename: str) -> tuple[str, str]:
    """커먼즈 파일의 960px 썸네일 URL과 표기 문구. 실패 시 ("", "")."""
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=20, params={
            "action": "query", "titles": f"File:{filename}", "prop": "imageinfo",
            "iiprop": "url|extmetadata", "iiurlwidth": 960, "format": "json"})
        page = next(iter(r.json()["query"]["pages"].values()))
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "")
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
        url = info.get("thumburl") or info.get("url") or ""
        if not url:
            return "", ""
        if re.search(r"public domain|pd|cc0", lic, re.I) or not lic:
            credit = "이미지 출처: Wikimedia Commons (퍼블릭 도메인)"
        else:
            # CC BY/BY-SA는 저작자·라이선스 표기가 이용 조건이다.
            credit = f"이미지 출처: Wikimedia Commons · {artist[:60] or '작자 미상'} · {lic}"
        return url, credit
    except Exception:
        return "", ""


def _norm_title(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").casefold()).strip()


def openlibrary_cover(w: dict) -> str:
    """오픈 라이브러리(키 불필요)에서 책 표지. 2026-09-27 사용자: "책 사진 넣어도 돼", "언론에서의
    이용은 공정이용으로 허락". 미술 기능 교훈대로 검색 결과를 그냥 믿지 않고, 제목이 같고
    저자 성이 일치하는 문서의 표지만 쓴다(합본·다른 책 표지 방지)."""
    title = w.get("original_title") or w.get("title_en") or ""
    author = w.get("author_en") or ""
    if not title or not author:
        return ""
    try:
        r = requests.get("https://openlibrary.org/search.json", headers=UA, timeout=20, params={
            "title": title, "author": author, "limit": 5, "fields": "title,author_name,cover_i"})
        docs = r.json().get("docs", [])
    except Exception:
        return ""
    surname = author.split()[-1].casefold()
    for d in docs:
        if not d.get("cover_i"):
            continue
        same_title = _norm_title(d.get("title")) == _norm_title(title)
        same_author = any(surname in (a or "").casefold() for a in d.get("author_name") or [])
        if same_title and same_author:
            return f"https://covers.openlibrary.org/b/id/{d['cover_i']}-L.jpg"
    return ""


def fetch_image(w: dict, store: bool = True) -> tuple[str, str]:
    """표지(한국어판 → 원서) → 커먼즈(초판본·작가 사진) → Pixabay."""
    kr = w.get("korean_edition") or {}
    candidates = []
    # 2026-09-28 사용자 지시: 표지 설명은 "1938 타이완 여행기 표지"처럼 간단히.
    label = f"{verified_title_ko(w) or w.get('original_title') or w.get('title_en') or ''} 표지".strip()
    if kr.get("cover"):
        candidates.append((kr["cover"], label))
    ol = openlibrary_cover(w)
    if ol:
        candidates.append((ol, label))
    for cover, credit in candidates:
        if not store:
            return cover, credit
        try:
            from image_store import store_image
            stored = store_image(cover, key_hint=f"literature_cover_{w['qid']}")
            if stored:
                return stored, credit
        except Exception:
            pass
    if w.get("image_file"):
        url, credit = commons_image(w["image_file"])
        if url:
            if not store:
                return url, credit
            try:
                from image_store import store_image
                return store_image(url, key_hint=f"literature_{w['qid']}") or url, credit
            except Exception:
                return url, credit
    if not store:
        return "(Pixabay 대체 이미지)", ""
    try:
        from article_image import fetch_seeded_pixabay_image
        seed = int(re.sub(r"\D", "", w["qid"]) or 0)
        # 크레딧을 비워두면 프론트가 R2 URL을 Pixabay로 표기한다(image-credit.js).
        return fetch_seeded_pixabay_image(BOOK_IMAGE_KEYWORDS, seed, f"literature_{w['qid']}"), ""
    except Exception:
        return "", ""


# ── 국가·지역 ───────────────────────────────────────────────
_COUNTRY_NORMALIZE = [
    (("대한민국", "한국", "조선", "대한제국", "고려"), "한국", "🇰🇷", "asia"),
    (("북한", "조선민주주의"), "북한", "🇰🇵", "asia"),
    (("일본",), "일본", "🇯🇵", "asia"),
    (("중국", "중화", "청나라", "명나라", "송나라", "당나라", "원나라"), "중국", "🇨🇳", "asia"),
    (("인도",), "인도", "🇮🇳", "asia"),
    (("영국", "잉글랜드", "그레이트브리튼", "스코틀랜드", "연합왕국"), "영국", "🇬🇧", "europe"),
    (("아일랜드",), "아일랜드", "🇮🇪", "europe"),
    (("프랑스",), "프랑스", "🇫🇷", "europe"),
    (("독일", "프로이센", "바이마르"), "독일", "🇩🇪", "europe"),
    (("러시아", "소련", "소비에트"), "러시아", "🇷🇺", "europe"),
    (("이탈리아",), "이탈리아", "🇮🇹", "europe"),
    (("스페인", "에스파냐"), "스페인", "🇪🇸", "europe"),
    (("오스트리아",), "오스트리아", "🇦🇹", "europe"),
    (("체코", "보헤미아"), "체코", "🇨🇿", "europe"),
    (("노르웨이",), "노르웨이", "🇳🇴", "europe"),
    (("스웨덴",), "스웨덴", "🇸🇪", "europe"),
    (("덴마크",), "덴마크", "🇩🇰", "europe"),
    (("폴란드",), "폴란드", "🇵🇱", "europe"),
    (("그리스",), "그리스", "🇬🇷", "europe"),
    (("포르투갈",), "포르투갈", "🇵🇹", "europe"),
    (("미국", "미합중국"), "미국", "🇺🇸", "global"),
    (("캐나다",), "캐나다", "🇨🇦", "global"),
    (("오스트레일리아", "호주"), "호주", "🇦🇺", "global"),
    (("콜롬비아",), "콜롬비아", "🇨🇴", "global"),
    (("아르헨티나",), "아르헨티나", "🇦🇷", "global"),
    (("멕시코",), "멕시코", "🇲🇽", "global"),
    (("브라질",), "브라질", "🇧🇷", "global"),
    (("나이지리아",), "나이지리아", "🇳🇬", "global"),
]


def country_info(label_ko: str | None) -> tuple[str, str, str]:
    """(국가 표기, 국기, region)"""
    if not label_ko:
        return "", "", "global"
    for keys, name, flag, region in _COUNTRY_NORMALIZE:
        if any(k in label_ko for k in keys):
            return name, flag, region
    return label_ko, "", "global"


# ── 프롬프트 ────────────────────────────────────────────────
def _years(b, d) -> str:
    fmt = lambda y: (f"기원전 {-y}" if y < 0 else str(y)) if y is not None else ""
    if b is None and d is None:
        return ""
    return f"{fmt(b)}~{fmt(d)}" if d is not None else f"{fmt(b)}~"  # 생존 작가는 "1962~"


def verified_title_ko(w: dict) -> str | None:
    """확인된 한국어 제목만 쓴다: 작품의 한국어 위키백과 문서가 있거나, 위키데이터 한국어
    라벨이 한국어 위키백과 본문 어딘가에 저자명·원제와 함께 실제로 쓰이는 경우
    (confirm_title_ko가 w["title_ko_confirmed"]를 세팅).
    2026-09-27 경과: 처음엔 "문서가 있어야만"으로 막았다가, 국내에 이미 『헝거 게임: 50번째
    추첨의 날』로 나온 책을 "(국내 번역판 제목 미확인)"이라고 틀리게 썼다(사용자 지적).
    라벨을 무조건 믿으면 편집자 임의 번역을 공식 제목처럼 쓸 위험이, 문서만 믿으면 출간작을
    미출간처럼 쓸 위험이 있어 — 본문 언급 대조로 중간을 잡는다."""
    if w.get("kowiki") or w.get("title_ko_confirmed"):
        # 위키 문서 구분용 괄호 제거 — 『아리랑 (소설)』 실사고(2026-09-27 dry-run)
        return re.sub(r"\s*\((소설|시|시집|책|고전 소설|문학)\)$", "", w.get("title_ko") or "") or None
    return None


def kakao_korean_edition(w: dict) -> dict | None:
    """카카오(다음) 책 검색으로 정식 한국어판을 찾는다(KAKAO_REST_API_KEY 필요, 없으면 None).
    2026-09-27 사용자: 『Sunrise on the Reaping』의 국내판은 "헝거 게임: 50번째 추첨의 날이잖아".
    위키데이터 한국어 라벨로 제목 검색 → 저자명이 일치하는 책만 → 종이책 우선 가장 먼저 나온 판."""
    key = os.getenv("KAKAO_REST_API_KEY")
    label = verified_title_ko({**w, "title_ko_confirmed": True})  # 라벨의 "(소설)" 등 괄호 제거본으로 검색
    author_ko = (w.get("author_ko") or "").replace(" ", "")
    if not key or not label or not author_ko:
        return None
    # 원래 한국어 작품은 번역판이 아니라 조회 불필요 — 하면 "아리랑(전12권+…)" 같은 세트 판본이 잡힌다.
    if country_info(w.get("author_country_ko"))[0] in ("한국", "북한"):
        return None
    try:
        r = requests.get("https://dapi.kakao.com/v3/search/book", timeout=15,
                         headers={"Authorization": f"KakaoAK {key}"},
                         params={"query": label, "target": "title", "size": 20})
        docs = r.json().get("documents", []) if r.status_code == 200 else []
    except Exception:
        return None
    nlabel = label.replace(" ", "")
    docs = [d for d in docs if nlabel in d.get("title", "").replace(" ", "")
            and not _SKIP_EDITION.search(d.get("title", "")) and not re.search(r"전\s*\d+\s*권", d.get("title", ""))
            and any(author_ko in (a or "").replace(" ", "") for a in d.get("authors", []))]
    if not docs:
        return None
    # 제목이 정확히 같은 판 우선 → ISBN10이 있는 쪽(대개 종이책, 전자책은 ISBN13만) → 가장 이른 출간일.
    docs.sort(key=lambda d: (d.get("title", "").replace(" ", "") != nlabel,
                             len((d.get("isbn") or "").split()) < 2, d.get("datetime") or "9999"))
    d = docs[0]
    from urllib.parse import urlparse, parse_qs
    thumb = d.get("thumbnail") or ""
    cover = parse_qs(urlparse(thumb).query).get("fname", [""])[0] or thumb  # 원본(약 458px) 우선
    return {"title": d["title"].strip(), "translators": d.get("translators") or [],
            "publisher": d.get("publisher") or "", "date": (d.get("datetime") or "")[:10],
            "cover": cover}


_HANGUL = re.compile(r"[가-힣]")
_SKIP_EDITION = re.compile(r"세트|박스|체험판|대역|합본|\[")


def kakao_author_titles(w: dict, limit: int = 12) -> list:
    """이 작가의 국내 출간작 정식 제목(카카오 책 검색, 저자 검색). 2026-09-27 실사고: 본문에서
    전편을 한국어 위키백과 표기 《명금과 뱀의 발라드》로 썼는데 국내판은 『노래하는 새와 뱀의
    발라드』 — 다른 작품 제목도 정식 국내판 제목을 확정값으로 준다. 원서·세트·체험판은 제외."""
    key = os.getenv("KAKAO_REST_API_KEY")
    author_ko = (w.get("author_ko") or "").replace(" ", "")
    if not key or not author_ko:
        return []
    try:
        r = requests.get("https://dapi.kakao.com/v3/search/book", timeout=15,
                         headers={"Authorization": f"KakaoAK {key}"},
                         params={"query": w["author_ko"], "target": "person", "size": 50})
        docs = r.json().get("documents", []) if r.status_code == 200 else []
    except Exception:
        return []
    titles, seen = [], set()
    for d in docs:
        t = re.sub(r"\s*\(.*?\)\s*", " ", d.get("title") or "").strip()
        t = re.sub(r"\s*[.:]?\s*\d+\s*(:.*)?$", "", t).strip()  # 권 번호 정리: "태백산맥 1: 제1부 …" → "태백산맥"
        key = t.replace(" ", "")
        if (any(author_ko == (a or "").replace(" ", "") for a in d.get("authors", []))
                and _HANGUL.search(t) and not re.search(r"[A-Za-z]", t)  # 한영 혼합 표기 제외
                and not _SKIP_EDITION.search(d.get("title") or "") and key not in seen):
            seen.add(key)
            titles.append(t)
    return titles[:limit]


def confirm_title_ko(w: dict) -> None:
    w["author_korean_titles"] = kakao_author_titles(w)
    _confirm_title_ko(w)


def _confirm_title_ko(w: dict) -> None:
    """위키데이터 한국어 라벨이 한국어 위키백과 본문 검색에서 저자명(또는 원제)과 함께 나오면
    확인된 제목으로 표시한다(키 불필요). 예: 라벨 "50번째 추첨의 날" → 영화 문서 「헝거게임
    50번째 추첨의 날」 본문에 "수잔 콜린스의 동명 소설", "Sunrise on the Reaping"이 함께 나옴."""
    kr = kakao_korean_edition(w)
    if kr:  # 정식 한국어판이 확인되면 그 제목이 최우선(시리즈명·콜론까지 정확)
        w["korean_edition"] = kr
        w["title_ko"] = kr["title"]
        w["title_ko_confirmed"] = True
        return
    label = verified_title_ko({**w, "title_ko_confirmed": True})
    if not label or w.get("kowiki") or len(label) < 2:
        return
    try:
        r = requests.get("https://ko.wikipedia.org/w/api.php", headers=UA, timeout=15, params={
            "action": "query", "list": "search", "srsearch": f'"{label}"', "srlimit": 5, "format": "json"})
        hits = r.json().get("query", {}).get("search", [])
    except Exception:
        return
    author_ko = (w.get("author_ko") or "").split()
    orig = (w.get("original_title") or w.get("title_en") or "").lower()
    for h in hits:
        text = h.get("title", "") + " " + re.sub(r"<[^>]+>", "", h.get("snippet", ""))
        if label not in text:
            continue
        if (author_ko and author_ko[-1] in text) or (orig and orig in text.lower()):
            w["title_ko_confirmed"] = True
            return


_ANNIV_PUB = {10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 100, 125, 150, 175, 200, 250, 300}
_ANNIV_PERSON = {100, 150, 200, 250, 300}


def _wd_date(t: str):
    """위키데이터 시간값("+2026-11-20T00:00:00Z") → date. 월·일이 00(연도 정밀도)이면 None."""
    m = re.match(r"\+(\d{4})-(\d{2})-(\d{2})", t or "")
    if not m or m.group(2) == "00" or m.group(3) == "00":
        return None
    from datetime import date
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def timeliness_hooks(w: dict) -> list:
    """"왜 지금 이 작품인가" — 코드가 위키데이터 확정값으로만 뽑는다(날짜를 Gemini가 짐작 안 하게).
    2026-09-27 사용자 지적: "2025년에 출시된 책이잖아. 그러면 이걸 지금 소개하는 이유가 있어야
    하는데", "오는 11월에 영화 개봉 예정인 점을 좀 부각하던가"."""
    hooks = []
    today = now_kst().date()
    y = today.year
    # 1) 이 작품이 원작(P144)인 영화·드라마 중 개봉이 최근 3개월~향후 1년
    try:
        from harvest_literature import wd_search, wd_entities, claim_values, label, sitelink
        adapt = wd_entities(wd_search(f"haswbstatement:P144={w['qid']}", limit=20))
        for e in adapt.values():
            types = set(claim_values(e, "P31"))
            kind = "영화" if "Q11424" in types else ("드라마" if types & {"Q5398426", "Q1259759"} else "영상화 작품")
            # 한국어 위키백과 문서 제목 우선 — 라벨은 초기 가제로 방치되기도 한다(실사고: 라벨
            # "헝거게임: 수확의 일출" vs 문서 "헝거게임 50번째 추첨의 날").
            kotitle = re.sub(r"\s*\(.*\)$", "", sitelink(e, "kowiki") or "")
            name = kotitle or label(e, "ko") or label(e, "en") or ""
            for t in claim_values(e, "P577"):
                d = _wd_date(t)
                if not d:
                    continue
                gap = (d - today).days
                # 시제를 문장에 박아 둔다 — "출간"만 주면 지난 날짜를 미래형으로 쓴 실사고(2026-09-27).
                if 0 <= gap <= 365:
                    hooks.append(f"이 작품을 원작으로 한 {kind} 「{name}」은(는) {d.year}년 {d.month}월 {d.day}일 "
                                 f"개봉(공개)할 예정이다(아직 개봉 전)")
                elif -90 <= gap < 0:
                    hooks.append(f"이 작품을 원작으로 한 {kind} 「{name}」은(는) {d.year}년 {d.month}월 {d.day}일 "
                                 f"이미 개봉(공개)했다")
    except Exception:
        pass
    # 2) 국내 번역판 출간이 최근 3개월 이내(또는 예정)
    kr = w.get("korean_edition")
    if kr and kr.get("date"):
        try:
            from datetime import date
            kd = date.fromisoformat(kr["date"])
            if -90 <= (kd - today).days <= 90:
                verb = "출간될 예정이다(아직 출간 전)" if kd > today else "이미 출간됐다"
                hooks.append(f"국내 번역판 『{kr['title']}』은(는) {kd.year}년 {kd.month}월 {kd.day}일 "
                             f"{kr['publisher']}에서 {verb}")
        except ValueError:
            pass
    # 3) 최근 수상
    for n in w.get("notes") or []:
        m = re.search(r"\((\d{4})\)", n)
        if m and int(m.group(1)) >= y - 1:
            hooks.append(f"{'올해' if int(m.group(1)) == y else '지난해'} {n}")
    # 3) 출간 연도·기념 연도
    py = w.get("pub_year")
    if py == y:
        hooks.append("올해 출간된 신작")
    elif py and (y - py) in _ANNIV_PUB:
        hooks.append(f"올해 출간 {y - py}주년")
    for key, what in (("author_birth", "탄생"), ("author_death", "서거")):
        v = w.get(key)
        if v and (y - v) in _ANNIV_PERSON:
            hooks.append(f"올해 작가 {what} {y - v}주년")
    return list(dict.fromkeys(hooks))


def hooks_block(hooks: list) -> str:
    if hooks:
        return "[시의성 포인트 — 지금 이 작품을 소개하는 이유(확정값)]\n" + "\n".join(f"- {h}" for h in hooks)
    return "[시의성 포인트] 확정된 것 없음"


def info_block(w: dict) -> str:
    """위키데이터로 확정된 기본 정보 — 프롬프트와 NVIDIA 대조 자료에 똑같이 들어간다."""
    title_ko = verified_title_ko(w)
    country, _, _ = country_info(w.get("author_country_ko"))
    orig = w.get("original_title") or w.get("title_en") or ""
    if title_ko and title_ko.replace(" ", "") == orig.replace(" ", ""):
        title_line = title_ko  # 한국 작품은 원제 = 한국어 제목이라 병기 안 함
    else:
        title_line = f"{title_ko} (원제: {orig})" if title_ko else f"원제: {orig}"
    author = w.get("author_ko") or w.get("author_en") or "작자 미상"
    author_meta = ", ".join(x for x in [w.get("author_en") if w.get("author_en") != author else "",
                                        _years(w.get("author_birth"), w.get("author_death")), country] if x)
    year = w.get("pub_year")
    year_line = (f"기원전 {-year}년" if year and year < 0 else f"{year}년") if year else "(확인되지 않음)"
    # 수상 이력이 없으면 줄 자체를 뺀다 — "(없음)"을 주면 "공식 수상 기록은 없으나"처럼 본문에 새어 나왔다.
    notes = ", ".join(w.get("notes") or [])
    kr = w.get("korean_edition")
    kr_line = ""
    if kr:
        who = f", {', '.join(kr['translators'])} 옮김" if kr["translators"] else ""
        kr_line = f"\n국내 출간: {kr['date']} {kr['publisher']}{who} (『{kr['title']}』)"
    kt = w.get("author_korean_titles") or []
    kt_line = f"\n이 작가의 국내 출간작(정식 한국어판 제목): {', '.join(f'『{t}』' for t in kt)}" if kt else ""
    return (f"[작품 기본 정보 — 확정값]\n작품: {title_line}\n"
            f"저자: {author}{f' ({author_meta})' if author_meta else ''}\n"
            f"발표·출간: {year_line}{kr_line}{f'{chr(10)}수상 이력: {notes}' if notes else ''}{kt_line}")


def build_prompt(w: dict, grounding: str, hooks: list | None = None) -> str:
    hooks = hooks or []
    if hooks:
        hook_rule = ("- ⚠️ 첫 문단은 위 [시의성 포인트]로 시작해 독자가 \"왜 지금 이 작품인가\"를 바로 알게 하세요. "
                     "기사 제목에도 반영하세요(예: \"11월 영화 개봉 앞둔 수잔 콜린스의 ○○\"). 날짜·수상 연도는 "
                     "[시의성 포인트]에 적힌 그대로 쓰세요.")
    else:
        hook_rule = ("- 첫 문단에서 이 작품이 지금도 읽히는 이유(수상·화제성·후대 영향 등, 근거에 있는 것만)를 "
                     "먼저 밝히세요. 날짜가 확정되지 않은 이벤트(개봉·출간 예정 등)를 지어내지 마세요.")
    w = {**w, "title_ko": verified_title_ko(w)}
    orig = w.get("original_title") or w.get("title_en") or ""
    if w.get("title_ko"):
        title_rule = f"- 작품의 한국어 제목은 『{w['title_ko']}』로 표기하세요."
    else:
        title_rule = (f"- 이 작품은 한국어 번역 제목이 확인되지 않았습니다. 원제({orig})를 그대로 쓰고, "
                      "공식 한국어판 제목처럼 보이는 제목을 지어내지 마세요. 본문 첫 등장 때 한 번만 "
                      "'(국내 번역판 제목 미확인)'이라고 밝혀 두세요 — 기사 제목(TITLE)에는 넣지 마세요.")
    return f"""당신은 프론티어 미디어 NewsFinal의 문화·예술 담당 에디터입니다.
매일 "문학작품 이야기" 코너에서 소개할 작품은 아래와 같습니다.

{info_block(w)}

{hooks_block(hooks)}

{grounding}

이 작품을 소개하는 한국어 기사를 작성하세요.
{hook_rule}
반드시 아래를 다루세요.
- 작품 이야기: 발표 배경, 주요 인물과 이야기의 출발점, 주제·문체·형식의 특징.
  ⚠️ 결말과 반전은 밝히지 마세요 — 독자가 직접 읽을 수 있게. 단 "결말은 숨겨져 있다"처럼
  이 지시 자체를 본문에 언급하지는 마세요.
- 작가 이야기: 생애와 활동 시기, 작가의 작품 세계에서 이 작품이 갖는 위치.
- 왜 지금 읽을 만한가: 문학사적 의의, 수상 이력, 후대에 미친 영향.

⚠️ 근거 규칙
- 위 [작품 기본 정보]와 위키백과 발췌에 실제로 나오는 사실만 재구성해 쓰세요. 연도·수상·인물
  관계·판매량·영화화 등 근거에 없는 세부사항은 지어내지 마세요. 근거가 다루지 않는 항목은
  언급하지 말고 넘어가세요.
- "위키백과에 따르면", "근거 자료에 따르면", "언론 보도에 따르면"처럼 자료의 존재나 모호한
  출처를 본문에서 언급하지 마세요.
- 매체명·코너명(NewsFinal, 프론티어 미디어, 문학작품 이야기)을 본문에 쓰지 말고 바로
  작품 이야기로 시작하세요.
- 근거 문장을 그대로 옮기지 말고 반드시 자기 문장으로 새로 쓰세요.
- 작품 본문 인용은 한 문장 이내로 한 번까지만.
{title_rule}
- 이 작품 말고 본문에 언급하는 다른 책의 제목은 [작품 기본 정보]의 "국내 출간작(정식 한국어판
  제목)"에 있으면 그 제목을 그대로 쓰세요(위키백과 발췌의 한국어 표기와 다르면 이쪽이 우선).
  거기 없으면 원제 그대로 쓰세요 — 한국어 제목을 새로 지어내지 마세요.

[문체 규칙]
- 본문은 4~6개 문단(각 문단은 빈 줄로 구분, 문단당 2~3문장).
- 모든 문장을 "-다"로 종결하세요("-습니다" 금지). 인용구 자체는 예외.
- 마크다운, 헤더, 홍보 문구 금지. 논평·칼럼 문체 대신 사실 서술형으로.
- 책 제목은 『』, 단편·시 제목은 「」로 감싸세요.
- 외국어권 인명·지명은 한글 음차로 쓰고 첫 등장 때 1회만 괄호로 원어를 병기하세요. 한국 인물은
  병기하지 않습니다. 다시 언급할 땐 이름만 쓰세요.
- 숫자에 3자리 콤마(,)를 쓰지 마세요.

출력 형식:
TITLE: (예: "제인 오스틴의 『오만과 편견』, 200년 넘게 읽히는 연애소설의 원형")
BODY: (본문)"""


def unverified_ko_titles(body: str, w: dict, ko_source: str) -> list:
    """본문 『…』 중 한글 제목인데 확인된 출처(정식 제목·작가 국내 출간작·한국어 근거자료)에 없는 것.
    2026-09-27 dry-run 실사고: 양솽쯔의 다른 작품을 『꽃 피는 시절』 등으로 Gemini가 번역해 붙였다 —
    "지어내지 마세요" 규칙을 줘도 어겨서 코드로 검사한다."""
    allowed = {re.sub(r"\s+", "", t) for t in [verified_title_ko(w) or ""] + (w.get("author_korean_titles") or []) if t}
    src = re.sub(r"\s+", "", ko_source or "")
    bad = []
    for t in re.findall(r"『([^』]+)』", body):
        k = re.sub(r"\s+", "", t)
        if _HANGUL.search(t) and k not in allowed and k not in src and t not in bad:
            bad.append(t)
    return bad


UNTRANSLATED_NOTE = "(국내 번역판 제목 미확인)"
_NOTE_RE = re.compile(r"\s*\(국내 번역판 제목 미확인\)")


def fix_untranslated_note(w: dict, title: str, body: str) -> tuple[str, str]:
    """"국내 번역판 제목 미확인" 표기를 코드로 확정: 기사 제목엔 절대 안 넣고, 번역 제목이
    검증 안 된 작품이면 본문 첫 원제 뒤에 딱 한 번. 2026-09-27 dry-run에서 "제목엔 넣지
    마세요" 지시를 Gemini가 무시하고 제목에 넣어 — 프롬프트 부탁 대신 코드로 강제."""
    title = _NOTE_RE.sub("", title).strip()
    body = _NOTE_RE.sub("", body)
    if not verified_title_ko(w):
        orig = w.get("original_title") or w.get("title_en") or ""
        marker = f"『{orig}』"
        if orig and marker in body:
            body = body.replace(marker, marker + UNTRANSLATED_NOTE, 1)
    return title, body


def generate(w: dict, grounding: str, ko_source: str) -> tuple[str, str] | None:
    hooks = timeliness_hooks(w)
    if hooks:
        print(f"  → 시의성 포인트: {hooks}")
    prompt = build_prompt(w, grounding, hooks)
    content = call_gemini(prompt)
    if content and has_column_style(content):
        content = call_gemini(prompt + "\n\n[재작성 지시] 논평/칼럼 문체가 섞였습니다. 사실 서술형으로 다시 쓰세요.") or content
    if not content:
        return None

    # 사실 검사 2겹: Gemini 고유명사 검사 + 계열이 다른 NVIDIA의 "자료에 근거 없는 문장" 대조.
    # 지적을 모아 한 번에 재생성하고, 재생성본도 NVIDIA가 여전히 문제 삼으면 이 작품은 버린다
    # (사람 검토 없이 바로 발행되는 기사라 explainer_writer보다 엄격하게).
    facts = f"{info_block(w)}\n\n{hooks_block(hooks)}\n\n{grounding}"
    # fabrication_guard의 "[위키 미확인]" 줄(이름이 위키 문서 제목으로 존재하는지)은 문학 기사에선
    # 소설 속 인물명·미번역 작품명·확인된 한국어 제목까지 전부 걸려 거의 항상 오탐이다 — 2026-09-27
    # dry-run에서 이 오탐 때문에 재생성하며 확인된 한국어 제목 『50번째 추첨의 날』이 원제로 되돌아갔다.
    # Gemini의 [이름]/[수식어] 판단과 NVIDIA 대조(실제 오류를 잡은 쪽)만 쓴다.
    # 원본 자료로는 위키 발췌만이 아니라 확정 정보·시의성 포인트까지 넘긴다 — 빼먹으면 확인된
    # 한국어 제목·영화 제목을 "원본에 없는 이름"으로 오판한다(2026-09-27 실사고).
    fab = "\n".join(line for line in (_fg_verify_no_fabricated_names(facts, content, call_gemini) or "").splitlines()
                    if not line.startswith("[위키 미확인]")).strip()
    issues = [x for x in (fab, unsupported_claims(content, facts)) if x]
    if issues:
        joined = "\n".join(issues)
        print(f"  ⚠️ 근거와 다른 내용 감지 → 재생성:\n    {joined[:300]}")
        content = call_gemini(prompt + f"\n\n[재작성 지시] 아래 지적된 부분이 근거와 다르거나 근거에 없습니다:\n"
                                       f"{joined}\n근거에 실제로 나온 내용만 쓰세요. [작품 기본 정보]의 작품 제목 "
                                       f"표기와 [시의성 포인트]는 그대로 유지하세요. 인명·지명·기관명은 근거의 원어를 "
                                       f"정확히 한글로 음차하고 첫 등장 때만 괄호로 원어를 병기하는 규칙을 그대로 "
                                       f"지키세요(영어 철자를 본문에 그대로 두지 마세요).") or content
        remaining = unsupported_claims(content, facts)
        if remaining:
            print(f"  ⛔ 재생성 후에도 근거 없는 문장 남음 → 이 작품은 건너뜀:\n    {remaining[:300]}")
            return None

    bad_titles = unverified_ko_titles(content, w, ko_source)
    if bad_titles:
        print(f"  ⚠️ 확인 안 된 한국어 제목 {bad_titles} → 재생성")
        content = call_gemini(prompt + f"\n\n[재작성 지시] 다음 한국어 제목은 국내 정식 제목으로 확인되지 않았습니다: "
                                       f"{', '.join(f'『{t}』' for t in bad_titles)}. 원제로 쓰거나 언급하지 마세요. "
                                       "[작품 기본 정보]의 작품 제목과 [시의성 포인트]는 그대로 유지하세요.") or content
        bad_titles = unverified_ko_titles(content, w, ko_source)
        if bad_titles:
            print(f"  ⛔ 재생성 후에도 확인 안 된 한국어 제목 {bad_titles} → 이 작품은 건너뜀")
            return None

    copied = copied_span(content, ko_source)
    if copied:
        print(f"  ⚠️ 근거 문장 복사 감지(「{copied}」) → 재생성")
        content = call_gemini(prompt + f"\n\n[재작성 지시] 다음 구간을 근거 자료에서 그대로 베꼈습니다: "
                                       f"「{copied}」. 모든 문장을 자기 표현으로 새로 쓰세요.") or content
        copied = copied_span(content, ko_source)
        if copied:
            print(f"  ⛔ 재생성 후에도 복사 구간 남음(「{copied}」) → 이 작품은 건너뜀")
            return None

    if has_polite_ending(content):
        content = to_plain_style(content)
    title, body = parse_article_output(content)
    if not title or not body:
        return None
    title, body = fix_untranslated_note(w, title, body)
    body = ensure_paragraphs(body)
    if len(body) < MIN_BODY_LEN:
        print(f"  ⚠️ 본문 너무 짧음({len(body)}자)")
        return None
    return title.strip().strip('"'), body


def insert_article(w: dict, title: str, body: str, image_url: str, image_credit: str) -> int:
    if detect_script_leak(title, body) or is_placeholder_response(title, body):
        print(f"  ⛔ 저장 차단(문자 혼입/거부 응답): {title[:60]}")
        return -1
    unwrapped = unwrap_json_body(body)
    if unwrapped is not None:
        if not unwrapped:
            return -1
        body = unwrapped
    country, flag, region = country_info(w.get("author_country_ko"))
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    work_name = verified_title_ko(w) or w.get("title_en") or w["qid"]
    return insert_final_article({
        "title_en": w.get("title_en") or work_name, "title_ko": title,
        "summary_en": "", "summary_ko": body,
        "url": f"{URL_PREFIX}{w['qid']}",
        "source": "NewsFinal", "category": "문화·예술", "subcategory": SUBCATEGORY,
        "region": region, "country": country, "country_flag": flag, "countries": [country] if country else [],
        "image_url": image_url, "image_credit": image_credit, "score": 1,
        "created_at": now_str, "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": f"문학작품 이야기 — {work_name}"}],
        "source_data": {"wikidata": w["qid"], "kowiki": w.get("kowiki"), "enwiki": w.get("enwiki"),
                        "source": w.get("source")},
        "sent_telegram": 0, "is_published": True,
    })


def main():
    dry = "--dry-run" in sys.argv
    force_qid = sys.argv[sys.argv.index("--qid") + 1] if "--qid" in sys.argv else None
    only_bucket = sys.argv[sys.argv.index("--bucket") + 1] if "--bucket" in sys.argv else None
    print(f"\n[literature_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")
    if not GEMINI_API_KEYS:
        print("  [SKIP] GEMINI_API_KEY 없음")
        return

    works = load_works()
    published = published_literature()
    if published is None:
        print("  [SKIP] 발행 이력 조회 실패 — 중복 발행 위험이 있어 이번 실행은 건너뜀")
        return
    today = now_kst().strftime("%Y-%m-%d")
    if not dry and not force_qid and any(created.startswith(today) for _, created in published):
        print(f"  → 오늘({today}) 문학작품 이야기 이미 발행됨 → 스킵")
        return
    done = {url for url, _ in published}

    if force_qid:
        candidates = [w for w in works if w["qid"] == force_qid]
        if not candidates:  # 목록 밖 작품(예: 실시간 신간)도 테스트할 수 있게 즉석에서 레코드 생성
            from harvest_literature import build_records
            candidates = build_records({force_qid: {"src": "manual", "notes": []}})
    else:
        candidates = pick_candidates(works, done, now_kst().date().toordinal(), only_bucket)
    if not candidates:
        print("  [SKIP] 쓸 작품이 없음(목록 소진 — harvest_literature.py로 목록을 늘릴 것)")
        return

    for w in candidates:
        print(f"  → 작품: {w.get('title_ko') or w.get('title_en')} / {w.get('author_ko') or w.get('author_en')} "
              f"({w['qid']}, {w.get('source')})")
        confirm_title_ko(w)
        grounding, ko_source = build_grounding(w)
        if len(grounding) < MIN_GROUNDING_LEN:
            print(f"  ⚠️ 근거자료 부족({len(grounding)}자) → 다음 작품")
            continue
        result = generate(w, grounding, ko_source)
        if not result:
            print("  ⚠️ 생성 실패 → 다음 작품")
            continue
        title, body = result
        image_url, credit = fetch_image(w, store=not dry)
        if dry:
            print(f"\n[이미지] {image_url}\n[크레딧] {credit}\n\n=== {title} ===\n{body}\n({len(body)}자)")
            return
        art_id = insert_article(w, title, body, image_url, credit)
        print(f"  {'✓' if art_id > 0 else '✗'} 발행: id={art_id} — {title}")
        return
    print("  [ERROR] 후보 작품 모두 실패")


if __name__ == "__main__":
    main()
