# -*- coding: utf-8 -*-
"""종목 비교 칼럼 → 개별 기업 분석 기사(2026-10-27, id=319604) 회귀 테스트. 실행: python scripts/test_company_analysis_format.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import gemini_writer as w  # noqa: E402

fool = {"title_en": "Airbnb vs. Coupang: Which Consumer Stock Is a Better Buy in 2026?",
        "url": "https://www.fool.com/coverage/better-buy/2026/10/27/airbnb-vs-coupang/"}
assert w.is_stock_comparison([fool])
assert w.is_stock_comparison([{"title_en": "Nvidia vs AMD: which stock to buy now"}])
assert w.is_stock_comparison([{"url": "https://www.fool.com/coverage/better-buy/x"}])
# 주식 비교가 아닌 vs(스포츠·소송)는 아니다
assert not w.is_stock_comparison([{"title_en": "Lakers vs. Celtics: 5 takeaways"}])
assert not w.is_stock_comparison([{"title_en": "Epic vs Apple ruling upheld"}])

assert w.enforce_company_analysis_title("에어비앤비 vs 쿠팡, 투자 매력 비교")[1]
assert w.enforce_company_analysis_title("쿠팡과 에어비앤비 실적 비교")[1]
assert w.enforce_company_analysis_title("쿠팡, 매출 345억 달러에도 순이익률 0.6%") == ("[기업 분석] 쿠팡, 매출 345억 달러에도 순이익률 0.6%", "")
assert w.enforce_company_analysis_title("[기업 분석] 쿠팡, 성장 둔화") == ("[기업 분석] 쿠팡, 성장 둔화", "")

# 프롬프트에 형식 지시가 붙는다
w.build_naming_hints = lambda *a, **k: ""; w.build_commodity_grounding = lambda *a, **k: ""; w.load_prompt = lambda *a, **k: k.get("fallback", "")
assert "개별 기업 분석" in w.build_issue_prompt([{**fool, "full_text": "x", "source": "Motley Fool"}])
print("ok")
