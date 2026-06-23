"""
gemini_summarizer.py
--------------------
DB에 저장된 기사 중 summary_ko가 빈약한 기사를 골라
Gemini Flash로 고품질 한국어 요약을 재생성합니다.

실행: python scripts/gemini_summarizer.py
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

GEMINI_MODEL = "gemini-3.1-flash-lite"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
] if k]

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
NEWSFINAL_CHANNEL = "@newsfinal"

_current_key_idx = 0
MAX_ARTICLES = 30
CALL_INTERVAL = 5


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _sb_url():
    return f"{SUPABASE_URL}/rest/v1/articles"


def get_articles_to_summarize(limit: int) -> list:
    since = (now_kst() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_en,title_ko,summary_en,summary_ko,source,category,subcategory,region,country,full_text",
            "created_at": f"gte.{since}",
            "summary_ko": "is.null",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30
    )
    short_res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_en,title_ko,summary_en,summary_ko,source,category,subcategory,region,country,full_text",
            "created_at": f"gte.{since}",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30
    )
    articles = []
    if res.status_code in (200, 206):
        articles.extend(res.json())
    if short_res.status_code in (200, 206):
        for a in short_res.json():
            sk = a.get("summary_ko") or ""
            if len(sk) < 100 and a not in articles:
                articles.append(a)
    return articles[:limit]


def update_summary(article_id: int, summary_ko: str):
    requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={"summary_ko": summary_ko},
        timeout=15
    )


# 공식/공공 소스 목록
OFFICIAL_SOURCES = {
    "APO", "AfDB", "WHO", "ASEAN", "ADB", "IMF", "World Bank",
    "African Union", "UNCTAD", "IFC", "WFP",
    "Vietnam Government", "VietnamPlus", "Indonesia Setkab",
    "Kazakhstan Inform", "Kazinform", "Uzbekistan President",
    "Saudi Press Agency", "Qatar News Agency", "Kuwait News Agency",
    "Philippine Information Agency", "Bangkok Post Economics",
}

def is_official_source(source: str) -> bool:
    if not source:
        return False
    source_lower = source.lower()
    if source_lower.startswith("apo ") or "africa-newsroom" in source_lower:
        return True
    for official in OFFICIAL_SOURCES:
        if official.lower() in source_lower:
            return True
    return False

# 프롬프트 캐시
_prompt_cache = {}

def load_prompt(name: str, fallback: str = "") -> str:
    global _prompt_cache
    if name in _prompt_cache:
        return _prompt_cache[name]
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/prompts",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            },
            params={"name": f"eq.{name}", "is_active": "eq.true", "order": "version.desc", "limit": "1"},
            timeout=10
        )
        if res.status_code in (200, 206):
            data = res.json()
            if data:
                _prompt_cache[name] = data[0]["content"]
                return _prompt_cache[name]
    except Exception as e:
        print(f"[WARN] 프롬프트 로드 실패 ({name}): {e}")
    _prompt_cache[name] = fallback
    return fallback


def build_prompt(article: dict) -> str:
    title = article.get("title_en") or article.get("title_ko") or ""
    summary = article.get("summary_en") or ""
    full_text = article.get("full_text") or ""
    source = article.get("source") or ""
    category = article.get("category") or ""
    country = article.get("country") or ""
    region = article.get("region") or ""
    content = full_text if full_text else summary
    has_full_text = bool(full_text)

    FALLBACK_RULES = """원문이 길면 더 길게 써도 됩니다.
본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 절대 포함하지 마세요.
날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다" 같은 논평/칼럼 문체는 금지입니다.
출처 매체의 구독/앱/서비스 홍보 문장은 제거하세요."""

    rules = load_prompt("summarizer_rules", fallback=FALLBACK_RULES)

    if is_official_source(source):
        template = load_prompt("summarizer_official", fallback="""당신은 프론티어 미디어 NewsFinal의 에디터입니다.

아래는 공식 기관/정부의 공식 발표 자료입니다.

[기사 정보]
- 제목(영문): {title}
- 출처: {source}
- 분야: {category}
- 국가/지역: {country} ({region})
- 원문 내용: {content}

