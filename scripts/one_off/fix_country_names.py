"""
fix_country_names.py
---------------------
이미 DB에 저장된 비표준 국가명("대한민국" 등)을 표준 표기("한국")로 일괄 정리.
1회성 실행 스크립트.

실행: python scripts/fix_country_names.py
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# gemini_writer.py의 COUNTRY_ALIASES와 동일하게 유지
COUNTRY_ALIASES = {
    "대한민국": "한국", "남한": "한국", "South Korea": "한국", "Korea": "한국",
    "미국": "미국", "USA": "미국", "United States": "미국",
    "중국": "중국", "China": "중국",
    "일본": "일본", "Japan": "일본",
    "Nigeria": "나이지리아",
    "Kenya": "케냐",
    "남아프리카공화국": "남아공", "남아프리카": "남아공", "South Africa": "남아공",
    "Vietnam": "베트남",
    "Indonesia": "인도네시아",
    "Thailand": "태국",
    "Philippines": "필리핀",
    "Egypt": "이집트",
    "사우디": "사우디아라비아", "Saudi Arabia": "사우디아라비아",
    "UAE": "아랍에미리트",
    "터키": "튀르키예", "Turkey": "튀르키예",
    "India": "인도",
}

def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

def fix():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[ERROR] 환경변수 없음")
        return

    total_fixed = 0
    for alias, standard in COUNTRY_ALIASES.items():
        # countries 배열 컬럼은 별도 처리 필요 — 우선 country 단일 컬럼만 정리
        res = requests.patch(
            f"{SUPABASE_URL}/rest/v1/articles?country=eq.{alias}",
            headers={**_headers(), "Prefer": "return=representation"},
            json={"country": standard},
        )
        if res.status_code in (200, 201):
            n = len(res.json())
            if n > 0:
                print(f"  '{alias}' → '{standard}': {n}건 수정")
                total_fixed += n
        else:
            print(f"  ⚠️ '{alias}' 처리 실패: {res.status_code}")

    print(f"\n✅ 총 {total_fixed}건 국가명 정규화 완료")

if __name__ == "__main__":
    fix()
