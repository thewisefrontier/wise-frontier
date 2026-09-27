# -*- coding: utf-8 -*-
"""literature_writer.py 회귀 테스트(2026-09-27). 실행: python scripts/test_literature_writer.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import literature_writer as w  # noqa: E402

# 복붙 감지: 근거자료 문장을 그대로 옮긴 구간(49자 이상)은 반드시 잡는다.
src = "오만과 편견은 1813년에 출간된 제인 오스틴의 소설로, 엘리자베스 베넷이 오만한 다아시와 만나며 겪는 갈등과 오해를 그린다."
copied = "소개하자면, " + src[:55] + " 이라는 작품이다."
assert w.copied_span(copied, src), "그대로 옮긴 구간을 못 잡음"
assert not w.copied_span("제인 오스틴이 1813년에 발표한 이 소설은 편견이 사랑을 가로막는 과정을 그린다.", src)

# 국가 정규화: 역사적 국가명도 현대 표기로.
assert w.country_info("그레이트브리튼 왕국") == ("영국", "🇬🇧", "europe")
assert w.country_info("러시아 제국")[0] == "러시아"
assert w.country_info("대한민국") == ("한국", "🇰🇷", "asia")
assert w.country_info(None) == ("", "", "global")

# 한국어 제목이 없는 작품은 제목을 지어내지 말라는 규칙이 프롬프트에 들어가야 한다.
base = {"qid": "Q1", "title_en": "Orbital", "original_title": "Orbital", "author_en": "Samantha Harvey",
        "author_birth": 1975, "author_country_ko": "영국", "pub_year": 2023, "notes": ["부커상(2024) 수상작"]}
p = w.build_prompt({**base, "title_ko": None, "author_ko": None}, "[작품]\n근거")
assert "국내 번역판 제목 미확인" in p and "지어내지 마세요" in p
p2 = w.build_prompt({**base, "title_ko": "오비탈", "author_ko": "서맨사 하비", "kowiki": "오비탈 (소설)"}, "[작품]\n근거")
assert "『오비탈』" in p2 and "미확인" not in p2
# 2026-09-27 실사고: 한국어 위키백과 문서 없이 위키데이터 라벨만 있는 제목은 공식 번역
# 제목이라는 보장이 없다 → 원제로 쓰고 "미확인" 표기.
p3 = w.build_prompt({**base, "title_ko": "50번째 추첨의 날", "author_ko": "수잔 콜린스"}, "[작품]\n근거")
assert "『50번째 추첨의 날』" not in p3 and "국내 번역판 제목 미확인" in p3
# 같은 라벨이라도 한국어 위키백과 본문에서 저자명과 함께 확인되면(confirm_title_ko) 쓴다 —
# 국내 출간작을 "미확인"이라고 쓴 실사고(사용자: "헝거 게임: 50번째 추첨의 날이잖아").
p4 = w.build_prompt({**base, "title_ko": "50번째 추첨의 날", "author_ko": "수잔 콜린스", "title_ko_confirmed": True}, "근거")
assert "『50번째 추첨의 날』" in p4 and "미확인" not in p4

# 표지 대조용 제목 정규화: 구두점·대소문자 차이는 같게, 합본 제목은 다르게.
assert w._norm_title("Sunrise on the Reaping") == w._norm_title("Sunrise On The Reaping!")
assert w._norm_title("The Hunger Games / Catching Fire / Sunrise on the Reaping") != w._norm_title("Sunrise on the Reaping")
assert "부커상(2024) 수상작" in p2

# "(국내 번역판 제목 미확인)"은 제목에서 빼고 본문 첫 원제 뒤에 딱 한 번(Gemini가 규칙 무시한 실사고).
t, b = w.fix_untranslated_note(
    {**base, "title_ko": None},
    "하비의 『Orbital』 (국내 번역판 제목 미확인), 우주정거장의 하루",
    "서맨사 하비의 『Orbital』은 2023년작이다. 『Orbital』(국내 번역판 제목 미확인)은 부커상을 받았다.")
assert "미확인" not in t
assert b.count("(국내 번역판 제목 미확인)") == 1 and b.startswith("서맨사 하비의 『Orbital』(국내 번역판 제목 미확인)은")
t2, b2 = w.fix_untranslated_note({**base, "title_ko": "오비탈", "kowiki": "오비탈"}, "『오비탈』", "『오비탈』은 ...")
assert "미확인" not in b2
assert "결말과 반전은 밝히지 마세요" in p2

# 작가의 국내 출간작 정식 제목이 확정값으로 들어가야 한다(《명금과 뱀의 발라드》 실사고).
pk = w.build_prompt({**base, "title_ko": None, "author_korean_titles": ["노래하는 새와 뱀의 발라드", "모킹제이"]}, "근거")
assert "『노래하는 새와 뱀의 발라드』" in pk and "이쪽이 우선" in pk

# 확인 안 된 한국어 제목 검사: 정식 제목·국내 출간작·한국어 근거에 있으면 통과, 지어낸 건 잡는다.
ww = {**base, "title_ko": "1938 타이완 여행기", "kowiki": "x", "author_korean_titles": ["1938 타이완 여행기"]}
body_t = "『1938 타이완 여행기』는 수상작이다. 작가는 『꽃 피는 시절』도 썼다. 『Taiwan Travelogue』는 영역본이다."
assert w.unverified_ko_titles(body_t, ww, "") == ["꽃 피는 시절"]
assert w.unverified_ko_titles(body_t, ww, "그는 꽃 피는 시절을 발표했다") == []

# 시의성 포인트: 날짜 정밀도가 연도뿐인 값은 버리고, 프롬프트 첫 문단 규칙이 들어가야 한다.
assert w._wd_date("+2026-11-20T00:00:00Z").month == 11
assert w._wd_date("+2026-00-00T00:00:00Z") is None
ph = w.build_prompt({**base, "title_ko": None}, "근거", ["이 작품을 원작으로 한 영화 「X」은(는) 2026년 11월 20일 개봉(공개)할 예정이다(아직 개봉 전)"])
assert "왜 지금 이 작품인가" in ph and "2026년 11월 20일" in ph

print("ok")
