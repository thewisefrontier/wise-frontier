# -*- coding: utf-8 -*-
"""분량 부족 보류 기사 보강 발행(gemini_writer, 2026-10-27) 회귀 테스트. 실행: python scripts/test_held_enrich.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import gemini_writer as w  # noqa: E402

log_thin = [{"note": "분량 부족 — 소스 부족으로 실질적 내용 없이 반복 서술(512자)"}]
H1 = {"id": 1, "title_ko": "프랑스 전국 시위 격화…파리 통행금지", "summary_ko": "시위", "update_log": log_thin, "source_data": {"src_ids": [10, 11]}}
H2 = {"id": 2, "title_ko": "케냐 홍수로 이재민 발생", "summary_ko": "홍수", "update_log": log_thin, "source_data": {"src_ids": [20]}}

# 조회 결과 필터: 분량 부족 + 원문 id 있음 + 아직 보강에 안 쓰인 것만
class R:
    status_code = 200
    def __init__(self, d): self._d = d
    def json(self): return self._d

rows = [H1, H2,
        {"id": 3, "title_ko": "x", "update_log": [{"note": "고유명사 날조 의심"}], "source_data": {"src_ids": [1]}},
        {"id": 4, "title_ko": "y", "update_log": log_thin, "source_data": {}},
        {"id": 5, "title_ko": "z", "update_log": log_thin + [{"note": "보강 반영 — #9로 발행"}], "source_data": {"src_ids": [1]}}]
w.requests.get = lambda *a, **k: R(rows)
w._held_thin_cache = None
assert [h["id"] for h in w.get_held_thin_articles()] == [1, 2]

# 같은 사건 판별: 키워드가 2개 이상 겹치는 후보만 LLM에 묻는다
asked = []
w._same_headline_event_llm = lambda ta, ba, tb, bb: asked.append(tb) or tb.startswith("프랑스")
cluster = [{"id": 99, "title_ko": "프랑스 시위 격화로 체포 1000명", "title_en": "France protests"}]
assert w.find_held_thin_article(cluster) is H1 and asked == [H1["title_ko"]]
asked.clear()
assert w.find_held_thin_article([{"id": 98, "title_ko": "일본 총선 결과 발표"}]) is None and asked == []   # 겹침 없음 → LLM 호출 없음

# 원문 합치기: 이번 클러스터에 이미 있는 원문은 빼고 조회
seen = {}
def fake_get(url, **k):
    seen.update(k["params"])
    return R([{"id": 11, "title_ko": "원문"}])
w.requests.get = fake_get
assert w.load_held_sources(H1, {10}) == [{"id": 11, "title_ko": "원문"}] and seen["id"] == "in.(11)"
assert w.load_held_sources({"source_data": {"src_ids": [10]}}, {10}) == []

# 보강 발행 표시: update_log에 "보강 반영"을 남기고 후보 목록에서 뺀다
patched = {}
w.update_article_fields = lambda aid, fields: patched.update({aid: fields})
w.mark_held_enriched(H1, 500)
assert "보강 반영" in patched[1]["update_log"][-1]["note"] and "#500" in patched[1]["update_log"][-1]["note"]
assert H1 not in w._held_thin_cache

print("ok")
