import feedparser
import requests
import os
import json
import hashlib
import time
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

RSS_FILE = "sources/rss_sources.txt"
STATE_FILE = "data/state.json"

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
    "middle_east": ["iraq", "iran", "yemen", "syria", "jordan", "lebanon", "saudi", "qatar", "kuwait", "oman", "bahrain", "uae", "emirates", "al monitor", "middle east eye"],
    "south_asia": ["pakistan", "bangladesh", "nepal", "sri lanka", "dawn", "himalayan", "daily star"],
    "caribbean": ["haiti", "jamaica", "trinidad", "dominican", "caribbean", "haitian times", "loop caribbean"],
    "latin_america": ["venezuela", "bolivia", "ecuador", "paraguay", "nicaragua", "salvador", "guatemala", "honduras", "news americas"]
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
    "ivory coast": ("🇨🇮", "코트디부아르"), "cote d'ivoire": ("🇨🇮", "코트디부아르"),
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
    # 카리브해
    "jamaica": ("🇯🇲", "자메이카"), "jamaican": ("🇯🇲", "자메이카"),
    "trinidad": ("🇹🇹", "트리니다드"), "tobago": ("🇹🇹", "트리니다드"),
    "barbados": ("🇧🇧", "바베이도스"), "bahamas": ("🇧🇸", "바하마"),
    "haiti": ("🇭🇹", "아이티"), "haitian": ("🇭🇹", "아이티"),
    "cuba": ("🇨🇺", "쿠바"), "cuban": ("🇨🇺", "쿠바"),
    "dominican": ("🇩🇴", "도미니카공화국"),
    "guyana": ("🇬🇾", "가이아나"), "suriname": ("🇸🇷", "수리남"),
    # 오세아니아
    "australia": ("🇦🇺", "호주"), "australian": ("🇦🇺", "호주"),
    "new zealand": ("🇳🇿", "뉴질랜드"),
    "timor": ("🇹🇱", "동티모르"),
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
    "israel": ("🇮🇱", "이스라엘"), "israeli": ("🇮🇱", "이스라엘"),
    "italy": ("🇮🇹", "이탈리아"), "italian": ("🇮🇹", "이탈리아"),
    "spain": ("🇪🇸", "스페인"), "spanish": ("🇪🇸", "스페인"),
    "netherlands": ("🇳🇱", "네덜란드"), "dutch": ("🇳🇱", "네덜란드"),
    "canada": ("🇨🇦", "캐나다"), "canadian": ("🇨🇦", "캐나다"),
    "portugal": ("🇵🇹", "포르투갈"), "portuguese": ("🇵🇹", "포르투갈"),
    # ── 프랑스어/현지 지명 ──
    "côte d'ivoire": ("🇨🇮", "코트디부아르"), "cote d'ivoire": ("🇨🇮", "코트디부아르"),
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
    sources = []
    with open(RSS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) == 4:
                sources.append({
                    "name":        parts[0],
                    "category":    parts[1],
                    "subcategory": parts[2],
                    "url":         parts[3]
                })
            elif len(parts) == 3:
                sources.append({
                    "name":        parts[0],
                    "category":    parts[1],
                    "subcategory": parts[1],
                    "url":         parts[2]
                })
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
    if len(summary) > 150:
        summary = summary[:150].rsplit(' ', 1)[0] + "..."
    return clean_text(summary)


# 원문 크롤링 불필요 사이트 (이미 RSS에 전문 제공)
SKIP_CRAWL_DOMAINS = {
    "allafrica.com", "africa-newsroom.com", "afdb.org",
    "imf.org", "worldbank.org", "afro.who.int", "au.int",
    "unctad.org", "ifc.org", "asean.org", "adb.org",
}

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
            if len(t) > 50:  # 너무 짧은 문장 제외
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
    # 자메이카
    "jamaica observer": ("🇯🇲", "자메이카"), "jamaica gleaner": ("🇯🇲", "자메이카"),
    "loop jamaica": ("🇯🇲", "자메이카"), "rjr news": ("🇯🇲", "자메이카"),
    # 트리니다드
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
}

def get_source_country(source_name: str):
    """소스명으로 기본 국가 반환"""
    s = source_name.lower()
    for key, val in SOURCE_COUNTRY_MAP.items():
        if key in s:
            return val
    return None, None

NOISE_KEYWORDS = [
    # 스포츠
    "football", "soccer", "cricket", "basketball", "rugby", "tennis",
    "golf", "athletics", "olympics", "match", "league", "tournament",
    "coach", "player", "transfer", "goal", "squad", "fixture", "champion",
    "premier league", "champions league", "world cup", "cup final",
    # 연예/문화
    "celebrity", "music", "wedding", "entertainment", "fashion", "movie",
    "film", "actor", "actress", "singer", "concert", "album",
    # 기타 노이즈
    "e-edition", "edition", "travel", "tourism", "leisure", "sumo",
    "festival", "horoscope", "obituary", "recipe", "weather forecast",
    "eurovision", "beauty pageant", "miss world", "miss universe",
    "lottery", "powerball", "lotto", "flag day", "national anthem",
    "palace", "museum", "zapping", "podcast", "5 ways", "how to celebrate",
    "royal", "heritage site", "archaeological", "co-owner", "ownership stake",
    "church", "pastor", "bishop", "prayer", "sermon", "national sound"
]

# 소프트 노이즈 — 수집은 하되 텔레그램/홈페이지 발송 안 함
SOFT_NOISE_KEYWORDS = [
    "anthropic", "openai", "nvidia", "apple inc", "microsoft corp",
    "meta platforms", "amazon.com", "tesla inc", "spacex", "chatgpt",
    "nasdaq", "s&p 500", "dow jones", "wall street", "new york stock",
    "federal reserve", "us federal", "fed rate",
]

def is_noise(title: str) -> bool:
    t = title.lower()
    return any(n in t for n in NOISE_KEYWORDS)

def is_soft_noise(title: str) -> bool:
    """수집은 하되 텔레그램/홈페이지 발송 안 할 기사"""
    t = title.lower()
    return any(n in t for n in SOFT_NOISE_KEYWORDS)

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

def fetch_source(s):
    """단일 소스에서 최신 기사 수집"""
    import socket
    name = s["name"]
    try:
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(10)
        feed = feedparser.parse(s["url"], request_headers={"User-Agent": "Mozilla/5.0"})
        socket.setdefaulttimeout(old_timeout)
        if not feed.entries:
            return None, name, "no_entries"
        latest = feed.entries[0]
        title = clean_text(latest.get("title", ""))
        link  = normalize_url(latest.get("link", ""))
        if not title or not link:
            return None, name, "no_title_link"
        return {"title": title, "link": link, "entry": latest, "source": s}, name, "ok"
    except Exception as e:
        return None, name, f"error: {e}"

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
            results.append(data)
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

    # 요약 추출
    summary_en = extract_summary(latest)

    # 원문 크롤링 (타임아웃 8초, 실패해도 계속)
    full_text = crawl_full_text(link, timeout=8)
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
    except Exception:
        title_ko = title

    # 요약 번역
    summary_ko = ""
    if summary_en:
        try:
            summary_ko = GoogleTranslator(source="auto", target="ko").translate(summary_en[:300])
            summary_ko = clean_text(summary_ko)
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
        )
        if article_id > 0:
            mark_sent_telegram(article_id)

        print(f"[SENT] [{category}>{subcategory}] [{country_name}] {title_ko}")
    else:
        print(f"[FAIL] {res}")
        rss_health[name]["fail"] += 1

save_state()
print(f"\n✅ 완료")
