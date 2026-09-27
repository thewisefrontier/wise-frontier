# -*- coding: utf-8 -*-
"""weekly_crypto_writer.py 회귀 테스트(2026-09-27). 실행: python scripts/test_weekly_crypto_writer.py"""
import os
import sys
from datetime import date

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import weekly_crypto_writer as w  # noqa: E402

# 2026-09-27 실사고: bare_name을 "코인"으로 줬다가 Gemini가 프롬프트 지시대로 이미
# "[주간 코인시황]"을 붙여 왔는데 dedup 매칭이 안 돼 접두어가 중복됐다.
assert w.enforce_title_prefix("[주간 코인시황] 비트코인 4%대 상승") == "[주간 코인시황] 비트코인 4%대 상승"
assert w.enforce_title_prefix("비트코인 4%대 상승") == "[주간 코인시황] 비트코인 4%대 상승"
assert w.enforce_title_prefix("[주간 코인시황]비트코인 4%대 상승") == "[주간 코인시황] 비트코인 4%대 상승"

btc = {"end": 84461.0, "prev": 81143.0, "pct": 4.09, "end_date": date(2026, 9, 27), "prev_date": date(2026, 9, 20)}
eth = {"end": 2699.0, "prev": 2643.0, "pct": 2.12, "end_date": date(2026, 9, 27), "prev_date": date(2026, 9, 20)}
w.fetch_headlines = lambda *a, **k: ["Bitcoin ETF Inflows Reach $2.4 Billion This Week - 24/7 Wall St."]
prompt = w.build_article_prompt(btc, eth)
assert "84,461" in prompt and "81,143" in prompt and "+4.09%" in prompt
assert "2,699" in prompt and "+2.12%" in prompt
assert "Bitcoin ETF Inflows" in prompt
assert "헤드라인에 없는 규제·정책" in prompt  # 날조 방지 지시 포함 확인

# 헤드라인이 하나도 없으면 억지로 채우라고 시키지 않는다
w.fetch_headlines = lambda *a, **k: []
prompt2 = w.build_article_prompt(btc, eth)
assert "관련 실제 보도 헤드라인" not in prompt2
assert "가격 흐름과 시장 전반" in prompt2

print("ok")
