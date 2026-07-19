"""
weather_report.py
------------------
전세계 주요국 + 프론티어마켓 국가들의 날씨를 실시간 API(Open-Meteo, 무료·키 불필요)로
조회한다. 한국을 제외한 국가들은 대륙 그룹(아프리카/동남아시아/중동/남아시아/중앙아시아/
중남미/북미/동아시아/유럽/오세아니아, 총 10개)으로 묶어 각 그룹당 기사 하나를 생성하고,
한국은 별도의 개별 기사로 발행한다. Gemini를 쓰지 않는다 — 실제 수치만 사용.

- 각 그룹은 대표 국가의 "현지 아침"(06~09시) 시간대에 발행한다 (IANA 타임존 기준).
- 현지 기준 월~금 아침: 오늘 날씨.
- 현지 기준 토요일 아침: 주말(토·일) 예보.
- 현지 기준 일요일 아침: 다음주(월~금) 예보.
- 모든 리포트는 요약 문단으로 시작한 뒤 국가별·지역별 상세 데이터가 이어진다.
- 워크플로우는 매시 정각에 실행되며, 이 스크립트가 그룹별로 "지금이 발행 시점인지" 판단한다.
- 대표 이미지는 Pixabay에서 조회해 자동 삽입한다 (태그 기반 검증 포함).

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

# ── 대륙 그룹 (한국 제외 — 한국은 별도 개별 기사) ──────────
# group_name: {region, tz(대표 국가 타임존), primary(대표국·이미지용), countries(그룹 소속 국가명 리스트)}
GROUPS = {
    "아프리카": {
        "region": "africa", "tz": "Africa/Lagos", "primary": "나이지리아",
        "countries": ["나이지리아", "케냐", "남아공", "이집트", "모로코", "알제리", "에티오피아", "가나", "탄자니아", "앙골라"],
    },
    "동남아시아": {
        "region": "southeast_asia", "tz": "Asia/Jakarta", "primary": "인도네시아",
        "countries": ["베트남", "인도네시아", "태국", "필리핀", "미얀마", "캄보디아", "말레이시아", "싱가포르"],
    },
    "중동": {
        "region": "middle_east", "tz": "Asia/Riyadh", "primary": "사우디아라비아",
        "countries": ["사우디아라비아", "아랍에미리트", "튀르키예", "이스라엘", "이란", "이라크", "카타르", "요르단"],
    },
    "남아시아": {
        "region": "south_asia", "tz": "Asia/Kolkata", "primary": "인도",
        "countries": ["인도", "방글라데시", "파키스탄", "스리랑카", "네팔"],
    },
    "중앙아시아": {
        "region": "central_asia", "tz": "Asia/Almaty", "primary": "카자흐스탄",
        "countries": ["카자흐스탄", "우즈베키스탄", "키르기스스탄"],
    },
    "중남미": {
        "region": "latin_america", "tz": "America/Sao_Paulo", "primary": "브라질",
        "countries": ["브라질", "멕시코", "아르헨티나", "칠레", "콜롬비아", "페루", "쿠바", "도미니카공화국"],
    },
    "북미": {
        "region": "north_america", "tz": "America/New_York", "primary": "미국",
        "countries": ["미국", "캐나다"],
    },
    "동아시아": {
        "region": "east_asia", "tz": "Asia/Shanghai", "primary": "중국",
        "countries": ["일본", "중국", "몽골"],
    },
    "유럽": {
        "region": "europe", "tz": "Europe/Berlin", "primary": "독일",
        "countries": ["영국", "독일", "프랑스", "이탈리아", "스페인", "러시아", "폴란드", "우크라이나"],
    },
    "오세아니아": {
        "region": "oceania", "tz": "Australia/Sydney", "primary": "호주",
        "countries": ["호주", "뉴질랜드"],
    },
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
    이 국가를 지금 실행해야 하는지, 어떤 리포트를 만들어야 하는지 판단.
    반환: (mode, local_now)
      mode: 'today'(오늘 날씨, 월~금) | 'weekend'(주말예보, 토요일 아침)
            | 'weekly'(다음주 월~금 예보, 일요일 아침)
    """
    local_now = get_local_now(tz_name)
    hour = local_now.hour
    weekday = local_now.weekday()  # 0=월 ... 6=일

    if not (MORNING_HOUR_START <= hour < MORNING_HOUR_END):
        return None, local_now

    if weekday == 6:  # 일요일 아침 — 다음주(월~금) 예보
        return "weekly", local_now
    if weekday == 5:  # 토요일 아침 — 주말(토·일) 예보
        return "weekend", local_now
    return "today", local_now  # 월~금 — 오늘 날씨


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

