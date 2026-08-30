import feedparser
import requests
import os
import json
import hashlib
import time
import calendar
import re
import sys
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from deep_translator import GoogleTranslator
from dotenv import load_dotenv
from rapidfuzz import fuzz
from db import init_db, is_url_exists, insert_article, mark_sent_telegram, now_kst

# UTF-8 출력 설정 (Windows 인코딩 오류 방지)
sys.stdout.reconfigure(encoding='utf-8')

# .env 파일에서 환경변수 로드
load_dotenv()

# DB 초기화
init_db()

# =========================
# CONFIG
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = "@TheWiseFrontier"

STATE_FILE = "data/state.json"

# RSS 발행일 필터 — 이 일수를 초과한 기사는 수집하지 않음
MAX_AGE_DAYS = float(os.getenv("MAX_ARTICLE_AGE_DAYS", "3"))
# 소스당 1회 수집 상한 (발행일 필터 통과분 기준)
MAX_ENTRIES_PER_SOURCE = int(os.getenv("MAX_ENTRIES_PER_SOURCE", "5"))

# =========================
# EMOJI MAP
# =========================

CATEGORY_EMOJI = {
    "경제":    "💰",
    "정치":    "🏛️",
    "세계":    "🌐",
    "IT·과학": "💡",
    "사회":    "🏘️",
    "생활/문화": "🎭"
}

REGION_KEYWORDS = {
    "africa": ["africa", "nigeria", "kenya", "ghana", "ethiopia", "egypt", "south africa", "allafrica", "maverick", "naira", "punch", "businessday", "businesstech", "guardian nigeria", "vanguard", "ghanaweb", "mining weekly", "engineering news"],
    "southeast_asia": ["asia", "krasia", "dealstreet", "techinasia", "vietnam", "indonesia", "thailand", "myanmar", "khmer", "malaysia", "philippine", "bangkok", "jakarta", "loop png", "pacific", "fiji", "solomon", "png"],
    "eastern_europe": ["emerging europe", "intellinews", "poland", "ukraine", "romania", "czechia", "kyiv", "warsaw", "caucasus", "azerbaijan"],
    "central_asia": ["kazakhstan", "uzbekistan", "kyrgyz", "tajik", "turkmen", "mongolia", "eurasianet", "caravanserai", "astana", "kun.uz", "kabar", "akipress"],
    "middle_east": ["iraq", "iran", "yemen", "syria", "jordan", "lebanon", "saudi", "qatar", "kuwait", "oman", "bahrain", "uae", "emirates", "al monitor", "middle east eye", "israel", "palestine", "ynet", "wafa", "globes", "israel21c", "haaretz", "jpost"],
    "south_asia": ["pakistan", "bangladesh", "nepal", "sri lanka", "dawn", "himalayan", "daily star", "india", "hindu", "business today", "economic times", "deccan chronicle"],
    "caribbean": ["haiti", "jamaica", "trinidad", "dominican", "caribbean", "haitian times", "loop caribbean", "jamaica gleaner", "caribbean news", "jamaica observer"],
    "latin_america": ["venezuela", "bolivia", "ecuador", "paraguay", "nicaragua", "salvador", "guatemala", "honduras", "news americas", "alo clandestino", "bolivia express", "telesur", "costa rica", "diario"],
    "oceania": ["australia", "new zealand", "abc news", "rnz", "stuff", "sydney", "auckland", "melbourne"]
}

def detect_region(source_name: str) -> str:
    name_lower = source_name.lower()
    for region, keywords in REGION_KEYWORDS.items():
        for kw in keywords:
            if kw in name_lower:
                return region
    return "global"

