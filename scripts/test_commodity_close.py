# 실사례 id=322142: 장중 현재가(WTI 88.04)가 '현재가'로 주입돼 기사에 나감 → 확정 종가만 쓴다.
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import gemini_writer as w

ny = ZoneInfo("America/New_York")
def ts(y, m, d): return int(datetime(y, m, d, 12, tzinfo=ny).timestamp())
r = {"timestamp": [ts(2026,11,2), ts(2026,11,3), ts(2026,11,4)],
     "indicators": {"quote": [{"close": [100.0, 96.09, 88.04]}]}}
# 11/4 장중(10시 ET): 오늘 봉 제외 → 마지막 확정 종가는 11/3
b = w._completed_closes(r, datetime(2026,11,4,10,tzinfo=ny))
assert [c for _, c in b] == [100.0, 96.09], b
# 11/4 정산 후(18시 ET): 오늘 봉 포함
b = w._completed_closes(r, datetime(2026,11,4,18,tzinfo=ny))
assert [c for _, c in b] == [100.0, 96.09, 88.04], b
print("ok")