# 이 태그가 있으면 제외 (인물/전쟁/지도/국기 등 날씨 기사와 안 맞는 이미지 배제)
IMAGE_TAG_BLACKLIST = {
    "war", "military", "soldier", "weapon", "gun", "conflict", "protest", "riot",
    "flag", "map", "person", "people", "portrait", "face", "man", "woman",
    "child", "children", "wedding", "funeral", "police", "crime", "accident",
    "corpse", "death", "blood", "nude", "naked", "sexy",
}

# 이 태그 중 하나라도 있으면 "도시/풍경" 이미지로 인정 (관련성 확인용)
IMAGE_TAG_ALLOWLIST = {
    "city", "skyline", "cityscape", "building", "buildings", "architecture",
    "landmark", "tower", "downtown", "urban", "street", "landscape",
    "sky", "sunset", "sunrise", "panorama", "travel", "tourism",
}


def _is_image_suitable(hit: dict, country_en: str) -> bool:
    tags = {t.strip().lower() for t in (hit.get("tags") or "").split(",")}
    if tags & IMAGE_TAG_BLACKLIST:
        return False
    country_words = set(country_en.lower().split())
    if tags & IMAGE_TAG_ALLOWLIST or (tags & country_words):
        return True
    return False


def fetch_country_image(country_name: str) -> str:
    """
    Pixabay에서 국가 대표 이미지(스카이라인/랜드마크)를 조회.
    카테고리를 'places'로 제한하고, 태그를 검사해 부적절하거나 무관한 이미지는 걸러낸다.
    적합한 이미지가 없으면 빈 문자열(이미지 없음)을 반환 — 억지로 아무 이미지나 넣지 않는다.
    실행 중 국가당 1회만 호출(캐싱).
    """
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
                "category": "places",
                "safesearch": "true",
                "orientation": "horizontal",
                "min_width": 640,
                "per_page": 10,
            },
            timeout=15,
        )
        image_url = ""
        if res.status_code == 200:
            hits = res.json().get("hits", [])
            for hit in hits:
                if _is_image_suitable(hit, country_en):
                    image_url = hit.get("largeImageURL", "")
                    break
            if not image_url:
                print(f"  ⚠️ {country_name}: 적합한 이미지 없음 — 이미지 없이 발행")
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


def _describe_condition(precip_prob):
    """강수확률 기반 간단 설명 문구"""
    if precip_prob is None:
        return ""
    if precip_prob >= 60:
        return " 비 소식이 있으니 우산을 챙기는 게 좋겠습니다."
    if precip_prob >= 30:
        return " 흐리거나 비가 오락가락할 수 있습니다."
    return " 비 소식 없이 대체로 맑은 날씨가 예상됩니다."


