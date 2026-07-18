"""
weather_report.py
------------------
프론티어마켓 주요국 + 한국 날씨를 실시간 API(Open-Meteo, 무료·키 불필요)로 조회해
대륙별로 정리한 데이터 기사를 생성한다. Gemini를 쓰지 않는다 — 실제 수치만 사용.

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


# ── 대상 도시 (대륙별) ──────────────────────────────────
# (표시명, 위도, 경도)
CITIES = {
    "아프리카": [
        ("나이지리아 라고스", 6.5244, 3.3792),
        ("케냐 나이로비", -1.2864, 36.8172),
        ("남아공 요하네스버그", -26.2041, 28.0473),
        ("이집트 카이로", 30.0444, 31.2357),
    ],
    "동남아시아": [
        ("베트남 하노이", 21.0278, 105.8342),
        ("인도네시아 자카르타", -6.2088, 106.8456),
        ("태국 방콕", 13.7563, 100.5018),
        ("필리핀 마닐라", 14.5995, 120.9842),
    ],
    "중동": [
        ("사우디아라비아 리야드", 24.7136, 46.6753),
        ("아랍에미리트 두바이", 25.2048, 55.2708),
        ("튀르키예 이스탄불", 41.0082, 28.9784),
    ],
    "남아시아": [
        ("인도 뉴델리", 28.6139, 77.2090),
        ("방글라데시 다카", 23.8103, 90.4125),
    ],
    "중앙아시아": [
        ("카자흐스탄 아스타나", 51.1694, 71.4491),
    ],
    "중남미": [
        ("브라질 상파울루", -23.5505, -46.6333),
        ("멕시코 멕시코시티", 19.4326, -99.1332),
    ],
    "한국": [
        ("서울", 37.5665, 126.9780),
    ],
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


def build_report():
    today_str = now_kst().strftime("%Y년 %m월 %d일")
    lines = []
    any_success = False

    for continent, cities in CITIES.items():
        lines.append(f"[{continent}]")
        for name, lat, lon in cities:
            w = fetch_weather(lat, lon)
            if w is None or w["temp"] is None:
                lines.append(f"- {name}: 데이터 없음")
                continue
            any_success = True
            tmax_str = f"{w['tmax']:.0f}" if w["tmax"] is not None else "?"
            tmin_str = f"{w['tmin']:.0f}" if w["tmin"] is not None else "?"
            lines.append(
                f"- {name}: 현재 {w['temp']:.0f}°C, {w['condition']} "
                f"(오늘 최고 {tmax_str}°C / 최저 {tmin_str}°C)"
            )
        lines.append("")

    if not any_success:
        return None, None

    title = f"오늘의 프론티어마켓 날씨 ({now_kst().strftime('%m월 %d일')})"
    body = f"{today_str} 기준 실시간 날씨입니다.\n\n" + "\n".join(lines).strip()
    return title, body


def save_report(title, body):
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    subcategory = f"weather_{now_kst().strftime('%Y%m%d')}"

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
        print("[SKIP] 오늘 날씨 리포트 이미 존재")
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
        "region": "global",
        "country": "",
        "country_flag": "",
        "countries": [],
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
        print(f"✅ 날씨 리포트 저장 완료 (id={art_id}): {title}")
    else:
        print(f"❌ 저장 실패: HTTP {res.status_code} - {res.text[:300]}")


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    print("[날씨] 데이터 수집 중...")
    title, body = build_report()
    if not title:
        print("❌ 모든 도시 날씨 조회 실패 — 저장하지 않음")
        return

    save_report(title, body)


if __name__ == "__main__":
    run()
