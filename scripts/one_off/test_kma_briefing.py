"""
test_kma_briefing.py
---------------------
weather_report.py의 fetch_kma_weather_briefing()만 단독으로 호출해
기상청 API허브(getWthrSituation) 연동이 정상 작동하는지 확인하는 1회성 테스트 스크립트.

실행: python scripts/one_off/test_kma_briefing.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from scripts.weather_report import fetch_kma_weather_briefing, KMA_BRIEFING_KEY

print(f"[TEST] KMA_BRIEFING_KEY: {'설정됨(길이=' + str(len(KMA_BRIEFING_KEY)) + ')' if KMA_BRIEFING_KEY else '없음'}")

for stn_id, label in [("108", "전국"), ("109", "서울·인천·경기"), ("159", "부산·울산·경남")]:
    print(f"\n--- stnId={stn_id} ({label}) ---")
    text = fetch_kma_weather_briefing(stn_id)
    if text:
        print(f"[성공] 길이={len(text)}자")
        print(text[:500])
    else:
        print("[실패 또는 데이터 없음]")