# 국가 감지 — (국기, 한국어 국가명)
COUNTRY_INFO = {
    "nigeria": ("🇳🇬", "나이지리아"), "nigerian": ("🇳🇬", "나이지리아"),
    "south africa": ("🇿🇦", "남아공"), "south african": ("🇿🇦", "남아공"),
    "kenya": ("🇰🇪", "케냐"), "kenyan": ("🇰🇪", "케냐"),
    "ghana": ("🇬🇭", "가나"), "ghanaian": ("🇬🇭", "가나"),
    "ethiopia": ("🇪🇹", "에티오피아"), "ethiopian": ("🇪🇹", "에티오피아"),
    "egypt": ("🇪🇬", "이집트"), "egyptian": ("🇪🇬", "이집트"),
    "tanzania": ("🇹🇿", "탄자니아"), "tanzanian": ("🇹🇿", "탄자니아"),
    "uganda": ("🇺🇬", "우간다"), "ugandan": ("🇺🇬", "우간다"),
    "rwanda": ("🇷🇼", "르완다"), "rwandan": ("🇷🇼", "르완다"),
    "senegal": ("🇸🇳", "세네갈"), "senegalese": ("🇸🇳", "세네갈"),
    "ivory coast": ("🇨🇮", "코트디부아르"), "cote divoire": ("🇨🇮", "코트디부아르"),
    "morocco": ("🇲🇦", "모로코"), "moroccan": ("🇲🇦", "모로코"),
    "angola": ("🇦🇴", "앙골라"), "angolan": ("🇦🇴", "앙골라"),
    "mozambique": ("🇲🇿", "모잠비크"),
    "zambia": ("🇿🇲", "잠비아"), "zambian": ("🇿🇲", "잠비아"),
    "zimbabwe": ("🇿🇼", "짐바브웨"), "zimbabwean": ("🇿🇼", "짐바브웨"),
    "congo": ("🇨🇩", "콩고"), "drc": ("🇨🇩", "콩고민주공화국"),
    "cameroon": ("🇨🇲", "카메룬"),
    "sudan": ("🇸🇩", "수단"), "sudanese": ("🇸🇩", "수단"),
    "libya": ("🇱🇾", "리비아"), "libyan": ("🇱🇾", "리비아"),
    "tunisia": ("🇹🇳", "튀니지"), "tunisian": ("🇹🇳", "튀니지"),
    "mali": ("🇲🇱", "말리"),
    "somalia": ("🇸🇴", "소말리아"), "somali": ("🇸🇴", "소말리아"),
    "malawi": ("🇲🇼", "말라위"),
    "vietnam": ("🇻🇳", "베트남"), "vietnamese": ("🇻🇳", "베트남"),
    "indonesia": ("🇮🇩", "인도네시아"), "indonesian": ("🇮🇩", "인도네시아"),
    "thailand": ("🇹🇭", "태국"), "thai": ("🇹🇭", "태국"),
    "philippines": ("🇵🇭", "필리핀"), "philippine": ("🇵🇭", "필리핀"),
    "malaysia": ("🇲🇾", "말레이시아"), "malaysian": ("🇲🇾", "말레이시아"),
    "myanmar": ("🇲🇲", "미얀마"), "burmese": ("🇲🇲", "미얀마"),
    "cambodia": ("🇰🇭", "캄보디아"), "cambodian": ("🇰🇭", "캄보디아"),
    "singapore": ("🇸🇬", "싱가포르"), "singaporean": ("🇸🇬", "싱가포르"),
    "laos": ("🇱🇦", "라오스"),
    "ukraine": ("🇺🇦", "우크라이나"), "ukrainian": ("🇺🇦", "우크라이나"),
    "poland": ("🇵🇱", "폴란드"), "polish": ("🇵🇱", "폴란드"),
    "romania": ("🇷🇴", "루마니아"), "romanian": ("🇷🇴", "루마니아"),
    "czechia": ("🇨🇿", "체코"), "czech": ("🇨🇿", "체코"),
    "hungary": ("🇭🇺", "헝가리"), "hungarian": ("🇭🇺", "헝가리"),
    "georgia": ("🇬🇪", "조지아"), "georgian": ("🇬🇪", "조지아"),
    "azerbaija": ("🇦🇿", "아제르바이잔"), "azerbaijan": ("🇦🇿", "아제르바이잔"),
    "trend az": ("🇦🇿", "아제르바이잔"),
    "kazakhstan": ("🇰🇿", "카자흐스탄"),
    "uzbekistan": ("🇺🇿", "우즈베키스탄"),
    # 남아시아
    "pakistan": ("🇵🇰", "파키스탄"), "pakistani": ("🇵🇰", "파키스탄"),
    "dawn pakistan": ("🇵🇰", "파키스탄"),
    "bangladesh": ("🇧🇩", "방글라데시"), "bangladeshi": ("🇧🇩", "방글라데시"),
    "nepal": ("🇳🇵", "네팔"), "nepali": ("🇳🇵", "네팔"),
    "sri lanka": ("🇱🇰", "스리랑카"), "sri lankan": ("🇱🇰", "스리랑카"),
    "india": ("🇮🇳", "인도"), "indian": ("🇮🇳", "인도"),
    # 중동
    "uae": ("🇦🇪", "UAE"), "emirates": ("🇦🇪", "UAE"),
    "saudi arabia": ("🇸🇦", "사우디"), "saudi": ("🇸🇦", "사우디"),
    "qatar": ("🇶🇦", "카타르"), "qatari": ("🇶🇦", "카타르"),
    "kuwait": ("🇰🇼", "쿠웨이트"), "kuwaiti": ("🇰🇼", "쿠웨이트"),
    "oman": ("🇴🇲", "오만"), "omani": ("🇴🇲", "오만"),
    "bahrain": ("🇧🇭", "바레인"), "bahraini": ("🇧🇭", "바레인"),
    "iraq": ("🇮🇶", "이라크"), "iraqi": ("🇮🇶", "이라크"),
    "iran": ("🇮🇷", "이란"), "iranian": ("🇮🇷", "이란"),
    "yemen": ("🇾🇪", "예멘"), "yemeni": ("🇾🇪", "예멘"),
    "syria": ("🇸🇾", "시리아"), "syrian": ("🇸🇾", "시리아"),
    "jordan": ("🇯🇴", "요르단"), "jordanian": ("🇯🇴", "요르단"),
    "lebanon": ("🇱🇧", "레바논"), "lebanese": ("🇱🇧", "레바논"),
    "israel": ("🇮🇱", "이스라엘"), "israeli": ("🇮🇱", "이스라엘"),
    "palestine": ("🇵🇸", "팔레스타인"), "palestinian": ("🇵🇸", "팔레스타인"),
    # 카리브해
    "haiti": ("🇭🇹", "아이티"), "haitian": ("🇭🇹", "아이티"),
    "jamaica": ("🇯🇲", "자메이카"), "jamaican": ("🇯🇲", "자메이카"),
    "trinidad": ("🇹🇹", "트리니다드"), "dominican": ("🇩🇴", "도미니카"),
    # 태평양
    "papua new guinea": ("🇵🇬", "파푸아뉴기니"), "png": ("🇵🇬", "파푸아뉴기니"),
    "fiji": ("🇫🇯", "피지"), "fijian": ("🇫🇯", "피지"),
    "solomon": ("🇸🇧", "솔로몬제도"),
    "vanuatu": ("🇻🇺", "바누아투"),
    # 중앙아시아
    "kyrgyzstan": ("🇰🇬", "키르기스스탄"), "kyrgyz": ("🇰🇬", "키르기스스탄"),
    "tajikistan": ("🇹🇯", "타지키스탄"), "tajik": ("🇹🇯", "타지키스탄"),
    "turkmenistan": ("🇹🇲", "투르크메니스탄"),
    "mongolia": ("🇲🇳", "몽골"), "mongolian": ("🇲🇳", "몽골"),
    # 라틴아메리카
    "venezuela": ("🇻🇪", "베네수엘라"), "venezuelan": ("🇻🇪", "베네수엘라"),
    "bolivia": ("🇧🇴", "볼리비아"), "bolivian": ("🇧🇴", "볼리비아"),
    "ecuador": ("🇪🇨", "에콰도르"), "ecuadorian": ("🇪🇨", "에콰도르"),
    "paraguay": ("🇵🇾", "파라과이"),
    "nicaragua": ("🇳🇮", "니카라과"),
    "el salvador": ("🇸🇻", "엘살바도르"),
    "guatemala": ("🇬🇹", "과테말라"),
    "honduras": ("🇭🇳", "온두라스"),
    # 오세아니아
    "australia": ("🇦🇺", "호주"), "australian": ("🇦🇺", "호주"),
    "new zealand": ("🇳🇿", "뉴질랜드"), "new zealander": ("🇳🇿", "뉴질랜드"),
    "timor": ("🇹🇱", "동티모르"),
    # 카리브해 추가
    "barbados": ("🇧🇧", "바베이도스"), "bahamas": ("🇧🇸", "바하마"),
    "cuba": ("🇨🇺", "쿠바"), "cuban": ("🇨🇺", "쿠바"),
    "guyana": ("🇬🇾", "가이아나"), "suriname": ("🇸🇷", "수리남"),
    # 아프리카 추가
    "burkina faso": ("🇧🇫", "부르키나파소"),
    "niger": ("🇳🇪", "니제르"),
    "chad": ("🇹🇩", "차드"),
    "guinea": ("🇬🇳", "기니"),
    "sierra leone": ("🇸🇱", "시에라리온"),
    "liberia": ("🇱🇷", "라이베리아"),
    "togo": ("🇹🇬", "토고"),
    "benin": ("🇧🇯", "베냉"),
    "gabon": ("🇬🇦", "가봉"),
    "botswana": ("🇧🇼", "보츠와나"),
    "namibia": ("🇳🇦", "나미비아"),
    "eswatini": ("🇸🇿", "에스와티니"),
    "lesotho": ("🇱🇸", "레소토"),
    "eritrea": ("🇪🇷", "에리트레아"),
    "djibouti": ("🇩🇯", "지부티"),
    "mauritius": ("🇲🇺", "모리셔스"),
    "madagascar": ("🇲🇬", "마다가스카르"),
    "seychelles": ("🇸🇨", "세이셸"),
    # 주요국 (글로벌 분류용)
    "united states": ("🇺🇸", "미국"), "american": ("🇺🇸", "미국"),
    "china": ("🇨🇳", "중국"), "chinese": ("🇨🇳", "중국"),
    "japan": ("🇯🇵", "일본"), "japanese": ("🇯🇵", "일본"),
    "france": ("🇫🇷", "프랑스"), "french": ("🇫🇷", "프랑스"),
    "germany": ("🇩🇪", "독일"), "german": ("🇩🇪", "독일"),
    "united kingdom": ("🇬🇧", "영국"), "british": ("🇬🇧", "영국"),
    "russia": ("🇷🇺", "러시아"), "russian": ("🇷🇺", "러시아"),
    "turkey": ("🇹🇷", "튀르키예"), "turkish": ("🇹🇷", "튀르키예"),
    "south korea": ("🇰🇷", "한국"), "korean": ("🇰🇷", "한국"),
    "brazil": ("🇧🇷", "브라질"), "brazilian": ("🇧🇷", "브라질"),
    "mexico": ("🇲🇽", "멕시코"), "mexican": ("🇲🇽", "멕시코"),
    "colombia": ("🇨🇴", "콜롬비아"), "colombian": ("🇨🇴", "콜롬비아"),
    "argentina": ("🇦🇷", "아르헨티나"), "argentine": ("🇦🇷", "아르헨티나"),
    "chile": ("🇨🇱", "칠레"), "chilean": ("🇨🇱", "칠레"),
    "peru": ("🇵🇪", "페루"), "peruvian": ("🇵🇪", "페루"),
    "italy": ("🇮🇹", "이탈리아"), "italian": ("🇮🇹", "이탈리아"),
    "spain": ("🇪🇸", "스페인"), "spanish": ("🇪🇸", "스페인"),
    "netherlands": ("🇳🇱", "네덜란드"), "dutch": ("🇳🇱", "네덜란드"),
    "canada": ("🇨🇦", "캐나다"), "canadian": ("🇨🇦", "캐나다"),
    "portugal": ("🇵🇹", "포르투갈"), "portuguese": ("🇵🇹", "포르투갈"),
    # ── 프랑스어/현지 지명 ──
    "cote divoire": ("🇨🇮", "코트디부아르"), "ivory coast": ("🇨🇮", "코트디부아르"), "abidjan": ("🇨🇮", "코트디부아르"),
    "abidjan": ("🇨🇮", "코트디부아르"), "ivoirien": ("🇨🇮", "코트디부아르"),
    "dakar": ("🇸🇳", "세네갈"), "sénégal": ("🇸🇳", "세네갈"),
    "bamako": ("🇲🇱", "말리"), "malien": ("🇲🇱", "말리"),
    "ouagadougou": ("🇧🇫", "부르키나파소"), "burkinabè": ("🇧🇫", "부르키나파소"),
    "niamey": ("🇳🇪", "니제르"), "nigérien": ("🇳🇪", "니제르"),
    "ndjamena": ("🇹🇩", "차드"), "tchad": ("🇹🇩", "차드"),
    "yaoundé": ("🇨🇲", "카메룬"), "yaounde": ("🇨🇲", "카메룬"), "cameroun": ("🇨🇲", "카메룬"),
    "douala": ("🇨🇲", "카메룬"),
    "kinshasa": ("🇨🇩", "DRC"), "rdc": ("🇨🇩", "DRC"),
    "brazzaville": ("🇨🇬", "콩고공화국"),
    "bangui": ("🇨🇫", "중앙아프리카"), "centrafrique": ("🇨🇫", "중앙아프리카"),
    "libreville": ("🇬🇦", "가봉"),
    "lomé": ("🇹🇬", "토고"), "lome": ("🇹🇬", "토고"),
    "cotonou": ("🇧🇯", "베냉"), "bénin": ("🇧🇯", "베냉"),
    "conakry": ("🇬🇳", "기니"), "guinée": ("🇬🇳", "기니"),
    "antananarivo": ("🇲🇬", "마다가스카르"),
    "port-au-prince": ("🇭🇹", "아이티"), "haïti": ("🇭🇹", "아이티"),
    # ── 아랍어 지명 (로마자) ──
    "khartoum": ("🇸🇩", "수단"), "al-khartoum": ("🇸🇩", "수단"),
    "mogadishu": ("🇸🇴", "소말리아"), "muqdisho": ("🇸🇴", "소말리아"),
    "tripoli": ("🇱🇾", "리비아"),
    "tunis": ("🇹🇳", "튀니지"), "tunisie": ("🇹🇳", "튀니지"),
    "alger": ("🇩🇿", "알제리"), "algérie": ("🇩🇿", "알제리"),
    "rabat": ("🇲🇦", "모로코"), "maroc": ("🇲🇦", "모로코"),
    "riyadh": ("🇸🇦", "사우디"),
    "abu dhabi": ("🇦🇪", "UAE"), "dubai": ("🇦🇪", "UAE"),
    "baghdad": ("🇮🇶", "이라크"),
    "amman": ("🇯🇴", "요르단"),
    "beirut": ("🇱🇧", "레바논"), "beyrouth": ("🇱🇧", "레바논"),
    "sanaa": ("🇾🇪", "예멘"),
    "jerusalem": ("🇵🇸", "팔레스타인"), "tel aviv": ("🇮🇱", "이스라엘"),
    # ── 포르투갈어 지명 ──
    "luanda": ("🇦🇴", "앙골라"), "angolano": ("🇦🇴", "앙골라"),
    "maputo": ("🇲🇿", "모잠비크"), "moçambique": ("🇲🇿", "모잠비크"),
    "cabo verde": ("🇨🇻", "카보베르데"),
    # ── 동남아/인도네시아어 지명 ──
    "jakarta": ("🇮🇩", "인도네시아"),
    "kuala lumpur": ("🇲🇾", "말레이시아"),
    "manila": ("🇵🇭", "필리핀"),
    "naypyidaw": ("🇲🇲", "미얀마"), "yangon": ("🇲🇲", "미얀마"),
    "phnom penh": ("🇰🇭", "캄보디아"),
    "vientiane": ("🇱🇦", "라오스"),
    "hanoi": ("🇻🇳", "베트남"), "ho chi minh": ("🇻🇳", "베트남"),
    # ── 중앙아시아 지명 ──
    "bishkek": ("🇰🇬", "키르기스스탄"),
    "dushanbe": ("🇹🇯", "타지키스탄"),
    "ashgabat": ("🇹🇲", "투르크메니스탄"),
    "tashkent": ("🇺🇿", "우즈베키스탄"),
    "astana": ("🇰🇿", "카자흐스탄"), "almaty": ("🇰🇿", "카자흐스탄"),
    "yerevan": ("🇦🇲", "아르메니아"), "armenia": ("🇦🇲", "아르메니아"),
    "baku": ("🇦🇿", "아제르바이잔"),
    "tbilisi": ("🇬🇪", "조지아"),
    # ── 오세아니아 지명 ──
    "sydney": ("🇦🇺", "호주"), "melbourne": ("🇦🇺", "호주"),
    "auckland": ("🇳🇿", "뉴질랜드"), "wellington": ("🇳🇿", "뉴질랜드"),
}

