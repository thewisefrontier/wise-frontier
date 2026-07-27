"""
external_trends.py
------------------
외부 소스에서 프론티어 마켓 트렌드 신호를 수집합니다.
- GDELT: 전세계 뉴스 이벤트 빈도
- Google Trends RSS: 국가별 급상승 검색어
- Reddit RSS: r/africa, r/geopolitics 등

gemini_summarizer.py에서 호출되어 트렌드 신호를 반환합니다.
"""

import re
import time
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from collections import Counter

KST = timezone(timedelta(hours=9))

def now_kst():
    return datetime.now(timezone.utc).astimezone(KST)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0; +https://newsfinal.co.kr)"
}

# ── Google Trends RSS ─────────────────────────────────────────

# 프론티어 국가 코드 → (한국어 국가명, 지역)
GTRENDS_COUNTRIES = {
    "NG": ("나이지리아", "africa"),
    "KE": ("케냐",      "africa"),
    "GH": ("가나",      "africa"),
    "ZA": ("남아공",    "africa"),
    "ET": ("에티오피아","africa"),
    "EG": ("이집트",    "africa"),
    "TZ": ("탄자니아",  "africa"),
    "UG": ("우간다",    "africa"),
    "RW": ("르완다",    "africa"),
    "VN": ("베트남",    "southeast_asia"),
    "ID": ("인도네시아","southeast_asia"),
    "PH": ("필리핀",    "southeast_asia"),
    "TH": ("태국",      "southeast_asia"),
    "MM": ("미얀마",    "southeast_asia"),
    "KH": ("캄보디아",  "southeast_asia"),
    "BD": ("방글라데시","south_asia"),
    "PK": ("파키스탄",  "south_asia"),
    "KZ": ("카자흐스탄","central_asia"),
    "UZ": ("우즈베키스탄","central_asia"),
    "HT": ("아이티",    "caribbean"),
}

# 노이즈 필터 — 연예/스포츠/로또 등
GTRENDS_NOISE = {
    "football","soccer","cricket","nba","premier league","champions league",
    "movie","film","actor","singer","album","concert","drama","series",
    "lottery","lotto","powerball","weather","horoscope","recipe",
    "celebrity","wedding","fashion","beauty","makeup","hairstyle",
    "축구","야구","농구","영화","드라마","연예","로또","날씨","요리",
}

def fetch_google_trends(timeout: int = 8) -> list:
    """
    Google Trends 급상승 검색어 수집.
    반환: [{"keyword": str, "country": str, "country_ko": str, "region": str, "source": "google_trends"}]
    """
    results = []
    for geo, (country_ko, region) in GTRENDS_COUNTRIES.items():
        try:
            url = f"https://trends.google.com/trends/trendingsearches/daily/rss?geo={geo}"
            feed = feedparser.parse(url, request_headers=HEADERS)
            if not feed.entries:
                continue
            for entry in feed.entries[:10]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                # 노이즈 필터
                t_lower = title.lower()
                if any(n in t_lower for n in GTRENDS_NOISE):
                    continue
                # 너무 짧은 키워드 제외
                if len(title) < 3:
                    continue
                results.append({
                    "keyword": title,
                    "country": country_ko,
                    "region": region,
                    "geo": geo,
                    "source": "google_trends",
                })
            time.sleep(0.3)
        except Exception as e:
            print(f"  [Gtrends] {geo} 수집 실패: {e}")
            continue
    return results


# ── Reddit RSS ───────────────────────────────────────────────

REDDIT_FEEDS = [
    ("r/africa",        "https://www.reddit.com/r/africa/new/.rss",          "africa"),
    ("r/geopolitics",   "https://www.reddit.com/r/geopolitics/new/.rss",     "global"),
    ("r/worldnews",     "https://www.reddit.com/r/worldnews/search.rss?q=africa+OR+nigeria+OR+kenya+OR+ethiopia+OR+ghana+OR+drc+OR+myanmar+OR+haiti&sort=new&restrict_sr=1", "global"),
    ("r/southeast_asia","https://www.reddit.com/r/southeastasia/new/.rss",   "southeast_asia"),
    ("r/Economics",     "https://www.reddit.com/r/economics/search.rss?q=africa+OR+frontier+market+OR+emerging+market&sort=new", "global"),
    ("r/collapse",      "https://www.reddit.com/r/collapse/new/.rss",        "global"),
]

REDDIT_NOISE = {
    "ama","meme","photo","picture","question","weekly","discussion",
    "daily","thread","megathread","mod","update","announcement",
}

