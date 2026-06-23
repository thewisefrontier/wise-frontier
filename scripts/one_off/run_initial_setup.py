"""
run_initial_setup.py
--------------------
초기 설정 한번에 실행:
1. econ_calendar_fetch.py — 경제 일정 DB 채우기
2. fetch_econ_indicators.py — IMF 경제지표 첫 수집
3. unpublish_duplicates.py — 중복 라이브 기사 미발행 처리
"""
import sys
import os

# scripts/ 폴더를 path에 추가
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

print("=" * 50)
print("1/3 경제 일정 DB 채우기")
print("=" * 50)
try:
    import econ_calendar_fetch
    econ_calendar_fetch.run()
except Exception as e:
    print(f"❌ econ_calendar_fetch 실패: {e}")

print()
print("=" * 50)
print("2/3 IMF 경제지표 수집")
print("=" * 50)
try:
    import fetch_econ_indicators
    fetch_econ_indicators.run()
except Exception as e:
    print(f"❌ fetch_econ_indicators 실패: {e}")

print()
print("=" * 50)
print("3/3 중복 라이브 기사 미발행 처리")
print("=" * 50)
try:
    import requests
    from dotenv import load_dotenv
    load_dotenv()

    SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    DUPLICATE_IDS = [6491, 6492, 6421, 6355, 6128, 6129]
    for id_ in DUPLICATE_IDS:
        res = requests.patch(
            f"{SUPABASE_URL}/rest/v1/articles?id=eq.{id_}",
            headers=headers,
            json={"is_published": False},
            timeout=15
        )
        if res.status_code in (200, 204):
            print(f"✅ id={id_} 미발행 처리 완료")
        else:
            print(f"❌ id={id_} 실패: {res.status_code}")
except Exception as e:
    print(f"❌ unpublish_duplicates 실패: {e}")

print()
print("=" * 50)
print("✅ 초기 설정 완료")
print("=" * 50)