원문 내용을 한국어로 정확하게 번역하세요. 팩트를 빠짐없이 살리고, 원문이 길면 번역도 충분히 길게 쓰세요.
{rules}
번역문만 출력하세요.""")
        return template.format(title=title, source=source, category=category,
                               country=country, region=region, content=content, rules=rules)

    elif has_full_text:
        template = load_prompt("summarizer_fulltext", fallback="""당신은 프론티어 미디어 NewsFinal의 에디터입니다.

아래는 {source}의 원문 기사입니다.

[기사 정보]
- 제목: {title}
- 분야: {category}
- 국가/지역: {country} ({region})
- 원문: {content}

원문의 팩트(수치, 인명, 날짜, 기관명)를 빠짐없이 살려서 한국어로 작성하세요.
{rules}
요약문만 출력하세요.""")
        return template.format(title=title, source=source, category=category,
                               country=country, region=region, content=content, rules=rules)

    else:
        template = load_prompt("summarizer_rss", fallback="""당신은 프론티어 미디어 NewsFinal의 에디터입니다.

아래 기사를 바탕으로 한국어 요약문을 작성하세요.

[기사 정보]
- 제목(영문): {title}
- 출처: {source}
- 분야: {category}
- 국가/지역: {country} ({region})
- 원문 요약(영문): {summary}