# 주요국 — 글로벌 카테고리로 분류
GLOBAL_COUNTRIES = {
    "미국", "중국", "일본", "프랑스", "독일", "영국", "러시아",
    "튀르키예", "한국", "브라질", "멕시코", "콜롬비아", "아르헨티나",
    "칠레", "페루", "이스라엘", "이탈리아", "스페인", "네덜란드",
    "캐나다", "포르투갈", "호주", "뉴질랜드",
}

def detect_countries(text: str, source: str = "") -> list:
    """텍스트에서 감지된 모든 국가 반환 [(flag, name), ...]"""
    import re
    t = text.lower()
    found = {}
    sorted_keys = sorted(COUNTRY_INFO.keys(), key=len, reverse=True)
    for keyword in sorted_keys:
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, t):
            flag, name = COUNTRY_INFO[keyword]
            if name not in found:
                found[name] = flag
    return [(flag, name) for name, flag in found.items()]

def detect_country(text: str, source: str = ""):
    """제목/요약에서 국가 정보 반환 (국기, 국가명)
    - 제목/요약 우선 검색
    - 출처명은 마지막 폴백으로만 사용
    - 단어 경계 체크로 오탐 방지
    """
    import re

    def find_in_text(t):
        t_lower = t.lower()
        # 긴 키워드 우선 매칭 (south africa가 africa보다 먼저)
        sorted_keys = sorted(COUNTRY_INFO.keys(), key=len, reverse=True)
        for keyword in sorted_keys:
            # 단어 경계로 매칭 (niger가 nigeria에 매칭되지 않도록)
            pattern = r'\b' + re.escape(keyword) + r'\b'
            if re.search(pattern, t_lower):
                return COUNTRY_INFO[keyword]
        return None

    # 1. 제목/요약에서 먼저 찾기
    result = find_in_text(text)
    if result:
        return result

    # 2. 출처명에서 폴백 (source-specific 키워드만)
    if source:
        source_lower = source.lower()
        source_specific = {
            "trend az": ("🇦🇿", "아제르바이잔"),
            "dawn pakistan": ("🇵🇰", "파키스탄"),
            "akipress": ("🇰🇬", "키르기스스탄"),
            "kabar": ("🇰🇬", "키르기스스탄"),
            "kun.uz": ("🇺🇿", "우즈베키스탄"),
            "astanatimes": ("🇰🇿", "카자흐스탄"),
        }
        for keyword, val in source_specific.items():
            if keyword in source_lower:
                return val

    return "", ""