def _find_capital(weather_list):
    for name, is_capital, w in weather_list:
        if is_capital:
            return name, w
    return (weather_list[0][0], weather_list[0][2]) if weather_list else (None, None)


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

    # 수도 기준 설명 문단
    cap_name, cap_w = _find_capital(weather_list)
    summary = ""
    if cap_w is not None:
        dates = list(cap_w["daily"].keys())
        cap_info = cap_w["daily"].get(dates[0]) if dates else None
        if cap_info and cap_info.get("tmax") is not None:
            condition = WEATHER_CODE_KO.get(cap_info["code"], "")
            summary = (
                f"{today_str}, {country_name}의 수도 {cap_name}은 {condition}이며 "
                f"최고 {fmt_num(cap_info['tmax'])}°C, 최저 {fmt_num(cap_info['tmin'])}°C가 예상됩니다."
                f"{_describe_condition(cap_info.get('precip_prob'))}"
                f" 그 외 주요 지역 날씨는 아래와 같습니다."
            )

    if not summary:
        summary = f"{today_str} 기준 {country_name} 주요 지역 실시간 날씨입니다."

    title = f"오늘의 {country_name} 날씨 ({local_now.strftime('%m월 %d일')}, 현지시간)"
    body = summary + "\n\n" + "\n".join(lines)
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

    # 수도 기준 설명 문단
    cap_name, cap_w = _find_capital(weather_list)
    summary = ""
    if cap_w is not None:
        dates = list(cap_w["daily"].keys())
        sat, sun = pick_weekend_dates(dates)
        sat_info = cap_w["daily"].get(sat) if sat else None
        sun_info = cap_w["daily"].get(sun) if sun else None
        if sat_info and sun_info and sat_info.get("tmax") is not None and sun_info.get("tmax") is not None:
            sat_cond = WEATHER_CODE_KO.get(sat_info["code"], "")
            sun_cond = WEATHER_CODE_KO.get(sun_info["code"], "")
            max_precip = max(sat_info.get("precip_prob") or 0, sun_info.get("precip_prob") or 0)
            summary = (
                f"이번 주말 {country_name}의 수도 {cap_name}은 토요일 {sat_cond}"
                f"(최고 {fmt_num(sat_info['tmax'])}°C/최저 {fmt_num(sat_info['tmin'])}°C), "
                f"일요일 {sun_cond}(최고 {fmt_num(sun_info['tmax'])}°C/최저 {fmt_num(sun_info['tmin'])}°C)로 예상됩니다."
                f"{_describe_condition(max_precip)}"
                f" 그 외 주요 지역 예보는 아래와 같습니다."
            )

    if not summary:
        summary = f"{today_str} 발표된 {country_name} 주요 지역 주말(토·일) 날씨 예보입니다."

    title = f"주말 {country_name} 날씨 예보 ({local_now.strftime('%m월 %d일')} 현지 토요일 아침 발표)"
    body = summary + "\n\n" + "\n".join(lines)
    return title, body


def build_weekly_report(country_name, weather_list, local_now: datetime):
    """일요일 아침 전용 — 다음주(월~금 5일) 예보"""
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]}, 현지시간)"
    lines = []
    any_success = False

    for name, is_capital, w in weather_list:
        label = f"{name}(수도)" if is_capital else name
        if w is None:
            lines.append(f"- {label}: 데이터 없음")
            continue
        # 일요일에 조회하므로 dates[0]=오늘(일요일), dates[1:6]=다음주 월~금
        dates = list(w["daily"].keys())[1:6]
        for d in dates:
            wd_ko = WEEKDAY_KO[datetime.strptime(d, "%Y-%m-%d").weekday()]
            lines.append(format_day_line(f"{label} {wd_ko}요일({d})", w["daily"].get(d)))
            any_success = any_success or w["daily"].get(d, {}).get("tmax") is not None

    if not any_success:
        return None, None

    # 수도 기준 설명 문단 (5일 범위 요약)
    cap_name, cap_w = _find_capital(weather_list)
    summary = ""
    if cap_w is not None:
        cap_dates = list(cap_w["daily"].keys())[1:6]
        cap_days = [cap_w["daily"].get(d) for d in cap_dates if cap_w["daily"].get(d, {}).get("tmax") is not None]
        if cap_days:
            tmax_all = [d["tmax"] for d in cap_days]
            tmin_all = [d["tmin"] for d in cap_days]
            rain_days = sum(1 for d in cap_days if (d.get("precip_prob") or 0) >= 50)
            rain_str = (
                f"5일 중 {rain_days}일 정도 비가 예상되니 참고하시기 바랍니다."
                if rain_days > 0 else "당분간 비 소식 없이 대체로 맑은 날씨가 이어질 전망입니다."
            )
            summary = (
                f"{today_str} 발표된 다음주(월~금) 전망입니다. {country_name}의 수도 {cap_name}은 "
                f"이번 주 최고 {fmt_num(max(tmax_all))}°C, 최저 {fmt_num(min(tmin_all))}°C 사이를 오갈 것으로 보입니다. "
                f"{rain_str} 그 외 주요 지역 예보는 아래와 같습니다."
            )

    if not summary:
        summary = f"{today_str} 발표된 {country_name} 주요 지역 다음주(월~금) 날씨 예보입니다."

    title = f"다음주 {country_name} 날씨 예보 (월~금, {local_now.strftime('%m월 %d일')} 현지 일요일 아침 발표)"
    body = summary + "\n\n" + "\n".join(lines)
    return title, body


