# -*- coding: utf-8 -*-
"""art_weekly_writer.py 회귀 테스트(2026-09-27, 위키백과 근거자료 주입).
위키백과 실 네트워크 호출을 사용한다(다른 writer 테스트와 달리 순수 로직만으로는
"엉뚱한 문서가 매치되는" 실사고를 재현할 수 없어 의도적으로 라이브 검증).
실행: python scripts/test_art_weekly_writer.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import art_weekly_writer as w  # noqa: E402

# 2026-09-27 실사고: "Shin Yun-bok"(artist_en, 로마자 표기 불일치) 검색이 실제
# 인물 문서가 아니라 완전히 무관한 TV드라마 "Painter of the Wind" 문서를
# 최상위로 반환했다 — 유사도 가드가 이 오탐을 걸러내는지 확인.
bad_title = w._wiki_search_title("Shin Yun-bok painter", "en")
assert bad_title != "Painter of the Wind", f"관련성 낮은 오매치가 걸러지지 않음: {bad_title!r}"

# 정상 매치(모네)는 그대로 통과해야 한다.
good_title = w._wiki_search_title("Claude Monet", "en")
assert good_title and "Monet" in good_title, f"정상 매치가 걸러짐: {good_title!r}"

# 근거자료 fetch가 실제 내용을 채워오는지(작품명만으론 동명 식물 문서가 잡히던
# 실사고 — 작가명을 붙여 검색하도록 수정됨).
grounding = w.fetch_wikipedia_grounding("클로드 모네 수련", "Water Lilies Monet")
assert len(grounding) > 100, "모네 수련 근거자료를 못 가져옴"
assert "모네" in grounding or "Monet" in grounding

# 프롬프트에 근거자료 블록과 날조 금지 지시가 실제로 들어가는지 확인.
artwork = w.ARTWORKS[0]
prompt = w.build_article_prompt(artwork, grounding)
assert "[근거 자료" in prompt
assert "지어내지 마세요" in prompt

# 작자 미상 공예품은 일일 후보에서 제외(2026-09-28 터코이즈 볼 사고).
from datetime import date as _d
assert w.is_unknown_artist({"artist_ko": "작자 미상", "artist_en": "Unknown"})
assert not w.is_unknown_artist({"artist_ko": "클로드 모네", "artist_en": "Claude Monet"})
assert all(not w.is_unknown_artist(w.get_daily_artwork(_d(2026, 1, 1).replace(month=1 + i % 12, day=1 + i % 28))[0]) for i in range(60))

assert w.is_not_painting({"museum_grounding": "분류: Ceramics-Pottery 소장 경위: x"})
assert w.is_not_painting({"museum_grounding": "재질/기법: 종이 분류: Codices"})
assert not w.is_not_painting({"museum_grounding": "분류: Paintings"}) and not w.is_not_painting({})
assert all(not w.is_not_painting(w.get_daily_artwork(_d(2026, 1 + i % 12, 1 + i % 28))[0]) for i in range(80))

print("ok")

# 같은 작가를 이미 소개했으면 프롬프트가 작가 소개를 줄이라고 지시한다(2026-09-28).
_aw = {"title_ko": "수련", "title_en": "Water Lilies", "artist_ko": "클로드 모네", "artist_en": "Claude Monet",
       "year_label": "1906", "country": "프랑스"}
assert "이미 소개한 적이 있습니다" in w.build_article_prompt(_aw, "근거", ["인상, 해돋이"])
assert "이미 소개한 적이 있습니다" not in w.build_article_prompt(_aw, "근거")
print("ok")


# iter_daily_candidates()가 get_daily_artwork()와 같은 첫 후보를 내놓는지(2026-09-30 리팩터링 회귀)
for i in range(30):
    d = _d(2026, 1 + i % 12, 1 + i % 28)
    assert next(w.iter_daily_candidates(d))["title_en"] == w.get_daily_artwork(d)[0]["title_en"]
print("iter_daily_candidates 회귀 테스트 ok")


# done 집합에 있는 작품은 순환에서 건너뛴다(2026-09-30 사용자 지시: "반복은 안 돼")
d0 = _d(2026, 6, 15)
first = w.get_daily_artwork(d0)[0]
skipped = w.get_daily_artwork(d0, done={first["title_en"]})[0]
assert skipped["title_en"] != first["title_en"]
# 목록 전체가 done이면(전부 발행 완료 상태) 더 내놓을 후보가 없다 — 조용히 예전 작품을 재사용하면 안 됨
all_titles = {a["title_en"] for a in w.ARTWORKS}
assert list(w.iter_daily_candidates(d0, done=all_titles)) == []
print("done 집합 순환 제외 테스트 ok")
