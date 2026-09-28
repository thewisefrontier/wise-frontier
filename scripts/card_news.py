# -*- coding: utf-8 -*-
"""
scripts/card_news.py
--------------------
발행된 기사 한 편 → 인스타그램 카드뉴스(캐러셀) JPEG 슬라이드 목록. 2026-09-28 시안.

원칙 — LLM을 쓰지 않는다: 카드 문구는 기사 본문의 문장을 **그대로** 골라 담는다(요약·재작성 금지).
숫자·이름이 기사와 달라질 여지를 없애기 위함. 슬라이드 = 표지 1 + 본문 최대 MAX_BODY + 마무리 1(인스타 캐러셀 상한 10장).

실행: python scripts/card_news.py <article.json> [출력폴더]   (시안 확인용)
"""
import html
import json
import os
import re
import sys

W, H = 1080, 1350  # 인스타 세로 피드 4:5
MAX_BODY = 4
BODY_CHARS = 130   # 본문 카드 한 장에 담을 글자 상한(문장 단위로 자름)
SITE = "NewsFinal"

# GitHub Actions(ubuntu)엔 한글 폰트가 없다 → 워크플로에 fonts-noto-cjk 설치 필요.
FONT = "'Noto Sans KR','Noto Sans CJK KR','Malgun Gothic','Apple SD Gothic Neo',sans-serif"

CSS = f"""
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:{W}px;height:{H}px;font-family:{FONT};color:#fff;background:#0e0e12;overflow:hidden;position:relative}}
.bg{{position:absolute;inset:0;background-size:cover;background-position:center}}
.pic{{position:absolute;left:0;right:0;top:0;height:640px;background-size:contain;background-repeat:no-repeat;background-position:center;background-color:rgba(0,0,0,.28)}}
.bar{{position:absolute;left:80px;top:672px;width:96px;height:8px;border-radius:4px;background:#e8b64c}}
.shade{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.05) 30%,rgba(0,0,0,.88) 100%)}}
.chip{{position:absolute;top:64px;left:64px;padding:12px 26px;border-radius:999px;background:#e8b64c;color:#1a1a1a;font-size:30px;font-weight:700}}
.title{{position:absolute;left:64px;right:64px;bottom:150px;font-size:76px;font-weight:800;line-height:1.28;word-break:keep-all;text-shadow:0 4px 24px rgba(0,0,0,.6)}}
.foot{{position:absolute;left:64px;right:64px;bottom:56px;display:flex;justify-content:space-between;font-size:28px;opacity:.85}}
.body{{position:absolute;left:80px;right:80px;top:712px;font-size:44px;line-height:1.62;font-weight:500;word-break:keep-all}}
.num{{position:absolute;bottom:56px;right:64px;font-size:28px;opacity:.7}}
.credit{{position:absolute;left:64px;right:64px;bottom:100px;font-size:22px;opacity:.6;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.end{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:28px;text-align:center}}
.end .logo{{font-size:96px;font-weight:800;color:#e8b64c}}
.end .sub{{font-size:40px;opacity:.9;line-height:1.5}}
"""


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def pick_body_cards(summary: str) -> list[str]:
    """문단마다 첫 문장부터 BODY_CHARS 안에서 문장 단위로 담는다(잘라 붙이지 않고 문장 통째로)."""
    cards = []
    for para in [p for p in re.split(r"\n\s*\n", summary) if p.strip()]:
        out = ""
        for s in _sentences(para):
            if out and len(out) + 1 + len(s) > BODY_CHARS:
                break
            if not out and len(s) > BODY_CHARS * 1.5:
                break  # 한 문장이 지나치게 길면 이 문단은 건너뜀
            out = f"{out} {s}".strip()
        if out:
            cards.append(out)
        if len(cards) >= MAX_BODY:
            break
    return cards


def artist_works(artist_en: str, n: int, exclude_url: str = "") -> list[dict]:
    """위키미디어 커먼즈 'Paintings by {작가}' 분류(하위 포함)에서 퍼블릭도메인 그림 n점. 없으면 [].
    표지와 같은 작품·상세컷·스케치는 뺀다. 메트로폴리탄은 모네 등이 공개 표시가 아니라 쓰지 않음(실측)."""
    if not artist_en:
        return []
    import requests
    from urllib.parse import unquote
    hdr = {"User-Agent": "NewsFinalBot/1.0 (https://wise-frontier.pages.dev)"}
    api = "https://commons.wikimedia.org/w/api.php"
    try:
        hits = requests.get(api, headers=hdr, timeout=40, params={
            "action": "query", "list": "search", "srnamespace": 6, "srlimit": 50, "format": "json",
            "srsearch": f'deepcategory:"Paintings by {artist_en}" filetype:bitmap'}).json()["query"]["search"]
        if not hits:
            return []
        pages = requests.get(api, headers=hdr, timeout=40, params={
            "action": "query", "format": "json", "prop": "imageinfo", "iiprop": "url|size|extmetadata",
            "iiurlwidth": 1080, "titles": "|".join(h["title"] for h in hits)}).json()["query"]["pages"].values()
    except Exception as e:
        print(f"  ⚠️ 작품 조회 실패: {e}")
        return []
    ex = unquote(exclude_url).replace("_", " ").lower()
    out, seen = [], set()
    for pg in sorted(pages, key=lambda x: x.get("index", 0)):
        ii = (pg.get("imageinfo") or [{}])[0]
        title = pg["title"][5:].rsplit(".", 1)[0]
        lic = (ii.get("extmetadata", {}).get("LicenseShortName", {}).get("value") or "").lower()
        key = re.sub(r"[^a-z0-9]", "", title.lower())[:24]
        if ("public domain" not in lic and "cc0" not in lic) or ii.get("width", 0) < 1200 or key in seen:
            continue
        if re.search(r"detail|crop|stamp|sketch|study|drawing|frame|cover|poster", title, re.I) or title.lower() in ex:
            continue
        seen.add(key)
        out.append({"url": ii["thumburl"], "title": title})
        if len(out) >= n:
            break
    return out


