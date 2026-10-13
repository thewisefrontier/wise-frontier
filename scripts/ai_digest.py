"""
ai_digest.py
------------
"오늘의 AI 소식" 연재 기사 생성 — 국가/지역을 따지지 않고 raw_candidates에서
AI(인공지능) 관련 키워드로 직접 필터링해 하루치 소식을 모아 소개한다.

daily_digest.py(자체 발행 기사를 국가별 테마로 재종합)와는 소스·형식이
다르다: 이쪽은 원문 raw_candidates를 직접 모아, 테마 재종합이 아니라 개별
소식을 섹션별로 소개하는 로스업(round-up) 형식이다. Gemini 호출·팩트가드·
저장 로직은 daily_digest.py를 그대로 재사용한다(중복 구현 방지).

실행: python scripts/ai_digest.py (digest.yml에서 매일 1회)
"""

from datetime import timedelta

from daily_digest import (
    now_kst, call_gemini_article, parse_title_and_body, fetch_article_image,
    format_digest_body, keep_first_local_time, _pub_day_label,
    load_prompt, detect_script_leak, is_placeholder_response,
    check_date_hallucination,
)
from article_store import insert_final_article, sb_headers, sb_url
from http_retry import get_session

requests = get_session()

# title_en/title_ko 양쪽에 거는 AI 키워드 — 2026-10-14 실측(Supabase raw_candidates
# 5일치 11만건): 이 조합으로 약 1,500건(하루 평균 300건) 매칭 확인. bare "ai-"는
# "Dubai-bound"처럼 무관한 단어의 부분문자열이 되는 걸 실측 중 직접 확인해서
# 뺐다(" ai "만 남김 — 공백 양쪽이 있어야 하므로 이 문제가 없다).
_AI_KEYWORDS_EN = [
    "openai", "chatgpt", "anthropic", "deepseek", "generative ai",
    "artificial intelligence", "machine learning", "large language model",
    "copilot", "genai", " ai ", "ai-generated", "ai-powered", "ai agent",
    "ai model", "ai chip", "ai startup", "ai regulation", "physical ai",
    "claude ai",
]
_AI_KEYWORDS_KO = [
    "인공지능", "생성형ai", "생성형 ai", "챗gpt", "오픈ai", "앤트로픽",
    "딥시크", "거대언어모델", "머신러닝",
]

AI_DIGEST_MIN_CANDIDATES = 3
AI_DIGEST_MAX_CANDIDATES = 25


