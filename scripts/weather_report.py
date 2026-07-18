"""
weather_report.py
------------------
전세계 주요국 + 프론티어마켓 국가들의 날씨를 실시간 API(Open-Meteo, 무료·키 불필요)로
조회해 국가별로 각각 별도의 기사를 생성한다. 각 기사는 수도를 포함해 그 나라의
여러 지역(2~5개 도시) 날씨를 함께 다룬다. Gemini를 쓰지 않는다 — 실제 수치만 사용.

- 각 국가의 "현지 아침"(06~09시) 시간대에 그 나라 리포트를 발행한다 (국가별 IANA 타임존 기준).
- 현지 기준 평일(월~목): 오늘 날씨. 현지 기준 금요일 아침: 주말(토·일) 예보.
- 현지 기준 월요일 아침에는 오늘 날씨에 더해 이번주(월~금) 예보도 함께 발행한다.
- 현지 기준 토·일요일은 발행하지 않음 (금요일 아침에 이미 주말 예보 발행).
- 워크플로우는 매시 정각에 실행되며, 이 스크립트가 국가별로 "지금이 그 나라 아침인지" 판단한다.
- 국가별 대표 이미지는 Pixabay에서 조회해 자동 삽입한다.

실행: python scripts/weather_report.py
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

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
COUNTRIES = {
    "나이지리아": ("africa", "Africa/Lagos", [
        ("아부자", 9.0765, 7.3986, True),
        ("라고스", 6.5244, 3.3792, False),
        ("카노", 12.0022, 8.5920, False),
        ("포트하커트", 4.8156, 7.0498, False),
        ("이바단", 7.3775, 3.9470, False),
    ]),
    "케냐": ("africa", "Africa/Nairobi", [
        ("나이로비", -1.2864, 36.8172, True),
        ("몸바사", -4.0435, 39.6682, False),
        ("키수무", -0.0917, 34.7680, False),
        ("나쿠루", -0.3031, 36.0800, False),
        ("엘도레트", 0.5143, 35.2698, False),
    ]),
    "남아공": ("africa", "Africa/Johannesburg", [
        ("프리토리아", -25.7479, 28.2293, True),
        ("케이프타운", -33.9249, 18.4241, False),
        ("요하네스버그", -26.2041, 28.0473, False),
        ("더반", -29.8587, 31.0218, False),
        ("블룸폰테인", -29.0852, 26.1596, False),
    ]),
    "이집트": ("africa", "Africa/Cairo", [
        ("카이로", 30.0444, 31.2357, True),
        ("알렉산드리아", 31.2001, 29.9187, False),
        ("아스완", 24.0889, 32.8998, False),
        ("룩소르", 25.6872, 32.6396, False),
        ("포트사이드", 31.2653, 32.3019, False),
    ]),
    "모로코": ("africa", "Africa/Casablanca", [
        ("라바트", 34.0209, -6.8416, True),
        ("카사블랑카", 33.5731, -7.5898, False),
        ("마라케시", 31.6295, -7.9811, False),
        ("페스", 34.0181, -5.0078, False),
        ("탕헤르", 35.7595, -5.8340, False),
    ]),
    "알제리": ("africa", "Africa/Algiers", [
        ("알제", 36.7538, 3.0588, True),
        ("오랑", 35.6969, -0.6331, False),
        ("콘스탄틴", 36.3650, 6.6147, False),
        ("안나바", 36.9000, 7.7667, False),
    ]),
    "에티오피아": ("africa", "Africa/Addis_Ababa", [
        ("아디스아바바", 9.0320, 38.7469, True),
        ("드레다와", 9.5931, 41.8661, False),
        ("메켈레", 13.4967, 39.4753, False),
        ("하와사", 7.0504, 38.4955, False),
    ]),
    "가나": ("africa", "Africa/Accra", [
        ("아크라", 5.6037, -0.1870, True),
        ("쿠마시", 6.6885, -1.6244, False),
        ("타말레", 9.4008, -0.8393, False),
        ("세콘디타코라디", 4.9344, -1.7133, False),
    ]),
    "탄자니아": ("africa", "Africa/Dar_es_Salaam", [
        ("도도마", -6.1630, 35.7516, True),
        ("다르에스살람", -6.7924, 39.2083, False),
        ("아루샤", -3.3869, 36.6830, False),
        ("음완자", -2.5164, 32.9175, False),
    ]),
    "앙골라": ("africa", "Africa/Luanda", [
        ("루안다", -8.8390, 13.2894, True),
        ("우암보", -12.7756, 15.7392, False),
        ("벵겔라", -12.5763, 13.4055, False),
    ]),
    "베트남": ("southeast_asia", "Asia/Ho_Chi_Minh", [
        ("하노이", 21.0278, 105.8342, True),
        ("호치민", 10.8231, 106.6297, False),
        ("다낭", 16.0544, 108.2022, False),
        ("하이퐁", 20.8449, 106.6881, False),
        ("껀터", 10.0452, 105.7469, False),
    ]),
    "인도네시아": ("southeast_asia", "Asia/Jakarta", [
        ("자카르타", -6.2088, 106.8456, True),
        ("수라바야", -7.2575, 112.7521, False),
        ("메단", 3.5952, 98.6722, False),
        ("덴파사르", -8.6500, 115.2167, False),
        ("마카사르", -5.1477, 119.4327, False),
    ]),
    "태국": ("southeast_asia", "Asia/Bangkok", [
        ("방콕", 13.7563, 100.5018, True),
        ("치앙마이", 18.7883, 98.9853, False),
        ("푸켓", 7.8804, 98.3923, False),
        ("콘깬", 16.4419, 102.8360, False),
        ("나콘랏차시마", 14.9799, 102.0977, False),
    ]),
    "필리핀": ("southeast_asia", "Asia/Manila", [
        ("마닐라", 14.5995, 120.9842, True),
        ("세부", 10.3157, 123.8854, False),
        ("다바오", 7.1907, 125.4553, False),
        ("바기오", 16.4023, 120.5960, False),
        ("일로일로", 10.7202, 122.5621, False),
    ]),
    "미얀마": ("southeast_asia", "Asia/Yangon", [
        ("네피도", 19.7633, 96.0785, True),
        ("양곤", 16.8661, 96.1951, False),
        ("만달레이", 21.9588, 96.0891, False),
    ]),
    "캄보디아": ("southeast_asia", "Asia/Phnom_Penh", [
        ("프놈펜", 11.5564, 104.9282, True),
        ("시엠립", 13.3633, 103.8564, False),
    ]),
    "말레이시아": ("southeast_asia", "Asia/Kuala_Lumpur", [
        ("쿠알라룸푸르", 3.1390, 101.6869, True),
        ("조호르바루", 1.4927, 103.7414, False),
        ("페낭", 5.4141, 100.3288, False),
    ]),
    "싱가포르": ("southeast_asia", "Asia/Singapore", [
        ("싱가포르", 1.3521, 103.8198, True),
    ]),
    "사우디아라비아": ("middle_east", "Asia/Riyadh", [
        ("리야드", 24.7136, 46.6753, True),
        ("제다", 21.4858, 39.1925, False),
        ("담맘", 26.4207, 50.0888, False),
        ("메카", 21.3891, 39.8579, False),
        ("메디나", 24.5247, 39.5692, False),
    ]),
    "아랍에미리트": ("middle_east", "Asia/Dubai", [
        ("아부다비", 24.4539, 54.3773, True),
        ("두바이", 25.2048, 55.2708, False),
        ("샤르자", 25.3573, 55.4033, False),
        ("알아인", 24.2075, 55.7447, False),
    ]),
    "튀르키예": ("middle_east", "Europe/Istanbul", [
        ("앙카라", 39.9334, 32.8597, True),
        ("이스탄불", 41.0082, 28.9784, False),
        ("이즈미르", 38.4237, 27.1428, False),
        ("안탈리아", 36.8969, 30.7133, False),
        ("부르사", 40.1826, 29.0665, False),
    ]),
    "이스라엘": ("middle_east", "Asia/Jerusalem", [
        ("예루살렘", 31.7683, 35.2137, True),
        ("텔아비브", 32.0853, 34.7818, False),
    ]),
    "이란": ("middle_east", "Asia/Tehran", [
        ("테헤란", 35.6892, 51.3890, True),
        ("이스파한", 32.6546, 51.6680, False),
        ("마슈하드", 36.2605, 59.6168, False),
        ("타브리즈", 38.0800, 46.2919, False),
    ]),
    "이라크": ("middle_east", "Asia/Baghdad", [
        ("바그다드", 33.3152, 44.3661, True),
        ("바스라", 30.5085, 47.7835, False),
        ("모술", 36.3350, 43.1189, False),
        ("아르빌", 36.1901, 44.0091, False),
    ]),
    "카타르": ("middle_east", "Asia/Qatar", [
        ("도하", 25.2854, 51.5310, True),
    ]),
    "요르단": ("middle_east", "Asia/Amman", [
        ("암만", 31.9454, 35.9284, True),
        ("자르카", 32.0728, 36.0876, False),
    ]),
    "인도": ("south_asia", "Asia/Kolkata", [
        ("뉴델리", 28.6139, 77.2090, True),
        ("뭄바이", 19.0760, 72.8777, False),
        ("콜카타", 22.5726, 88.3639, False),
        ("첸나이", 13.0827, 80.2707, False),
        ("벵갈루루", 12.9716, 77.5946, False),
    ]),
    "방글라데시": ("south_asia", "Asia/Dhaka", [
        ("다카", 23.8103, 90.4125, True),
        ("치타공", 22.3569, 91.7832, False),
        ("실헷", 24.8949, 91.8687, False),
        ("쿨나", 22.8456, 89.5403, False),
    ]),
    "파키스탄": ("south_asia", "Asia/Karachi", [
        ("이슬라마바드", 33.6844, 73.0479, True),
        ("카라치", 24.8607, 67.0011, False),
        ("라호르", 31.5497, 74.3436, False),
        ("페샤와르", 34.0151, 71.5249, False),
        ("물탄", 30.1575, 71.5249, False),
    ]),
    "스리랑카": ("south_asia", "Asia/Colombo", [
        ("콜롬보", 6.9271, 79.8612, True),
        ("캔디", 7.2906, 80.6337, False),
    ]),
    "네팔": ("south_asia", "Asia/Kathmandu", [
        ("카트만두", 27.7172, 85.3240, True),
        ("포카라", 28.2096, 83.9856, False),
    ]),
    "카자흐스탄": ("central_asia", "Asia/Almaty", [
        ("아스타나", 51.1694, 71.4491, True),
        ("알마티", 43.2220, 76.8512, False),
        ("심켄트", 42.3417, 69.5901, False),
    ]),
    "우즈베키스탄": ("central_asia", "Asia/Tashkent", [
        ("타슈켄트", 41.2995, 69.2401, True),
        ("사마르칸트", 39.6270, 66.9750, False),
    ]),
    "키르기스스탄": ("central_asia", "Asia/Bishkek", [
        ("비슈케크", 42.8746, 74.5698, True),
    ]),
    "브라질": ("latin_america", "America/Sao_Paulo", [
        ("브라질리아", -15.8267, -47.9218, True),
        ("상파울루", -23.5505, -46.6333, False),
        ("리우데자네이루", -22.9068, -43.1729, False),
        ("살바도르", -12.9777, -38.5016, False),
    ]),
    "멕시코": ("latin_america", "America/Mexico_City", [
        ("멕시코시티", 19.4326, -99.1332, True),
        ("과달라하라", 20.6597, -103.3496, False),
        ("몬테레이", 25.6866, -100.3161, False),
        ("칸쿤", 21.1619, -86.8515, False),
    ]),
    "아르헨티나": ("latin_america", "America/Argentina/Buenos_Aires", [
        ("부에노스아이레스", -34.6037, -58.3816, True),
        ("코르도바", -31.4201, -64.1888, False),
        ("로사리오", -32.9442, -60.6505, False),
    ]),
    "칠레": ("latin_america", "America/Santiago", [
        ("산티아고", -33.4489, -70.6693, True),
        ("발파라이소", -33.0472, -71.6127, False),
    ]),
    "콜롬비아": ("latin_america", "America/Bogota", [
        ("보고타", 4.7110, -74.0721, True),
        ("메데인", 6.2442, -75.5812, False),
        ("칼리", 3.4516, -76.5320, False),
    ]),
    "페루": ("latin_america", "America/Lima", [
        ("리마", -12.0464, -77.0428, True),
        ("아레키파", -16.4090, -71.5375, False),
    ]),
    "쿠바": ("caribbean", "America/Havana", [
        ("아바나", 23.1136, -82.3666, True),
    ]),
    "도미니카공화국": ("caribbean", "America/Santo_Domingo", [
        ("산토도밍고", 18.4861, -69.9312, True),
    ]),
    "미국": ("north_america", "America/New_York", [
        ("워싱턴DC", 38.9072, -77.0369, True),
        ("뉴욕", 40.7128, -74.0060, False),
        ("로스앤젤레스", 34.0522, -118.2437, False),
        ("시카고", 41.8781, -87.6298, False),
    ]),
    "캐나다": ("north_america", "America/Toronto", [
        ("오타와", 45.4215, -75.6972, True),
        ("토론토", 43.6532, -79.3832, False),
        ("밴쿠버", 49.2827, -123.1207, False),
    ]),
    "일본": ("east_asia", "Asia/Tokyo", [
        ("도쿄", 35.6762, 139.6503, True),
        ("오사카", 34.6937, 135.5023, False),
        ("삿포로", 43.0618, 141.3545, False),
        ("후쿠오카", 33.5904, 130.4017, False),
    ]),
    "중국": ("east_asia", "Asia/Shanghai", [
        ("베이징", 39.9042, 116.4074, True),
        ("상하이", 31.2304, 121.4737, False),
        ("광저우", 23.1291, 113.2644, False),
        ("청두", 30.5728, 104.0668, False),
    ]),
    "몽골": ("east_asia", "Asia/Ulaanbaatar", [
        ("울란바토르", 47.8864, 106.9057, True),
    ]),
    "영국": ("europe", "Europe/London", [
        ("런던", 51.5074, -0.1278, True),
        ("맨체스터", 53.4808, -2.2426, False),
    ]),
    "독일": ("europe", "Europe/Berlin", [
        ("베를린", 52.5200, 13.4050, True),
        ("뮌헨", 48.1351, 11.5820, False),
        ("프랑크푸르트", 50.1109, 8.6821, False),
    ]),
    "프랑스": ("europe", "Europe/Paris", [
        ("파리", 48.8566, 2.3522, True),
        ("마르세유", 43.2965, 5.3698, False),
    ]),
    "이탈리아": ("europe", "Europe/Rome", [
        ("로마", 41.9028, 12.4964, True),
        ("밀라노", 45.4642, 9.1900, False),
    ]),
    "스페인": ("europe", "Europe/Madrid", [
        ("마드리드", 40.4168, -3.7038, True),
        ("바르셀로나", 41.3874, 2.1686, False),
    ]),
    "러시아": ("europe", "Europe/Moscow", [
        ("모스크바", 55.7558, 37.6173, True),
        ("상트페테르부르크", 59.9311, 30.3609, False),
        ("노보시비르스크", 55.0084, 82.9357, False),
    ]),
    "폴란드": ("europe", "Europe/Warsaw", [
        ("바르샤바", 52.2297, 21.0122, True),
    ]),
    "우크라이나": ("europe", "Europe/Kyiv", [
        ("키이우", 50.4501, 30.5234, True),
    ]),
    "호주": ("oceania", "Australia/Sydney", [
        ("캔버라", -35.2809, 149.1300, True),
        ("시드니", -33.8688, 151.2093, False),
        ("멜버른", -37.8136, 144.9631, False),
        ("퍼스", -31.9505, 115.8605, False),
    ]),
    "뉴질랜드": ("oceania", "Pacific/Auckland", [
        ("웰링턴", -41.2865, 174.7762, True),
        ("오클랜드", -36.8485, 174.7633, False),
    ]),
    "한국": ("global", "Asia/Seoul", [
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

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

MORNING_HOUR_START = 6
MORNING_HOUR_END = 9  # [6,9) 시 사이에 그 나라 아침으로 판단


def get_local_now(tz_name: str) -> datetime:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))


def should_run_now(tz_name: str):
    """
    이 국가를 지금 실행해야 하는지 판단.
    반환: (실행여부: bool, weekend_mode: bool, local_now: datetime)
    """
    local_now = get_local_now(tz_name)
    hour = local_now.hour
    weekday = local_now.weekday()  # 0=월 ... 6=일

    if weekday in (5, 6):  # 현지 토·일요일은 건너뜀 (금요일에 주말예보 이미 발행)
        return False, False, local_now

    if not (MORNING_HOUR_START <= hour < MORNING_HOUR_END):
        return False, False, local_now

    weekend_mode = (weekday == 4)  # 현지 금요일 아침 → 주말 예보
    return True, weekend_mode, local_now


def fetch_full_weather(lat, lon):
    """Open-Meteo 현재 날씨 + 10일 일별 상세 예보 조회"""
    try:
        res = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current_weather": "true",
                "daily": ",".join([
                    "weathercode",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "apparent_temperature_max",
                    "apparent_temperature_min",
                    "precipitation_sum",
                    "precipitation_probability_max",
                    "windspeed_10m_max",
                    "uv_index_max",
                ]),
                "timezone": "auto",
                "forecast_days": 10,
            },
            timeout=15,
        )
        if res.status_code != 200:
            return None
        data = res.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        if not dates:
            return None

        by_date = {}
        for i, d in enumerate(dates):
            def g(key):
                arr = daily.get(key)
                return arr[i] if arr and i < len(arr) else None
            by_date[d] = {
                "code": g("weathercode"),
                "tmax": g("temperature_2m_max"),
                "tmin": g("temperature_2m_min"),
                "feels_max": g("apparent_temperature_max"),
                "feels_min": g("apparent_temperature_min"),
                "precip": g("precipitation_sum"),
                "precip_prob": g("precipitation_probability_max"),
                "wind_max": g("windspeed_10m_max"),
                "uv": g("uv_index_max"),
            }

        return {
            "current": data.get("current_weather", {}),
            "daily": by_date,
        }
    except Exception as e:
        print(f"  ⚠️ 날씨 조회 실패 ({lat},{lon}): {e}")
        return None


def fmt_num(v, digits=0):
    if v is None:
        return "?"
    return f"{v:.{digits}f}"


def format_day_line(label, day_info, include_current=None):
    """하루치 날씨를 한 줄로 포맷"""
    if not day_info or day_info.get("tmax") is None:
        return f"- {label}: 데이터 없음"

    condition = WEATHER_CODE_KO.get(day_info["code"], f"코드{day_info['code']}")

    detail = (
        f"최고 {fmt_num(day_info['tmax'])}°C/최저 {fmt_num(day_info['tmin'])}°C "
        f"(체감 {fmt_num(day_info['feels_max'])}°C/{fmt_num(day_info['feels_min'])}°C), "
        f"강수확률 {fmt_num(day_info['precip_prob'])}%, 강수량 {fmt_num(day_info['precip'], 1)}mm, "
        f"최대풍속 {fmt_num(day_info['wind_max'])}km/h, 자외선지수 {fmt_num(day_info['uv'], 1)}"
    )

    if include_current is not None:
        return f"- {label}: {condition}, 현재 {fmt_num(include_current)}°C, {detail}"
    return f"- {label}: {condition}, {detail}"


def pick_weekend_dates(dates: list) -> tuple:
    """dates(YYYY-MM-DD 리스트)에서 가장 가까운 토요일·일요일 날짜를 찾는다"""
    sat = sun = None
    for d in dates:
        wd = datetime.strptime(d, "%Y-%m-%d").weekday()  # 5=토, 6=일
        if wd == 5 and sat is None:
            sat = d
        elif wd == 6 and sun is None:
            sun = d
        if sat and sun:
            break
    return sat, sun


# 국가명 → 이미지 검색용 영문명
COUNTRY_EN = {
    "나이지리아": "Nigeria", "케냐": "Kenya", "남아공": "South Africa", "이집트": "Egypt",
    "모로코": "Morocco", "알제리": "Algeria", "에티오피아": "Ethiopia", "가나": "Ghana",
    "탄자니아": "Tanzania", "앙골라": "Angola",
    "베트남": "Vietnam", "인도네시아": "Indonesia", "태국": "Thailand", "필리핀": "Philippines",
    "미얀마": "Myanmar", "캄보디아": "Cambodia", "말레이시아": "Malaysia", "싱가포르": "Singapore",
    "사우디아라비아": "Saudi Arabia", "아랍에미리트": "United Arab Emirates", "튀르키예": "Turkey",
    "이스라엘": "Israel", "이란": "Iran", "이라크": "Iraq", "카타르": "Qatar", "요르단": "Jordan",
    "인도": "India", "방글라데시": "Bangladesh", "파키스탄": "Pakistan", "스리랑카": "Sri Lanka",
    "네팔": "Nepal",
    "카자흐스탄": "Kazakhstan", "우즈베키스탄": "Uzbekistan", "키르기스스탄": "Kyrgyzstan",
    "브라질": "Brazil", "멕시코": "Mexico", "아르헨티나": "Argentina", "칠레": "Chile",
    "콜롬비아": "Colombia", "페루": "Peru",
    "쿠바": "Cuba", "도미니카공화국": "Dominican Republic",
    "미국": "United States", "캐나다": "Canada",
    "일본": "Japan", "중국": "China", "몽골": "Mongolia",
    "영국": "United Kingdom", "독일": "Germany", "프랑스": "France", "이탈리아": "Italy",
    "스페인": "Spain", "러시아": "Russia", "폴란드": "Poland", "우크라이나": "Ukraine",
    "호주": "Australia", "뉴질랜드": "New Zealand",
    "한국": "South Korea",
}

_image_cache = {}  # 국가별 이미지 URL 캐시 (같은 실행에서 today+weekly 재사용)


def fetch_country_image(country_name: str) -> str:
    """Pixabay에서 국가 대표 이미지(스카이라인/랜드마크) 1건 조회. 실행 중 국가당 1회만 호출."""
    if country_name in _image_cache:
        return _image_cache[country_name]

    if not PIXABAY_API_KEY:
        _image_cache[country_name] = ""
        return ""

    country_en = COUNTRY_EN.get(country_name, country_name)
    query = f"{country_en} skyline landmark"

    try:
        res = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "safesearch": "true",
                "orientation": "horizontal",
                "per_page": 3,
            },
            timeout=15,
        )
        image_url = ""
        if res.status_code == 200:
            hits = res.json().get("hits", [])
            if hits:
                image_url = hits[0].get("largeImageURL", "")
        else:
            print(f"  ⚠️ Pixabay {res.status_code}: {res.text[:100]}")
        _image_cache[country_name] = image_url
        return image_url
    except Exception as e:
        print(f"  ⚠️ Pixabay 실패 ({country_name}): {e}")
        _image_cache[country_name] = ""
        return ""


def fetch_cities_weather(cities: list) -> list:
    """도시 목록에 대해 날씨를 한 번씩만 조회 (오늘/주말/주간 리포트가 이 결과를 공유해서 재사용)"""
    results = []
    for name, lat, lon, is_capital in cities:
        w = fetch_full_weather(lat, lon)
        results.append((name, is_capital, w))
    return results


def build_today_report(country_name, weather_list, local_now: datetime):
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]}, 현지시간)"
    lines = []
    any_success = False

    for name, is_capital, w in weather_list:
        label = f"{name}(수도)" if is_capital else name
        if w is None:
            lines.append(f"- {label}: 데이터 없음")
            continue
        dates = list(w["daily"].keys())
        today_key = dates[0] if dates else None
        today_info = w["daily"].get(today_key) if today_key else None
        current_temp = w["current"].get("temperature")
        lines.append(format_day_line(label, today_info, include_current=current_temp))
        any_success = any_success or (today_info and today_info.get("tmax") is not None)

    if not any_success:
        return None, None

    title = f"오늘의 {country_name} 날씨 ({local_now.strftime('%m월 %d일')}, 현지시간)"
    body = f"{today_str} 기준 {country_name} 주요 지역 실시간 날씨입니다.\n\n" + "\n".join(lines)
    return title, body


def build_weekend_report(country_name, weather_list, local_now: datetime):
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]}, 현지시간)"
    lines = []
    any_success = False

    for name, is_capital, w in weather_list:
        label = f"{name}(수도)" if is_capital else name
        if w is None:
            lines.append(f"- {label}: 데이터 없음")
            continue
        dates = list(w["daily"].keys())
        sat, sun = pick_weekend_dates(dates)
        if sat:
            lines.append(format_day_line(f"{label} 토요일({sat})", w["daily"].get(sat)))
            any_success = any_success or w["daily"].get(sat, {}).get("tmax") is not None
        if sun:
            lines.append(format_day_line(f"{label} 일요일({sun})", w["daily"].get(sun)))
            any_success = any_success or w["daily"].get(sun, {}).get("tmax") is not None

    if not any_success:
        return None, None

    title = f"주말 {country_name} 날씨 예보 ({local_now.strftime('%m월 %d일')} 현지 금요일 아침 발표)"
    body = f"{today_str} 발표된 {country_name} 주요 지역 주말(토·일) 날씨 예보입니다.\n\n" + "\n".join(lines)
    return title, body


def build_weekly_report(country_name, weather_list, local_now: datetime):
    """월요일 아침 전용 — 이번주(월~금 5일) 예보"""
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]}, 현지시간)"
    lines = []
    any_success = False

    for name, is_capital, w in weather_list:
        label = f"{name}(수도)" if is_capital else name
        if w is None:
            lines.append(f"- {label}: 데이터 없음")
            continue
        dates = list(w["daily"].keys())[:5]  # 오늘(월)부터 금요일까지 5일
        for d in dates:
            wd_ko = WEEKDAY_KO[datetime.strptime(d, "%Y-%m-%d").weekday()]
            lines.append(format_day_line(f"{label} {wd_ko}요일({d})", w["daily"].get(d)))
            any_success = any_success or w["daily"].get(d, {}).get("tmax") is not None

    if not any_success:
        return None, None

    title = f"이번주 {country_name} 날씨 예보 (월~금, {local_now.strftime('%m월 %d일')} 현지 월요일 아침 발표)"
    body = f"{today_str} 발표된 {country_name} 주요 지역 이번주(월~금) 날씨 예보입니다.\n\n" + "\n".join(lines)
    return title, body


def save_report(country_name, region, title, body, kind: str, local_now: datetime):
    """kind: 'today' | 'weekend' | 'weekly' — DB 중복방지 태그 구분용"""
    now_str_kst = now_kst().strftime("%Y-%m-%d %H:%M")  # DB created_at은 사이트 표준(KST)으로 저장
    tag = f"{kind}{local_now.strftime('%Y%m%d')}"
    subcategory = f"weather_{country_name}_{tag}"

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
        print(f"  [SKIP] {country_name}({kind}) 리포트 이미 존재 (현지 {local_now.strftime('%Y-%m-%d %H:%M')})")
        return

    image_url = fetch_country_image(country_name)

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
        "image_url": image_url,
        "score": 1,
        "created_at": now_str_kst,
        "first_published_at": now_str_kst,
        "update_log": [{"timestamp": now_str_kst, "note": "최초 게시"}],
        "sent_telegram": 0,
        "is_published": True,
        "posted_blog": 0,
        "dedup_reviewed": True,
    }
    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        art_id = data[0].get("id", -1) if data else -1
        print(f"  ✅ {country_name}({kind}) 저장 완료 (id={art_id}, 현지 {local_now.strftime('%H:%M')})")
    else:
        print(f"  ❌ {country_name}({kind}) 저장 실패: HTTP {res.status_code} - {res.text[:200]}")


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    print(f"[날씨] {len(COUNTRIES)}개국 점검 — 현재 UTC {datetime.now(timezone.utc).strftime('%H:%M')}")

    ran = 0
    for country_name, (region, tz_name, cities) in COUNTRIES.items():
        do_run, weekend_mode, local_now = should_run_now(tz_name)
        if not do_run:
            continue

        is_monday = (local_now.weekday() == 0)
        mode_label = "주말예보" if weekend_mode else ("오늘날씨+주간예보" if is_monday else "오늘날씨")
        print(f"→ {country_name} (현지 {local_now.strftime('%H:%M')}, {mode_label})")

        weather_list = fetch_cities_weather(cities)

        if weekend_mode:
            title, body = build_weekend_report(country_name, weather_list, local_now)
            if title:
                save_report(country_name, region, title, body, "weekend", local_now)
                ran += 1
            else:
                print(f"  ❌ {country_name} 모든 도시 조회 실패 — 건너뜀")
        else:
            title, body = build_today_report(country_name, weather_list, local_now)
            if title:
                save_report(country_name, region, title, body, "today", local_now)
                ran += 1
            else:
                print(f"  ❌ {country_name} 모든 도시 조회 실패 — 건너뜀")

            if is_monday:
                wtitle, wbody = build_weekly_report(country_name, weather_list, local_now)
                if wtitle:
                    save_report(country_name, region, wtitle, wbody, "weekly", local_now)
                    ran += 1

    print(f"[날씨] 완료 — 이번 실행에서 {ran}건 처리")


if __name__ == "__main__":
    run()