def _capital_day_info(weather_list, date_key):
    """weather_list에서 수도의 특정 날짜 day_info를 반환"""
    cap_name, cap_w = _find_capital(weather_list)
    if cap_w is None:
        return cap_name, None
    return cap_name, cap_w["daily"].get(date_key)


def build_group_today_report(group_name, countries_data, local_now: datetime):
    """countries_data: [(country_name, weather_list), ...] — 대륙 그룹 오늘 날씨"""
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]}, 현지시간)"

    capitals_info = []  # (country_name, capital_name, day_info)
    country_blocks = []
    any_success = False

    for country_name, weather_list in countries_data:
        lines = [f"[{country_name}]"]
        cap_name, cap_w = _find_capital(weather_list)
        cap_today = None
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
            if today_info and today_info.get("tmax") is not None:
                any_success = True
                if is_capital:
                    cap_today = today_info
        country_blocks.append("\n".join(lines))
        capitals_info.append((country_name, cap_name, cap_today))

    if not any_success:
        return None, None

    successful_caps = [c for c in capitals_info if c[2] is not None]
    summary = f"{today_str} 기준 {group_name} 주요국 실시간 날씨입니다."
    if successful_caps:
        tmax_all = [c[2]["tmax"] for c in successful_caps]
        tmin_all = [c[2]["tmin"] for c in successful_caps]
        rainy = [c[0] for c in successful_caps if (c[2].get("precip_prob") or 0) >= 50]
        summary = (
            f"{today_str} 기준 {group_name} 주요국은 최저 {fmt_num(min(tmin_all))}°C에서 "
            f"최고 {fmt_num(max(tmax_all))}°C 사이의 기온을 보이고 있습니다."
        )
        if rainy:
            summary += f" {', '.join(rainy[:5])} 등에서는 비 소식이 있습니다."
        else:
            summary += " 대체로 비 소식 없이 맑은 날씨입니다."
        summary += " 국가별 상세는 아래와 같습니다."

    title = f"오늘의 {group_name} 날씨 ({local_now.strftime('%m월 %d일')}, 현지시간)"
    body = summary + "\n\n" + "\n\n".join(country_blocks)
    return title, body


