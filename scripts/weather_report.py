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
- 여러 날짜(주말·주간)를 다룰 때는 도시당 한 줄로 압축해서 보여준다(하루하루 나열하지 않음).
- 워크플로우는 매시 정각에 실행되며, 이 스크립트가 그룹별로 "지금이 발행 시점인지" 판단한다.
- 대표 이미지는 Pixabay에서 조회해 자동 삽입한다 (태그 기반 검증 포함).

실행: python scripts/weather_report.py
"""

import math
import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
KMA_API_KEY = os.getenv("KMA_API_KEY", "")
KMA_BRIEFING_KEY = os.getenv("KMA_BRIEFING_KEY", "")

# ── Gemini 설정 ──────────────────────────────────────────────
GEMINI_MODEL_PRIMARY  = "gemini-3.5-flash-lite"
GEMINI_MODEL_FALLBACK = "gemini-3.1-flash-lite"
GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

_current_key_idx = 0
_exhausted_keys_primary  = set()  # RPD 소진 키 (3.5)
_exhausted_keys_fallback = set()  # RPD 소진 키 (3.1)


def call_gemini_weather(prompt: str, max_tokens: int = 1200) -> str | None:
    """날씨 기사 본문 생성용 Gemini 호출 (키 로테이션)."""
    global _current_key_idx, _exhausted_keys_primary, _exhausted_keys_fallback

    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": max_tokens,
        },
    }

    n = len(GEMINI_API_KEYS)
    model_stages = [
        (GEMINI_MODEL_PRIMARY,  _exhausted_keys_primary),
        (GEMINI_MODEL_FALLBACK, _exhausted_keys_fallback),
    ]

    for model, exhausted in model_stages:
        available = [i for i in range(n) if i not in exhausted]
        if not available:
            print(f"  [{model}] 모든 Gemini 키 RPD 소진 → 다음 모델로")
            continue

        ordered = sorted(available, key=lambda i: (i - _current_key_idx) % n)

        for idx in ordered:
            api_key = GEMINI_API_KEYS[idx]
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                res = requests.post(url, json=payload, timeout=(10, 60))
                if res.status_code == 200:
                    _current_key_idx = (idx + 1) % n
                    cands = res.json().get("candidates", [])
                    if not cands:
                        return None
                    parts = cands[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts).strip()
                    return text if text else None
                elif res.status_code == 429:
                    print(f"  [429] {model} 키 {idx+1} RPD 소진 — 블랙리스트")
                    exhausted.add(idx)
                    continue
                elif res.status_code == 503:
                    print(f"  [503] {model} 키 {idx+1} 과부하 → 다음 키")
                    continue
                else:
                    print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                    return None
            except requests.exceptions.Timeout:
                print(f"  [TIMEOUT] {model} 키 {idx+1} → 다음 키")
                continue
            except Exception as e:
                print(f"[ERROR] {e}")
                return None

    print("[ERROR] 모든 Gemini 모델/키 소진 또는 응답 없음")
    return None


def _strip_local_time_kr(text: str) -> str:
    """한국 날씨 기사 전용 후처리. Gemini가 지시를 어기고 날짜 뒤에
    "(현지시간)"을 붙이는 경우가 있어(강제성 없는 프롬프트 지시라 준수율이
    들쭉날쭉함), 코드 단에서 강제 제거. 한국 기사는 KST 기준이라
    "(현지시간)" 표기가 불필요·부적절하다. 국가·괄호 표기 변형까지 흡수.
    예: "15일(현지시간)" → "15일", "15일 (현지 시간)" → "15일"."""
    if not text:
        return text
    # 날짜(N일) 직후에 오는 (현지시간)/(현지 시간) 및 앞 공백 제거
    text = re.sub(r"(\d{1,2}\s*일)\s*[（(]\s*현지\s*시간\s*[)）]", r"\1", text)
    # 날짜와 무관하게 남은 (현지시간) 표기도 제거(공백 정리 포함)
    text = re.sub(r"\s*[（(]\s*현지\s*시간\s*[)）]", "", text)
    return text


def _ensure_paragraphs(text: str, target: int = 3) -> str:
    """Gemini가 프롬프트의 '2~3개 문단으로 나누어 작성' 지시를 어기고
    \\n\\n 없이 한 덩어리로 응답하는 경우가 있어(강제성 없는 지시라 준수율이
    들쭉날쭉함), 코드 단에서 문장(-다.) 단위로 강제 분할하는 안전장치.
    이미 \\n\\n이 있으면(모델이 지시를 따른 경우) 손대지 않고 그대로 반환.
    문장이 2개 이상이면 항상 최소 2개 문단으로 분할한다(짧은 리드 문단도 포함)."""
    if not text or "\n\n" in text:
        return text
    # "-다." 체 종결(전 프롬프트에서 강제)을 문장 경계로 사용 —
    # 소수점(예: 2.5mm) 등 다른 마침표와 섞이지 않아 안전.
    sentences = [s.strip() for s in re.split(r"(?<=다\.)\s+", text.strip()) if s.strip()]
    if len(sentences) < 2:
        return text  # 문장이 1개뿐이면 분할 불가
    actual_target = min(target, len(sentences) - 1)
    actual_target = max(actual_target, 2)
    n = len(sentences)
    size = math.ceil(n / actual_target)
    groups = [sentences[i:i + size] for i in range(0, n, size)]
    return "\n\n".join(" ".join(g) for g in groups)


def _parse_gemini_weather_response(raw: str | None) -> tuple[str, str]:
    """Gemini 응답(JSON)에서 title·body 분리. 실패 시 ("", raw)."""
    if not raw:
        return "", ""
    try:
        import json as _json
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned.strip())
        parsed = _json.loads(cleaned)
        return parsed.get("title", ""), _ensure_paragraphs(parsed.get("body", ""))
    except Exception:
        return "", _ensure_paragraphs(raw)  # fallback: 본문 전체를 body로


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
MORNING_HOUR_END = 9


def get_local_now(tz_name: str) -> datetime:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name))


def should_run_now(tz_name: str):
    local_now = get_local_now(tz_name)
    hour = local_now.hour
    weekday = local_now.weekday()

    if not (MORNING_HOUR_START <= hour < MORNING_HOUR_END):
        return None, local_now

    if weekday == 6:
        return "weekly", local_now
    if weekday == 5:
        return "weekend", local_now
    return "today", local_now


def _summarize_ampm(hourly: dict) -> dict:
    times = hourly.get("time", [])
    codes = hourly.get("weathercode", [])
    precip_probs = hourly.get("precipitation_probability", [])
    hours = []
    for i in range(min(24, len(times))):
        hours.append({
            "hour": i,
            "code": codes[i] if i < len(codes) else None,
            "precip_prob": precip_probs[i] if i < len(precip_probs) else None,
        })

    def pick(hour_range, rep_hour):
        subset = [h for h in hours if hour_range[0] <= h["hour"] <= hour_range[1]]
        if not subset:
            return None, None
        rep = min(subset, key=lambda h: abs(h["hour"] - rep_hour))
        max_precip = max((h["precip_prob"] or 0) for h in subset)
        return rep["code"], max_precip

    am_code, am_precip = pick((6, 11), 9)
    pm_code, pm_precip = pick((12, 18), 15)
    return {"am_code": am_code, "am_precip": am_precip, "pm_code": pm_code, "pm_precip": pm_precip}


def fetch_full_weather(lat, lon, retries: int = 3):
    last_err = None
    for attempt in range(retries):
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
                    "hourly": "weathercode,precipitation_probability",
                    "timezone": "auto",
                    "forecast_days": 10,
                },
                timeout=15,
            )
            if res.status_code == 200:
                data = res.json()
                daily = data.get("daily", {})
                dates = daily.get("time", [])
                if not dates:
                    last_err = "empty daily data"
                else:
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
                    ampm = _summarize_ampm(data.get("hourly", {}))
                    return {
                        "current": data.get("current_weather", {}),
                        "daily": by_date,
                        "ampm": ampm,
                    }
            else:
                last_err = f"HTTP {res.status_code}"
        except Exception as e:
            last_err = str(e)

        if attempt < retries - 1:
            time.sleep(2)

    print(f"  ⚠️ 날씨 조회 실패 ({lat},{lon}, {retries}회 시도): {last_err}")
    return None


def fmt_num(v, digits=0):
    if v is None:
        return "?"
    return f"{v:.{digits}f}"


def format_day_line(label, day_info, include_current=None):
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


def _describe_trend(day_entries_with_data: list) -> str:
    if len(day_entries_with_data) < 2:
        return ""

    first_wd, first_info = day_entries_with_data[0]
    last_wd, last_info = day_entries_with_data[-1]
    n = len(day_entries_with_data)

    diff = last_info["tmax"] - first_info["tmax"]
    if diff >= 3:
        temp_trend = f"{first_wd}요일 이후 점차 더워지는 흐름이며"
    elif diff <= -3:
        temp_trend = f"{first_wd}요일 이후 점차 선선해지는 흐름이며"
    else:
        temp_trend = "한 주 내내 비슷한 기온대를 유지하며"

    rain_positions = [i for i, (_, info) in enumerate(day_entries_with_data) if (info.get("precip_prob") or 0) >= 50]
    if not rain_positions:
        rain_part = "비 소식 없이 대체로 맑겠습니다"
    else:
        avg_pos = sum(rain_positions) / len(rain_positions)
        if avg_pos < n / 3:
            rain_part = f"{first_wd}요일 무렵 비 소식이 집중되겠습니다"
        elif avg_pos > n * 2 / 3:
            rain_part = f"{last_wd}요일 무렵 비 소식이 있겠습니다"
        else:
            rain_part = "주 중반 비 소식이 있겠습니다"

    return f"{temp_trend} {rain_part}."


def format_multi_day_line(label: str, day_entries: list) -> str:
    chain_parts = []
    tmax_list = []
    tmin_list = []
    rain_days = []
    valid_entries = []
    any_data = False

    for wd, info in day_entries:
        if not info or info.get("tmax") is None:
            chain_parts.append(f"{wd} 데이터없음")
            continue
        any_data = True
        valid_entries.append((wd, info))
        condition = WEATHER_CODE_KO.get(info["code"], "")
        chain_parts.append(f"{wd} {condition}({fmt_num(info['tmax'])}°/{fmt_num(info['tmin'])}°)")
        tmax_list.append((info["tmax"], wd))
        tmin_list.append((info["tmin"], wd))
        if (info.get("precip_prob") or 0) >= 50:
            rain_days.append(wd)

    if not any_data:
        return f"- {label}: 데이터 없음"

    chain = " → ".join(chain_parts)
    max_t, max_wd = max(tmax_list)
    min_t, min_wd = min(tmin_list)
    extra = f" 최고 {fmt_num(max_t)}°C({max_wd})·최저 {fmt_num(min_t)}°C({min_wd})"
    if rain_days:
        extra += f", {'·'.join(rain_days)}요일 비 소식"

    trend = _describe_trend(valid_entries)
    trend_part = f" {trend}" if trend else ""

    return f"- {label}:{trend_part} [{chain}].{extra}"


def pick_weekend_dates(dates: list) -> tuple:
    sat = sun = None
    for d in dates:
        wd = datetime.strptime(d, "%Y-%m-%d").weekday()
        if wd == 5 and sat is None:
            sat = d
        elif wd == 6 and sun is None:
            sun = d
        if sat and sun:
            break
    return sat, sun


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

_image_cache = {}

IMAGE_TAG_BLACKLIST = {
    "war", "military", "soldier", "weapon", "gun", "conflict", "protest", "riot",
    "flag", "map", "person", "people", "portrait", "face", "man", "woman",
    "child", "children", "wedding", "funeral", "police", "crime", "accident",
    "corpse", "death", "blood", "nude", "naked", "sexy",
}

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
    results = []
    for i, (name, lat, lon, is_capital) in enumerate(cities):
        if i > 0:
            time.sleep(3)
        w = fetch_full_weather(lat, lon)
        results.append((name, is_capital, w))
    return results


def _describe_condition(precip_prob):
    if precip_prob is None:
        return ""
    if precip_prob >= 60:
        return " 비 소식이 있으니 우산 준비가 필요하다."
    if precip_prob >= 30:
        return " 흐리거나 비가 오락가락할 수 있습니다."
    return " 비 소식 없이 대체로 맑은 날씨가 예상됩니다."


# ── 기상청(KMA) 단기예보 ──────────────────────────────────────

KMA_GRID_OVERRIDE = {
    "서울": (60, 127),
    "부산": (98, 76),
    "대구": (89, 90),
    "광주": (60, 74),
    "제주": (52, 38),
}


def _latlon_to_kma_grid(lat: float, lon: float) -> tuple:
    import math
    RE = 6371.00877
    GRID = 5.0
    SLAT1 = 30.0
    SLAT2 = 60.0
    OLON = 126.0
    OLAT = 38.0
    XO = 43
    YO = 136

    DEGRAD = math.pi / 180.0
    re = RE / GRID
    slat1 = SLAT1 * DEGRAD
    slat2 = SLAT2 * DEGRAD
    olon = OLON * DEGRAD
    olat = OLAT * DEGRAD

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + lat * DEGRAD * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * DEGRAD - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    x = int(ra * math.sin(theta) + XO + 1.5)
    y = int(ro - ra * math.cos(theta) + YO + 1.5)
    return x, y


def _kma_grid_for_city(name: str, lat: float, lon: float) -> tuple:
    if name in KMA_GRID_OVERRIDE:
        return KMA_GRID_OVERRIDE[name]
    return _latlon_to_kma_grid(lat, lon)


KMA_WARNING_STN = {"서울": 108, "부산": 159, "대구": 143, "광주": 156, "제주": 184}


def fetch_kma_weather_briefing(stn_id: str = "108", retries: int = 2):
    """
    기상청 API허브 '동네예보 통보문 조회서비스' 중 getWthrSituation(기상개황) 호출.
    예보관이 작성한 종합 해설문(wfSv1)을 반환. KMA_BRIEFING_KEY가 없거나 실패 시 None.
    stn_id 기본값 108 = 전국 종합.
    """
    if not KMA_BRIEFING_KEY:
        return None

    last_err = None
    for attempt in range(retries):
        try:
            res = requests.get(
                "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstMsgService/getWthrSituation",
                params={
                    "authKey": KMA_BRIEFING_KEY,
                    "numOfRows": 1,
                    "pageNo": 1,
                    "dataType": "JSON",
                    "stnId": stn_id,
                },
                timeout=15,
            )
            if res.status_code != 200:
                last_err = f"HTTP {res.status_code} / body={res.text[:150]}"
            else:
                content_type = res.headers.get("Content-Type", "")
                if "json" not in content_type.lower():
                    last_err = f"JSON 아닌 응답 (Content-Type={content_type}): {res.text[:150]}"
                else:
                    data = res.json()
                    header = data.get("response", {}).get("header", {})
                    if header.get("resultCode") != "00":
                        last_err = f"KMA {header.get('resultCode')}: {header.get('resultMsg')}"
                    else:
                        items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
                        if isinstance(items, dict):
                            items = [items]
                        if not items:
                            last_err = "items 비어있음"
                        else:
                            text = (items[0].get("wfSv1") or "").strip()
                            return text if text else None
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries - 1:
            time.sleep(2)

    print(f"  ⚠️ 기상개황(예보관 해설) 조회 실패 (stnId={stn_id}): {last_err}")
    return None


def fetch_kma_warning_title(name: str, local_now: datetime, retries: int = 2):
    stn_id = KMA_WARNING_STN.get(name)
    if not stn_id or not KMA_API_KEY:
        return None

    date_str = local_now.strftime("%Y%m%d")
    for attempt in range(retries):
        try:
            res = requests.get(
                "http://apis.data.go.kr/1360000/WthrWrnInfoService/getWthrWrnList",
                params={
                    "serviceKey": KMA_API_KEY,
                    "numOfRows": 20,
                    "pageNo": 1,
                    "dataType": "JSON",
                    "stnId": stn_id,
                    "fromTmFc": date_str,
                    "toTmFc": date_str,
                },
                timeout=15,
            )
            if res.status_code != 200:
                continue
            data = res.json()
            header = data.get("response", {}).get("header", {})
            code = header.get("resultCode")
            if code == "03":
                return None
            if code != "00":
                continue
            items = data.get("response", {}).get("body", {}).get("items", {})
            item_list = items.get("item", []) if isinstance(items, dict) else []
            if isinstance(item_list, dict):
                item_list = [item_list]
            if not item_list:
                return None
            item_list.sort(key=lambda x: x.get("tmFc", ""), reverse=True)
            title = item_list[0].get("title", "")
            if not title or "해제" in title:
                return None
            return title
        except Exception:
            continue
    return None


def _kma_base_datetime(local_now: datetime) -> tuple:
    candidates = [2, 5, 8, 11, 14, 17, 20, 23]
    date = local_now.date()
    hour, minute = local_now.hour, local_now.minute
    chosen = None
    for h in reversed(candidates):
        if hour > h or (hour == h and minute >= 10):
            chosen = h
            break
    if chosen is None:
        date = date - timedelta(days=1)
        chosen = 23
    return date.strftime("%Y%m%d"), f"{chosen:02d}00"


KMA_SKY_KO = {1: "맑음", 3: "구름많음", 4: "흐림"}
KMA_PTY_KO = {0: None, 1: "비", 2: "비/눈", 3: "눈", 4: "소나기", 5: "빗방울", 6: "빗방울눈날림", 7: "눈날림"}


def _kma_condition(sky, pty) -> str:
    sky = int(sky) if sky is not None else 1
    pty = int(pty) if pty is not None else 0
    p = KMA_PTY_KO.get(pty)
    if p:
        base = KMA_SKY_KO.get(sky, "흐림")
        return f"{base}, {p}" if base in ("흐림", "구름많음") else p
    return KMA_SKY_KO.get(sky, "-")


def fetch_kma_vilage_fcst_all(name: str, lat: float, lon: float, local_now: datetime, retries: int = 3):
    if not KMA_API_KEY:
        print(f"  ⚠️ 기상청 조회 스킵 ({name}): KMA_API_KEY 없음")
        return None

    nx, ny = _kma_grid_for_city(name, lat, lon)
    base_date, base_time = _kma_base_datetime(local_now)

    last_err = None
    for attempt in range(retries):
        try:
            res = requests.get(
                "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst",
                params={
                    "serviceKey": KMA_API_KEY,
                    "numOfRows": 1000,
                    "pageNo": 1,
                    "dataType": "JSON",
                    "base_date": base_date,
                    "base_time": base_time,
                    "nx": nx,
                    "ny": ny,
                },
                timeout=15,
            )
            if res.status_code != 200:
                last_err = f"HTTP {res.status_code} / body={res.text[:150]}"
            else:
                content_type = res.headers.get("Content-Type", "")
                if "json" not in content_type.lower():
                    last_err = f"JSON 아닌 응답 (Content-Type={content_type}): {res.text[:150]}"
                else:
                    data = res.json()
                    header = data.get("response", {}).get("header", {})
                    if header.get("resultCode") != "00":
                        last_err = f"KMA {header.get('resultCode')}: {header.get('resultMsg')}"
                    else:
                        items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
                        by_date_time = {}
                        for it in items:
                            d = it.get("fcstDate")
                            by_date_time.setdefault(d, {}).setdefault(it["fcstTime"], {})[it["category"]] = it["fcstValue"]

                        tmn_by_date, tmx_by_date = {}, {}
                        for it in items:
                            d = it.get("fcstDate")
                            if it["category"] == "TMN" and d not in tmn_by_date:
                                tmn_by_date[d] = float(it["fcstValue"])
                            if it["category"] == "TMX" and d not in tmx_by_date:
                                tmx_by_date[d] = float(it["fcstValue"])

                        # TMP(3시간 기온) 기반 fallback — TMN/TMX가 없는 날짜(주로 발표 당일)를 위한 보조 최저·최고
                        tmp_min_by_date, tmp_max_by_date = {}, {}
                        for d, by_time in by_date_time.items():
                            tmp_vals = [
                                float(by_time[t]["TMP"])
                                for t in by_time
                                if "TMP" in by_time[t]
                            ]
                            if tmp_vals:
                                tmp_min_by_date[d] = min(tmp_vals)
                                tmp_max_by_date[d] = max(tmp_vals)

                        result = {}
                        for d, by_time in by_date_time.items():
                            tmin_v = tmn_by_date.get(d)
                            tmax_v = tmx_by_date.get(d)
                            used_fallback = False
                            if tmin_v is None or tmax_v is None:
                                # TMN/TMX 누락 시 TMP 최소·최대로 대체 (발표 당일에 흔함)
                                if d in tmp_min_by_date and d in tmp_max_by_date:
                                    tmin_v = tmp_min_by_date[d] if tmin_v is None else tmin_v
                                    tmax_v = tmp_max_by_date[d] if tmax_v is None else tmax_v
                                    used_fallback = True
                                else:
                                    continue

                            def cat_at(t, cat):
                                return by_time.get(t, {}).get(cat)

                            am_sky, am_pty = cat_at("0900", "SKY"), cat_at("0900", "PTY")
                            pm_sky, pm_pty = cat_at("1500", "SKY"), cat_at("1500", "PTY")
                            am_pops = [int(by_time[t]["POP"]) for t in by_time if "0600" <= t <= "1100" and "POP" in by_time[t]]
                            pm_pops = [int(by_time[t]["POP"]) for t in by_time if "1200" <= t <= "1800" and "POP" in by_time[t]]
                            wsd_all = [float(by_time[t]["WSD"]) for t in by_time if "WSD" in by_time[t]]

                            # 오늘(발표 당일)처럼 오전 시간대가 이미 지난 경우 오전 카테고리가 없을 수 있음 → 있는 시간대 중 아무거나로 대체
                            if am_sky is None and am_pty is None:
                                any_time = next(iter(sorted(by_time.keys())), None)
                                if any_time:
                                    am_sky, am_pty = cat_at(any_time, "SKY"), cat_at(any_time, "PTY")

                            result[d] = {
                                "tmin": tmin_v,
                                "tmax": tmax_v,
                                "am_condition": _kma_condition(am_sky, am_pty),
                                "pm_condition": _kma_condition(pm_sky, pm_pty),
                                "am_precip": max(am_pops) if am_pops else None,
                                "pm_precip": max(pm_pops) if pm_pops else None,
                                "wind_max": max(wsd_all) * 3.6 if wsd_all else None,
                                "tmp_fallback": used_fallback,
                            }

                        if result:
                            return result
                        last_err = f"TMN/TMX/TMP 데이터 없음 base={base_date} {base_time}, nx={nx} ny={ny}, 원본항목수={len(items)}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries - 1:
            time.sleep(2)

    print(f"  ⚠️ 기상청 조회 실패 ({name}, nx={nx} ny={ny}, base={base_date} {base_time}): {last_err}")
    return None


def format_kma_line(label: str, kma: dict) -> str:
    if not kma:
        return f"▲ {label} : 데이터 없음"
    am_p = kma["am_precip"] if kma["am_precip"] is not None else "-"
    pm_p = kma["pm_precip"] if kma["pm_precip"] is not None else "-"
    wind = f", 최대풍속 {fmt_num(kma['wind_max'])}km/h" if kma.get("wind_max") is not None else ""
    return (
        f"▲ {label} : [오전 {kma['am_condition']}, 오후 {kma['pm_condition']}] "
        f"({fmt_num(kma['tmin'])}∼{fmt_num(kma['tmax'])}) "
        f"<오전 강수확률 {am_p}%, 오후 강수확률 {pm_p}%>{wind}"
    )


def _kma_fetch_cities(cities: list, local_now: datetime) -> list:
    out = []
    for i, (name, lat, lon, is_capital) in enumerate(cities):
        by_date = fetch_kma_vilage_fcst_all(name, lat, lon, local_now)
        out.append((name, is_capital, by_date))
        if i < len(cities) - 1:
            time.sleep(1)
    return out


def build_korea_today_report_kma(cities: list, local_now: datetime):
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]})"
    target_date = local_now.strftime("%Y%m%d")
    lines = []
    valid = []

    fetched = _kma_fetch_cities(cities, local_now)
    for name, is_capital, by_date in fetched:
        label = f"{name}(수도)" if is_capital else name
        kma = by_date.get(target_date) if by_date else None
        lines.append(format_kma_line(label, kma))
        if kma:
            valid.append((name, is_capital, kma))
        elif by_date:
            print(f"  ⚠️ {name}: 기상청 응답은 정상 수신했으나 대상일({target_date}) 데이터 없음. 응답 포함 날짜: {sorted(by_date.keys())}")

    if not valid:
        return None, None

    cap = next((k for n, c, k in valid if c), valid[0][2])
    cap_name = next((n for n, c, k in valid if c), valid[0][0])

    warnings = []
    for i, (n, c, k) in enumerate(valid):
        w = fetch_kma_warning_title(n, local_now)
        if w:
            warnings.append((n, w))
        if i < len(valid) - 1:
            time.sleep(1)

    tmax_all = [k["tmax"] for _, _, k in valid]
    tmin_all = [k["tmin"] for _, _, k in valid]
    rain_cities = [n for n, c, k in valid if (k.get("pm_precip") or k.get("am_precip") or 0) >= 60]
    windy_cities = [n for n, c, k in valid if (k.get("wind_max") or 0) >= 40]
    warn_text = ", ".join(f"{n}({w})" for n, w in warnings) if warnings else ""

    city_data_lines = []
    for n, c, k in valid:
        city_data_lines.append(
            f"- {n}: 오전 {k.get('am_condition','-')}, 오후 {k.get('pm_condition','-')}, "
            f"최저 {fmt_num(k['tmin'])}°C, 최고 {fmt_num(k['tmax'])}°C, "
            f"오전 강수확률 {fmt_num(k.get('am_precip') or 0)}%, 오후 강수확률 {fmt_num(k.get('pm_precip') or 0)}%"
            + (f", 최대풍속 {fmt_num(k['wind_max'])}km/h" if k.get("wind_max") else "")
        )

    briefing = fetch_kma_weather_briefing("108")
    briefing_block = f"\n[기상청 예보관 해설(참고용 — 사실 확인 및 문맥 보강에만 활용)]\n{briefing}\n" if briefing else ""

    gemini_prompt = f"""다음은 한국 기상청 단기예보 자료다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
