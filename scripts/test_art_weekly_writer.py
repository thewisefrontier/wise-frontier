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

print("ok")
