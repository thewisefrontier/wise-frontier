# -*- coding: utf-8 -*-
"""fetch_wikimedia_image(allow_artwork=True) 회귀 테스트(2026-09-26).

art_weekly_writer.py(명화 소개 기사)가 공용 위키미디어 검색을 쓰는데,
카테고리 배제어에 "painting"이 있어 진짜 작품 사진이 전부 걸리고
"painting" 분류가 안 붙은 디테일 클로즈업만 통과하던 실사고(모네 "수련"
기사에 붓터치 클로즈업만 나옴) 재발 방지.

실행: python scripts/test_article_image_artwork_filter.py
"""
import os
import sys

os.environ.setdefault("PIXABAY_API_KEY", "")
sys.path.insert(0, os.path.dirname(__file__))
import article_image as ai  # noqa: E402


def _page(title, categories="", license_key="pd", width=1000, height=800):
    return {
        "imageinfo": [{
            "mime": "image/jpeg", "width": width, "height": height,
            "thumburl": f"https://example.org/{title}",
            "extmetadata": {
                "Categories": {"value": categories},
                "License": {"value": license_key},
                "AttributionRequired": {"value": "false"},
            },
        }],
        "title": f"File:{title}",
    }


class R:
    def __init__(self, pages):
        self._pages = pages
        self.status_code = 200
    def json(self):
        return {"query": {"pages": {str(i): p for i, p in enumerate(self._pages)}}}


# 진짜 작품 사진("Categories"에 Paintings by ... 포함)은 allow_artwork=False면 배제되고
# True면 통과한다.
pages = [_page("Water-Lily Pond and Weeping Willow.jpg", categories="Paintings by Claude Monet")]
ai.requests.get = lambda *a, **k: R(pages)
assert ai.fetch_wikimedia_image("Water Lilies Monet", allow_artwork=False) == (None, None)
url, credit = ai.fetch_wikimedia_image("Water Lilies Monet", allow_artwork=True)
assert url == "https://example.org/Water-Lily Pond and Weeping Willow.jpg", url

# "Detail of ..." 클로즈업은 allow_artwork=True여도 배제, 대신 다음 후보(진짜 작품)가 나온다.
pages = [
    _page("Detail of Water Lilies 01.jpg", categories="Paintings by Claude Monet"),
    _page("Water-Lily Pond and Weeping Willow.jpg", categories="Paintings by Claude Monet"),
]
ai.requests.get = lambda *a, **k: R(pages)
url, credit = ai.fetch_wikimedia_image("Water Lilies Monet", allow_artwork=True)
assert "Detail" not in url and "Water-Lily Pond" in url, url

# 풍자화/의인화는 allow_artwork=True여도 여전히 배제된다.
pages = [_page("1831 cholera cartoon.jpg", categories="1831 cartoons, Personifications of disease")]
ai.requests.get = lambda *a, **k: R(pages)
assert ai.fetch_wikimedia_image("cholera", allow_artwork=True) == (None, None)

print("ok")