def build_group_weekend_report(group_name, countries_data, local_now: datetime):
    """대륙 그룹 주말(토·일) 예보"""
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]}, 현지시간)"

    capitals_info = []
    country_blocks = []
    any_success = False

    for country_name, weather_list in countries_data:
        lines = [f"[{country_name}]"]
        cap_name, cap_w = _find_capital(weather_list)
        cap_sat = cap_sun = None
        for name, is_capital, w in weather_list:
            label = f"{name}(수도)" if is_capital else name
            if w is None:
                lines.append(f"- {label}: 데이터 없음")
                continue
            dates = list(w["daily"].keys())
            sat, sun = pick_weekend_dates(dates)
            if sat:
                info = w["daily"].get(sat)
                lines.append(format_day_line(f"{label} 토요일({sat})", info))
                if info and info.get("tmax") is not None:
                    any_success = True
                    if is_capital:
                        cap_sat = info
            if sun:
                info = w["daily"].get(sun)
                lines.append(format_day_line(f"{label} 일요일({sun})", info))
                if info and info.get("tmax") is not None:
                    any_success = True
                    if is_capital:
                        cap_sun = info
        country_blocks.append("\n".join(lines))
        capitals_info.append((country_name, cap_sat, cap_sun))

    if not any_success:
        return None, None

    successful = [c for c in capitals_info if c[1] and c[2]]
    summary = f"{today_str} 발표된 {group_name} 주요국 주말(토·일) 날씨 예보입니다."
    if successful:
        all_tmax = [c[1]["tmax"] for c in successful] + [c[2]["tmax"] for c in successful]
        all_tmin = [c[1]["tmin"] for c in successful] + [c[2]["tmin"] for c in successful]
        rainy = [c[0] for c in successful if max(c[1].get("precip_prob") or 0, c[2].get("precip_prob") or 0) >= 50]
        summary = (
            f"이번 주말 {group_name} 주요국은 최저 {fmt_num(min(all_tmin))}°C에서 "
            f"최고 {fmt_num(max(all_tmax))}°C 사이를 오갈 전망입니다."
        )
        if rainy:
            summary += f" {', '.join(rainy[:5])} 등에서는 비 소식이 있습니다."
        else:
            summary += " 대체로 비 소식 없이 맑을 전망입니다."
        summary += " 국가별 상세는 아래와 같습니다."

    title = f"주말 {group_name} 날씨 예보 ({local_now.strftime('%m월 %d일')} 현지 토요일 아침 발표)"
    body = summary + "\n\n" + "\n\n".join(country_blocks)
    return title, body


def build_group_weekly_report(group_name, countries_data, local_now: datetime):
    """대륙 그룹 다음주(월~금) 예보 — 일요일 아침 전용"""
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]}, 현지시간)"

    capitals_ranges = []  # (country_name, [day_info,...])
    country_blocks = []
    any_success = False

    for country_name, weather_list in countries_data:
        lines = [f"[{country_name}]"]
        cap_name, cap_w = _find_capital(weather_list)
        cap_days = []
        for name, is_capital, w in weather_list:
            label = f"{name}(수도)" if is_capital else name
            if w is None:
                lines.append(f"- {label}: 데이터 없음")
                continue
            dates = list(w["daily"].keys())[1:6]
            for d in dates:
                info = w["daily"].get(d)
                wd_ko = WEEKDAY_KO[datetime.strptime(d, "%Y-%m-%d").weekday()]
                lines.append(format_day_line(f"{label} {wd_ko}요일({d})", info))
                if info and info.get("tmax") is not None:
                    any_success = True
                    if is_capital:
                        cap_days.append(info)
        country_blocks.append("\n".join(lines))
        capitals_ranges.append((country_name, cap_days))

    if not any_success:
        return None, None

    successful = [c for c in capitals_ranges if c[1]]
    summary = f"{today_str} 발표된 {group_name} 주요국 다음주(월~금) 날씨 예보입니다."
    if successful:
        all_tmax = [d["tmax"] for c in successful for d in c[1]]
        all_tmin = [d["tmin"] for c in successful for d in c[1]]
        rainy = [c[0] for c in successful if any((d.get("precip_prob") or 0) >= 50 for d in c[1])]
        summary = (
            f"{today_str} 발표된 다음주(월~금) 전망입니다. {group_name} 주요국은 최저 {fmt_num(min(all_tmin))}°C에서 "
            f"최고 {fmt_num(max(all_tmax))}°C 사이를 오갈 것으로 보입니다."
        )
        if rainy:
            summary += f" {', '.join(rainy[:5])} 등에서는 비 소식이 있는 날이 있으니 참고하시기 바랍니다."
        else:
            summary += " 당분간 비 소식 없이 대체로 맑은 날씨가 이어질 전망입니다."
        summary += " 국가별 상세는 아래와 같습니다."

    title = f"다음주 {group_name} 날씨 예보 (월~금, {local_now.strftime('%m월 %d일')} 현지 일요일 아침 발표)"
    body = summary + "\n\n" + "\n\n".join(country_blocks)
    return title, body


