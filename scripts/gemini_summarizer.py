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
from dotenv import load_dotenv

load_dotenv()

GEMINI_MODEL = "gemini-3.1-flash-lite"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
] if k]

_current_key_idx = 0
MAX_ARTICLES = 30
CALL_INTERVAL = 5

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
기사 문체로 작성하세요. "~를 보여줍니다", "~을 도모하고 있습니다" 같은 논평/칼럼 문체는 금지입니다."""

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


def call_gemini(prompt: str, retry: int = 2) -> str | None:
    global _current_key_idx
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 500,
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


if __name__ == "__main__":
    run()