기사의 핵심 내용을 한국어로 작성하세요. 팩트를 중심으로 쓰되 억지로 줄이지 마세요.
{rules}
요약문만 출력하세요.""")
        return template.format(title=title, source=source, category=category,
                               country=country, region=region,
                               content=content, summary=summary, rules=rules)


def call_gemini(prompt: str, retry: int = 2, max_tokens: int = 500) -> str | None:
    global _current_key_idx
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": max_tokens,
        }
    }

    while _current_key_idx < len(GEMINI_API_KEYS):
        api_key = GEMINI_API_KEYS[_current_key_idx]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={api_key}"
        )
        try:
            res = requests.post(url, json=payload, timeout=(10, 30))
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif res.status_code == 429:
                print(f"  [429] 키 {_current_key_idx+1} 한도 초과 → 키 {_current_key_idx+2}로 전환")
                _current_key_idx += 1
            else:
                print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                return None
        except requests.exceptions.Timeout:
            print(f"  [TIMEOUT] 키 {_current_key_idx+1} — 넘어갑니다.")
            return None
        except Exception as e:
            print(f"[ERROR] {e}")
            return None

    print("[ERROR] 모든 키 소진")
    return None


# ── 장기 이슈 트래커 ────────────────────────────────────────

# 추적할 키워드 그룹 — (그룹명, 카테고리, [키워드 목록])
TREND_KEYWORDS = [
    ("에볼라",       "사회",    ["ebola", "에볼라", "hemorrhagic fever", "출혈열", "MVD", "marburg"]),
    ("mpox",        "사회",    ["mpox", "monkeypox", "원숭이두창"]),
    ("콜레라",       "사회",    ["cholera", "콜레라"]),
    ("수단 분쟁",    "정치·외교", ["sudan", "RSF", "수단", "다르푸르", "darfur", "khartoum"]),
    ("DRC 분쟁",    "정치·외교", ["DRC", "congo", "콩고", "M23", "키부", "kivu"]),
    ("소말리아",     "정치·외교", ["somalia", "소말리아", "al-shabaab", "알샤바브"]),
    ("미얀마",       "정치·외교", ["myanmar", "미얀마", "junta", "군부", "NUG"]),
    ("아이티",       "사회",    ["haiti", "아이티", "gang", "갱단"]),
    ("사헬 쿠데타",  "정치·외교", ["sahel", "사헬", "mali", "말리", "niger", "burkina", "부르키나"]),
    ("중앙아프리카",  "정치·외교", ["central african", "중앙아프리카", "CAR", "bangui"]),
]

# 추적 윈도우: 7일간 기사에서 키워드 빈도 분석
TREND_WINDOW_DAYS = 7
TREND_MIN_ARTICLES = 3   # 최소 N건 이상 등장해야 트렌드로 판단
TREND_CHECK_HOURS  = 12  # 마지막 추적기사 생성 후 N시간 이내면 스킵


def get_trend_articles(keywords: list, days: int = 7) -> list:
    """지난 N일간 특정 키워드가 포함된 수집 기사 반환"""
    since = (now_kst() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    all_articles = []
    for kw in keywords:
        try:
            res = requests.get(
                _sb_url(),
                headers=_sb_headers(),
                params={
                    "select": "id,title_en,title_ko,summary_ko,summary_en,full_text,source,country,category,region,created_at",
                    "source": "neq.NewsFinal",
                    "created_at": f"gte.{since}",
                    "or": f"(title_en.ilike.*{kw}*,title_ko.ilike.*{kw}*,summary_en.ilike.*{kw}*)",
                    "order": "created_at.desc",
                    "limit": "50",
                },
                timeout=15
            )
            if res.status_code in (200, 206):
                all_articles.extend(res.json())
        except Exception as e:
            print(f"  ⚠️ 트렌드 조회 실패 ({kw}): {e}")

    # 중복 제거
    seen = set()
    unique = []
    for a in all_articles:
        if a["id"] not in seen:
            seen.add(a["id"])
            unique.append(a)
    return unique


def trend_article_exists(group_name: str) -> bool:
    """최근 N시간 내 해당 트렌드 추적 기사가 이미 있는지 확인"""
    since = (now_kst() - timedelta(hours=TREND_CHECK_HOURS)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id",
                "source": "eq.NewsFinal",
                "subcategory": f"eq.trend_{group_name}",
                "created_at": f"gte.{since}",
                "limit": "1",
            },
            timeout=10
        )
        if res.status_code in (200, 206):
            return len(res.json()) > 0
    except Exception:
        pass
    return False


def save_trend_article(group_name: str, title: str, body: str,
                       category: str, country: str, region: str,
                       countries: list) -> int:
    """트렌드 추적 기사 저장"""
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    payload = {
        "title_en": title, "title_ko": title,
        "summary_en": "", "summary_ko": body,
        "url": f"internal://trend_{group_name}_{now_kst().strftime('%Y%m%d%H')}",
        "source": "NewsFinal",
        "category": category,
        "subcategory": f"trend_{group_name}",
        "region": region,
        "country": country,
        "country_flag": "",
        "countries": countries or ([country] if country else []),
        "score": 2,  # 라이브 탭에 바로 표시
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "트렌드 추적 최초 게시"}],
        "sent_telegram": 0,
        "is_published": True,
        "posted_blog": 0,
    }
    try:
        res = requests.post(_sb_url(), headers=_sb_headers(), json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
    except Exception as e:
        print(f"  ⚠️ 트렌드 기사 저장 실패: {e}")
    return -1


def run_trend_tracker():
    """장기 이슈 트렌드 감지 및 추적 기사 생성"""
    if not GEMINI_API_KEYS:
        return

    print("\n[트렌드 트래커] 장기 이슈 분석 시작...")

    for group_name, category, keywords in TREND_KEYWORDS:
        articles = get_trend_articles(keywords, days=TREND_WINDOW_DAYS)

        if len(articles) < TREND_MIN_ARTICLES:
            print(f"  [{group_name}] {len(articles)}건 — 임계값 미달, 스킵")
            continue

        print(f"  [{group_name}] {len(articles)}건 감지 → 추적 기사 생성 검토")

        if trend_article_exists(group_name):
            print(f"  [{group_name}] 최근 {TREND_CHECK_HOURS}시간 내 이미 생성됨 — 스킵")
            continue

        # 최신 기사 최대 8건으로 Gemini 프롬프트 구성
        top = sorted(articles, key=lambda a: a.get("created_at", ""), reverse=True)[:8]
        today_str = now_kst().strftime("%Y년 %m월 %d일")

        article_list = ""
        for i, a in enumerate(top, 1):
            t = a.get("title_ko") or a.get("title_en") or ""
            body = a.get("full_text") or a.get("summary_ko") or a.get("summary_en") or ""
            article_list += f"{i}. [{a.get('source','')}] {t}\n"
            if body:
                article_list += f"   {body[:300]}\n\n"

        prompt = f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 지난 {TREND_WINDOW_DAYS}일간 [{group_name}] 관련 기사 {len(articles)}건의 주요 내용입니다. ({today_str})

[수집된 관련 기사]
{article_list}

이 기사들을 종합해 현재 진행 중인 상황을 정리하는 추적 기사를 작성하세요.
- 현재 상황이 어떻게 전개되고 있는지 시간 순으로 정리하세요.
- 수치, 인명, 날짜, 기관명 등 구체적 팩트를 최대한 살리세요.
- 한국 투자자/독자 관점에서 왜 중요한지 한 문단으로 마무리하세요.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- 한국어로만 작성하세요.

아래 형식으로 출력:
제목: (현재 상황을 담은 추적 기사 제목)
국가: (주요 대상 국가 1개, 없으면 "없음")
관련국가: (관련국 최대 4개, 없으면 "없음")
분야: ({category})
본문: (추적 기사 본문)"""

        content = call_gemini(prompt, max_tokens=2000)
        if not content:
            print(f"  [{group_name}] ❌ Gemini 생성 실패")
            continue

        # 파싱
        title, body, country, gen_category, countries = "", content, "", category, []
        for line in content.strip().split("\n"):
            if line.startswith("제목:"):
                title = line.replace("제목:", "").strip()
            elif line.startswith("국가:"):
                c = line.replace("국가:", "").strip()
                if c not in ("없음", "-", ""):
                    country = c
            elif line.startswith("관련국가:"):
                raw = line.replace("관련국가:", "").strip()
                if raw not in ("없음", "-", ""):
                    countries = [x.strip() for x in raw.split(",") if x.strip()]
            elif line.startswith("분야:"):
                gen_category = line.replace("분야:", "").strip() or category
            elif line.startswith("본문:"):
                idx = content.find("본문:")
                body = content[idx + 3:].strip()
                break

        if not title:
            title = f"{group_name} 동향 — {today_str}"

        # 지역 추론
        region_map = {
            "아프리카": "africa", "나이지리아": "africa", "케냐": "africa",
            "수단": "africa", "콩고": "africa", "소말리아": "africa",
            "말리": "africa", "부르키나파소": "africa", "중앙아프리카": "africa",
            "미얀마": "southeast_asia", "아이티": "caribbean",
        }
        region = region_map.get(country, "africa")

        article_id = save_trend_article(
            group_name=group_name, title=title, body=body,
            category=gen_category, country=country, region=region,
            countries=countries
        )

        if article_id > 0:
            print(f"  [{group_name}] ✅ 추적 기사 생성 (id={article_id}): {title}")
            # 텔레그램 발송
            if TELEGRAM_TOKEN:
                try:
                    preview = body[:300]
                    url = f"https://newsfinal.co.kr/article.html?id={article_id}"
                    msg = f"📡 트렌드 추적\n\n*{title}*\n\n{preview}{'…' if len(body) > 300 else ''}\n\n[전체 기사 보기]({url})"
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        data={"chat_id": NEWSFINAL_CHANNEL, "text": msg,
                              "parse_mode": "Markdown", "disable_web_page_preview": False},
                        timeout=15
                    )
                except Exception:
                    pass
        else:
            print(f"  [{group_name}] ❌ 저장 실패")

        time.sleep(CALL_INTERVAL)

    print("[트렌드 트래커] 완료")


