"""
daily_digest.py
----------------
하루치 NewsFinal 자체 기사를 종합해 "오늘의 프론티어 마켓 다이제스트" 생성.
StockHub의 "오늘의 핵심 테마" 패턴을 프론티어 마켓에 맞게 적용.

실행: python scripts/daily_digest.py
하루 1회 실행 권장 (예: KST 22:00 / UTC 13:00)
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

_current_key_idx = 0


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_url():
    return f"{SUPABASE_URL}/rest/v1/articles"


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


def get_yesterday_own_articles(limit=200):
    """어제(KST) 발행된 NewsFinal 자체 기사 전체 (다이제스트 제외) — 하루 결산용"""
    yesterday = (now_kst() - timedelta(days=1)).strftime("%Y-%m-%d")
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_ko,summary_ko,category,country,countries,region,created_at,subcategory",
            "source": "eq.NewsFinal",
            "is_published": "eq.true",
            "created_at": f"like.{yesterday}%",
            "subcategory": "not.like.digest_%",
            "order": "created_at.asc",
            "limit": str(limit),
        },
        timeout=30
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


def digest_exists_for_today() -> bool:
    """오늘(KST) 이미 다이제스트를 발행했는지 확인 — subcategory 키는 발행일 기준"""
    today_key = f"digest_{now_kst().strftime('%Y%m%d')}"
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={"select": "id", "subcategory": f"eq.{today_key}", "limit": "1"},
        timeout=15
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def call_gemini(prompt, max_tokens=3000):
    global _current_key_idx
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.5, "maxOutputTokens": max_tokens},
    }

    while _current_key_idx < len(GEMINI_API_KEYS):
        api_key = GEMINI_API_KEYS[_current_key_idx]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={api_key}"
        )
        try:
            res = requests.post(url, json=payload, timeout=(10, 45))
            if res.status_code == 200:
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif res.status_code == 429:
                print(f"  [429] 키 {_current_key_idx+1} 한도 초과 → 키 {_current_key_idx+2}로 전환")
                _current_key_idx += 1
            else:
                print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                return None
        except requests.exceptions.Timeout:
            print(f"  [TIMEOUT] 키 {_current_key_idx+1}")
            return None
        except Exception as e:
            print(f"[ERROR] {e}")
            return None

    print("[ERROR] 모든 키 소진")
    return None


def build_digest_prompt(articles):
    today_str = now_kst().strftime("%Y년 %m월 %d일")  # 발행일(오늘) 기준 — 신문 날짜와 동일

    # 국가별로 그룹화해서 제공 (Gemini가 패턴 찾기 쉽도록)
    by_country = {}
    for a in articles:
        countries = a.get("countries") or ([a.get("country")] if a.get("country") else [])
        countries = [c for c in countries if c] or ["글로벌"]
        for c in countries:
            by_country.setdefault(c, []).append(a)

    article_list = ""
    for country, items in by_country.items():
        article_list += f"\n[{country}]\n"
        for a in items:
            title = a.get("title_ko") or ""
            summary = (a.get("summary_ko") or "")[:200]
            article_list += f"- {title}\n  {summary}\n"

    rules = load_prompt("digest_rules", fallback="""[작성 규칙]
- 어제 하루 동안 NewsFinal이 다룬 프론티어 마켓 기사들을 종합해 "어제의 핵심 테마"를 정리하는 일일 결산 다이제스트를 작성하세요.
- 개별 기사를 단순 나열하지 말고, 여러 국가/기사에 걸쳐 공통적으로 나타나는 패턴, 테마, 트렌드를 중심으로 통찰을 제공하세요.
- 예: "이번 주 여러 아프리카 국가에서 통화 평가절하 압력이 동시에 나타남", "동남아 국가들의 외국인직접투자 유치 경쟁 심화" 같은 교차 비교형 분석을 우선하세요.
- 지역별/테마별로 섹션을 나누고, 각 섹션은 불릿(- 로 시작)으로 핵심을 정리하세요.
- 마크다운 문법(**굵게**, ##제목)을 쓰지 말고 일반 텍스트와 줄바꿈, "- " 불릿만 사용하세요.
- 전문 형식 헤더([도시=출처] 등)나 매체 홍보 문구를 넣지 마세요.
- 다룬 기사가 적으면 무리하게 늘리지 말고 있는 그대로 간결하게 작성하세요.
- 한국어로만 작성하세요.""")

    return f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 어제 하루 동안 NewsFinal이 다룬 프론티어 마켓 기사 목록입니다. 국가별로 정리되어 있습니다.

{article_list}

{rules}

아래 형식으로 출력:
제목: (다이제스트의 핵심을 담은 제목, 예: "{today_str} 프론티어 마켓 — 통화 압력과 인프라 투자 확대")
본문: (다이제스트 본문)"""


def parse_title_and_body(text):
    title = ""
    body = text
    lines = text.strip().split("\n")
    for i, line in enumerate(lines):
        if line.startswith("제목:"):
            title = line.replace("제목:", "").strip()
            body = "\n".join(lines[i+1:]).strip()
            if body.startswith("본문:"):
                body = body[3:].strip()
            break
    return title, body


def save_digest(title, body, article_count):
    today_key = f"digest_{now_kst().strftime('%Y%m%d')}"
    payload = {
        "title_en": title,
        "title_ko": title,
        "summary_en": "",
        "summary_ko": body,
        "url": f"internal://{today_key}",
        "source": "NewsFinal",
        "category": "다이제스트",
        "subcategory": today_key,  # 발행일(오늘) 기준 키 — 홈 노출 판단 기준
        "region": "global",
        "country": "",
        "country_flag": "",
        "score": article_count,
        "created_at": now_kst().strftime("%Y-%m-%d %H:%M"),  # 실제 발행 시각(오늘 새벽) — 홈 노출 판단 기준
        "sent_telegram": 0,
        "is_published": True,
        "posted_blog": 0,
    }
    res = requests.post(_sb_url(), headers=_sb_headers(), json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0].get("id", -1) if data else -1
    print(f"[ERROR] 저장 실패: {res.status_code} {res.text[:200]}")
    return -1


def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    if digest_exists_for_today():
        print("[SKIP] 오늘 다이제스트 이미 생성됨")
        return

    articles = get_yesterday_own_articles()
    print(f"[다이제스트] 어제 자체 기사 {len(articles)}건 발견")

    if len(articles) < 3:
        print("[SKIP] 다이제스트 작성에 충분한 기사가 없습니다 (최소 3건 필요)")
        return

    prompt = build_digest_prompt(articles)
    content = call_gemini(prompt, max_tokens=3000)

    if not content:
        print("[ERROR] Gemini 응답 없음")
        return

    title, body = parse_title_and_body(content)
    if not title:
        today_str = now_kst().strftime('%Y년 %m월 %d일')
        title = f"{today_str} 프론티어 마켓 다이제스트"

    article_id = save_digest(title, body or content, len(articles))
    if article_id > 0:
        print(f"✅ 다이제스트 저장 완료 (id={article_id}): {title}")
    else:
        print("❌ 다이제스트 저장 실패")


if __name__ == "__main__":
    run()