def fetch_reddit(timeout: int = 10) -> list:
    """
    Reddit RSS 수집.
    반환: [{"title": str, "url": str, "subreddit": str, "region": str, "source": "reddit"}]
    """
    results = []
    for subreddit, feed_url, region in REDDIT_FEEDS:
        try:
            feed = feedparser.parse(
                feed_url,
                request_headers={**HEADERS, "Accept": "application/rss+xml"}
            )
            if not feed.entries:
                continue
            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                if not title or len(title) < 10:
                    continue
                t_lower = title.lower()
                if any(n in t_lower for n in REDDIT_NOISE):
                    continue
                # 업보트 수 추출 (가능하면)
                score = 0
                if hasattr(entry, "score"):
                    try:
                        score = int(entry.score)
                    except Exception:
                        pass
                results.append({
                    "title": title,
                    "url": entry.get("link", ""),
                    "subreddit": subreddit,
                    "region": region,
                    "score": score,
                    "source": "reddit",
                })
            time.sleep(0.5)
        except Exception as e:
            print(f"  [Reddit] {subreddit} 수집 실패: {e}")
            continue
    return results


# ── GDELT ────────────────────────────────────────────────────

# 프론티어 국가별 GDELT 검색어
GDELT_QUERIES = [
    ("나이지리아",    "africa",        "nigeria"),
    ("케냐",         "africa",        "kenya"),
    ("가나",         "africa",        "ghana"),
    ("에티오피아",   "africa",        "ethiopia"),
    ("남아공",       "africa",        "south africa"),
    ("탄자니아",     "africa",        "tanzania"),
    ("DRC",          "africa",        "congo DRC"),
    ("수단",         "africa",        "sudan"),
    ("소말리아",     "africa",        "somalia"),
    ("사헬",         "africa",        "sahel mali burkina niger"),
    ("베트남",       "southeast_asia","vietnam"),
    ("인도네시아",   "southeast_asia","indonesia"),
    ("미얀마",       "southeast_asia","myanmar"),
    ("방글라데시",   "south_asia",    "bangladesh"),
    ("아이티",       "caribbean",     "haiti"),
    ("카자흐스탄",   "central_asia",  "kazakhstan"),
]

def fetch_gdelt(timeout: int = 12) -> list:
    """
    GDELT Article Search API로 국가별 최신 뉴스 이벤트 수집.
    반환: [{"title": str, "url": str, "country": str, "region": str, "source": "gdelt"}]
    """
    results = []
    for country_ko, region, query in GDELT_QUERIES:
        try:
            url = (
                f"https://api.gdeltproject.org/api/v2/doc/doc"
                f"?query={requests.utils.quote(query)}"
                f"&mode=artlist&maxrecords=10&timespan=1d"
                f"&sort=datedesc&format=json"
            )
            res = requests.get(url, headers=HEADERS, timeout=timeout)
            if res.status_code != 200:
                continue
            data = res.json()
            articles = data.get("articles", [])
            for a in articles:
                title = a.get("title", "").strip()
                if not title or len(title) < 10:
                    continue
                results.append({
                    "title": title,
                    "url": a.get("url", ""),
                    "country": country_ko,
                    "region": region,
                    "source": "gdelt",
                    "seendate": a.get("seendate", ""),
                    "language": a.get("language", ""),
                })
            time.sleep(0.5)
        except Exception as e:
            print(f"  [GDELT] {country_ko} 수집 실패: {e}")
            continue
    return results


# ── GDELT (국가 무관, 전 세계 초대형 이슈) ──────────────────────
# 프론티어 마켓이 핵심이지만, 국내 언론이 이미 다루는 톱뉴스라는 이유만으로
# 진짜 초대형 이슈(대형 재난·사망자 다수 발생 등)를 배제하지는 않는다는 방침에 따라
# 국가 제한 없이 전 세계를 대상으로 별도 감지.
GDELT_GLOBAL_DISASTER_QUERY = "wildfire OR earthquake OR flood OR hurricane OR typhoon OR (state of emergency)"


def fetch_gdelt_global_major(timeout: int = 15) -> list:
    """
    국가 제한 없이 전 세계 초대형 재난/위기성 이슈를 GDELT에서 감지.
    반환: [{"title": str, "url": str, "country": "", "region": "global_major", "source": "gdelt_global"}]
    """
    results = []
    try:
        url = (
            f"https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={requests.utils.quote(GDELT_GLOBAL_DISASTER_QUERY)}"
            f"&mode=artlist&maxrecords=50&timespan=1d"
            f"&sort=datedesc&format=json"
        )
        res = requests.get(url, headers=HEADERS, timeout=timeout)
        if res.status_code == 200:
            data = res.json()
            for a in data.get("articles", []):
                title = a.get("title", "").strip()
                if not title or len(title) < 10:
                    continue
                results.append({
                    "title": title,
                    "url": a.get("url", ""),
                    "country": "",
                    "region": "global_major",
                    "source": "gdelt_global",
                    "seendate": a.get("seendate", ""),
                })
    except Exception as e:
        print(f"  [GDELT-Global] 수집 실패: {e}")
    return results


# ── 신호 합산 ────────────────────────────────────────────────