# =========================
# STATE
# =========================

def load_state():
    default = {
        "rss_health": {},
        "daily_count": 0,
        "last_reset": now_kst().strftime("%Y-%m-%d")
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                # sent 키는 무시 (DB로 이전)
                saved.pop("sent", None)
                default.update(saved)
        except (json.JSONDecodeError, Exception) as e:
            print(f"[WARNING] state.json 손상 — 초기화: {e}")
    return default

state = load_state()
rss_health = state["rss_health"]

today = now_kst().strftime("%Y-%m-%d")
if state["last_reset"] != today:
    state["daily_count"] = 0
    state["last_reset"] = today

# =========================
# RSS LOADER
# =========================

def load_rss():
    """Supabase rss_sources 테이블에서 소스 로드 (파일 노출 방지)"""
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    res = requests.get(
        f"{supabase_url}/rest/v1/rss_sources",
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
        },
        params={"select": "name,category,subcategory,url", "is_active": "eq.true", "limit": "1000"},
        timeout=15,
    )
    res.raise_for_status()
    sources = res.json()
    if not sources:
        raise RuntimeError("rss_sources 테이블이 비어 있습니다. 마이그레이션 SQL을 먼저 실행하세요.")
    print(f"✅ RSS 소스 {len(sources)}개 로드 (Supabase)")
    return sources

# =========================
# UTIL
# =========================

def normalize_url(url):
    try:
        p = urlparse(url)
        return urlunparse(p._replace(query="", fragment=""))
    except:
        return url

def fingerprint(title, source):
    return hashlib.md5(
        f"{title.lower().strip()}|{source}".encode()
    ).hexdigest()

SIMILARITY_THRESHOLD = 75

def is_duplicate(title, seen_titles):
    for seen in seen_titles:
        score = fuzz.token_sort_ratio(title.lower(), seen.lower())
        if score >= SIMILARITY_THRESHOLD:
            return True
    return False

# 번역 API가 예외 대신 에러 페이지 본문을 문자열로 반환하는 경우가 있다.
# (deep_translator GoogleTranslator, 500/429 응답 시) 검증 없이 저장하면 제목이 오염된다.
_TRANS_ERR_MARKS = (
    "That's an error",
    "That's all we know",
    "Server Error",
    "Error 500",
    "Error 502",
    "Error 503",
    "unusual traffic from your computer network",
    "Our systems have detected",
)


def _is_bad_translation(t) -> bool:
    """번역 결과가 실제 번역문이 아니라 에러 페이지인지 판정."""
    if not t or not isinstance(t, str):
        return True
    return any(m in t for m in _TRANS_ERR_MARKS)


def clean_text(text):
    if not text:
        return ""
    # HTML 태그 제거
    text = re.sub(r'<[^>]+>', '', text)
    # HTML 엔티티 디코딩
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    text = re.sub(r'&#x[0-9a-fA-F]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)
    # 특수문자 정리
    replacements = {
        '\u2019': "'", '\u2018': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
        '\xa0': ' '
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)
    # 연속 공백 정리
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def escape_markdown(text: str) -> str:
    """텔레그램 Markdown 특수문자 이스케이프"""
    if not text:
        return ""
    chars = ['*', '_', '[', ']', '(', ')', '`', '#']
    for c in chars:
        text = text.replace(c, f'\\{c}')
    return text

def extract_summary(entry):
    summary = entry.get("summary", "") or entry.get("description", "")
    if not summary:
        return ""
    summary = re.sub(r'<[^>]+>', '', summary)
    summary = re.sub(r'\s+', ' ', summary).strip()
    # 오류 페이지 감지
    if any(x in summary.lower() for x in ["error 500", "error 404", "server error", "that's an error", "please try again"]):
        return ""
    # 2026-08-30 사용자 지적: 150자 제한에 문서화된 근거가 없었음. full_text가
    # 크롤링 안 되는 소스(특히 구글뉴스)는 이 요약이 사실상 유일한 본문
    # 재료인데, 여기서 이미 뭉개지면 이후 어떤 단계에서 절단 제한을 풀어도
    # 소용없다 — 가장 상류 병목이었음. 토큰 비용이 실제 제약이 아니므로 제거.
    return clean_text(summary)


# ── 사진 캡션·크레딧 제거 ─────────────────────────────────────────────
# 원문 <p>에 섞여 들어오는 사진 캡션은 "설명문 + 날짜 + © + 크레딧" 구조가 흔하다.
# 자료사진이면 캡션 날짜가 사건 날짜와 수개월씩 벌어진다.
# 실사고(2026-07-30): France 24 기사(7/29 발행)의 캡션
#   "Afghan Taliban soldiers look toward the Pakistani border, 27 February 2026. © Wahidullah Kakar, AP"
# 를 Gemini가 보도 시점으로 오인해 "2026년 2월(현지시간) 보도했다"로 기사화(id=38770).
# ⚠️ 날짜는 © '앞' 문장에 있다 → ©부터 잘라내면 소용없고, © 직전 문장까지 함께 제거해야 한다.
_CREDIT_MARK_RE = re.compile(r'&copy;|\(c\)\s|©', re.IGNORECASE)

# 캡션 전용 조각(접두로 시작) — 통째로 버린다
_CAPTION_PREFIX_RE = re.compile(
    r'^\s*(?:Photos?|Images?|Pictured?|Caption|Credits?|Cover image|File photo|Handout|'
    r'Foto|Légende|Legende)\s*[:\-\u2013\u2014|]',
    re.IGNORECASE,
)

# 문장 경계 (© 직전 캡션 문장의 시작점을 찾는 데 사용)
_SENT_END_RE = re.compile(r'[.!?][\"\'\)\]]?\s')

# © 뒤 크레딧 표기 — 대문자 시작 토큰·연도·연결어의 짧은 연속으로 본다.
# 소문자 일반명사가 나오면 본문 시작으로 판단해 멈춘다.
_CREDIT_TAIL_RE = re.compile(
    r'^[\s:\-\u2013\u2014|]*'
    r'(?:[A-Z\u00c0-\u00dc][\w.\u2019\'\-]*|\d{1,4}(?:\s*[-\u2013]\s*\d{2,4})?|and|de|du|des|ve|par|via)'
    r'(?:[\s,\-\u2013\u2014/&]+'
    r'(?:[A-Z\u00c0-\u00dc][\w.\u2019\'\-]*|\d{1,4}(?:\s*[-\u2013]\s*\d{2,4})?|and|de|du|des|ve|par|via)'
    r'){0,5}'
)

CAPTION_LEAD_MAX = 220   # © 앞을 캡션 설명문으로 보고 제거할 최대 길이(초과 시 본문으로 판단해 보존)
CREDIT_TAIL_MAX = 50     # © 뒤 크레딧으로 보고 제거할 최대 길이
CAPTION_KEEP_MIN = 100   # 제거 후 남은 길이가 이보다 짧으면 조각 전체를 버린다


def strip_photo_credits(text: str) -> str:
    """단락에서 사진 캡션·저작권 크레딧을 제거한다.

    - 캡션 접두로 시작하는 단락은 통째로 버린다.
    - © 표기가 있으면 '직전 문장 + © + 직후 크레딧'을 제거하고 나머지는 살린다.
    - 제거 결과가 CAPTION_KEEP_MIN 미만이면 단락 전체를 버린다.
    """
    if not text:
        return ""
    if _CAPTION_PREFIX_RE.match(text):
        return ""

    out = text
    for _ in range(4):  # 한 단락에 크레딧이 여러 번 나올 수 있다
        m = _CREDIT_MARK_RE.search(out)
        if not m:
            break
        start, end = m.start(), m.end()

        # ① © 앞: 캡션 설명문을 문장 단위로 제거
        # ⚠️ ©가 문장 끝 바로 뒤에 오는 경우(캡션이 온전한 한 문장인 전형적 형태)
        #    마지막 경계를 쓰면 캡션이 그대로 남는다 → 그 앞 경계(=캡션 문장의 시작)를 써야 한다.
        bounds = [b.end() for b in _SENT_END_RE.finditer(out[:start])]
        if bounds and start - bounds[-1] <= 3:
            cut_from = bounds[-2] if len(bounds) >= 2 else 0
        else:
            cut_from = bounds[-1] if bounds else 0
        if start - cut_from > CAPTION_LEAD_MAX:
            cut_from = start  # 너무 길면 본문일 가능성 → 앞부분 보존

        # ② © 뒤: 크레딧 표기만 상한 내에서 제거
        tm = _CREDIT_TAIL_RE.match(out[end:end + CREDIT_TAIL_MAX])
        cut_to = end + (tm.end() if tm else 0)

        out = (out[:cut_from] + " " + out[cut_to:]).strip()

    out = re.sub(r'\s+', ' ', out).strip()
    if len(out) < CAPTION_KEEP_MIN:
        return ""
    return out


# 원문 크롤링 불필요 사이트 (이미 RSS에 전문 제공)
SKIP_CRAWL_DOMAINS = {
    "allafrica.com", "africa-newsroom.com", "afdb.org",
    "imf.org", "worldbank.org", "afro.who.int", "au.int",
    "unctad.org", "ifc.org", "asean.org", "adb.org",
}

# ── arXiv 전용 처리 ────────────────────────────────────────
# 문제 2건이 겹쳐 있다:
#   ① arxiv.org/abs 페이지의 <p> 태그에는 초록이 없다. 초록은 <blockquote>에 있고
#      <p>에는 "BibTeX 인용 정보", "14,508 KB" 같은 UI 안내문뿐이라 그게 본문으로 수집됐다.
#   ② 피드가 수개월 전 논문을 재발행한다(실측: 8/3 수집분에 4월 논문 3건).
#      RSS의 published는 목록 갱신일이라 실제 제출일과 다르다.
# → 공식 Atom API로 초록(<summary>)과 최초 제출일(<published>)을 직접 받는다.
#   API 스펙: arXiv API User's Manual 3.3.2.1 / rate limit 3req/s
ARXIV_API = "http://export.arxiv.org/api/query?id_list="
ARXIV_MAX_AGE_DAYS = 30          # 제출일이 이보다 오래되면 수집하지 않는다
ARXIV_MIN_ABSTRACT = 100         # 초록이 이보다 짧으면 확보 실패로 본다
ARXIV_CALL_INTERVAL = 3.0        # 매뉴얼 권고 최소 간격(초)

_ARXIV_ID_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/"
    r"([0-9]{4}\.[0-9]{4,5}|[a-z\-]+(?:\.[A-Za-z]{2})?/[0-9]{7})"
    r"(?:v[0-9]+)?", re.I
)

