# -*- coding: utf-8 -*-
"""비트코인 주간 등락 계산이 월~금(5거래일) 창이 아니라 실제 7일 전 대비인지
검증(2026-09-26, 사용자 지적 — "비트코인은 5일이 아니라 7일 기준으로 봐야
하는거 아닐까"). 주말 종가를 포함해 최신 종가 vs 정확히 7일 전 종가를 비교해야
하는데, 예전엔 주식과 동일하게 금~금만 봐서 주말 변동이 통째로 빠졌었다.

실행: python scripts/test_weekly_market_card_crypto_window.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import weekly_market_card as wmc  # noqa: E402

FRI = date(2026, 9, 25)


def rows_for(days_back_prices):
    """오늘(토, FRI+1)까지 매일 하루씩, days_back_prices[0]가 가장 오래된 값."""
    n = len(days_back_prices)
    start = FRI + timedelta(days=1) - timedelta(days=n - 1)
    return [(start + timedelta(days=i), p) for i, p in enumerate(days_back_prices)]


# 지난주 토(-9) 지난주 금(-8) ... 이번주 금(-1) 이번주 토(0=오늘)
prices = list(range(100, 111))  # 10일치, 마지막이 오늘(토)
wmc.fetch_daily = lambda symbol: (rows_for(prices), {})

r = wmc.weekly_change("BTC-USD", FRI, limit_pct=1000)
assert r["end_date"] == FRI + timedelta(days=1), r  # 오늘(토) 종가를 씀
assert r["end"] == prices[-1]
assert r["prev_date"] == r["end_date"] - timedelta(days=7)
assert r["prev"] == prices[-8]  # 정확히 7일 전
old_style_pct = (prices[-2] / prices[-9] - 1) * 100  # 예전 금~금 계산이면 이 값
assert abs(r["pct"] - old_style_pct) > 0.01, "새 계산이 예전 금~금 계산과 달라야 함(주말 반영)"

# 주식은 그대로 월~금 창(변경 없음)
wmc.fetch_daily = lambda symbol: (rows_for(prices), {"regularMarketPrice": prices[-2]})
r2 = wmc.weekly_change("^GSPC", FRI, limit_pct=1000)
assert r2["end_date"] == FRI  # 금요일 그대로

print("ok")
