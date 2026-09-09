"""
scripts/text_crawl.py
-------------------------
rss_fetcher.py의 crawl_full_text() 및 그 보조 함수를 그대로 복사해
독립 모듈로 뗀 것. rss_fetcher.py는 최상위 코드가 import 시점에 곧바로
실행되는 구조라(RSS 수집 루프 + DB 쓰기) 다른 스크립트가 직접
import할 수 없다 — 운영 중인 파이프라인을 건드리는 리스크 없이 같은
로직을 gdelt_fetcher.py 등 새 수집기에서도 쓰기 위해 복제했다.
rss_fetcher.py 쪽 원본을 고치면 이 파일도 맞춰 갱신할 것.

2026-09-08 도입 — 사용자 지시("GDELT 전역 뉴스 검색 API... 진행해")로
신설한 gdelt_fetcher.py에서 사용.
"""

import re
import unicodedata
from urllib.parse import urlparse

import requests

try:
    import trafilatura
except Exception:
    trafilatura = None

try:
    from googlenewsdecoder import gnewsdecoder
except Exception:
    gnewsdecoder = None


def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    text = re.sub(r'&#x[0-9a-fA-F]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)
    replacements = {
        '\u2019': "'", '\u2018': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
        '\xa0': ' '
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# 원문 크롤링 불필요 사이트 (이미 RSS에 전문 제공)
SKIP_CRAWL_DOMAINS = {
    "allafrica.com", "africa-newsroom.com", "afdb.org",
    "imf.org", "worldbank.org", "afro.who.int", "au.int",
    "unctad.org", "ifc.org", "asean.org", "adb.org",
}

_CREDIT_MARK_RE = re.compile(r'&copy;|\(c\)\s|©', re.IGNORECASE)

_CAPTION_PREFIX_RE = re.compile(
    r'^\s*(?:Photos?|Images?|Pictured?|Caption|Credits?|Cover image|File photo|Handout|'
    r'Foto|Légende|Legende)\s*[:\-\u2013\u2014|]',
    re.IGNORECASE,
)

_SENT_END_RE = re.compile(r'[.!?][\"\'\)\]]?\s')

_CREDIT_TAIL_RE = re.compile(
    r'^[\s:\-\u2013\u2014|]*'
    r'(?:[A-Z\u00c0-\u00dc][\w.\u2019\'\-]*|\d{1,4}(?:\s*[-\u2013]\s*\d{2,4})?|and|de|du|des|ve|par|via)'
    r'(?:[\s,\-\u2013\u2014/&]+'
    r'(?:[A-Z\u00c0-\u00dc][\w.\u2019\'\-]*|\d{1,4}(?:\s*[-\u2013]\s*\d{2,4})?|and|de|du|des|ve|par|via)'
    r'){0,5}'
)

CAPTION_LEAD_MAX = 220
CREDIT_TAIL_MAX = 50
CAPTION_KEEP_MIN = 100


def strip_photo_credits(text: str) -> str:
    """단락에서 사진 캡션·저작권 크레딧을 제거한다."""
    if not text:
        return ""
    if _CAPTION_PREFIX_RE.match(text):
        return ""

    out = text
    for _ in range(4):
        m = _CREDIT_MARK_RE.search(out)
        if not m:
            break
        start, end = m.start(), m.end()

        bounds = [b.end() for b in _SENT_END_RE.finditer(out[:start])]
        if bounds and start - bounds[-1] <= 3:
            cut_from = bounds[-2] if len(bounds) >= 2 else 0
        else:
            cut_from = bounds[-1] if bounds else 0
        if start - cut_from > CAPTION_LEAD_MAX:
            cut_from = start

        tm = _CREDIT_TAIL_RE.match(out[end:end + CREDIT_TAIL_MAX])
        cut_to = end + (tm.end() if tm else 0)

        out = (out[:cut_from] + " " + out[cut_to:]).strip()

    out = re.sub(r'\s+', ' ', out).strip()
    if len(out) < CAPTION_KEEP_MIN:
        return ""
    return out


def resolve_google_news_url(url: str) -> str:
    """news.google.com/rss/articles/... 리다이렉트 URL을 실제 게시처 URL로 해독.

    2026-09-09까지는 crawl_full_text() 내부에서만(본문 크롤링 시점) 쓰였고
    private(_)로 숨겨져 있었다 — 그래서 domestic_kr_fetcher.py가 소스
    태그·저장 URL을 결정할 때는 이 해독을 안 거친 원본 구글뉴스 리다이렉트
    링크를 그대로 썼다(실사고: 다국어 채널 기사의 출처가 실제 매체 대신
    "news.google.com"으로 표시됨, 사용자 지적: "Source: news.google.com...
    우리가 언제 이런거 붙였지?"). rss_fetcher.py 등 본편은 애초에 이런
    문제가 없었다 — public으로 바꿔 수집기들이 저장 시점에도 쓸 수 있게 한다.
    """
    if gnewsdecoder is None or "news.google.com" not in url:
        return url
    try:
        result = gnewsdecoder(url, interval=1)
        if result and result.get("status") and result.get("decoded_url"):
            return result["decoded_url"]
    except Exception:
        pass
    return url


# 기존 호출부(crawl_full_text 내부) 하위호환용 별칭.
_resolve_google_news_url = resolve_google_news_url


def _is_garbled(text: str, sample: int = 2000) -> bool:
    """trafilatura.fetch_url()이 압축 해제를 잘못 처리해 바이너리를
    문자열로 그대로 반환하는 경우를 제어문자·깨진문자 비율로 감지."""
    if not text:
        return True
    s = text[:sample]
    bad = sum(1 for c in s if c == "�" or (unicodedata.category(c) == "Cc" and c not in "\n\r\t"))
    return (bad / max(len(s), 1)) > 0.05


def _crawl_full_text_regex_fallback(url: str, timeout: int) -> str:
    """trafilatura 미설치 시(또는 둘 다 실패한 최후 수단)용 원래 구현."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code != 200:
            return ""

        html = res.text
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

        article_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
        if article_match:
            text_html = article_match.group(1)
        else:
            main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL | re.IGNORECASE)
            text_html = main_match.group(1) if main_match else html

        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', text_html, re.DOTALL | re.IGNORECASE)
        texts = []
        for p in paragraphs:
            t = re.sub(r'<[^>]+>', '', p).strip()
            t = re.sub(r'\s+', ' ', t)
            if len(t) <= 50:
                continue
            t = strip_photo_credits(t)
            if t:
                texts.append(t)

        full_text = ' '.join(texts)
        return clean_text(full_text) if len(full_text) > 100 else ""
    except Exception:
        return ""


def _postprocess_trafilatura_text(text: str) -> str:
    if not text or len(text) <= 100:
        return ""
    paras = []
    for p in text.split("\n"):
        p = p.strip()
        if len(p) <= 50:
            continue
        p = strip_photo_credits(p)
        if p:
            paras.append(p)
    joined = " ".join(paras)
    return clean_text(joined) if len(joined) > 100 else ""


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def extract_og_image(url: str, timeout: int = 8) -> str:
    """페이지의 og:image/twitter:image 메타태그에서 대표 이미지 URL을 뽑는다.
    Gemini 호출 없는 순수 HTML 파싱 — domestic_kr_fetcher.py처럼 이미지
    검색용 Gemini 클라이언트가 없는 가벼운 수집기에서 쓴다(2026-09-09,
    사용자 지적: "사진도 없고" — DomesticKR 기사에 이미지가 전혀 없었음)."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)",
            "Accept": "text/html,application/xhtml+xml",
        }
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code != 200:
            return ""
        m = _OG_IMAGE_RE.search(res.text[:200000])
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def crawl_full_text(url: str, timeout: int = 10) -> str:
    """원문 URL에서 본문 텍스트 추출 (trafilatura 우선, regex 파서 최종 폴백)."""
    domain = urlparse(url).netloc.replace("www.", "")
    if domain in SKIP_CRAWL_DOMAINS:
        return ""

    url = _resolve_google_news_url(url)
    domain = urlparse(url).netloc.replace("www.", "")
    if domain in SKIP_CRAWL_DOMAINS:
        return ""

    if trafilatura is not None:
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded and not _is_garbled(downloaded):
                text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
                result = _postprocess_trafilatura_text(text or "")
                if result and not _is_garbled(result):
                    return result
        except Exception:
            pass
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
            res = requests.get(url, headers=headers, timeout=timeout)
            if res.status_code == 200:
                text = trafilatura.extract(res.text, include_comments=False, include_tables=False)
                result = _postprocess_trafilatura_text(text or "")
                if result:
                    return result
        except Exception:
            pass

    return _crawl_full_text_regex_fallback(url, timeout)