_arxiv_last_call = [0.0]


def arxiv_id_from_url(url):
    """arXiv 논문 URL에서 식별자를 뽑는다. arXiv가 아니면 None."""
    if not url:
        return None
    m = _ARXIV_ID_RE.search(str(url))
    return m.group(1) if m else None


def _iso_age_days(iso_str):
    """ISO8601(UTC) 문자열의 경과일수. 파싱 실패 시 None."""
    try:
        tt = time.strptime(str(iso_str)[:19], "%Y-%m-%dT%H:%M:%S")
        return (time.time() - calendar.timegm(tt)) / 86400.0
    except Exception:
        return None


def fetch_arxiv_meta(arxiv_id, timeout=12):
    """arXiv Atom API에서 (초록, 최초제출일ISO)를 가져온다. 실패 시 (None, None)."""
    import xml.etree.ElementTree as ET
    try:
        wait = ARXIV_CALL_INTERVAL - (time.time() - _arxiv_last_call[0])
        if wait > 0:
            time.sleep(wait)
        _arxiv_last_call[0] = time.time()

        res = requests.get(ARXIV_API + arxiv_id,
                           headers={"User-Agent": "NewsFinalBot/1.0"},
                           timeout=timeout)
        if res.status_code != 200:
            return None, None

        ns = {"a": "http://www.w3.org/2005/Atom"}
        entry = ET.fromstring(res.content).find("a:entry", ns)
        if entry is None:
            return None, None

        # 없는 ID를 요청하면 id가 .../api/errors 인 엔트리가 돌아온다
        eid = entry.find("a:id", ns)
        if eid is not None and eid.text and "/api/errors" in eid.text:
            return None, None

        node = entry.find("a:summary", ns)
        abstract = ""
        if node is not None and node.text:
            abstract = clean_text(re.sub(r"\s+", " ", node.text).strip())
        if len(abstract) < ARXIV_MIN_ABSTRACT:
            abstract = ""

        node = entry.find("a:published", ns)
        published = node.text.strip() if (node is not None and node.text) else ""
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", published):
            published = ""

        return (abstract or None), (published or None)
    except Exception:
        return None, None


def crawl_full_text(url: str, timeout: int = 10) -> str:
    """원문 URL에서 본문 텍스트 추출"""
    try:
        domain = urlparse(url).netloc.replace("www.", "")
        if domain in SKIP_CRAWL_DOMAINS:
            return ""

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code != 200:
            return ""

        html = res.text

        # BeautifulSoup 없이 간단 파싱 — <article>, <main>, <p> 태그 추출
        # 스크립트/스타일 제거
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)

        # article 태그 우선
        article_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL|re.IGNORECASE)
        if article_match:
            text_html = article_match.group(1)
        else:
            # main 태그
            main_match = re.search(r'<main[^>]*>(.*?)</main>', html, re.DOTALL|re.IGNORECASE)
            text_html = main_match.group(1) if main_match else html

        # p 태그 내용 추출
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', text_html, re.DOTALL|re.IGNORECASE)
        texts = []
        for p in paragraphs:
            t = re.sub(r'<[^>]+>', '', p).strip()
            t = re.sub(r'\s+', ' ', t)
            if len(t) <= 50:  # 너무 짧은 문장 제외
                continue
            t = strip_photo_credits(t)  # 사진 캡션·크레딧 제거(캡션 날짜 오인 방지)
            if t:
                texts.append(t)

        full_text = ' '.join(texts)
        return clean_text(full_text) if len(full_text) > 100 else ""

    except Exception:
        return ""

