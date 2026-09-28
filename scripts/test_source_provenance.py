# -*- coding: utf-8 -*-
"""원문 출처 저장(source_data.src_ids) 회귀 테스트(2026-09-29).
① 위키 미확인 이름의 원문 대조가 잘린 프롬프트 대신 원문(facts)을 쓰는지 ② 재검수가 저장된 원본 id로 원문을 찾는지.
실행: python scripts/test_source_provenance.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("SUPABASE_URL", "http://x")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
import fabrication_guard as g  # noqa: E402
import reverify_held as rh  # noqa: E402

# ① facts가 있으면 names_without_source_support에 facts가 간다
seen = {}
g.extract_candidate_names = lambda body, fn: ["칼리드 빈 살만"]
g.wikipedia_confirms = lambda n: False
g.names_without_source_support = lambda names, src: seen.setdefault("src", src) and []
g.verify_no_fabricated_names("규칙만 있는 프롬프트", "본문", lambda *a, **k: "없음", facts="خالد بن سلمان 원문")
assert seen["src"] == "خالد بن سلمان 원문", seen
seen.clear()
g.verify_no_fabricated_names("원문+규칙 프롬프트", "본문", lambda *a, **k: "없음")
assert seen["src"] == "원문+규칙 프롬프트"                       # facts 없으면 종전대로


# ② src_ids가 있으면 키워드 검색 없이 그 id로 원문을 읽는다
class R:
    status_code = 200
    def __init__(self, data): self._d = data
    def json(self): return self._d


calls = []
def fake_get(url, headers=None, timeout=None, params=None):
    calls.append(params)
    return R([{"id": 7, "source": "UAE - WAM", "title_en": "منصور بن زايد يتوجه", "summary_en": "", "full_text": "نص كامل"}])
rh.requests.get = fake_get
rh.search_keywords = lambda a: (_ for _ in ()).throw(AssertionError("키워드 검색을 타면 안 됨"))
facts, scores = rh.find_facts({"summary_ko": "본문", "source_data": {"src_ids": [7, 8]}, "created_at": "2026-09-29 05:20"})
assert "نص كامل" in facts and scores == ["src_ids"], facts
assert calls[0]["id"] == "in.(7,8)"

# src_ids가 없으면 종전 키워드 검색 경로
rh.search_keywords = lambda a: []
assert rh.find_facts({"summary_ko": "본문", "source_data": None, "created_at": "2026-09-29 05:20"}) == ("", [])
print("ok")
