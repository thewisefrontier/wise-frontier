# -*- coding: utf-8 -*-
"""같은 사건 중복 → 기존 기사 갱신(2026-10-29, 스페인 열차 추돌 #319964·#319985) 회귀 테스트.
실행: python scripts/test_dup_merge.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import gemini_writer as w  # noqa: E402

# 앞 두 글자가 같은 한글 단어는 prefix=True에서만 공통어
a, b = {"스페인", "고속철도", "정면충돌", "참사"}, {"스페인", "토레도서", "고속열차", "충돌"}
assert w._fuzzy_keyword_overlap(a, b) == 2              # 스페인 + 충돌⊂정면충돌
assert w._fuzzy_keyword_overlap(a, b, prefix=True) == 3  # + 고속철도/고속열차
assert w._fuzzy_keyword_overlap({"AI칩"}, {"AI모델"}, prefix=True) == 0   # 한글 두 글자 접두만


class R:
    def __init__(self, d, code=200):
        self._d, self.status_code = d, code

    def json(self):
        return self._d


# 3차: 공통어 많은 엉뚱한 후보가 1등이어도 2등까지 LLM에 묻는다
cands = [
    {"id": 1, "title_ko": "스페인 총선 앞두고 산체스 총리 지지율 하락 참사", "summary_ko": "x"},
    {"id": 2, "title_ko": "스페인 토레도서 고속열차 두 대 충돌…사상자 수십 명 발생", "summary_ko": "y"},
]
w.requests.post = lambda *a, **k: R([], 200)
w.requests.get = lambda *a, **k: R(cands)
asked = []
w._same_headline_event_llm = lambda t, bd, ct, cb: asked.append(ct) or "고속열차" in ct
got, score = w.find_similar_article("스페인 고속철도 정면충돌 참사…사망자 최소 68명으로 늘어", [], body="본문")
assert got and got["id"] == 2 and score == 60, (got, asked)

# 생성 뒤 병합: 발행 기사만, 실행당 상한
merged = []
import gemini_summarizer  # noqa: E402
gemini_summarizer.merge_trend_article = lambda ex, t, bd, note: merged.append((ex["id"], len(ex.get("update_log") or []))) or True
w._postgen_merges = 0
w.requests.get = lambda *a, **k: R([{"id": 7, "title_ko": "기존", "summary_ko": "본문", "update_log": [{"note": "최초 게시"}], "is_published": True}])
assert w.merge_into_existing({"id": 7}, "새 제목", "새 본문") and merged == [(7, 1)]   # update_log도 넘긴다
w.requests.get = lambda *a, **k: R([{"id": 8, "is_published": False}])
assert not w.merge_into_existing({"id": 8}, "t", "b")                                 # 미발행 기사엔 안 붙인다
w.requests.get = lambda *a, **k: R([{"id": 7, "title_ko": "기존", "summary_ko": "본문", "update_log": [], "is_published": True}])
w._postgen_merges = w.MAX_POSTGEN_MERGES_PER_RUN
assert not w.merge_into_existing({"id": 7}, "t", "b")                                 # 상한
print("ok")