# 소스명 → 기본 국가 매핑
SOURCE_COUNTRY_MAP = {
    # 나이지리아
    "nairametrics": ("🇳🇬", "나이지리아"), "businessday nigeria": ("🇳🇬", "나이지리아"),
    "punch": ("🇳🇬", "나이지리아"), "vanguard": ("🇳🇬", "나이지리아"),
    "premium times": ("🇳🇬", "나이지리아"), "guardian nigeria": ("🇳🇬", "나이지리아"),
    "allafrica nigeria": ("🇳🇬", "나이지리아"),
    # 가나
    "ghanaweb": ("🇬🇭", "가나"), "ghana business": ("🇬🇭", "가나"),
    "joy business": ("🇬🇭", "가나"), "allafrica ghana": ("🇬🇭", "가나"),
    # 케냐
    "business daily africa": ("🇰🇪", "케냐"), "nation africa": ("🇰🇪", "케냐"),
    "standard media kenya": ("🇰🇪", "케냐"), "kenyan wall street": ("🇰🇪", "케냐"),
    "allafrica kenya": ("🇰🇪", "케냐"),
    # 남아공
    "businesstech": ("🇿🇦", "남아공"), "daily maverick": ("🇿🇦", "남아공"),
    "fin24": ("🇿🇦", "남아공"), "business insider south africa": ("🇿🇦", "남아공"),
    "mining weekly": ("🇿🇦", "남아공"),
    # 에티오피아
    "addis fortune": ("🇪🇹", "에티오피아"), "the reporter ethiopia": ("🇪🇹", "에티오피아"),
    "allafrica ethiopia": ("🇪🇹", "에티오피아"),
    # 르완다
    "rwanda new times": ("🇷🇼", "르완다"), "allafrica rwanda": ("🇷🇼", "르완다"),
    # 베트남
    "vietnam investment review": ("🇻🇳", "베트남"), "vietnam news": ("🇻🇳", "베트남"),
    "vnexpress": ("🇻🇳", "베트남"), "vietnamplus": ("🇻🇳", "베트남"),
    # 인도네시아
    "jakarta post": ("🇮🇩", "인도네시아"), "indonesia setkab": ("🇮🇩", "인도네시아"),
    # 태국
    "bangkok post": ("🇹🇭", "태국"),
    # 필리핀
    "philippine star": ("🇵🇭", "필리핀"), "rappler": ("🇵🇭", "필리핀"),
    "philippine information agency": ("🇵🇭", "필리핀"),
    # 인도
    "the hindu": ("🇮🇳", "인도"), "hindu business": ("🇮🇳", "인도"),
    "business today india": ("🇮🇳", "인도"), "economic times": ("🇮🇳", "인도"),
    "deccan chronicle": ("🇮🇳", "인도"),
    # 자메이카
    "jamaica observer": ("🇯🇲", "자메이카"), "jamaica gleaner": ("🇯🇲", "자메이카"),
    "loop jamaica": ("🇯🇲", "자메이카"), "rjr news": ("🇯🇲", "자메이카"),
    # 카리브해 일반
    "caribbean news now": ("🇯🇲", "카리브해"),
    # 카자흐스탄
    "astana times": ("🇰🇿", "카자흐스탄"), "kazinform": ("🇰🇿", "카자흐스탄"),
    "kazakhstan inform": ("🇰🇿", "카자흐스탄"),
    # 우즈베키스탄
    "kun.uz": ("🇺🇿", "우즈베키스탄"), "uzbekistan president": ("🇺🇿", "우즈베키스탄"),
    # 키르기스스탄
    "akipress": ("🇰🇬", "키르기스스탄"), "kabar": ("🇰🇬", "키르기스스탄"),
    # 아제르바이잔
    "trend az": ("🇦🇿", "아제르바이잔"), "trend azerbaijan": ("🇦🇿", "아제르바이잔"),
    # 방글라데시
    "daily star bangladesh": ("🇧🇩", "방글라데시"), "dhaka tribune": ("🇧🇩", "방글라데시"),
    "the business standard bangladesh": ("🇧🇩", "방글라데시"),
    # 파키스탄
    "dawn pakistan": ("🇵🇰", "파키스탄"),
    # 사우디
    "saudi press agency": ("🇸🇦", "사우디아라비아"),
    # 카타르
    "qatar news agency": ("🇶🇦", "카타르"),
    # 이스라엘
    "ynet": ("🇮🇱", "이스라엘"), "israel21c": ("🇮🇱", "이스라엘"),
    "globes": ("🇮🇱", "이스라엘"), "jpost": ("🇮🇱", "이스라엘"),
    "haaretz": ("🇮🇱", "이스라엘"), "times of israel": ("🇮🇱", "이스라엘"),
    # 팔레스타인
    "wafa": ("🇵🇸", "팔레스타인"), "palinfo": ("🇵🇸", "팔레스타인"),
    "palestine chronicle": ("🇵🇸", "팔레스타인"), "palestine news": ("🇵🇸", "팔레스타인"),
    # 오세아니아
    "abc news australia": ("🇦🇺", "호주"), "stuff nz": ("🇳🇿", "뉴질랜드"),
    "rnz": ("🇳🇿", "뉴질랜드"),
    # 라틴아메리카
    "alo clandestino": ("🇻🇪", "베네수엘라"),
    "bolivia express": ("🇧🇴", "볼리비아"),
    "telesur": ("🇻🇪", "베네수엘라"),
    "diario": ("🇨🇷", "코스타리카"),
}

def get_source_country(source_name: str):
    """소스명으로 기본 국가 반환"""
    s = source_name.lower()
    for key, val in SOURCE_COUNTRY_MAP.items():
        if key in s:
            return val
    return None, None

NOISE_KEYWORDS = [
    # 스포츠 — 종목명·대회명처럼 스포츠 외 의미가 거의 없는 것만 하드 차단.
    # ⚠️ 일반명사와 겹치는 8개("match" "league" "coach" "player" "transfer"
    #    "goal" "squad" "champion")는 OBSERVE_KEYWORDS로 이관됨(2026-07-29).
    #    단어경계로도 해결이 안 되는 '의미 충돌'이라 관찰 후 판단한다.
    "football", "soccer", "cricket", "basketball", "rugby", "tennis",
    "golf", "athletics", "olympics", "tournament", "fixture",
    "premier league", "champions league", "world cup", "cup final",
    # ※ 연예/문화 키워드는 OBSERVE_KEYWORDS(관찰용 소프트 노이즈)로 이관됨
    # 기타 노이즈
    # "travel", "tourism" 제거(2026-07-29): 여행 정보 기능(is_travel/travel_guides)과
    # 정면 충돌. 실제로 여행 기사 36건 중 영문 제목에 travel/tourism이 든 건 0건이었다.
    "e-edition", "edition", "sumo",
    "horoscope", "obituary", "recipe", "weather forecast",
    "eurovision", "beauty pageant", "miss world", "miss universe",
    "lottery", "powerball", "lotto", "flag day", "national anthem",
    "zapping", "podcast", "5 ways", "how to celebrate",
    "co-owner", "ownership stake", "national sound",
    # 일일 시세표 — 매일 같은 형태로 반복 생성되는 저가치 콘텐츠.
    # ⚠️ "gold" / "gold price" 처럼 넓게 잡으면 금 산업·시장 분석 기사까지
    #    날아간다(Mining.com "Goldman cuts gold price forecast",
    #    Joy Business Ghana "export earnings hit $11.1bn on surging gold prices" 등).
    #    시세표 제목에만 나타나는 표현으로 좁힐 것.
    "per tola", "check opening rates", "check new rates",
    "opening rates on", "currency exchange rates in",
    # 매체 자체의 일일 뉴스 다이제스트/모음 — 우리 사이트에는 이미 자체
    # "데일리 다이제스트" 기능이 있어 겹친다(2026-08-10 사용자 확정, 용납 불가).
    # 무관한 사건 여러 개를 한 기사에 욱여넣는 형식이라 verify_single_topic이
    # 가끔 놓친다(실사고 id=61648: "텔레그래프 투데이가 전하는 8일자 주요
    # 뉴스 동향" — 증시·테니스·홍수·국경통제·교통사고·연예 6~7건이 한 기사에
    # 섞여 발행됨. id=6273, 64613도 같은 유형). RSS 수집 단계에서 아예 차단해
    # 클러스터링·단독기사화 파이프라인까지 안 가게 막는다.
    # 실측 확인된 반복 소스: OCHA Africa("Today's top news: 국가나열"),
    # Punch Business("Morning/Evening/Afternoon recap: ... other top stories"),
    # Egypt Independent("... and the day's other top stories").
    "today's top news", "other top stories", "day's other top stories",
    "morning recap", "evening recap", "afternoon recap",
    "news roundup", "news round-up", "daily briefing", "morning briefing",
    "news at a glance", "today's headlines", "headlines today",
]

# 소프트 노이즈 — 수집은 하되 텔레그램/홈페이지 발송 안 함
SOFT_NOISE_KEYWORDS = [
    "anthropic", "openai", "nvidia", "apple inc", "microsoft corp",
    "meta platforms", "amazon.com", "tesla inc", "spacex", "chatgpt",
    "nasdaq", "s&p 500", "dow jones", "wall street", "new york stock",
    "federal reserve", "us federal", "fed rate",
]

