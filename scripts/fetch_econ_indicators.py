"""
fetch_econ_indicators.py
-------------------------
IMF DataMapper API(공식, 무료, 인증불필요)로 프론티어 국가들의
거시경제 지표(GDP성장률, 인플레이션, 실업률, 경상수지)를 가져와
docs/data/econ_indicators.json으로 저장.

IMF WEO는 매년 4월·10월에 갱신되므로, 이 스크립트는 반기(6개월)
주기로 실행하면 충분함 — econ_indicators.yml 워크플로우 참고.

실행: python scripts/fetch_econ_indicators.py
"""
import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

DATAMAPPER_URL = "https://www.imf.org/external/datamapper/api/v1"
OUTPUT_FILE = "docs/data/econ_indicators.json"

# 인디케이터 코드 (IMF 공식)
INDICATORS = {
    "NGDP_RPCH": "gdp_growth",       # 실질 GDP 성장률 (%)
    "PCPIPCH": "inflation",          # 인플레이션율, 소비자물가 평균 (%)
    "LUR": "unemployment",           # 실업률 (%)
    "BCA_NGDPD": "current_account",  # 경상수지 (GDP 대비 %)
}

# 프론티어 마켓 국가 (ISO 3166-1 alpha-3 코드)
COUNTRIES = {
    "NGA": "나이지리아",
    "KEN": "케냐",
    "ZAF": "남아공",
    "VNM": "베트남",
    "IDN": "인도네시아",
    "THA": "태국",
    "PHL": "필리핀",
    "EGY": "이집트",
}

HEADERS = {"Accept": "application/json"}


def fetch_indicator(indicator_code, country_codes, periods):
    """IMF DataMapper API에서 특정 지표의 여러 국가 데이터를 한 번에 조회"""
    countries_path = "/".join(country_codes)
    url = f"{DATAMAPPER_URL}/{indicator_code}/{countries_path}"
    params = {"periods": ",".join(str(p) for p in periods)}
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=20)
        print(f"    [디버그] 요청 URL: {res.url}")
        print(f"    [디버그] 상태코드: {res.status_code}")
        if res.status_code != 200:
            print(f"  ⚠️ {indicator_code} 조회 실패: {res.status_code}")
            print(f"    응답 본문(앞부분): {res.text[:300]}")
            return {}
        data = res.json()
        values = data.get("values", {}).get(indicator_code, {})
        if not values:
            print(f"    [디버그] 응답에 values가 비어있음. 전체 키: {list(data.keys())}")
        return values
    except Exception as e:
        print(f"  ⚠️ {indicator_code} 조회 예외: {type(e).__name__}: {e}")
        return {}


def run():
    current_year = now_kst().year
    # 최근 확정치 + 향후 전망치까지 포함 (전년 ~ 익년)
    periods = [current_year - 1, current_year, current_year + 1]

    print(f"[경제지표 수집] {len(COUNTRIES)}개국 × {len(INDICATORS)}개 지표 수집 중...")
    print(f"[경제지표 수집] 조회 연도: {periods}")

    country_codes = list(COUNTRIES.keys())
    result = {
        "updated_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        "source": "IMF World Economic Outlook (WEO), via IMF DataMapper API",
        "source_url": "https://www.imf.org/external/datamapper",
        "countries": {}
    }

    # 지표별로 한 번씩만 호출 (국가는 한 번에 묶어서 요청 — API 호출 횟수 최소화)
    indicator_data = {}
    for ind_code, ind_key in INDICATORS.items():
        print(f"  → {ind_code} ({ind_key}) 조회 중...")
        indicator_data[ind_key] = fetch_indicator(ind_code, country_codes, periods)
        time.sleep(1)  # API 예의상 텀

    # 국가별로 재구성
    for code, name_ko in COUNTRIES.items():
        country_entry = {"country_code": code, "country_name": name_ko}
        for ind_key, data in indicator_data.items():
            country_values = data.get(code, {})
            # 가장 최신 연도(확정치 우선)부터 값 찾기
            latest_value = None
            latest_year = None
            for year in sorted(country_values.keys(), reverse=True):
                val = country_values.get(year)
                if val is not None:
                    latest_value = val
                    latest_year = year
                    break
            country_entry[ind_key] = latest_value
            country_entry[f"{ind_key}_year"] = latest_year
        result["countries"][code] = country_entry
        print(f"  ✅ {name_ko}: GDP성장률={country_entry.get('gdp_growth')}% "
              f"({country_entry.get('gdp_growth_year')}), "
              f"인플레이션={country_entry.get('inflation')}% "
              f"({country_entry.get('inflation_year')})")

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 저장 완료: {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
