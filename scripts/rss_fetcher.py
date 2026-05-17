import feedparser
import requests
import os
import json
import hashlib
import time
import re
import sys
from urllib.parse import urlparse, urlunparse
from deep_translator import GoogleTranslator
from dotenv import load_dotenv
from rapidfuzz import fuzz

# UTF-8 출력 설정 (Windows 인코딩 오류 방지)
sys.stdout.reconfigure(encoding='utf-8')

# .env 파일에서 환경변수 로드
load_dotenv()

# =========================
# CONFIG
# =========================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = "@TheWiseFrontier"

RSS_FILE = "sources/rss_sources.txt"
STATE_FILE = "data/state.json"

MIN_SCORE = 3

# =========================
# EMOJI MAP
# =========================

CATEGORY_EMOJI = {
    "macro":      "📊",
    "finance":    "💰",
    "resource":   "⛏️",
    "politics":   "🏛️",
    "tech":       "💡",
    "society":    "🏘️",
    "investment": "📈"
}

REGION_EMOJI = {
    "africa":         "🌍",
    "southeast_asia": "🌏",
    "eastern_europe": "🌐",
    "central_asia":   "🏔️",
    "middle_east":    "🌙",
    "south_asia":     "🌏",
    "caribbean":      "🌴",
    "latin_america":  "🌎",
    "global":         "🗺️"
}

# 소스 이름으로 리전 자동 분류
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
}

def detect_country(text: str):
    """제목/요약에서 국가 정보 반환 (국기, 국가명)"""
    text_lower = text.lower()
    for keyword, (flag, name) in COUNTRY_INFO.items():
        if keyword in text_lower:
            return flag, name
    return "", ""

# =========================
# STATE
# =========================

def load_state():
    default = {
        "sent": {},
        "rss_health": {},
        "daily_count": 0,
        "last_reset": time.strftime("%Y-%m-%d")
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
            default.update(saved)
    return default

state = load_state()
sent_db = state["sent"]
rss_health = state["rss_health"]

today = time.strftime("%Y-%m-%d")
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
            if len(parts) != 3:
                continue
            sources.append({
                "name":     parts[0],
                "category": parts[1],
                "url":      parts[2]
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
    replacements = {
        '\u2019': "'", '\u2018': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
        '\xa0': ' '
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)
    return text.strip()

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

# =========================
# SCORE
# =========================

def score_news(title, category):
    t = title.lower()
    score = 0

    weights = {
        "macro":      5,
        "finance":    4,
        "resource":   4,
        "politics":   3,
        "tech":       2,
        "society":    2,
        "investment": 4
    }
    score += weights.get(category, 1)

    important = {
        "imf":          4,
        "central bank": 4,
        "inflation":    3,
        "oil":          3,
        "mining":       3,
        "investment":   2,
        "gdp":          3,
        "ipo":          3,
        "merger":       3,
        "acquisition":  3
    }
    for k, v in important.items():
        if k in t:
            score += v

    noise = [
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
        "festival", "horoscope", "obituary", "recipe", "weather forecast"
    ]
    for n in noise:
        if n in t:
            score -= 10

    return score

# =========================
# TELEGRAM
# =========================

def send_telegram(title_ko, summary_ko, link, source_name, category, region, country_flag, country_name):
    cat_emoji    = CATEGORY_EMOJI.get(category, "📌")
    region_emoji = REGION_EMOJI.get(region, "🗺️")
    cat_tag      = f"#{category.upper()}"
    country_str  = f" {country_flag} {country_name}" if country_flag else ""
    summary_str  = f"\n\n_{summary_ko}_" if summary_ko else ""

    msg = (
        f"{region_emoji}{country_str} {cat_emoji} {cat_tag}\n\n"
        f"*{title_ko}*"
        f"{summary_str}\n\n"
        f"📎 {source_name}\n"
        f"🔗 {link}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    res = requests.post(url, data={
        "chat_id":    CHAT_ID,
        "text":       msg,
        "parse_mode": "Markdown"
    })
    return res.json()

# =========================
# SAVE
# =========================

def save_state():
    os.makedirs("data", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# =========================
# MAIN
# =========================

sources = load_rss()
seen_titles = []

for s in sources:
    name     = s["name"]
    category = s["category"]
    region   = detect_region(name)

    if name not in rss_health:
        rss_health[name] = {"ok": 0, "fail": 0, "status": "active"}

    try:
        feed = feedparser.parse(s["url"])
    except:
        rss_health[name]["fail"] += 1
        continue

    if not feed.entries:
        rss_health[name]["fail"] += 1
        continue

    latest = feed.entries[0]
    title  = clean_text(latest.get("title", ""))
    link   = normalize_url(latest.get("link", ""))

    if not title or not link:
        continue

    fp = fingerprint(title, name)
    if fp in sent_db:
        continue

    if is_duplicate(title, seen_titles):
        print(f"[SKIP] 유사 기사 중복 — {title[:50]}")
        continue
    seen_titles.append(title)

    score = score_news(title, category)
    if score < MIN_SCORE:
        print(f"[SKIP] 낮은 점수({score}) — {title[:50]}")
        continue

    # 요약 추출
    summary_en = extract_summary(latest)

    # 국가 감지 (제목 + 요약 + 소스 이름)
    country_flag, country_name = detect_country(title + " " + summary_en + " " + name)

    # 한국어 번역
    try:
        title_ko = GoogleTranslator(source="auto", target="ko").translate(title)
        title_ko = clean_text(title_ko)
    except:
        title_ko = title

    # 요약 번역
    summary_ko = ""
    if summary_en:
        try:
            summary_ko = GoogleTranslator(source="auto", target="ko").translate(summary_en)
            summary_ko = clean_text(summary_ko)
        except:
            summary_ko = summary_en

    # 텔레그램 발송
    res = send_telegram(title_ko, summary_ko, link, name, category, region, country_flag, country_name)

    if res.get("ok"):
        state["daily_count"] += 1
        sent_db[fp] = True
        rss_health[name]["ok"] += 1
        print(f"[SENT] [{region}|{category}] {country_flag} {title_ko}")
    else:
        print(f"[FAIL] {res}")
        rss_health[name]["fail"] += 1

save_state()
print(f"\n✅ 완료")