def _compile_keywords(words):
    """키워드 목록을 단어경계 정규식으로 컴파일.

    단순 `in` 부분일치는 다른 단어 속에 묻힌 키워드까지 잡아 심각한 오탐을 낳았다.
    실측(2026-07-29): "actor"가 f[actor]ies·re[actor]s에 걸려 나이지리아 섬유공장,
    모로코 배터리 기가팩토리, MIT 원자로 기사가 전부 차단되고 있었다.
    → 앞뒤 단어경계를 강제하고 단순 복수형(-s)만 허용한다.
      "factories"는 안 걸리고 "films"는 걸린다.
      단, "churches"처럼 -es 복수는 매칭되지 않으니 필요하면 목록에 따로 넣을 것.
    """
    return re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in words) + r")s?\b",
        re.IGNORECASE,
    )


_NOISE_RE = _compile_keywords(NOISE_KEYWORDS)


def is_noise(title: str) -> bool:
    return bool(_NOISE_RE.search(title or ""))

# 관찰용(2026-07-29~) — 원래 하드 차단이던 문화·사회 키워드를 임시로 여기로 옮겼다.
# 목적: 하드 차단은 DB에 흔적이 안 남아 "무엇을 버려왔는지" 측정이 불가능했다.
# 소프트 노이즈는 수집·번역만 하고 발행은 안 하므로, 며칠 쌓아 실제 제목을 보고
# 어떤 키워드를 영구 해제할지 판단한다.
#   비용: 번역은 GoogleTranslator(무료), sent_telegram=0이라 기사 생성 단계 진입 안 함.
# 판단이 끝나면 이 목록은 비우고, 남길 것만 NOISE_KEYWORDS로 되돌린다.
OBSERVE_KEYWORDS = [
    # ⚠️ 제거됨(2026-07-29 관찰 1일차): "actor" "film" "movie" "palace"
    #   - "actor" → f[actor]ies / re[actor]s 오염. 제조업·원자력 기사가 통째로 차단됨
    #   - "film"  → [film]ed 오염 (범죄 촬영 사건기사)
    #   - "palace" → 필리핀 대통령궁(Malacanang Palace) 등 정치 기사 오염
    #   - "movie" → 영화 사칭 피싱 등 보안 기사 오염
    #
    # ⚠️ 영구 해제됨(2026-07-29 관찰 결과): 연예/문화 9개 + 문화유산/종교/왕실 11개
    #   ("celebrity" "music" "wedding" "entertainment" "fashion" "actress"
    #    "singer" "concert" "album" / "museum" "royal" "heritage site"
    #    "archaeological" "church" "pastor" "bishop" "prayer" "sermon"
    #    "festival" "leisure")
    #   실측: 관찰 기사 중 이 그룹에 걸린 11건 가운데 5건이 일반 뉴스 오탐이었다.
    #     - "festival" → 헝가리 축제 칼부림(사건사고), 홍콩 쇼핑페스티벌 아세안 확장(경제)
    #     - "pastor"   → 목사 로맨스 사기(범죄)
    #     - "concert"  → 퀸 1986 부다페스트 공연과 소련 블록(역사·정치)
    #     - "heritage site" → 남수단 첫 유네스코 등재(국가 뉴스)
    #   나머지 6건도 순수 문화 기사이나, 편집 방향상 프론티어 지역 사회·문화는
    #   차별화 자산이라 차단 대상이 아니다(문화·예술 비중 1.4%로 오히려 부족).
    #   → 되돌리지 말 것.
    # 스포츠에서 이관(2026-07-29) — 일반명사와 의미가 겹쳐 오탐 우려가 큰 것들.
    #   transfer → 기술이전·정권이양·송금
    #   goal     → 감축목표·생산목표
    #   player   → 시장의 주요 플레이어
    #   champion → (정책을) 옹호하다
    #   league   → 아랍연맹(Arab League)
    #   squad    → 암살조(death squad) 등 분쟁 보도
    #   match / coach → mismatch, 상용차 coach 등
    # 관찰 후 스포츠 전용 표현만 NOISE로 되돌릴 것.
    "match", "league", "coach", "player", "transfer", "goal", "squad", "champion",
]


_SOFT_NOISE_RE = _compile_keywords(SOFT_NOISE_KEYWORDS + OBSERVE_KEYWORDS)


def is_soft_noise(title: str) -> bool:
    """수집은 하되 텔레그램/홈페이지 발송 안 할 기사"""
    return bool(_SOFT_NOISE_RE.search(title or ""))

# =========================
# TELEGRAM
# =========================

def send_telegram(title_ko, summary_ko, link, source_name, category, subcategory, region, country_name):
    cat_tag     = f"#{category} #{subcategory}" if subcategory and subcategory != category else f"#{category}"
    region_tag  = f" #{region}" if region else ""
    country_tag = f" #{country_name}" if country_name else ""

    import html
    title_safe   = html.escape(title_ko or "")
    summary_safe = html.escape(summary_ko or "")
    summary_str  = f"\n\n<i>{summary_safe}</i>" if summary_safe else ""

    msg = (
        f"{cat_tag}{region_tag}{country_tag}\n\n"
        f"<b>{title_safe}</b>"
        f"{summary_str}\n\n"
        f"📎 {source_name}\n"
        f"🔗 {link}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, data={
        "chat_id":    CHAT_ID,
        "text":       msg,
        "parse_mode": "HTML"
    })
    return res.json()

# =========================
# SAVE
# =========================

def save_state():
    os.makedirs("data", exist_ok=True)
    # sent는 저장하지 않음 (DB로 이전)
    save_data = {k: v for k, v in state.items() if k != "sent"}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)

# =========================
# MAIN
# =========================

# =========================
# RSS 수집 함수 (병렬 처리용)
# =========================