def aggregate_signals(
    gtrends: list,
    reddit: list,
    gdelt: list,
    gdelt_global: list = None,
) -> list:
    """
    세 소스(+전 세계 초대형 이슈 감지)의 신호를 합산해 주목도 높은 토픽 목록 반환.
    같은 키워드/토픽이 여러 소스에서 나올수록 점수 높음.
    반환: [{"topic": str, "score": int, "sources": list, "countries": list, "region": str, "titles": list}]
    """
    gdelt_global = gdelt_global or []
    topic_data = {}  # topic_key → {score, sources, countries, region, titles}

    def add_signal(key, score_add, source, country, region, title):
        key = key.lower().strip()
        if len(key) < 3:
            return
        if key not in topic_data:
            topic_data[key] = {
                "topic": key, "score": 0,
                "sources": set(), "countries": set(),
                "region": region, "titles": [],
            }
        topic_data[key]["score"] += score_add
        topic_data[key]["sources"].add(source)
        if country:
            topic_data[key]["countries"].add(country)
        if title and title not in topic_data[key]["titles"]:
            topic_data[key]["titles"].append(title)

    # Google Trends — 같은 국가에서 급상승 = 높은 신뢰도
    for g in gtrends:
        add_signal(
            g["keyword"], score_add=3,
            source="google_trends",
            country=g["country"],
            region=g["region"],
            title=g["keyword"],
        )

    # Reddit — 업보트 높을수록 가중
    for r in reddit:
        # 제목에서 핵심 키워드 추출 (첫 4단어)
        words = r["title"].split()[:5]
        key = " ".join(words)
        boost = 2 + min(r.get("score", 0) // 100, 3)  # 업보트 100당 +1, 최대 +3
        add_signal(
            key, score_add=boost,
            source="reddit",
            country="",
            region=r["region"],
            title=r["title"],
        )

    # GDELT (국가별) — 여러 나라에서 같은 이슈 = 강한 신호
    gdelt_title_counter = Counter(
        a["title"][:40].lower() for a in gdelt
    )
    for a in gdelt:
        key = a["title"][:40]
        freq_bonus = gdelt_title_counter.get(key.lower(), 1)
        add_signal(
            key, score_add=1 + freq_bonus,
            source="gdelt",
            country=a["country"],
            region=a["region"],
            title=a["title"],
        )

    # GDELT-Global (국가 무관, 전 세계 초대형 이슈) — 짧은 시간에 유사 제목 기사가
    # 많이 몰릴수록 전 세계적으로 크게 다뤄지는 진짜 초대형 이슈라고 판단해 가중치를 크게 줌.
    gdelt_global_title_counter = Counter(
        a["title"][:40].lower() for a in gdelt_global
    )
    for a in gdelt_global:
        key = a["title"][:40]
        freq_bonus = gdelt_global_title_counter.get(key.lower(), 1)
        add_signal(
            key, score_add=6 + freq_bonus,  # 단일 언급도 EXT_MIN_SCORE(5pt) 통과하도록 기본 점수 상향
            source="gdelt_global",
            country="",
            region="global_major",
            title=a["title"],
        )

    # 직렬화
    results = []
    for k, v in topic_data.items():
        # 여러 소스에서 잡히면 보너스
        multi_source_bonus = (len(v["sources"]) - 1) * 5
        results.append({
            "topic": v["topic"],
            "score": v["score"] + multi_source_bonus,
            "sources": list(v["sources"]),
            "countries": list(v["countries"]),
            "region": v["region"],
            "titles": v["titles"][:5],
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def collect_external_trends(verbose: bool = True) -> list:
    """
    외부 트렌드 신호 수집 메인 함수.
    반환: aggregate_signals() 결과 (점수 내림차순)
    """
    if verbose:
        print("\n[외부 트렌드] 수집 시작...")

    gtrends = fetch_google_trends()
    if verbose:
        print(f"  Google Trends: {len(gtrends)}개 키워드")

    reddit = fetch_reddit()
    if verbose:
        print(f"  Reddit: {len(reddit)}개 포스트")

    gdelt = fetch_gdelt()
    if verbose:
        print(f"  GDELT: {len(gdelt)}개 기사")

    gdelt_global = fetch_gdelt_global_major()
    if verbose:
        print(f"  GDELT-Global(초대형 이슈): {len(gdelt_global)}개 기사")

    signals = aggregate_signals(gtrends, reddit, gdelt, gdelt_global)
    if verbose:
        print(f"  합산 신호: {len(signals)}개 토픽")
        for s in signals[:10]:
            src = "+".join(s["sources"])
            countries = ",".join(list(s["countries"])[:3])
            print(f"    [{s['score']}pt/{src}] {s['topic'][:50]} — {countries}")

    return signals


if __name__ == "__main__":
    signals = collect_external_trends(verbose=True)
    print(f"\n상위 20개 트렌드:")
    for s in signals[:20]:
        print(f"  [{s['score']}] {s['topic']} ({','.join(s['sources'])})")