def save_report(name, region, title, body, kind: str, local_now: datetime, countries: list, image_country: str, key: str):
    """
    name: 표시용 이름(국가명 또는 대륙 그룹명) — country 필드와 위젯 표시에 사용
    key: subcategory에 쓸 고유 키(국가명 또는 'group_아프리카' 등, 다른 종류끼리 충돌 방지)
    countries: DB의 countries 배열에 넣을 국가명 리스트
    image_country: Pixabay 이미지 검색에 사용할 국가명(그룹이면 대표국)
    kind: 'today' | 'weekend' | 'weekly' — DB 중복방지 태그 구분용
    """
    now_str_kst = now_kst().strftime("%Y-%m-%d %H:%M")  # DB created_at은 사이트 표준(KST)으로 저장
    tag = f"{kind}{local_now.strftime('%Y%m%d')}"
    subcategory = f"weather_{key}_{tag}"

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
        print(f"  [SKIP] {name}({kind}) 리포트 이미 존재 (현지 {local_now.strftime('%Y-%m-%d %H:%M')})")
        return

    image_url = fetch_country_image(image_country)

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
        "country": name,
        "country_flag": "",
        "countries": countries,
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
        print(f"  ✅ {name}({kind}) 저장 완료 (id={art_id}, 현지 {local_now.strftime('%H:%M')})")
    else:
        print(f"  ❌ {name}({kind}) 저장 실패: HTTP {res.status_code} - {res.text[:200]}")


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    print(f"[날씨] 대륙그룹 {len(GROUPS)}개 + 한국 점검 — 현재 UTC {datetime.now(timezone.utc).strftime('%H:%M')}")

    GROUP_BUILDERS = {
        "today": build_group_today_report,
        "weekend": build_group_weekend_report,
        "weekly": build_group_weekly_report,
    }
    COUNTRY_BUILDERS = {
        "today": build_today_report,
        "weekend": build_weekend_report,
        "weekly": build_weekly_report,
    }
    MODE_LABEL = {"today": "오늘날씨", "weekend": "주말예보", "weekly": "다음주예보"}

    ran = 0

    # ── 대륙 그룹 처리 ──
    for group_name, gconf in GROUPS.items():
        mode, local_now = should_run_now(gconf["tz"])
        if mode is None:
            continue

        print(f"→ [그룹] {group_name} (현지 {local_now.strftime('%H:%M')}, {MODE_LABEL[mode]})")

        countries_data = []
        for country_name in gconf["countries"]:
            _, _, cities = COUNTRIES[country_name]
            weather_list = fetch_cities_weather(cities)
            countries_data.append((country_name, weather_list))

        title, body = GROUP_BUILDERS[mode](group_name, countries_data, local_now)
        if title:
            save_report(
                group_name, gconf["region"], title, body, mode, local_now,
                countries=gconf["countries"], image_country=gconf["primary"],
                key=f"group_{group_name}",
            )
            ran += 1
        else:
            print(f"  ❌ {group_name} 모든 국가 조회 실패 — 건너뜀")

    # ── 한국은 별도 개별 기사 ──
    region, tz_name, cities = COUNTRIES["한국"]
    mode, local_now = should_run_now(tz_name)
    if mode is not None:
        print(f"→ 한국 (현지 {local_now.strftime('%H:%M')}, {MODE_LABEL[mode]})")
        weather_list = fetch_cities_weather(cities)
        title, body = COUNTRY_BUILDERS[mode]("한국", weather_list, local_now)
        if title:
            save_report(
                "한국", region, title, body, mode, local_now,
                countries=["한국"], image_country="한국", key="한국",
            )
            ran += 1
        else:
            print(f"  ❌ 한국 모든 도시 조회 실패 — 건너뜀")

    print(f"[날씨] 완료 — 이번 실행에서 {ran}건 처리")


if __name__ == "__main__":
    run()
