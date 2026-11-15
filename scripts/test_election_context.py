import sys, os
from datetime import date
sys.path.insert(0, os.path.dirname(__file__))
from news_context import election_context as ec, flag_bare_us_presidential as fl
c = ec(date(2026, 11, 15))
assert "2026-11-03 미국 중간선거" in c and "2028-11-07" in c and "2024년" in c, c
assert "2028-11-07 미국 대선" in ec(date(2028, 6, 1)) and "2024-11-05" not in ec(date(2028, 6, 1)).split("다음")[0]
# 실사례(2026-11-15): 321954·307289 — 연도 없는 "미국 대선"만 경고
assert fl("최근 가상자산 시장은 미국 대선을 앞두고 불확실성이 커졌다.", date(2026, 11, 3))
# 오탐 대조: 연도·과거/차기 단서가 있거나 대선 해가 아니면 통과
assert not fl("2024년 미국 대선 직후 비트코인이 급등했다.", date(2026, 11, 3))
assert not fl("2028년 미국 대선 출마를 선언했다.", date(2026, 11, 3))
assert not fl("차기 미국 대선을 앞두고 후보 구도가 짜이고 있다.", date(2026, 11, 3))
assert not fl("미국 대선을 앞두고 불확실성이 커졌다.", date(2028, 10, 1))
print("ok")
# 저장 경로 경고(글은 그대로, update_log에만 추가)
import article_store, news_context
from datetime import date as _d
_real = news_context.flag_bare_us_presidential
news_context.flag_bare_us_presidential = lambda t, today=None: _real(t, _d(2026, 11, 3))
_p = {"title_ko": "비트코인 1.48% 상승…미 대선 불확실성 속 반등", "summary_ko": "x", "created_at": "2026-11-04 06:27"}
article_store._warn_bare_us_presidential(_p)
assert _p["title_ko"].count("대선") == 1 and "경고" in _p["update_log"][0]["note"], _p
print("ok warn")