def guess_artist_en(article: dict) -> str:
    """기사에 artist_en이 없으면 제목의 한글 작가명으로 art_weekly_writer.ARTWORKS에서 찾는다."""
    if article.get("artist_en"):
        return article["artist_en"]
    if article.get("subcategory") != "고전명화이야기":
        return ""
    os.environ.setdefault("SUPABASE_URL", "http://fake")
    os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
    sys.path.insert(0, os.path.dirname(__file__))
    from art_weekly_writer import ARTWORKS
    return next((a["artist_en"] for a in ARTWORKS if a["artist_ko"] in article["title_ko"]), "")


def tone_gradient(img_url: str) -> str:
    """작품 평균색을 어둡게 눌러 만든 그라데이션(흐린 사진 배경은 탁해 보여 폐기). 실패하면 무채색."""
    default = "linear-gradient(160deg,#2a2a34,#0e0e12)"
    if not img_url:
        return default
    try:
        import colorsys
        import io
        import requests
        from PIL import Image
        r = requests.get(img_url, timeout=20, headers={"User-Agent": "NewsFinalBot/1.0"})
        r.raise_for_status()
        px = Image.open(io.BytesIO(r.content)).convert("RGB").resize((1, 1), Image.LANCZOS).getpixel((0, 0))
        h, sat, _ = colorsys.rgb_to_hsv(*[c / 255 for c in px])
        top = "#%02x%02x%02x" % tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h, min(sat, .55), .30))
        bot = "#%02x%02x%02x" % tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h, min(sat, .45), .09))
        return f"linear-gradient(160deg,{top},{bot})"
    except Exception as e:
        print(f"  ⚠️ 배경색 추출 실패(무채색 사용): {e}")
        return default


def _page(inner: str) -> str:
    return f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{inner}</body></html>"


def build_slides_html(article: dict) -> list[str]:
    e = html.escape
    img = article.get("image_url") or ""
    bg = f"background-image:url('{html.escape(img, quote=True)}')" if img else "background:#23232b"
    chip = e(article.get("subcategory") or article.get("category") or "")
    grad = f"background:{tone_gradient(img)}"
    cards = pick_body_cards(article.get("summary_ko", ""))
    total = len(cards) + 2

    slides = [_page(
        f"<div class='bg' style=\"{bg}\"></div><div class='shade'></div>"
        f"<div class='chip'>{chip}</div><div class='title'>{e(article['title_ko'])}</div>"
        f"<div class='foot'><span>{SITE}</span><span>넘겨서 보기 →</span></div>")]
    works = artist_works(guess_artist_en(article), len(cards), img)
    for i, c in enumerate(cards, 2):
        w = works[i - 2] if i - 2 < len(works) else None
        g = f"background:{tone_gradient(w['url'])}" if w else grad
        pic = (f"<div class='pic' style=\"background-image:url('{html.escape(w['url'], quote=True)}')\"></div>"
               f"<div class='bar'></div>" if w else "<div class='bar' style='top:300px'></div>")
        credit = (f"<div class='credit'>{e(w['title'][:72])} · Wikimedia Commons, Public domain</div>" if w else "")
        top = "top:712px" if w else "top:350px;font-size:56px"
        slides.append(_page(
            f"<div class='bg' style=\"{g}\"></div>{pic}"
            f"<div class='body' style='{top}'>{e(c)}</div>{credit}"
            f"<div class='foot'><span>{SITE}</span></div><div class='num'>{i}/{total}</div>"))
    slides.append(_page(
        f"<div class='bg' style=\"{grad}\"></div><div class='end'><div class='logo'>{SITE}</div>"
        f"<div class='sub'>전체 기사는 프로필 링크에서<br>확인하세요</div></div>"))
    return slides


def render_jpegs(article: dict) -> list[bytes]:
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": W, "height": H})
        for doc in build_slides_html(article):
            page.set_content(doc, wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            out.append(page.screenshot(type="jpeg", quality=90))  # 인스타 발행 API는 JPEG만 허용
        b.close()
    return out


if __name__ == "__main__":
    art = json.load(open(sys.argv[1], encoding="utf-8"))
    d = sys.argv[2] if len(sys.argv) > 2 else "."
    os.makedirs(d, exist_ok=True)
    for i, jpg in enumerate(render_jpegs(art), 1):
        open(os.path.join(d, f"card_{i}.jpg"), "wb").write(jpg)
        print(f"card_{i}.jpg {len(jpg):,}B")