def entry_age_days(entry):
    """RSS 항목의 발행 경과일수 반환. 날짜 필드가 없으면 None."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        tt = entry.get(key)
        if not tt:
            continue
        try:
            return (time.time() - calendar.timegm(tt)) / 86400.0
        except Exception:
            continue
    return None


def entry_published_iso(entry):
    """RSS 항목의 원문 발행일을 ISO8601(UTC) 문자열로 반환. 없으면 None.

    entry_age_days()와 같은 필드를 같은 우선순위로 본다.
    미래 날짜나 20년 이상 과거는 피드 오류로 보고 버린다.
    """
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        tt = entry.get(key)
        if not tt:
            continue
        try:
            ts = calendar.timegm(tt)
        except Exception:
            continue
        now_ts = time.time()
        if ts > now_ts + 86400 or ts < now_ts - 86400 * 365 * 20:
            continue
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        except Exception:
            continue
    return None


def fetch_source(s):
    """단일 소스에서 최근 기사 다건 수집 (발행일 필터 적용)"""
    import socket
    name = s["name"]
    try:
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10)
        feed = feedparser.parse(s["url"], request_headers={"User-Agent": "Mozilla/5.0"})
        socket.setdefaulttimeout(old_timeout)
        if not feed.entries:
            return [], name, "no_entries"

        items = []
        too_old = 0
        no_date = 0
        # 상한의 4배까지만 훑음 (오래된 항목이 앞쪽에 몰린 피드 대비)
        for entry in feed.entries[: MAX_ENTRIES_PER_SOURCE * 4]:
            if len(items) >= MAX_ENTRIES_PER_SOURCE:
                break
            age = entry_age_days(entry)
            if age is not None and age > MAX_AGE_DAYS:
                too_old += 1
                continue
            if age is None:
                no_date += 1
            title = clean_text(entry.get("title", ""))
            link  = normalize_url(entry.get("link", ""))
            if not title or not link:
                continue
            items.append({"title": title, "link": link, "entry": entry, "source": s})

        if not items:
            # 전부 기간 초과인 경우는 소스 장애가 아니므로 별도 상태로 구분
            return [], name, ("too_old" if too_old else "no_title_link")
        if no_date:
            print(f"[날짜없음] {name} — {no_date}건 (필터 미적용 통과)")
        return items, name, "ok"
    except Exception as e:
        return [], name, f"error: {e}"

# =========================
# MAIN
# =========================

sources    = load_rss()
seen_titles = []

# 병렬로 RSS 수집
print(f"[수집] {len(sources)}개 소스 병렬 수집 시작...")
results = []
with ThreadPoolExecutor(max_workers=20) as executor:
    futures = {executor.submit(fetch_source, s): s for s in sources}
    for future in as_completed(futures):
        data, name, status = future.result()
        if name not in rss_health:
            rss_health[name] = {"ok": 0, "fail": 0, "status": "active"}
        if status == "ok" and data:
            results.extend(data)
        elif status == "too_old":
            # 발행일 초과로 수집 대상이 없는 정상 상태 — 소스 장애로 집계하지 않음
            rss_health[name]["too_old"] = rss_health[name].get("too_old", 0) + 1
            print(f"[SKIP] 발행일 초과 — {name}")
        else:
            rss_health[name]["fail"] += 1

print(f"[수집] {len(results)}개 기사 수집 완료")

# 수집된 기사 처리 및 발송
for data in results:
    s           = data["source"]
    name        = s["name"]
    category    = s["category"]
    subcategory = s["subcategory"]
    region      = detect_region(name)
    title       = data["title"]
    link        = data["link"]
    latest      = data["entry"]
    src_published = entry_published_iso(latest)

    # arXiv는 페이지 크롤링 대신 공식 API로 초록·제출일을 받는다(위 주석 참조)
    arxiv_abstract = ""
    _axid = arxiv_id_from_url(link)
    if _axid:
        _abs, _pub = fetch_arxiv_meta(_axid)
        if _pub:
            src_published = _pub
            _age = _iso_age_days(_pub)
            if _age is not None and _age > ARXIV_MAX_AGE_DAYS:
                print(f"[SKIP] arXiv 제출 {_age:.0f}일 경과 — {title[:50]}")
                continue
        if not _abs:
            # 초록이 없으면 본문이 페이지 UI 안내문으로 채워진다 → 수집하지 않는다
            print(f"[SKIP] arXiv 초록 확보 실패 — {title[:50]}")
            continue
        arxiv_abstract = _abs

    fp = fingerprint(title, name)

    if is_url_exists(link):
        continue

    if is_duplicate(title, seen_titles):
        print(f"[SKIP] 유사 기사 중복 — {title[:50]}")
        continue
    seen_titles.append(title)

    if is_noise(title):
        print(f"[SKIP] 노이즈 — {title[:50]}")
        continue

    # 소프트 노이즈 — DB에만 저장, 텔레그램/홈페이지 발송 안 함
    soft_noise = is_soft_noise(title)


    # 요약 추출
    summary_en = extract_summary(latest)

    # 언어 감지 (간단한 휴리스틱)
    def detect_lang(text):
        if not text:
            return "en"
        t = text[:200]
        if any('\u0600' <= c <= '\u06ff' for c in t):
            return "ar"
        if any(c in t for c in "éèêëàâùûüôîïœç"):
            return "fr"
        if any(c in t for c in "ãõçáéíóúâêîôû") and any(w in t.lower() for w in ["da","de","do","dos","das","em","para","por"]):
            return "pt"
        if any(w in t.lower().split() for w in ["yang","dan","untuk","dengan","dalam","tidak","ini","itu","dari","pada"]):
            return "id"
        return "en"

    src_lang = detect_lang(title + " " + (summary_en or ""))

    # 원문 크롤링 (타임아웃 8초, 실패해도 계속) — arXiv는 API 초록을 그대로 쓴다
    full_text = arxiv_abstract or crawl_full_text(link, timeout=8)
    if full_text:
        print(f"  [크롤링] {len(full_text)}자 추출")

    # 국가 감지 — 기사 내용 기반으로만 판단 (소스 국가는 폴백으로만 사용 안 함)
    content_flag, content_country = detect_country(title + " " + summary_en, source=name)
    all_countries = detect_countries(title + " " + summary_en, source=name)
    country_names = [n for _, n in all_countries]

    if country_names:
        # 프론티어 국가 우선
        frontier_countries = [n for n in country_names if n not in GLOBAL_COUNTRIES]
        if frontier_countries:
            country_name = frontier_countries[0]
            country_flag = next((f for f, n in all_countries if n == country_name), "")
        else:
            # 주요국만 있으면 글로벌
            country_name = country_names[0]
            country_flag = next((f for f, n in all_countries if n == country_name), "")
            category = "글로벌"
            region = "global"
    else:
        # 내용에서 어떤 국가도 감지 안 됨 → 글로벌
        country_name, country_flag = "", ""
        category = "글로벌"
        region = "global"
        country_names = []

    # 한국어 번역
    try:
        title_ko = GoogleTranslator(source="auto", target="ko").translate(title[:500])
        title_ko = clean_text(title_ko)
        if _is_bad_translation(title_ko):
            print(f"[번역실패] 원문 유지 — {title[:50]}")
            title_ko = title
    except Exception:
        title_ko = title

    # 요약 번역
    # 2026-08-30 사용자 지적: 300자 제한에 근거 없었음. deep_translator의
    # GoogleTranslator는 대략 5000자까지 지원하므로 그 안에서 넉넉하게 씀
    # (한도 초과 시에도 아래 except가 잡아 기존과 동일하게 빈 문자열로
    # 폴백하므로 더 나빠질 게 없음).
    summary_ko = ""
    if summary_en:
        try:
            summary_ko = GoogleTranslator(source="auto", target="ko").translate(summary_en[:4500])
            summary_ko = clean_text(summary_ko)
            if _is_bad_translation(summary_ko):
                summary_ko = ""
        except Exception:
            summary_ko = ""


    # 비영어 소스면 full_text 앞에 언어 태그 추가
    if src_lang != "en" and full_text:
        lang_labels = {"fr": "[원문: 프랑스어]", "ar": "[원문: 아랍어]",
                       "pt": "[원문: 포르투갈어]", "id": "[원문: 인도네시아어]"}
        lang_tag = lang_labels.get(src_lang, "[원문: " + src_lang + "]")
        full_text = lang_tag + "\n" + full_text

    # 텔레그램 발송 (소프트 노이즈는 스킵)
    if soft_noise:
        article_id = insert_article(
            title_en=title, title_ko=title_ko,
            summary_en=summary_en, summary_ko=summary_ko,
            url=link, source=name, category=category,
            subcategory=subcategory, region=region,
            country=country_name, country_flag=country_flag,
            score=0, full_text=full_text,
            countries=country_names,
            is_published=False,
            source_published_at=src_published,
        )
        print(f"[SOFT] [{category}] [{country_name}] {title_ko[:50]}")
        continue

    res = send_telegram(title_ko, summary_ko, link, name, category, subcategory, region, country_name)

    if res.get("ok"):
        state["daily_count"] += 1
        rss_health[name]["ok"] += 1

        # DB 저장 — RSS 기사는 is_published=False (홈페이지 미노출)
        article_id = insert_article(
            title_en=title, title_ko=title_ko,
            summary_en=summary_en, summary_ko=summary_ko,
            url=link, source=name, category=category,
            subcategory=subcategory, region=region,
            country=country_name, country_flag=country_flag,
            score=0, full_text=full_text,
            countries=country_names,
            is_published=False,
            source_published_at=src_published,
        )
        if article_id > 0:
            mark_sent_telegram(article_id)

        print(f"[SENT] [{category}>{subcategory}] [{country_name}] {title_ko}")
    else:
        print(f"[FAIL] {res}")
        rss_health[name]["fail"] += 1

save_state()
print(f"\n✅ 완료")
