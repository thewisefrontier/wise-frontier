"""
weather_report.py
------------------
프론티어마켓 주요국 + 한국의 날씨를 실시간 API(Open-Meteo, 무료·키 불필요)로 조회해
국가별로 각각 별도의 기사를 생성한다. 각 기사는 수도를 포함해 그 나라의
여러 지역(4~5개 도시) 날씨를 함께 다룬다. Gemini를 쓰지 않는다 — 실제 수치만 사용.

실행: python scripts/weather_report.py
"""

import os
import requests
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _sb_url():
    return f"{SUPABASE_URL}/rest/v1/articles"


# ── 국가별 도시 목록 (첫 번째가 수도) ──────────────────────
# 국가명: (지역구분, [(도시명, 위도, 경도, 수도여부), ...])
COUNTRIES = {
    "나이지리아": ("africa", [
        ("아부자", 9.0765, 7.3986, True),
        ("라고스", 6.5244, 3.3792, False),
        ("카노", 12.0022, 8.5920, False),
        ("포트하커트", 4.8156, 7.0498, False),
    ]),
    "케냐": ("africa", [
        ("나이로비", -1.2864, 36.8172, True),
        ("몸바사", -4.0435, 39.6682, False),
        ("키수무", -0.0917, 34.7680, False),
        ("나쿠루", -0.3031, 36.0800, False),
    ]),
    "남아공": ("africa", [
        ("프리토리아", -25.7479, 28.2293, True),
        ("케이프타운", -33.9249, 18.4241, False),
        ("요하네스버그", -26.2041, 28.0473, False),
        ("더반", -29.8587, 31.0218, False),
    ]),
    "이집트": ("africa", [
        ("카이로", 30.0444, 31.2357, True),
        ("알렉산드리아", 31.2001, 29.9187, False),
        ("아스완", 24.0889, 32.8998, False),
        ("룩소르", 25.6872, 32.6396, False),
    ]),
    "베트남": ("southeast_asia", [
        ("하노이", 21.0278, 105.8342, True),
        ("호치민", 10.8231, 106.6297, False),
        ("다낭", 16.0544, 108.2022, False),
        ("껀터", 10.0452, 105.7469, False),
    ]),
    "인도네시아": ("southeast_asia", [
        ("자카르타", -6.2088, 106.8456, True),
        ("수라바야", -7.2575, 112.7521, False),
        ("메단", 3.5952, 98.6722, False),
        ("덴파사르", -8.6500, 115.2167, False),
    ]),
    "태국": ("southeast_asia", [
        ("방콕", 13.7563, 100.5018, True),
        ("치앙마이", 18.7883, 98.9853, False),
        ("푸켓", 7.8804, 98.3923, False),
        ("콘깬", 16.4419, 102.8360, False),
    ]),
    "필리핀": ("southeast_asia", [
        ("마닐라", 14.5995, 120.9842, True),
        ("세부", 10.3157, 123.8854, False),
        ("다바오", 7.1907, 125.4553, False),
        ("바기오", 16.4023, 120.5960, False),
    ]),
    "사우디아라비아": ("middle_east", [
        ("리야드", 24.7136, 46.6753, True),
        ("제다", 21.4858, 39.1925, False),
        ("담맘", 26.4207, 50.0888, False),
        ("메카", 21.3891, 39.8579, False),
    ]),
    "아랍에미리트": ("middle_east", [
        ("아부다비", 24.4539, 54.3773, True),
        ("두바이", 25.2048, 55.2708, False),
        ("샤르자", 25.3573, 55.4033, False),
        ("알아인", 24.2075, 55.7447, False),
    ]),
    "튀르키예": ("middle_east", [
        ("앙카라", 39.9334, 32.8597, True),
        ("이스탄불", 41.0082, 28.9784, False),
        ("이즈미르", 38.4237, 27.1428, False),
        ("안탈리아", 36.8969, 30.7133, False),
    ]),
    "인도": ("south_asia", [
        ("뉴델리", 28.6139, 77.2090, True),
        ("뭄바이", 19.0760, 72.8777, False),
        ("콜카타", 22.5726, 88.3639, False),
        ("첸나이", 13.0827, 80.2707, False),
        ("벵갈루루", 12.9716, 77.5946, False),
    ]),
    "방글라데시": ("south_asia", [
        ("다카", 23.8103, 90.4125, True),
        ("치타공", 22.3569, 91.7832, False),
        ("실헷", 24.8949, 91.8687, False),
        ("쿨나", 22.8456, 89.5403, False),
    ]),
    "카자흐스탄": ("central_asia", [
        ("아스타나", 51.1694, 71.4491, True),
        ("알마티", 43.2220, 76.8512, False),
        ("심켄트", 42.3417, 69.5901, False),
    ]),
    "브라질": ("latin_america", [
        ("브라질리아", -15.8267, -47.9218, True),
        ("상파울루", -23.5505, -46.6333, False),
        ("리우데자네이루", -22.9068, -43.1729, False),
        ("마나우스", -3.1190, -60.0217, False),
    ]),
    "멕시코": ("latin_america", [
        ("멕시코시티", 19.4326, -99.1332, True),
        ("과달라하라", 20.6597, -103.3496, False),
        ("몬테레이", 25.6866, -100.3161, False),
        ("칸쿤", 21.1619, -86.8515, False),
    ]),
    "한국": ("global", [
        ("서울", 37.5665, 126.9780, True),
        ("부산", 35.1796, 129.0756, False),
        ("대구", 35.8714, 128.6014, False),
        ("광주", 35.1595, 126.8526, False),
        ("제주", 33.4996, 126.5312, False),
    ]),
}

