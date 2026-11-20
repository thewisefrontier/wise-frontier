# -*- coding: utf-8 -*-
"""트렌드 중복 판정 4차(LLM) 후보 순서 회귀 테스트(2026-11-21, 뉴올리언스 유람선 충돌 3건 중복 발행).
실행: python scripts/test_trend_dup_rank.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import gemini_summarizer as g  # noqa: E402

assert g._fuzzy_shared({"미시시피강", "충돌"}, {"미시시피강서", "충돌로", "전복"}) == 2

target = {"id": 325398, "title_ko": "미시시피강서 유조선 충돌로 유람선 전복…인명 피해는 없어"}
newer = [{"id": 325400 + i, "title_ko": t} for i, t in enumerate(
    ["트럼프 행정부, 이란 테러 계획 수사", "미 FAA 항공 대란 일단락", "뉴욕 브루클린 아파트 붕괴 8명 사망",
     "캘리포니아 폭우 홍수 경보", "미 중간선거 최종 승패 확정"])]
# 예전 방식(같은 나라 최근 3건)은 하루 전 같은 사건 기사를 못 본다
assert target not in ([target] + newer)[-3:]
for new in ("뉴올리언스 항구, 미시시피강 유조선 충돌 사고 관련 선박 압류 소송 제기",
            "미국 미시시피강서 탱커선과 유람선 충돌… 32명 부상·항만 당국 법적 대응"):
    ranked = g._rank_for_llm(new, [target] + newer)
    assert ranked and ranked[0]["id"] == 325398, (new, [a["id"] for a in ranked])
assert g._rank_for_llm("수단 정부군 엘파시르 탈환", newer) == []      # 공유 단어 없으면 LLM에 안 올린다
print("ok")