전국 최저기온: {fmt_num(min(tmin_all))}°C / 최고기온: {fmt_num(max(tmax_all))}°C
{cap_name}: 오전 {cap['am_condition']}, 오후 {cap['pm_condition']}, 최저 {fmt_num(cap['tmin'])}°C, 최고 {fmt_num(cap['tmax'])}°C
강수확률 높은 지역(60% 이상): {', '.join(rain_cities) if rain_cities else '없음'}
강풍 예상 지역(40km/h 이상): {', '.join(windy_cities) if windy_cities else '없음'}
기상 특보 발효: {warn_text if warn_text else '없음'}
지역별 상세:
{chr(10).join(city_data_lines)}
{briefing_block}
[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일"로 시작할 것(다른 숫자 사용 금지, "(현지시간)" 붙이지 말 것). "오늘", 절대연도(2026년 등) 절대 금지
- 700자 이상 작성
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 첫 문장은 반드시 "{local_now.day}일 한국의 날씨는"으로 시작해 전국 기상을 한 문장으로 개관한 뒤, 이어서 {cap_name} 등 지역별 상세로 들어갈 것. {cap_name}을 "수도"로 지칭하지 말 것(예: "수도 {cap_name}" 금지, 그냥 "{cap_name}"만 사용)
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- {cap_name} 날씨를 중심으로 서술하되, 특이기상(강수·강풍·특보)은 구체적으로 언급
- 전국 기온 범위로 마무리
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_kma_today = call_gemini_weather(gemini_prompt)
    _title_kma_today, lede = _parse_gemini_weather_response(_raw_kma_today)
    if not lede:
        lede = (
            f"{local_now.day}일 한국의 날씨는 지역별로 편차를 보이겠다. 기상청에 따르면 {cap_name}은 오전 {cap['am_condition']}이고 오후 {cap['pm_condition']}일 것으로 예상된다. "
            f"{cap_name}의 최저기온은 {fmt_num(cap['tmin'])}°C, 최고기온은 {fmt_num(cap['tmax'])}°C를 기록할 전망이다. "
            f"전국적으로는 최저 {fmt_num(min(tmin_all))}°C에서 최고 {fmt_num(max(tmax_all))}°C의 분포를 보일 전망이다."
        )

    lede = _strip_local_time_kr(lede)
    max_temp = fmt_num(max([k["tmax"] for _, _, k in valid]))
    title = _title_kma_today if _title_kma_today else f"한국, {_weather_phrase(lede, max_temp)}"
    legend = "[지역별 날씨 전망] [오전/오후](최저∼최고기온) | 강수확률"
    body = lede + "\n\n" + legend + "\n\n" + "\n".join(lines)
    return title, body


def build_korea_weekend_report_kma(cities: list, local_now: datetime):
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]})"
    sat_date = local_now.strftime("%Y%m%d")
    sun_date = (local_now + timedelta(days=1)).strftime("%Y%m%d")

    lines = []
    valid = []

    fetched = _kma_fetch_cities(cities, local_now)
    for name, is_capital, by_date in fetched:
        label = f"{name}(수도)" if is_capital else name
        sat_kma = by_date.get(sat_date) if by_date else None
        sun_kma = by_date.get(sun_date) if by_date else None
        if by_date and not (sat_kma or sun_kma):
            print(f"  ⚠️ {name}: 기상청 응답은 정상 수신했으나 토({sat_date})·일({sun_date}) 데이터 없음. 응답 포함 날짜: {sorted(by_date.keys())}")

        parts = []
        if sat_kma:
            parts.append(format_kma_line(f"{label} 토요일", sat_kma))
        else:
            parts.append(f"▲ {label} 토요일 : 데이터 없음")
        if sun_kma:
            parts.append(format_kma_line(f"{label} 일요일", sun_kma))
        else:
            parts.append(f"▲ {label} 일요일 : 데이터 없음")
        lines.extend(parts)

        if sat_kma or sun_kma:
            valid.append((name, is_capital, sat_kma, sun_kma))

    if not valid:
        return None, None

    cap = next(((s, u) for n, c, s, u in valid if c), (valid[0][2], valid[0][3]))
    cap_name = next((n for n, c, s, u in valid if c), valid[0][0])
    cap_sat, cap_sun = cap

    warnings = []
    for i, (n, c, s, u) in enumerate(valid):
        w = fetch_kma_warning_title(n, local_now)
        if w:
            warnings.append((n, w))
        if i < len(valid) - 1:
            time.sleep(1)

    rain_cities = [
        n for n, c, s, u in valid
        if (s and max(s.get("am_precip") or 0, s.get("pm_precip") or 0) >= 60)
        or (u and max(u.get("am_precip") or 0, u.get("pm_precip") or 0) >= 60)
    ]
    warn_text = ", ".join(f"{n}({w})" for n, w in warnings) if warnings else ""

    sat_desc = (
        f"오전 {cap_sat['am_condition']}, 오후 {cap_sat['pm_condition']}, "
        f"최저 {fmt_num(cap_sat['tmin'])}°C, 최고 {fmt_num(cap_sat['tmax'])}°C"
    ) if cap_sat else "데이터 없음"
    sun_desc = (
        f"오전 {cap_sun['am_condition']}, 오후 {cap_sun['pm_condition']}, "
        f"최저 {fmt_num(cap_sun['tmin'])}°C, 최고 {fmt_num(cap_sun['tmax'])}°C"
    ) if cap_sun else "데이터 없음"

    briefing = fetch_kma_weather_briefing("108")
    briefing_block = f"\n[기상청 예보관 해설(참고용 — 사실 확인 및 문맥 보강에만 활용)]\n{briefing}\n" if briefing else ""

    gemini_prompt = f"""다음은 한국 기상청 단기예보 주말 자료다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
{cap_name} 토요일: {sat_desc}
{cap_name} 일요일: {sun_desc}
강수확률 높은 지역(60% 이상, 토·일 중 하루라도): {', '.join(rain_cities) if rain_cities else '없음'}
기상 특보 발효: {warn_text if warn_text else '없음'}
{briefing_block}
[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일"로 시작할 것(다른 숫자 사용 금지, "(현지시간)" 붙이지 말 것). "오늘", "이번 주말", 절대연도 절대 금지
- 700자 이상 작성
- 첫 문장은 반드시 "{local_now.day}일 이번 주말 한국의 날씨는"으로 시작해 전국 기상을 한 문장으로 개관한 뒤, 이어서 {cap_name} 등 지역별 상세로 들어갈 것. {cap_name}을 "수도"로 지칭하지 말 것(예: "수도 {cap_name}" 금지, 그냥 "{cap_name}"만 사용)
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- 토요일·일요일 날씨를 구분해 서술하되, 특이기상(강수·특보)은 구체적으로 언급
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_kma_wknd = call_gemini_weather(gemini_prompt)
    _title_kma_wknd, lede = _parse_gemini_weather_response(_raw_kma_wknd)
    if not lede:
        lede = (
            f"{local_now.day}일 이번 주말 한국의 날씨는 지역별로 편차를 보이겠다. 기상청 단기예보에 따르면 {cap_name}은 "
            f"토요일 {sat_desc}, 일요일 {sun_desc}이 예상된다."
            + (f" {', '.join(rain_cities)} 등은 강수확률이 높아 우산 준비가 필요하다." if rain_cities else "")
        )

    lede = _strip_local_time_kr(lede)
    sat_f = "맑음" if cap_sat and "맑" in (cap_sat.get("am_condition") or "") else ("비" if cap_sat and "비" in (cap_sat.get("am_condition") or "") else "흐림")
    sun_f = "맑음" if cap_sun and "맑" in (cap_sun.get("am_condition") or "") else ("비" if cap_sun and "비" in (cap_sun.get("am_condition") or "") else "흐림")
    title = _title_kma_wknd if _title_kma_wknd else f"주말 한국, {_weekend_phrase(sat_f, sun_f)}"
    legend = "다음은 지역별 주말 날씨 전망입니다.\n[토요일, 일요일](최저∼최고기온) <오전 강수확률, 오후 강수확률>"
    body = lede + "\n\n" + legend + "\n\n" + "\n".join(lines)
    return title, body


# ── 기상청(KMA) 중기예보 ──────────────────────────────────────

KMA_MID_LAND_CODE = {
    "서울": "11B00000", "부산": "11H20000", "대구": "11H10000",
    "광주": "11F20000", "제주": "11G00000",
}
KMA_MID_TEMP_CODE = {
    "서울": "11B10101", "부산": "11H20201", "대구": "11H10701",
    "광주": "11F20501", "제주": "11G00201",
}


def _kma_mid_tmfc(local_now: datetime) -> str:
    date = local_now.date()
    hour, minute = local_now.hour, local_now.minute
    if hour > 18 or (hour == 18 and minute >= 30):
        chosen = 18
    elif hour > 6 or (hour == 6 and minute >= 30):
        chosen = 6
    else:
        date = date - timedelta(days=1)
        chosen = 18
    return f"{date.strftime('%Y%m%d')}{chosen:02d}00"


def _fetch_kma_mid(endpoint: str, reg_id: str, tm_fc: str, retries: int = 3):
    if not KMA_API_KEY:
        return None
    last_err = None
    for attempt in range(retries):
        try:
            res = requests.get(
                f"http://apis.data.go.kr/1360000/MidFcstInfoService/{endpoint}",
                params={
                    "serviceKey": KMA_API_KEY,
                    "numOfRows": 10,
                    "pageNo": 1,
                    "dataType": "JSON",
                    "regId": reg_id,
                    "tmFc": tm_fc,
                },
                timeout=15,
            )
            if res.status_code == 200:
                data = res.json()
                header = data.get("response", {}).get("header", {})
                if header.get("resultCode") == "00":
                    items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
                    if items:
                        return items[0]
                    last_err = "empty items"
                else:
                    last_err = f"KMA {header.get('resultCode')}: {header.get('resultMsg')}"
            else:
                last_err = f"HTTP {res.status_code}"
        except Exception as e:
            last_err = str(e)
        if attempt < retries - 1:
            time.sleep(2)
    print(f"  ⚠️ {endpoint} 조회 실패 ({reg_id}): {last_err}")
    return None


def format_kma_merged_line(label: str, entries: list) -> str:
    chain_parts, tmax_list, tmin_list, rain_days = [], [], [], []
    any_data = False
    for wd, info in entries:
        if not info:
            chain_parts.append(f"{wd} 데이터없음")
            continue
        any_data = True
        chain_parts.append(f"{wd} {info['condition']}({fmt_num(info['tmax'])}°/{fmt_num(info['tmin'])}°)")
        tmax_list.append((info["tmax"], wd))
        tmin_list.append((info["tmin"], wd))
        if (info.get("precip_prob") or 0) >= 50:
            rain_days.append(wd)

    if not any_data:
        return f"- {label}: 데이터 없음"

    chain = " → ".join(chain_parts)
    max_t, max_wd = max(tmax_list)
    min_t, min_wd = min(tmin_list)
    extra = f" 최고 {fmt_num(max_t)}°C({max_wd})·최저 {fmt_num(min_t)}°C({min_wd})"
    if rain_days:
        extra += f", {'·'.join(rain_days)}요일 비 소식"
    return f"- {label}: {chain}.{extra}"


def build_korea_weekly_report_kma(cities: list, local_now: datetime):
    today_str = local_now.strftime("%Y년 %m월 %d일") + f"({WEEKDAY_KO[local_now.weekday()]})"

    tm_fc = _kma_mid_tmfc(local_now)
    tm_fc_date = datetime.strptime(tm_fc[:8], "%Y%m%d").date()

    mon_date = (local_now + timedelta(days=1)).date()
    tue_date = (local_now + timedelta(days=2)).date()
    wed_date = (local_now + timedelta(days=3)).date()
    thu_date = (local_now + timedelta(days=4)).date()
    fri_date = (local_now + timedelta(days=5)).date()

    thu_offset = (thu_date - tm_fc_date).days
    fri_offset = (fri_date - tm_fc_date).days
    if not (4 <= thu_offset <= 10) or not (4 <= fri_offset <= 10):
        print(f"  ⚠️ 중기예보 오프셋 범위 밖(목={thu_offset}, 금={fri_offset}) — Open-Meteo로 대체")
        return None, None

    lines = []
    cap_entries = None
    any_valid_city = False

    for i, (name, lat, lon, is_capital) in enumerate(cities):
        label = f"{name}(수도)" if is_capital else name

        by_date = fetch_kma_vilage_fcst_all(name, lat, lon, local_now)
        land = _fetch_kma_mid("getMidLandFcst", KMA_MID_LAND_CODE.get(name), tm_fc) if name in KMA_MID_LAND_CODE else None
        ta = _fetch_kma_mid("getMidTa", KMA_MID_TEMP_CODE.get(name), tm_fc) if name in KMA_MID_TEMP_CODE else None
        if i < len(cities) - 1:
            time.sleep(1)

        entries = []
        for d, wd_label in [(mon_date, "월"), (tue_date, "화"), (wed_date, "수")]:
            info = by_date.get(d.strftime("%Y%m%d")) if by_date else None
            if info:
                entries.append((wd_label, {
                    "tmax": info["tmax"], "tmin": info["tmin"],
                    "condition": info["pm_condition"], "precip_prob": info.get("pm_precip"),
                }))
            else:
                entries.append((wd_label, None))

        for offset, wd_label in [(thu_offset, "목"), (fri_offset, "금")]:
            if land and ta:
                tmax_v = ta.get(f"taMax{offset}")
                tmin_v = ta.get(f"taMin{offset}")
                cond = land.get(f"wf{offset}Pm") or land.get(f"wf{offset}Am") or land.get(f"wf{offset}")
                precip = land.get(f"rnSt{offset}Pm") or land.get(f"rnSt{offset}")
                if tmax_v is not None and tmin_v is not None:
                    entries.append((wd_label, {
                        "tmax": float(tmax_v), "tmin": float(tmin_v),
                        "condition": cond or "-",
                        "precip_prob": int(precip) if precip is not None else None,
                    }))
                else:
                    entries.append((wd_label, None))
            else:
                entries.append((wd_label, None))

        lines.append(format_kma_merged_line(label, entries))
        if any(info is not None for _, info in entries):
            any_valid_city = True
        if is_capital:
            cap_entries = entries

    if not any_valid_city:
        return None, None

    cap_valid = [(wd, info) for wd, info in cap_entries if info] if cap_entries else []
    tmax_all = [info["tmax"] for _, info in cap_valid]
    tmin_all = [info["tmin"] for _, info in cap_valid]
    rain_days = [wd for wd, info in cap_valid if (info.get("precip_prob") or 0) >= 50]
    day_by_day = ", ".join(
        f"{wd}요일 {info['condition']} {fmt_num(info['tmax'])}°C/{fmt_num(info['tmin'])}°C"
        for wd, info in cap_valid
    )

    briefing = fetch_kma_weather_briefing("108")
    briefing_block = f"\n[기상청 예보관 해설(참고용 — 사실 확인 및 문맥 보강에만 활용)]\n{briefing}\n" if briefing else ""

    gemini_prompt = f"""다음은 한국 기상청 단기·중기예보 기반 다음주(월~금) 날씨 자료다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
