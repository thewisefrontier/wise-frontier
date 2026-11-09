# 회귀: id=323929 아르헨티나 교황 방문 기사에 타지마할 사진 — 명소 태그로 다른 나라 사진을 거른다
from article_image import filter_foreign_place_hits, allowed_countries_from_text
allowed = allowed_countries_from_text("레오 14세 교황 아르헨티나 방문 우루과이 페루")
taj = {"id": 1, "tags": "taj mahal, architecture, monument, reflection"}
ok_ba = {"id": 2, "tags": "buenos aires, obelisk, city"}
generic = {"id": 3, "tags": "church, cathedral, religion"}
kept = filter_foreign_place_hits([taj, ok_ba, generic], allowed)
assert [h["id"] for h in kept] == [2, 3], kept
print("ok")