def get_ai_candidates(limit=AI_DIGEST_MAX_CANDIDATES):
    since = (now_kst() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    or_terms = [f"title_en.ilike.*{kw}*" for kw in _AI_KEYWORDS_EN]
    or_terms += [f"title_ko.ilike.*{kw}*" for kw in _AI_KEYWORDS_KO]
    res = requests.get(
        sb_url("raw_candidates"),
        headers=sb_headers(),
        params={
            "select": "id,title_en,title_ko,full_text,url,source_published_at,created_at,score",
            "created_at": f"gte.{since}",
            "or": f"({','.join(or_terms)})",
            "order": "score.desc",
            "limit": str(limit),
        },
        timeout=30,
    )
    if res.status_code in (200, 206):
        return res.json()
    print(f"  ⚠️ AI 후보 조회 실패: {res.status_code} {res.text[:200]}")
    return []


def ai_digest_exists_for_today() -> bool:
    today_key = f"ai_digest_{now_kst().strftime('%Y%m%d')}"
    res = requests.get(
        sb_url(),
        headers=sb_headers(),
        params={"select": "id", "subcategory": f"eq.{today_key}", "limit": "1"},
        timeout=15,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def build_ai_digest_prompt(candidates):
    article_list = ""
    for c in candidates:
        title = c.get("title_en") or c.get("title_ko") or ""
        body = (c.get("full_text") or "")[:500]
        pub = _pub_day_label(c.get("source_published_at") or c.get("created_at"))
        pub_tag = f" [{pub} 발행]" if pub else ""
        article_list += f"\n- {title}{pub_tag}\n  {body}\n"

    rules = load_prompt("ai_digest_rules", fallback="""[작성 규칙]
- 아래는 지난 24시간 동안 전세계에서 나온 AI(인공지능) 관련 소식 원문 목록입니다. 국가는 따지지 말고 소식 자체의 중요도로만 고르세요.
- 같은 사건을 여러 원문이 다루면 하나로 합쳐서 한 번만 다루세요. 중요도가 낮거나 중복되는 소식은 과감히 빼고, 실제로 의미 있는 소식 4~8개만 고르세요.
- 본문은 소식 하나당 대괄호 소제목 하나로 구성하세요(예: "[오픈AI, 신규 모델 공개]"). 소제목은 그 소식의 핵심을 구체적으로 담으세요 — "기타 소식" 같은 모호한 제목은 쓰지 마세요.
- 각 소제목 아래에 "- "로 시작하는 불릿 1~3개로 핵심 사실(누가·무엇을·언제·수치)을 서술하세요. 의견·전망·논평은 넣지 말고 사실 전달체로만 쓰세요.
- 소제목과 소제목 사이는 반드시 빈 줄로 구분하세요.
- 마크다운 문법(**굵게**, ##제목)은 쓰지 말고, 대괄호 소제목 + 불릿 형식만 쓰세요.
- 날짜 표기는 각 원문에 표시된 발행일을 "N일(현지시간)" 형식으로 쓰세요. 표시되지 않은 날짜는 지어내지 마세요.
- 특정 언론사·매체 이름을 근거로 들지 마세요("~라고 OO가 보도했다" 금지) — 사실만 바로 서술하세요.
- 한국어로만 작성하세요.""")

    return f"""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 지난 24시간 동안 전세계에서 나온 AI(인공지능) 관련 소식 원문입니다.

{article_list}

{rules}

아래 형식으로 출력:
제목: [오늘의 AI 소식] 뒤에 오늘 가장 중요한 소식을 담은 부제목을 붙이세요. 예: "[오늘의 AI 소식] 오픈AI 신규 모델 공개"
본문: (다이제스트 본문)"""


def _ai_digest_sources(candidates: list) -> list:
    """date_guard 근거용 — 다이제스트 자체 기사가 아니라 raw_candidates 원문을 그대로 넘긴다."""
    out = []
    for c in candidates:
        out.append({
            "id": c.get("id"),
            "full_text": c.get("full_text") or "",
            "title_en": c.get("title_en") or "",
            "summary_en": "",
            "source_published_at": c.get("source_published_at") or c.get("created_at") or "",
        })
    return out


def save_ai_digest(title, body, candidate_count, image_url="", published=True, guard_note=""):
    if detect_script_leak(title, body):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title[:60]}")
        return -1
    if is_placeholder_response(title, body):
        print(f"  ❌ Gemini 응답이 실제 기사가 아님 → 저장 차단: {title[:60]}")
        return -1
    today_key = f"ai_digest_{now_kst().strftime('%Y%m%d')}"
    payload = {
        "title_en": title,
        "title_ko": title,
        "summary_en": "",
        "summary_ko": body,
        "url": f"internal://{today_key}",
        "source": "NewsFinal",
        "category": "IT·과학",
        "subcategory": today_key,
        "region": "global",
        "country": "",
        "country_flag": "",
        "image_url": image_url,
        "score": candidate_count,
        "created_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        "sent_telegram": 0,
        "is_published": published,
        "posted_blog": 0,
    }
    if guard_note:
        payload["update_log"] = [{"timestamp": now_kst().strftime("%Y-%m-%d %H:%M"), "note": guard_note}]
    return insert_final_article(payload)


def run():
    if ai_digest_exists_for_today():
        print("[SKIP] 오늘 AI 소식 다이제스트 이미 생성됨")
        return

    candidates = get_ai_candidates()
    print(f"[AI 소식] 최근 24시간 AI 관련 원문 {len(candidates)}건 발견")

    if len(candidates) < AI_DIGEST_MIN_CANDIDATES:
        print(f"[SKIP] AI 소식 작성에 충분한 원문이 없습니다 (최소 {AI_DIGEST_MIN_CANDIDATES}건 필요)")
        return

    prompt = build_ai_digest_prompt(candidates)
    content = call_gemini_article(prompt, max_tokens=4000)

    if not content:
        print("[ERROR] Gemini 응답 없음")
        return

    title, body = parse_title_and_body(content)
    if not body:
        print("[ERROR] 본문 파싱 실패(라벨 '제목:' 없음) — 저장하지 않음")
        return
    if not title:
        title = "[오늘의 AI 소식] 주요 동향"
    raw_body = body

    dg_bad, dg_reason = check_date_hallucination(
        raw_body, _ai_digest_sources(candidates), base_date=now_kst().date()
    )

    body = format_digest_body(keep_first_local_time(body))
    image_url = fetch_article_image(title, body)
    if dg_bad:
        print(f"⛔ 날짜 환각 의심 → 미발행: {dg_reason}")

    article_id = save_ai_digest(
        title, body, len(candidates), image_url=image_url,
        published=not dg_bad,
        guard_note=(f"날짜 환각 의심 미발행 — {dg_reason}" if dg_bad else ""),
    )
    if article_id > 0:
        print(f"✅ AI 소식 다이제스트 저장 완료 (id={article_id}): {title}")
    else:
        print("❌ AI 소식 다이제스트 저장 실패")


if __name__ == "__main__":
    run()
