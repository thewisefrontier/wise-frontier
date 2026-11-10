# -*- coding: utf-8 -*-
"""위키미디어 인물 사진 선택 회귀 테스트(2026-11-11, id=323060 — 카타르 총리 기사에 일본 총리 면담 사진).
실행: python scripts/test_wiki_person_photo.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import article_image as a  # noqa: E402

OK_META = {"License": {"value": "cc-by-4.0"}, "AttributionRequired": {"value": "false"}}


def page(pid, index, title):
    return {"pageid": pid, "index": index, "title": "File:" + title,
            "imageinfo": [{"mime": "image/jpeg", "width": 800, "height": 600, "url": "u/" + title,
                           "thumburl": "t/" + title, "extmetadata": OK_META}]}


class R:
    status_code = 200
    def __init__(self, pages): self.pages = pages
    def json(self): return {"query": {"pages": {str(p["pageid"]): p for p in self.pages}}}


def pick(query, pages, **kw):
    a.requests.get = lambda *x, **k: R(pages)
    url, _ = a.fetch_wikimedia_image(query, **kw)
    return url


# 실제 커먼즈 검색 결과(2026-11-11) — pageid가 작은 면담 사진이 dict 앞에 와도 검색 순위·단독 사진을 따른다
qatar = [page(1, 7, "Fumio Kishida and Mohammed bin Abdulrahman Al Thani at the Japanese Kantei (2).jpg"),
         page(2, 1, "Minister of External Affairs, Dr. Subrahmanyam Jaishankar & Prime Minister of Qatar.jpg"),
         page(3, 4, "Mohammed bin Abdulrahman Al Thani, Prime Minister of Qatar.jpg"),
         page(4, 3, "Mohammed bin Abdulrahman al-Thani (cropped).jpg")]
assert pick("Mohammed bin Abdulrahman Al Thani", qatar) == "t/Mohammed bin Abdulrahman al-Thani (cropped).jpg"
# 단독·잘라낸 사진이 없으면 검색 순위대로(면담 사진은 끝까지 제외)
assert pick("Mohammed bin Abdulrahman Al Thani", qatar[:3]) == "t/Mohammed bin Abdulrahman Al Thani, Prime Minister of Qatar.jpg"
# 흉내꾼 사진 제외
trump = [page(5, 2, "Kim Jong-un and Donald Trump impersonators.jpg"), page(6, 3, "Donald Trump swearing in ceremony.jpg")]
assert pick("Donald Trump", trump) == "t/Donald Trump swearing in ceremony.jpg"
# 검색어 자체에 and가 있으면(국가명 등) 거르지 않는다
tt = [page(7, 1, "Trinidad and Tobago Port of Spain.jpg")]
assert pick("Trinidad and Tobago", tt) == "t/Trinidad and Tobago Port of Spain.jpg"
# 명화 소개는 작품 제목의 and를 거르지 않는다
art = [page(8, 1, "Water-Lily Pond and Weeping Willow.jpg")]
assert pick("Water-Lily Pond", art, allow_artwork=True) == "t/Water-Lily Pond and Weeping Willow.jpg"
print("ok")

# 차단어는 단어 단위: "Photographs by ..." 분류는 그래프가 아니다 / 진짜 차트·그림 분류는 계속 막는다
assert not a._has_avoid_word("photographs by the u.s. department of state|pd us dos", a._WIKI_TITLE_AVOID_WORDS)
assert a._has_avoid_word("cholera deaths in great britain", a._WIKI_TITLE_AVOID_WORDS)
assert a._has_avoid_word("election maps of serbia", a._WIKI_TITLE_AVOID_WORDS)
assert a._has_avoid_word("1831 cartoons|cholera in art", a._WIKI_CATEGORY_AVOID_WORDS)
assert not a._has_avoid_word("maple leaf flag", a._WIKI_TITLE_AVOID_WORDS)
print("단어 단위 차단 ok")
