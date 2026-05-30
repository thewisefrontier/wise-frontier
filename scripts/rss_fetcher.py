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
from db import init_db, is_url_exists, insert_article, mark_sent_telegram

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
}

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
    replacements = {
        '\u2019': "'", '\u2018': "'",
        '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-',
        '\xa0': ' '
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)
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

# =========================
# 노이즈 필터
# =========================

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

def is_noise(title: str) -> bool:
    t = title.lower()
    return any(n in t for n in NOISE_KEYWORDS)

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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

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
    if fp in sent_db:
        continue

    if is_url_exists(link):
        sent_db[fp] = True
        continue

    if is_duplicate(title, seen_titles):
        print(f"[SKIP] 유사 기사 중복 — {title[:50]}")
        continue
    seen_titles.append(title)

    if is_noise(title):
        print(f"[SKIP] 노이즈 — {title[:50]}")
        continue

    # 요약 추출
    summary_en = extract_summary(latest)

    # 국가 감지
    country_flag, country_name = detect_country(title + " " + summary_en + " " + name, source=name)

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

    # 텔레그램 발송
    res = send_telegram(title_ko, summary_ko, link, name, category, subcategory, region, country_name)

    if res.get("ok"):
        state["daily_count"] += 1
        sent_db[fp] = True
        rss_health[name]["ok"] += 1

        # DB 저장
        article_id = insert_article(
            title_en=title,
            title_ko=title_ko,
            summary_en=summary_en,
            summary_ko=summary_ko,
            url=link,
            source=name,
            category=category,
            subcategory=subcategory,
            region=region,
            country=country_name,
            country_flag=country_flag,
            score=0
        )
        if article_id > 0:
            mark_sent_telegram(article_id)

        print(f"[SENT] [{category}>{subcategory}] [{country_name}] {title_ko}")
    else:
        print(f"[FAIL] {res}")
        rss_health[name]["fail"] += 1

save_state()
print(f"\n✅ 완료")