def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음 — gemini_summarizer 건너뜀")
        return

    # API 연결 테스트
    print(f"[체크] Gemini API 연결 테스트... (키 {len(GEMINI_API_KEYS)}개)")
    test = call_gemini("ping", retry=1)
    if test is None:
        print("[SKIP] Gemini API 응답 없음 — 건너뜀")
        return
    print("[체크] ✅ API 연결 확인")

    articles = get_articles_to_summarize(MAX_ARTICLES)
    print(f"[요약 고도화] 대상 기사 {len(articles)}건")

    success = 0
    for i, article in enumerate(articles):
        prompt = build_prompt(article)
        has_full = bool(article.get("full_text"))
        summary_ko = call_gemini(prompt)

        if summary_ko:
            update_summary(article["id"], summary_ko)
            src = "원문" if has_full else "RSS요약"
            print(f"[{i+1}/{len(articles)}] ✅ [{src}] {article['title_ko'] or article['title_en'][:50]}")
            success += 1
        else:
            print(f"[{i+1}/{len(articles)}] ❌ 실패 — {article['title_en'][:50]}")

        # API 한도 준수
        if i < len(articles) - 1:
            time.sleep(CALL_INTERVAL)

    print(f"\n✅ 요약 고도화 완료: {success}/{len(articles)}건 성공")

    # 장기 이슈 트렌드 추적
    run_trend_tracker()


if __name__ == "__main__":
    run()
