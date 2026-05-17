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
    "global":         "🗺️"
}

# 소스 이름으로 리전 자동 분류
REGION_KEYWORDS = {
    "africa": ["africa", "nigeria", "kenya", "ghana", "ethiopia", "egypt", "south africa", "allafrica", "maverick", "naira", "punch", "businessday", "businesstech", "guardian nigeria", "vanguard", "ghanaweb", "mining weekly", "engineering news"],
    "southeast_asia": ["asia", "nikkei", "krasia", "dealstreet", "techinasia", "vietnam", "indonesia", "thailand", "myanmar", "khmer", "malaysia", "philippine", "bangkok", "jakarta"],
    "eastern_europe": ["emerging europe", "intellinews", "poland", "ukraine", "romania", "czechia", "kyiv", "warsaw", "caucasus", "azerbaijan"]
}

def detect_region(source_name: str) -> str:
    name_lower = source_name.lower()
    for region, keywords in REGION_KEYWORDS.items():
        for kw in keywords:
            if kw in name_lower:
                return region
    return "global"

# 국가 감지
COUNTRY_FLAGS = {
    "nigeria": "🇳🇬", "nigerian": "🇳🇬",
    "south africa": "🇿🇦", "south african": "🇿🇦",
    "kenya": "🇰🇪", "kenyan": "🇰🇪",
    "ghana": "🇬🇭", "ghanaian": "🇬🇭",
    "ethiopia": "🇪🇹", "ethiopian": "🇪🇹",
    "egypt": "🇪🇬", "egyptian": "🇪🇬",
    "tanzania": "🇹🇿", "tanzanian": "🇹🇿",
    "uganda": "🇺🇬", "ugandan": "🇺🇬",
    "rwanda": "🇷🇼", "rwandan": "🇷🇼",
    "senegal": "🇸🇳", "senegalese": "🇸🇳",
    "ivory coast": "🇨🇮", "cote d'ivoire": "🇨🇮",
    "morocco": "🇲🇦", "moroccan": "🇲🇦",
    "angola": "🇦🇴", "angolan": "🇦🇴",
    "mozambique": "🇲🇿",
    "zambia": "🇿🇲", "zambian": "🇿🇲",
    "zimbabwe": "🇿🇼", "zimbabwean": "🇿🇼",
    "congo": "🇨🇩", "drc": "🇨🇩",
    "cameroon": "🇨🇲",
    "sudan": "🇸🇩", "sudanese": "🇸🇩",
    "libya": "🇱🇾", "libyan": "🇱🇾",
    "tunisia": "🇹🇳", "tunisian": "🇹🇳",
    "mali": "🇲🇱",
    "somalia": "🇸🇴", "somali": "🇸🇴",
    "malawi": "🇲🇼",
    "vietnam": "🇻🇳", "vietnamese": "🇻🇳",
    "indonesia": "🇮🇩", "indonesian": "🇮🇩",
    "thailand": "🇹🇭", "thai": "🇹🇭",
    "philippines": "🇵🇭", "philippine": "🇵🇭",
    "malaysia": "🇲🇾", "malaysian": "🇲🇾",
    "myanmar": "🇲🇲", "burmese": "🇲🇲",
    "cambodia": "🇰🇭", "cambodian": "🇰🇭",
    "singapore": "🇸🇬", "singaporean": "🇸🇬",
    "laos": "🇱🇦",
    "ukraine": "🇺🇦", "ukrainian": "🇺🇦",
    "poland": "🇵🇱", "polish": "🇵🇱",
    "romania": "🇷🇴", "romanian": "🇷🇴",
    "czechia": "🇨🇿", "czech": "🇨🇿",
    "hungary": "🇭🇺", "hungarian": "🇭🇺",
    "georgia": "🇬🇪", "georgian": "🇬🇪",
    "azerbaijan": "🇦🇿",
    "kazakhstan": "🇰🇿",
    "uzbekistan": "🇺🇿",
}

def detect_country(text: str) -> str:
    text_lower = text.lower()
    for country, flag in COUNTRY_FLAGS.items():
        if country in text_lower:
            return flag
    return ""

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

    noise = ["football", "celebrity", "music", "wedding", "entertainment", "e-edition", "edition"]
    for n in noise:
        if n in t:
            score -= 10

    return score

# =========================
# TELEGRAM
# =========================

def send_telegram(title_ko, summary_ko, link, source_name, category, region, country_flag):
    cat_emoji    = CATEGORY_EMOJI.get(category, "📌")
    region_emoji = REGION_EMOJI.get(region, "🗺️")
    cat_tag      = f"#{category.upper()}"
    country_str  = f" {country_flag}" if country_flag else ""
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

    # 국가 감지
    country_flag = detect_country(title + " " + summary_en)

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
    res = send_telegram(title_ko, summary_ko, link, name, category, region, country_flag)

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
