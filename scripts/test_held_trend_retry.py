# -*- coding: utf-8 -*-
"""분량 부족 보류 트렌드의 보강 재시도(2026-10-27) 회귀 테스트. 실행: python scripts/test_held_trend_retry.py"""
import os
import sys
from datetime import timedelta

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import article_store as st  # noqa: E402
import gemini_summarizer as g  # noqa: E402


def ago(h):
    return (g.now_kst() - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")


thin = [{"note": "분량 부족 미발행 — 트렌드 신호가 빈약해 실질 내용 없음(312자, 하한 400자)"}]
dense = [{"note": "밀도 부족 미발행(NVIDIA 교차판정) — VERDICT: THIN"}]
pub = {"id": 1, "is_published": True, "update_log": [{"note": "트렌드 추적 최초 게시"}], "created_at": ago(1)}
held_old = {"id": 2, "is_published": False, "update_log": thin, "created_at": ago(4)}
held_new = {"id": 3, "is_published": False, "update_log": dense, "created_at": ago(1)}
other_unpub = {"id": 4, "is_published": False, "update_log": [{"note": "고유명사 날조 의심 — x"}], "created_at": ago(5)}

assert st.is_held_thin(held_old) and st.is_held_thin(held_new) and not st.is_held_thin(pub) and not st.is_held_thin(other_unpub)
assert not st.is_held_thin({**held_old, "update_log": thin + [{"note": "보강 반영 — #9로 발행"}]})

# 3시간 지난 보류 기사는 재시도를 막지 않는다(그 사이 원문이 쌓였으면 다시 써 본다)
assert g._rows_blocking_retry([held_old]) == []
# 막 보류된 기사(3시간 이내)·발행 기사·다른 사유 미발행은 그대로 막는다
assert g._rows_blocking_retry([held_new]) == [held_new]
assert g._rows_blocking_retry([pub, held_old]) == [pub]
assert g._rows_blocking_retry([other_unpub]) == [other_unpub]
# 같은 주제 보류가 2건 쌓이면 원문이 끝내 안 느는 것 — 더 시도하지 않는다
assert g._rows_blocking_retry([held_old, {**held_old, "id": 5}]) != []


# 발행되면 같은 주제의 보류 기사에 "보강 반영"을 남긴다
class R:
    status_code = 200
    def __init__(self, d): self._d = d
    def json(self): return self._d


patched = {}
st.requests.get = lambda *a, **k: R([held_old, other_unpub])
st.requests.patch = lambda *a, **k: patched.update({k["params"]["id"]: k["json"]}) or R({})
st._mark_enriched_held_trends({"source": "NewsFinal", "subcategory": "realtrend_France"}, 100)
assert list(patched) == ["eq.2"] and "#100" in patched["eq.2"]["update_log"][-1]["note"]
patched.clear()
st._mark_enriched_held_trends({"source": "NewsFinal", "subcategory": "cluster_x"}, 100)   # 트렌드가 아니면 무시
assert patched == {}

print("ok")
