"""장중특징주 마감 표현 게이트 + 야후/AV 교차검증 회귀 테스트.

실사례(2026-10-28): 319975 아마존 — 장중(23:38 KST)인데 "253.18달러로 거래를 마쳤다",
야후 +0.03% / AV +1.61%가 "2차 확인 일치"로 통과. 319974 구글도 "거래를 마쳤다".
실행: python scripts/test_intraday_close_wording.py
"""
from datetime import date

import frontier_markets_writer as fm
from intraday_stock_digest import close_wording

# 마감 표현 게이트
assert close_wording("전일 대비 0.03% 상승한 253.18달러로 거래를 마쳤다.")          # 319975
assert close_wording("나스닥에서 장을 마감했다.")
assert close_wording("이날 종가는 324.37달러였다.")
assert close_wording("장중 저점 부근에서 마감하며 하락 압력이 지속됐다.")   # 319404
assert not close_wording("전일 종가 대비 1.18% 오른 324.37달러에 거래되고 있다.")
assert not close_wording("장중 한때 4% 넘게 오르기도 했다.")

# 교차검증 허용폭 — 최근 로그 실측 쌍
pd = fm._pct_disagree
assert pd(0.03, 1.61)        # AMZN 319975
assert pd(-1.10, -0.14)      # AAPL
for a, b in [(1.10, 1.19), (1.18, 1.19), (-0.96, -0.96), (0.28, 0.35), (0.03, 0.05),
             (0.19, 0.28), (-1.24, -1.23), (0.79, 0.94), (0.51, 0.48), (0.70, 0.70)]:
    assert not pd(a, b), (a, b)
assert pd(0.5, -0.5)         # 부호 반대
assert pd(-0.018, 0.02)      # 2026-08-30 다우 사고(부호)


# AV 기준일이 기사 거래일과 다르면(장중 무료 응답 = 전 거래일) 비교 불가
class _Res:
    status_code = 200
    def __init__(self, day): self.day = day
    def json(self): return {"Global Quote": {"07. latest trading day": self.day, "10. change percent": "1.6100%"}}


fm.ALPHA_VANTAGE_API_KEY = "x"
fm.requests.get = lambda *a, **k: _Res("2026-10-27")
assert fm.fetch_alphavantage_pct("AMZN", date(2026, 10, 28)) is None
assert fm.fetch_alphavantage_pct("AMZN") == 1.61
fm.requests.get = lambda *a, **k: _Res("2026-10-28")
assert fm.fetch_alphavantage_pct("AMZN", date(2026, 10, 28)) == 1.61
print("OK")