서울 다음주 예보: {day_by_day if day_by_day else '데이터 없음'}
서울 최고기온 범위: {fmt_num(max(tmax_all)) if tmax_all else '?'}°C
서울 최저기온 범위: {fmt_num(min(tmin_all)) if tmin_all else '?'}°C
비 소식 있는 요일: {', '.join(rain_days) + '요일' if rain_days else '없음'}
{briefing_block}
[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일"로 시작할 것(다른 숫자 사용 금지, "(현지시간)" 붙이지 말 것). "이번 주", "다음주", 절대연도 절대 금지
- 700자 이상 작성
- 첫 문장은 반드시 "{local_now.day}일 기준 다음 주 한국의 날씨는"으로 시작해 전체 흐름을 한 문장으로 개관한 뒤, 이어서 서울 등 요일별 상세로 들어갈 것. 서울을 "수도"로 지칭하지 말 것(예: "수도 서울" 금지, 그냥 "서울"만 사용)
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- 요일별 날씨 흐름을 순서대로 서술하고, 비 소식·기온 특이사항 강조
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_kma_wkly = call_gemini_weather(gemini_prompt)
    _title_kma_wkly, summary = _parse_gemini_weather_response(_raw_kma_wkly)
    if not summary:
        summary = (
            f"{local_now.day}일 기준 다음 주 한국의 날씨는 "
            f"최고 {fmt_num(max(tmax_all)) if tmax_all else '?'}°C, "
            f"최저 {fmt_num(min(tmin_all)) if tmin_all else '?'}°C 사이를 오갈 전망이다."
            + (f" {'·'.join(rain_days)}요일에 비 소식이 있다." if rain_days else " 당분간 비 소식은 없을 전망이다.")
        )

    summary = _strip_local_time_kr(summary)
    title = _title_kma_wkly if _title_kma_wkly else f"다음주 한국, {_weekly_phrase(summary, fmt_num(max(tmax_all)) if tmax_all else '?')}"
    body = summary + "\n\n다음은 지역별 날씨 전망입니다.\n\n" + "\n".join(lines)
    return title, body


def _weather_phrase(lede: str, max_temp_str: str, min_temp_str: str = "") -> str:
    if "폭염" in lede or "폭서" in lede:
        return f"최고 {max_temp_str}°C…폭염 기승"
    if "열대야" in lede:
        return f"최고 {max_temp_str}°C…열대야 주의"
    if "태풍" in lede:
        return f"최고 {max_temp_str}°C…태풍 영향"
    if "특보" in lede and "비" in lede:
        return f"최고 {max_temp_str}°C…호우 특보"
    if "폭설" in lede or "대설" in lede:
        return f"최고 {max_temp_str}°C…대설 특보"
    if "뇌우" in lede or "천둥" in lede:
        return f"최고 {max_temp_str}°C…곳곳 뇌우"
    if "소나기" in lede:
        return f"최고 {max_temp_str}°C…오후 소나기"
    if "비" in lede and "강풍" in lede:
        return f"최고 {max_temp_str}°C…비·강풍"
    if "비" in lede:
        return f"최고 {max_temp_str}°C…비 소식"
    if "흐림" in lede or "구름" in lede:
        return f"최고 {max_temp_str}°C…흐리고 습해"
    try:
        t = float(max_temp_str)
        if t >= 35:
            return f"최고 {max_temp_str}°C…폭염 수준"
        if t >= 30:
            return f"최고 {max_temp_str}°C…무더운 날씨"
        if t <= 5:
            return f"최고 {max_temp_str}°C…쌀쌀한 날씨"
        if t <= 0:
            return f"최고 {max_temp_str}°C…강추위"
    except (ValueError, TypeError):
        pass
    return f"최고 {max_temp_str}°C…맑고 더운 날씨" if "맑" in lede else f"최고 {max_temp_str}°C…대체로 맑아"


def _weekend_phrase(sat_f: str, sun_f: str) -> str:
    if sat_f == sun_f:
        cond = "맑은 날씨" if sat_f == "맑음" else ("비 소식" if sat_f == "비" else "흐린 날씨")
        return f"토·일 {cond}"
    sat_ko = "맑음" if sat_f == "맑음" else ("비" if sat_f == "비" else "흐림")
    sun_ko = "맑음" if sun_f == "맑음" else ("비" if sun_f == "비" else "흐림")
    return f"토요일 {sat_ko}…일요일 {sun_ko}"


def _weekly_phrase(lede_or_summary: str, max_temp_str: str) -> str:
    if "비" in lede_or_summary and "맑" in lede_or_summary:
        return f"비 소식 뒤 맑아져…최고 {max_temp_str}°C"
    if "비" in lede_or_summary:
        return f"비 오는 날 포함…최고 {max_temp_str}°C"
    if "폭염" in lede_or_summary:
        return f"폭염 지속…최고 {max_temp_str}°C"
    return f"대체로 맑아…최고 {max_temp_str}°C"


def _find_capital(weather_list):
    for name, is_capital, w in weather_list:
        if is_capital:
            return name, w
    return (weather_list[0][0], weather_list[0][2]) if weather_list else (None, None)


def format_ampm_line(label: str, today_info: dict, ampm: dict) -> str:
    if not today_info or today_info.get("tmax") is None:
        return f"▲ {label} : 데이터 없음"

    am_code = ampm.get("am_code") if ampm else None
    pm_code = ampm.get("pm_code") if ampm else None
    am_precip = ampm.get("am_precip") if ampm else None
    pm_precip = ampm.get("pm_precip") if ampm else None

    am_cond = WEATHER_CODE_KO.get(am_code, WEATHER_CODE_KO.get(today_info["code"], "-")) if am_code is not None else WEATHER_CODE_KO.get(today_info["code"], "-")
    pm_cond = WEATHER_CODE_KO.get(pm_code, WEATHER_CODE_KO.get(today_info["code"], "-")) if pm_code is not None else WEATHER_CODE_KO.get(today_info["code"], "-")

    tmin = fmt_num(today_info["tmin"])
    tmax = fmt_num(today_info["tmax"])
    am_p = fmt_num(am_precip) if am_precip is not None else "-"
    pm_p = fmt_num(pm_precip) if pm_precip is not None else "-"
    extra = f" 체감 {fmt_num(today_info['feels_max'])}°C, 최대풍속 {fmt_num(today_info['wind_max'])}km/h"

    return (
        f"▲ {label} : [오전 {am_cond}, 오후 {pm_cond}] ({tmin}∼{tmax}) "
        f"<오전 강수확률 {am_p}%, 오후 강수확률 {pm_p}%>{extra}"
    )


def build_today_report(country_name, weather_list, local_now: datetime):
    today_str = f"{local_now.day}일(현지시간)"  # 절대날짜(년/월/요일) 금지 규칙 준수
    lines = []
    valid = []
    any_success = False

    for name, is_capital, w in weather_list:
        label = f"{name}(수도)" if is_capital else name
        if w is None:
            lines.append(f"▲ {label} : 데이터 없음")
            continue
        dates = list(w["daily"].keys())
        today_key = dates[0] if dates else None
        today_info = w["daily"].get(today_key) if today_key else None
        ampm = w.get("ampm", {})
        lines.append(format_ampm_line(label, today_info, ampm))
        if today_info and today_info.get("tmax") is not None:
            any_success = True
            valid.append((name, is_capital, today_info))

    if not any_success:
        return None, None

    cap_name, cap_w = _find_capital(weather_list)
    cap_today = next((info for n, c, info in valid if c), None)

    tmax_all = [info["tmax"] for _, _, info in valid]
    tmin_all = [info["tmin"] for _, _, info in valid]
    feels_all = [info["feels_max"] for _, _, info in valid if info.get("feels_max") is not None]
    rain_cities = [(n, info) for n, c, info in valid if (info.get("precip") or 0) > 0]
    thunder_cities = [n for n, c, info in valid if info.get("code") in (95, 96, 99)]
    windy_cities = [n for n, c, info in valid if (info.get("wind_max") or 0) >= 40]
    heat_cities = [n for n, c, info in valid if (info.get("tmax") or 0) >= 33 or (info.get("feels_max") or 0) >= 33]

    cap_desc = ""
    if cap_today:
        cap_condition = WEATHER_CODE_KO.get(cap_today["code"], "")
        cap_desc = (
            f"{cap_condition}, 최고 {fmt_num(cap_today['tmax'])}°C, 최저 {fmt_num(cap_today['tmin'])}°C, "
            f"강수확률 {fmt_num(cap_today.get('precip_prob') or 0)}%"
        )

    gemini_prompt = f"""다음은 {country_name} 주요 도시 날씨 데이터다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
수도 {cap_name}: {cap_desc if cap_desc else '데이터 없음'}
전국 최저기온: {fmt_num(min(tmin_all))}°C / 최고기온: {fmt_num(max(tmax_all))}°C
체감 최고기온: {fmt_num(max(feels_all))}°C {'(온열질환 주의)' if feels_all and max(feels_all) >= 33 else ''}
강수 예상 지역: {', '.join(f"{n}({fmt_num(info['precip'],1)}mm)" for n, info in rain_cities[:6]) if rain_cities else '없음'}
뇌우 예상 지역: {', '.join(thunder_cities[:5]) if thunder_cities else '없음'}
강풍 예상 지역(40km/h↑): {', '.join(windy_cities[:5]) if windy_cities else '없음'}
폭염 지역(체감 33°C↑): {', '.join(heat_cities[:5]) if heat_cities else '없음'}

[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일(현지시간)"으로 시작할 것(다른 숫자 사용 금지). "오늘", 절대연도 절대 금지
- 700자 이상 작성
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- 수도 날씨 서술 후 특이기상(뇌우·강풍·폭염·강수) 언급, 전국 기온 범위로 마무리
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_today = call_gemini_weather(gemini_prompt)
    _title_today, summary = _parse_gemini_weather_response(_raw_today)
    if not summary:
        summary = (
            f"{today_str}, {country_name}의 수도 {cap_name}은 {cap_desc}이 예상된다. "
            f"전국적으로는 최저 {fmt_num(min(tmin_all))}°C에서 최고 {fmt_num(max(tmax_all))}°C 분포를 보일 전망이다."
        )
    summary += "\n\n다음은 지역별 날씨 전망입니다."

    max_temp = fmt_num(max([k["tmax"] for _, _, k in valid]))
    title = _title_today if _title_today else f"{country_name}, {_weather_phrase(summary, max_temp)}"
    body = summary + "\n\n" + "\n".join(lines)
    return title, body


def build_weekend_report(country_name, weather_list, local_now: datetime):
    today_str = f"{local_now.day}일(현지시간)"  # 절대날짜(년/월/요일) 금지 규칙 준수
    lines = []
    any_success = False

    for name, is_capital, w in weather_list:
        label = f"{name}(수도)" if is_capital else name
        if w is None:
            lines.append(f"- {label}: 데이터 없음")
            continue
        dates = list(w["daily"].keys())
        sat, sun = pick_weekend_dates(dates)
        entries = []
        if sat:
            entries.append(("토", w["daily"].get(sat)))
        if sun:
            entries.append(("일", w["daily"].get(sun)))
        lines.append(format_multi_day_line(label, entries))
        any_success = any_success or any(e[1] and e[1].get("tmax") is not None for e in entries)

    if not any_success:
        return None, None

    cap_name, cap_w = _find_capital(weather_list)
    sat_info = sun_info = None
    sat_cond = sun_cond = ""
    if cap_w is not None:
        dates = list(cap_w["daily"].keys())
        sat, sun = pick_weekend_dates(dates)
        sat_info = cap_w["daily"].get(sat) if sat else None
        sun_info = cap_w["daily"].get(sun) if sun else None
        sat_cond = WEATHER_CODE_KO.get(sat_info["code"], "") if sat_info else ""
        sun_cond = WEATHER_CODE_KO.get(sun_info["code"], "") if sun_info else ""

    sat_desc = (
        f"{sat_cond}, 최고 {fmt_num(sat_info['tmax'])}°C, 최저 {fmt_num(sat_info['tmin'])}°C, "
        f"강수확률 {fmt_num(sat_info.get('precip_prob') or 0)}%"
    ) if sat_info else "데이터 없음"
    sun_desc = (
        f"{sun_cond}, 최고 {fmt_num(sun_info['tmax'])}°C, 최저 {fmt_num(sun_info['tmin'])}°C, "
        f"강수확률 {fmt_num(sun_info.get('precip_prob') or 0)}%"
    ) if sun_info else "데이터 없음"

    gemini_prompt = f"""다음은 {country_name} 주요 도시 주말 날씨 데이터다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
수도 {cap_name} 토요일: {sat_desc}
수도 {cap_name} 일요일: {sun_desc}

[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일(현지시간)"으로 시작할 것(다른 숫자 사용 금지). "이번 주말", 절대연도 절대 금지
- 700자 이상 작성
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- 토요일·일요일을 구분해 수도 날씨를 서술하고, 특이기상(강수·강풍 등) 언급
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_wknd = call_gemini_weather(gemini_prompt)
    _title_wknd, summary = _parse_gemini_weather_response(_raw_wknd)
    if not summary:
        summary = (
            f"{today_str} 기준 {country_name}의 수도 {cap_name}은 토요일 {sat_desc}, 일요일 {sun_desc}이 예상된다. "
            f"그 외 주요 지역 예보는 아래와 같다."
        )

    sat_f = "맑음" if sat_info and WEATHER_CODE_KO.get(sat_info.get("code"), "") in ("맑음", "구름조금") else ("비" if sat_info and sat_info.get("precip_prob", 0) >= 50 else "흐림")
    sun_f = "맑음" if sun_info and WEATHER_CODE_KO.get(sun_info.get("code"), "") in ("맑음", "구름조금") else ("비" if sun_info and sun_info.get("precip_prob", 0) >= 50 else "흐림")
    title = _title_wknd if _title_wknd else f"주말 {country_name}, {_weekend_phrase(sat_f, sun_f)}"
    body = summary + "\n\n" + "\n".join(lines)
    return title, body


def build_weekly_report(country_name, weather_list, local_now: datetime):
    today_str = f"{local_now.day}일(현지시간)"  # 절대날짜(년/월/요일) 금지 규칙 준수
    lines = []
    any_success = False

    for name, is_capital, w in weather_list:
        label = f"{name}(수도)" if is_capital else name
        if w is None:
            lines.append(f"- {label}: 데이터 없음")
            continue
        dates = list(w["daily"].keys())[1:6]
        entries = [(WEEKDAY_KO[datetime.strptime(d, "%Y-%m-%d").weekday()], w["daily"].get(d)) for d in dates]
        lines.append(format_multi_day_line(label, entries))
        any_success = any_success or any(e[1] and e[1].get("tmax") is not None for e in entries)

    if not any_success:
        return None, None

    cap_name, cap_w = _find_capital(weather_list)
    cap_entries = []
    tmax_all_cap = []
    tmin_all_cap = []
    rain_days_cap = []
    day_by_day_cap = ""
    if cap_w is not None:
        cap_dates = list(cap_w["daily"].keys())[1:6]
        cap_entries = [(WEEKDAY_KO[datetime.strptime(d, "%Y-%m-%d").weekday()], cap_w["daily"].get(d)) for d in cap_dates]
        cap_days = [info for _, info in cap_entries if info and info.get("tmax") is not None]
        if cap_days:
            tmax_all_cap = [d["tmax"] for d in cap_days]
            tmin_all_cap = [d["tmin"] for d in cap_days]
            rain_days_cap = [wd for wd, info in cap_entries if info and (info.get("precip_prob") or 0) >= 50]
            day_by_day_cap = ", ".join(
                f"{wd}요일 {WEATHER_CODE_KO.get(info['code'], '')} {fmt_num(info['tmax'])}°C/{fmt_num(info['tmin'])}°C"
                for wd, info in cap_entries if info and info.get("tmax") is not None
            )

    gemini_prompt = f"""다음은 {country_name} 다음주(월~금) 날씨 예보 데이터다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
수도 {cap_name} 요일별 예보: {day_by_day_cap if day_by_day_cap else '데이터 없음'}
수도 주간 최고기온: {fmt_num(max(tmax_all_cap)) if tmax_all_cap else '?'}°C
수도 주간 최저기온: {fmt_num(min(tmin_all_cap)) if tmin_all_cap else '?'}°C
비 소식 요일: {', '.join(rain_days_cap) + '요일' if rain_days_cap else '없음'}

[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일(현지시간)"으로 시작할 것(다른 숫자 사용 금지). "다음주", 절대연도 절대 금지
- 700자 이상 작성
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- 요일별 날씨 흐름을 순서대로 서술하고, 비 소식·기온 특이사항 강조
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_wkly = call_gemini_weather(gemini_prompt)
    _title_wkly, summary = _parse_gemini_weather_response(_raw_wkly)
    if not summary:
        summary = (
            f"{today_str} 발표된 {country_name} 주요 지역 다음주(월~금) 날씨 예보다. "
            f"수도 {cap_name}은 최고 {fmt_num(max(tmax_all_cap)) if tmax_all_cap else '?'}°C, "
            f"최저 {fmt_num(min(tmin_all_cap)) if tmin_all_cap else '?'}°C 사이를 오갈 전망이다."
        )

    max_cap = fmt_num(max(tmax_all_cap)) if tmax_all_cap else "?"
    title = _title_wkly if _title_wkly else f"다음주 {country_name}, {_weekly_phrase(summary, max_cap)}"
    body = summary + "\n\n" + "\n".join(lines)
    return title, body


def _capital_day_info(weather_list, date_key):
    cap_name, cap_w = _find_capital(weather_list)
    if cap_w is None:
        return cap_name, None
    return cap_name, cap_w["daily"].get(date_key)


def build_group_today_report(group_name, countries_data, local_now: datetime):
    today_str = f"{local_now.day}일(현지시간)"  # 절대날짜(년/월/요일) 금지 규칙 준수

    capitals_info = []
    country_blocks = []
    all_valid = []
    any_success = False

    for country_name, weather_list in countries_data:
        lines = [f"[{country_name}]"]
        cap_name, cap_w = _find_capital(weather_list)
        cap_today = None
        for name, is_capital, w in weather_list:
            label = f"{name}(수도)" if is_capital else name
            if w is None:
                lines.append(f"▲ {label} : 데이터 없음")
                continue
            dates = list(w["daily"].keys())
            today_key = dates[0] if dates else None
            today_info = w["daily"].get(today_key) if today_key else None
            ampm = w.get("ampm", {})
            lines.append(format_ampm_line(label, today_info, ampm))
            if today_info and today_info.get("tmax") is not None:
                any_success = True
                all_valid.append((country_name, name, is_capital, today_info))
                if is_capital:
                    cap_today = today_info
        country_blocks.append("\n".join(lines))
        capitals_info.append((country_name, cap_name, cap_today))

    if not any_success:
        return None, None

    successful_caps = [c for c in capitals_info if c[2] is not None]

    tmax_all = [c[2]["tmax"] for c in successful_caps] if successful_caps else []
    tmin_all = [c[2]["tmin"] for c in successful_caps] if successful_caps else []
    rainy = [c[0] for c in successful_caps if (c[2].get("precip_prob") or 0) >= 50]
    thunder = [f"{cn}({city})" for cn, city, ic, info in all_valid if info.get("code") in (95, 96, 99)]
    windy = [f"{cn}({city})" for cn, city, ic, info in all_valid if (info.get("wind_max") or 0) >= 40]
    heat = [f"{cn}({city})" for cn, city, ic, info in all_valid if (info.get("tmax") or 0) >= 38 or (info.get("feels_max") or 0) >= 38]

    cap_summary_lines = "\n".join(
        f"- {cn}({cap}): 최고 {fmt_num(info['tmax'])}°C, 최저 {fmt_num(info['tmin'])}°C, "
        f"날씨 {WEATHER_CODE_KO.get(info.get('code'), '-')}, 강수확률 {fmt_num(info.get('precip_prob') or 0)}%"
        for cn, cap, info in successful_caps
    )

    gemini_prompt = f"""다음은 {group_name} 주요국 수도 날씨 데이터다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
지역 최저기온: {fmt_num(min(tmin_all)) if tmin_all else '?'}°C / 최고기온: {fmt_num(max(tmax_all)) if tmax_all else '?'}°C
비 소식 국가: {', '.join(rainy[:6]) if rainy else '없음'}
뇌우 지역: {', '.join(thunder[:5]) if thunder else '없음'}
강풍 지역(40km/h↑): {', '.join(windy[:5]) if windy else '없음'}
폭염 지역(38°C↑): {', '.join(heat[:5]) if heat else '없음'}
국가별 수도 날씨:
{cap_summary_lines}

[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일(현지시간)"으로 시작할 것(다른 숫자 사용 금지). "오늘", 절대연도 절대 금지
- 700자 이상 작성
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- 지역 전체 기온 범위로 시작해 특이기상(뇌우·강풍·폭염·강수) 국가를 구체적으로 언급
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_grp_today = call_gemini_weather(gemini_prompt)
    _title_grp_today, summary = _parse_gemini_weather_response(_raw_grp_today)
    if not summary:
        summary = (
            f"{today_str} 기준 {group_name} 주요국은 최저 {fmt_num(min(tmin_all)) if tmin_all else '?'}°C에서 "
            f"최고 {fmt_num(max(tmax_all)) if tmax_all else '?'}°C 사이의 기온을 보인다."
            + (f" {', '.join(rainy[:5])} 등에서 비 소식이 있다." if rainy else " 대체로 비 소식 없이 맑은 날씨다.")
        )
    summary += "\n\n다음은 국가별·지역별 날씨 전망입니다."

    _g_tmax_list = [c[2]["tmax"] for c in successful_caps if c[2] and c[2].get("tmax") is not None]
    max_temp = fmt_num(max(_g_tmax_list)) if _g_tmax_list else "?"
    title = _title_grp_today if _title_grp_today else f"{group_name}, {_weather_phrase(summary, max_temp)}"
    body = summary + "\n\n" + "\n\n".join(country_blocks)
    return title, body


def build_group_weekend_report(group_name, countries_data, local_now: datetime):
    today_str = f"{local_now.day}일(현지시간)"  # 절대날짜(년/월/요일) 금지 규칙 준수

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
            entries = []
            sat_info = w["daily"].get(sat) if sat else None
            sun_info = w["daily"].get(sun) if sun else None
            if sat:
                entries.append(("토", sat_info))
                if sat_info and sat_info.get("tmax") is not None:
                    any_success = True
                    if is_capital:
                        cap_sat = sat_info
            if sun:
                entries.append(("일", sun_info))
                if sun_info and sun_info.get("tmax") is not None:
                    any_success = True
                    if is_capital:
                        cap_sun = sun_info
            lines.append(format_multi_day_line(label, entries))
        country_blocks.append("\n".join(lines))
        capitals_info.append((country_name, cap_sat, cap_sun))

    if not any_success:
        return None, None

    successful = [c for c in capitals_info if c[1] and c[2]]

    all_tmax = ([c[1]["tmax"] for c in successful] + [c[2]["tmax"] for c in successful]) if successful else []
    all_tmin = ([c[1]["tmin"] for c in successful] + [c[2]["tmin"] for c in successful]) if successful else []
    rainy = [c[0] for c in successful if max(c[1].get("precip_prob") or 0, c[2].get("precip_prob") or 0) >= 50]

    cap_weekend_lines = "\n".join(
        f"- {cn}: 토요일 {WEATHER_CODE_KO.get(sat.get('code'), '-')} "
        f"최고{fmt_num(sat['tmax'])}°C/최저{fmt_num(sat['tmin'])}°C 강수{fmt_num(sat.get('precip_prob') or 0)}%, "
        f"일요일 {WEATHER_CODE_KO.get(sun.get('code'), '-')} "
        f"최고{fmt_num(sun['tmax'])}°C/최저{fmt_num(sun['tmin'])}°C 강수{fmt_num(sun.get('precip_prob') or 0)}%"
        for cn, sat, sun in successful
    )

    gemini_prompt = f"""다음은 {group_name} 주요국 주말 날씨 데이터다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
지역 최저기온: {fmt_num(min(all_tmin)) if all_tmin else '?'}°C / 최고기온: {fmt_num(max(all_tmax)) if all_tmax else '?'}°C
비 소식 국가: {', '.join(rainy[:6]) if rainy else '없음'}
국가별 수도 주말 예보:
{cap_weekend_lines}

[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일(현지시간)"으로 시작할 것(다른 숫자 사용 금지). "이번 주말", 절대연도 절대 금지
- 700자 이상 작성
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- 토요일·일요일로 구분해 주요국 날씨 흐름 서술, 특이기상 강조
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_grp_wknd = call_gemini_weather(gemini_prompt)
    _title_grp_wknd, summary = _parse_gemini_weather_response(_raw_grp_wknd)
    if not summary:
        summary = (
            f"{group_name} 주요국은 이번 주말 최저 {fmt_num(min(all_tmin)) if all_tmin else '?'}°C에서 "
            f"최고 {fmt_num(max(all_tmax)) if all_tmax else '?'}°C 사이를 오갈 전망이다."
            + (f" {', '.join(rainy[:5])} 등에서는 비 소식이 있다." if rainy else " 대체로 비 소식 없이 맑을 전망이다.")
        )

    sat_f = "비" if sum(1 for _, s, _ in successful if (s.get("precip_prob") or 0) >= 50) > len(successful) / 2 else "맑음"
    sun_f = "비" if sum(1 for _, _, u in successful if (u.get("precip_prob") or 0) >= 50) > len(successful) / 2 else "맑음"
    title = _title_grp_wknd if _title_grp_wknd else f"주말 {group_name}, {_weekend_phrase(sat_f, sun_f)}"
    body = summary + "\n\n" + "\n\n".join(country_blocks)
    return title, body


def build_group_weekly_report(group_name, countries_data, local_now: datetime):
    today_str = f"{local_now.day}일(현지시간)"  # 절대날짜(년/월/요일) 금지 규칙 준수

    capitals_ranges = []
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
            entries = []
            for d in dates:
                info = w["daily"].get(d)
                wd_ko = WEEKDAY_KO[datetime.strptime(d, "%Y-%m-%d").weekday()]
                entries.append((wd_ko, info))
                if info and info.get("tmax") is not None:
                    any_success = True
                    if is_capital:
                        cap_days.append(info)
            lines.append(format_multi_day_line(label, entries))
        country_blocks.append("\n".join(lines))
        capitals_ranges.append((country_name, cap_days))

    if not any_success:
        return None, None

    successful = [c for c in capitals_ranges if c[1]]

    all_tmax = [d["tmax"] for c in successful for d in c[1]] if successful else []
    all_tmin = [d["tmin"] for c in successful for d in c[1]] if successful else []
    rainy = [c[0] for c in successful if any((d.get("precip_prob") or 0) >= 50 for d in c[1])]

    cap_weekly_lines = "\n".join(
        f"- {cn}: 주간 최고 {fmt_num(max(d['tmax'] for d in days))}°C/"
        f"최저 {fmt_num(min(d['tmin'] for d in days))}°C"
        + (f", 비 소식 있음" if any((d.get('precip_prob') or 0) >= 50 for d in days) else "")
        for cn, days in successful
    )

    gemini_prompt = f"""다음은 {group_name} 주요국 다음주(월~금) 날씨 예보 데이터다. 이를 바탕으로 뉴스 기사 본문(서두 문단)을 작성하라.

[날씨 데이터]
지역 최저기온: {fmt_num(min(all_tmin)) if all_tmin else '?'}°C / 최고기온: {fmt_num(max(all_tmax)) if all_tmax else '?'}°C
비 소식 있는 국가: {', '.join(rainy[:6]) if rainy else '없음'}
국가별 주간 요약:
{cap_weekly_lines}

[작성 규칙]
- 뉴스 기사 형식. 종결어미는 반드시 "-다" 체
- 본문은 2~3개 문단으로 나누어 작성. JSON body 값에서 문단 구분은 반드시 \\n\\n(개행 두 번)으로 표시
- 날짜 표기: 오늘은 {local_now.day}일이다. 본문은 반드시 "{local_now.day}일(현지시간)"으로 시작할 것(다른 숫자 사용 금지). "다음주", 절대연도 절대 금지
- 700자 이상 작성
- "주목됩니다", "기대됩니다", "보입니다", "있습니다" 등 논평·경어체 금지
- 타 매체명 언급 금지
- 제공된 날씨 수치 데이터에만 근거해 작성할 것. 지정학적 상황·분쟁·외교·경제·유가·증시 등 날씨 외 내용 추가 절대 금지
- 지역 전체 기온 범위로 시작해 비 소식 국가와 특이기상을 구체적으로 언급
- 본문만 출력(제목·소제목 불필요)

응답은 반드시 아래 JSON 형식으로만 출력하라 (마크다운 코드블록 없이):
{{"title": "제목", "body": "본문..."}}

제목 작성 규칙:
- 20자 내외. 지역명은 앞에 붙이지 말 것(본문에서 유추 가능)
- 핵심 기상 현상 + 체감 표현 중심 (예: "수도권 물폭탄…남부는 찜통더위")
- "최고 N°C…비 소식" 같은 기계적 패턴 금지"""

    _raw_grp_wkly = call_gemini_weather(gemini_prompt)
    _title_grp_wkly, summary = _parse_gemini_weather_response(_raw_grp_wkly)
    if not summary:
        summary = (
            f"{today_str} 발표된 다음주(월~금) 전망이다. {group_name} 주요국은 최저 {fmt_num(min(all_tmin)) if all_tmin else '?'}°C에서 "
            f"최고 {fmt_num(max(all_tmax)) if all_tmax else '?'}°C 사이를 오갈 것으로 보인다."
            + (f" {', '.join(rainy[:5])} 등에서는 비 소식이 있다." if rainy else " 당분간 비 소식 없이 대체로 맑은 날씨가 이어질 전망이다.")
        )

    _gw_tmax_list = [d["tmax"] for c in successful for d in c[1]]
    _gw_max = fmt_num(max(_gw_tmax_list)) if _gw_tmax_list else "?"
    title = _title_grp_wkly if _title_grp_wkly else f"다음주 {group_name}, {_weekly_phrase(summary, _gw_max)}"
    body = summary + "\n\n" + "\n\n".join(country_blocks)
    return title, body


def save_report(name, region, title, body, kind: str, local_now: datetime, countries: list, image_country: str, key: str):
    now_str_kst = now_kst().strftime("%Y-%m-%d %H:%M")
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
    print(f"[DEBUG] KMA_API_KEY: {'설정됨(길이=' + str(len(KMA_API_KEY)) + ')' if KMA_API_KEY else '없음'}")
    print(f"[DEBUG] KMA_BRIEFING_KEY: {'설정됨(길이=' + str(len(KMA_BRIEFING_KEY)) + ')' if KMA_BRIEFING_KEY else '없음'}")
    print(f"[DEBUG] GEMINI 키 개수: {len(GEMINI_API_KEYS)}")

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

    for group_name, gconf in GROUPS.items():
        mode, local_now = should_run_now(gconf["tz"])
        if mode is None:
            continue

        print(f"→ [그룹] {group_name} (현지 {local_now.strftime('%H:%M')}, {MODE_LABEL[mode]})")

        countries_data = []
        for ci, country_name in enumerate(gconf["countries"]):
            if ci > 0:
                time.sleep(3)
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

    region, tz_name, cities = COUNTRIES["한국"]
    mode, local_now = should_run_now(tz_name)
    if mode is not None:
        print(f"→ 한국 (현지 {local_now.strftime('%H:%M')}, {MODE_LABEL[mode]})")

        title = body = None
        if mode == "today" and KMA_API_KEY:
            title, body = build_korea_today_report_kma(cities, local_now)
            if title:
                print("  (기상청 단기예보 사용)")
            else:
                print("  ⚠️ 기상청 조회 실패 — Open-Meteo로 대체")
        elif mode == "weekend" and KMA_API_KEY:
            title, body = build_korea_weekend_report_kma(cities, local_now)
            if title:
                print("  (기상청 단기예보 사용)")
            else:
                print("  ⚠️ 기상청 조회 실패 — Open-Meteo로 대체")
        elif mode == "weekly" and KMA_API_KEY:
            title, body = build_korea_weekly_report_kma(cities, local_now)
            if title:
                print("  (기상청 단기+중기예보 사용)")
            else:
                print("  ⚠️ 기상청 조회 실패 — Open-Meteo로 대체")

        if not title:
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