# WMO 날씨 코드 → 한국어 설명
WEATHER_CODE_KO = {
    0: "맑음", 1: "대체로 맑음", 2: "부분적으로 흐림", 3: "흐림",
    45: "안개", 48: "짙은 안개",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비",
    56: "약한 어는 이슬비", 57: "어는 이슬비",
    61: "약한 비", 63: "비", 65: "강한 비",
    66: "약한 어는 비", 67: "어는 비",
    71: "약한 눈", 73: "눈", 75: "강한 눈", 77: "가루눈",
    80: "약한 소나기", 81: "소나기", 82: "강한 소나기",
    85: "약한 눈소나기", 86: "강한 눈소나기",
    95: "뇌우", 96: "약한 우박 동반 뇌우", 99: "강한 우박 동반 뇌우",
}


def fetch_weather(lat, lon):
    """Open-Meteo 현재 날씨 + 오늘 최고/최저 조회"""
    try:
        res = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current_weather": "true",
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto",
            },
            timeout=15,
        )
        if res.status_code != 200:
            return None
        data = res.json()
        current = data.get("current_weather", {})
        daily = data.get("daily", {})
        temp = current.get("temperature")
        code = current.get("weathercode")
        tmax = (daily.get("temperature_2m_max") or [None])[0]
        tmin = (daily.get("temperature_2m_min") or [None])[0]
        return {
            "temp": temp,
            "condition": WEATHER_CODE_KO.get(code, f"코드{code}"),
            "tmax": tmax,
            "tmin": tmin,
        }
    except Exception as e:
        print(f"  ⚠️ 날씨 조회 실패 ({lat},{lon}): {e}")
        return None


def build_country_report(country_name, cities):
    today_str = now_kst().strftime("%Y년 %m월 %d일")
    lines = []
    any_success = False

    for name, lat, lon, is_capital in cities:
        w = fetch_weather(lat, lon)
        label = f"{name}(수도)" if is_capital else name
        if w is None or w["temp"] is None:
            lines.append(f"- {label}: 데이터 없음")
            continue
        any_success = True
        tmax_str = f"{w['tmax']:.0f}" if w["tmax"] is not None else "?"
        tmin_str = f"{w['tmin']:.0f}" if w["tmin"] is not None else "?"
        lines.append(
            f"- {label}: 현재 {w['temp']:.0f}°C, {w['condition']} "
            f"(오늘 최고 {tmax_str}°C / 최저 {tmin_str}°C)"
        )

    if not any_success:
        return None, None

    title = f"오늘의 {country_name} 날씨 ({now_kst().strftime('%m월 %d일')})"
    body = f"{today_str} 기준 {country_name} 주요 지역 실시간 날씨입니다.\n\n" + "\n".join(lines)
    return title, body


def save_report(country_name, region, title, body):
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    subcategory = f"weather_{country_name}_{now_kst().strftime('%Y%m%d')}"

    # 오늘 이미 생성됐는지 확인 (중복 방지)
    check = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id",
            "source": "eq.NewsFinal",
            "subcategory": f"eq.{subcategory}",
            "limit": "1",
        },
        timeout=15,
    )
    if check.status_code in (200, 206) and check.json():
        print(f"  [SKIP] {country_name} 오늘 날씨 리포트 이미 존재")
        return

    payload = {
        "title_en": title,
        "title_ko": title,
        "summary_en": "",
        "summary_ko": body,
        "url": f"internal://{subcategory}",
        "source": "NewsFinal",
        "category": "날씨",
        "subcategory": subcategory,
        "region": region,
        "country": country_name,
        "country_flag": "",
        "countries": [country_name],
        "image_url": "",
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "최초 게시"}],
        "sent_telegram": 0,
        "is_published": True,
        "posted_blog": 0,
        "dedup_reviewed": True,  # 날씨 기사는 매일 유사 제목이라 중복탐지 대상에서 제외
    }
    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        art_id = data[0].get("id", -1) if data else -1
        print(f"  ✅ {country_name} 날씨 저장 완료 (id={art_id})")
    else:
        print(f"  ❌ {country_name} 저장 실패: HTTP {res.status_code} - {res.text[:200]}")


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    print(f"[날씨] {len(COUNTRIES)}개국 개별 리포트 생성 시작...")
    for country_name, (region, cities) in COUNTRIES.items():
        print(f"→ {country_name}")
        title, body = build_country_report(country_name, cities)
        if not title:
            print(f"  ❌ {country_name} 모든 도시 조회 실패 — 건너뜀")
            continue
        save_report(country_name, region, title, body)

    print("[날씨] 완료")


if __name__ == "__main__":
    run()
