"""
다른 나라 지명이 박힌 Pixabay 사진 차단 회귀 테스트 (2026-10-13 신설).
실사고 2건(id=308347 미국 날씨 ← 런던 웨스트민스터 궁, id=308571 미 연준 ← 프랑크푸르트 ECB)의
실제 태그로 재현한다. 네트워크 없이 돈다: python scripts/test_article_image_foreign_place.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from article_image import filter_foreign_place_hits, allowed_countries_from_text  # noqa: E402
from weather_report import _is_image_suitable  # noqa: E402

WESTMINSTER = {"id": 2892, "tags": "westminster palace, building, night, river, landmark, london, thames, uk"}
NYC = {"id": 4854718, "tags": "nyc, new york city, america, usa, city, skyline, united states, landmark"}
ECB = {"id": 5377160, "tags": "ecb, european central bank, money, bank, frankfurt, skyscraper, europe"}
GENERIC_BANK = {"id": 1, "tags": "bank, finance, money, building"}
CAPITOL_ODD = {"id": 2212135, "tags": "capitol, building, washington, state, usa, america, united, landmark"}

# 날씨: 미국 사진만 통과, 런던은 "landmark" 태그가 있어도 탈락, 국가 표시 없는 사진도 탈락
assert not _is_image_suitable(WESTMINSTER, "미국")
assert _is_image_suitable(NYC, "미국")
assert _is_image_suitable(CAPITOL_ODD, "미국")
assert not _is_image_suitable(GENERIC_BANK, "미국")
assert _is_image_suitable(WESTMINSTER, "영국")

# 일반 기사: 미 연준 기사에 ECB 건물 탈락, 지명 없는 일반 은행 사진은 통과
fed_text = "미 연준, 지역은행 규제 현대화 추진 미국 연방준비제도(Fed)가 13일 ..."
kept = filter_foreign_place_hits([ECB, GENERIC_BANK], allowed_countries_from_text(fed_text), "bank regulation")
assert kept == [GENERIC_BANK], kept

# 유럽 기사면 ECB 사진은 정답
ecb_text = "유럽중앙은행이 독일 프랑크푸르트에서 금리를 동결했다"
assert ECB in filter_foreign_place_hits([ECB], allowed_countries_from_text(ecb_text), "central bank")

# 검색어에 지명이 있으면 그 나라 사진 허용("new york stock exchange building")
assert NYC in filter_foreign_place_hits([NYC], set(), "new york stock exchange building")
assert NYC not in filter_foreign_place_hits([NYC], set(), "stock market chart")

print("test_article_image_foreign_place ok")
