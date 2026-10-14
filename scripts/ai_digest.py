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
from gemini_writer import extract_core_kw
from http_retry import get_session

requests = get_session()

# title_en/title_ko 양쪽에 거는 AI 키워드 — 2026-10-14 실측(Supabase raw_candidates
# 5일치 11만건): 이 조합으로 약 1,500건(하루 평균 300건) 매칭 확인. bare "ai-"는
# "Dubai-bound"처럼 무관한 단어의 부분문자열이 되는 걸 실측 중 직접 확인해서
# 뺐다(" ai "만 남김 — 공백 양쪽이 있어야 하므로 이 문제가 없다).
#
# ⚠️ 2026-10-14 2차 실사고: 첫 배포 직후 "구글 Gemini 4 Argon 정식 출시"
# 기사가 다이제스트에 통째로 빠졌다. 원인은 score 정렬(사용 전 삭제)이
# 아니라 — 이 키워드 목록에 "gemini"가 애초에 없었다("제미나이/Gemini"는
# 점성술 별자리·NASA 제미니 계획과 겹쳐 오탐 우려로 처음부터 뺐음). 그래서
# "Gemini 4 Argon"처럼 그 자체로 "AI"라는 단어를 안 쓰는 제목은 다른
# 기사가 우연히 "AI model" 같은 문구를 같이 썼을 때만 걸렸다(9건 중 5건만
# 걸리고 나머지는 전혀 안 보임 — 직접 재현 확인). "gemini 4"처럼 버전
# 숫자를 붙이면 별자리 운세 문구("Gemini: today's horoscope")와 겹칠 일이
# 없어 안전하게 구체적으로 잡을 수 있다.
_AI_KEYWORDS_EN = [
    "openai", "chatgpt", "anthropic", "deepseek", "generative ai",
    "artificial intelligence", "machine learning", "large language model",
    "copilot", "genai", " ai ", "ai-generated", "ai-powered", "ai agent",
    "ai model", "ai chip", "ai startup", "ai regulation", "physical ai",
    "claude ai", "gemini 2", "gemini 3", "gemini 4", "gemini 5",
    "google gemini", "gemini ai", "gemini model", "gemini app",
]
_AI_KEYWORDS_KO = [
    "인공지능", "생성형ai", "생성형 ai", "챗gpt", "오픈ai", "앤트로픽",
    "딥시크", "거대언어모델", "머신러닝", "제미나이",
]

AI_DIGEST_MIN_CANDIDATES = 3
# 2026-10-14 실사고: order=score.desc로 25건만 가져왔다가 "구글 Gemini 4
# Argon 정식 출시"(같은 날 raw_candidates에 9건 이상 중복 보도)를 통째로
# 놓쳤다. raw_candidates.score는 이 수집 경로(RSS/구글뉴스 집계)에선 전부
# 0으로만 찍혀 있어 "score desc" 정렬이 사실상 무작위 추출이었다.
# created_at.desc(최신순)로만 바꿔봤지만 그것도 틀렸다 — 하루 매칭량이
# 300~400건(시간당 15건)이라, 최신 60건만 보면 Argon처럼 몇 시간 전에
# 몰아서 나온 1등 소식이 그 사이 쌓인 잡다한 단건 기사들에 밀려 똑같이
# 빠졌다(직접 재현 확인). 근본 수정: 하루 전체 풀을 가져와 같은 사건을
# 다루는 기사 수(제목 핵심 키워드 2개 이상 겹치는 쌍 — gemini_writer의
# extract_core_kw 재사용)를 세어 "중복 보도 횟수 = 중요도"로 직접 랭킹한다.
AI_DIGEST_POOL_LIMIT = 500   # 24시간 전체 매칭 풀(실측 하루 300~400건)
AI_DIGEST_MAX_CANDIDATES = 40  # Gemini에 넘길 대표 소식 수(클러스터당 1건)


def _ai_or_filter() -> str:
    or_terms = [f"title_en.ilike.*{kw}*" for kw in _AI_KEYWORDS_EN]
    or_terms += [f"title_ko.ilike.*{kw}*" for kw in _AI_KEYWORDS_KO]
    return f"({','.join(or_terms)})"


def get_ai_candidates(limit=AI_DIGEST_MAX_CANDIDATES, pool_limit=AI_DIGEST_POOL_LIMIT):
    since = (now_kst() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    res = requests.get(
        sb_url("raw_candidates"),
        headers=sb_headers(),
        params={
            "select": "id,title_en,title_ko,source_published_at,created_at",
            "created_at": f"gte.{since}",
            "or": _ai_or_filter(),
            "order": "created_at.desc",
            "limit": str(pool_limit),
        },
        timeout=30,
    )
    if res.status_code not in (200, 206):
        print(f"  ⚠️ AI 후보 조회 실패: {res.status_code} {res.text[:200]}")
        return []
    pool = res.json()
    if not pool:
        return []

    kw_sets = [extract_core_kw(f"{p.get('title_en') or ''} {p.get('title_ko') or ''}") for p in pool]
    dup_count = [0] * len(pool)
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            if len(kw_sets[i] & kw_sets[j]) >= 2:
                dup_count[i] += 1
                dup_count[j] += 1

    # 중복 보도 횟수 desc, 동률이면 최신순(인덱스가 작을수록 최신 — pool이 created_at.desc)
    order = sorted(range(len(pool)), key=lambda i: (-dup_count[i], i))

    selected, selected_kws = [], []
    for i in order:
        if len(selected) >= limit:
            break
        if any(len(kw_sets[i] & sk) >= 2 for sk in selected_kws):
            continue  # 이미 같은 사건의 대표가 선정됨 — 중복 제외
        pool[i]["_dup_count"] = dup_count[i]
        selected.append(pool[i])
        selected_kws.append(kw_sets[i])

    ids = [p["id"] for p in selected if p.get("id")]
    if ids:
        res2 = requests.get(
            sb_url("raw_candidates"),
            headers=sb_headers(),
            params={"select": "id,full_text", "id": f"in.({','.join(str(i) for i in ids)})"},
            timeout=30,
        )
        full_text_by_id = {r["id"]: r.get("full_text") or "" for r in res2.json()} if res2.status_code in (200, 206) else {}
        for p in selected:
            p["full_text"] = full_text_by_id.get(p.get("id"), "")

    return selected


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
    # 같은 사건 중복 보도는 get_ai_candidates()에서 이미 대표 1건으로 합쳐뒀다
    # (_dup_count = 같은 사건을 다룬 다른 원문 수). 목록에 중복을 그대로
    # 늘어놓고 Gemini가 반복을 세게 하는 대신, 코드가 직접 센 숫자를 그대로
    # 알려준다 — 숫자가 명확하면 LLM이 중요도를 놓칠 여지가 줄어든다.
    article_list = ""
    for c in candidates:
        title = c.get("title_en") or c.get("title_ko") or ""
        body = (c.get("full_text") or "")[:250]
        pub = _pub_day_label(c.get("source_published_at") or c.get("created_at"))
        pub_tag = f" [{pub} 발행]" if pub else ""
        dup = c.get("_dup_count") or 0
        dup_tag = f" [다른 매체 {dup}곳도 같은 소식 보도]" if dup >= 1 else ""
        article_list += f"\n- {title}{pub_tag}{dup_tag}\n  {body}\n"

    rules = load_prompt("ai_digest_rules", fallback="""[작성 규칙]
- 아래는 지난 24시간 동안 전세계에서 나온 AI(인공지능) 관련 소식입니다(이미 같은 사건끼리는 대표 1건으로 합쳐둔 상태). 국가는 따지지 말고 소식 자체의 중요도로만 고르세요.
- 각 소식 옆의 "[다른 매체 N곳도 같은 소식 보도]" 표시는 그 소식이 그만큼 널리 보도됐다는 뜻입니다 — 숫자가 큰 소식을 최우선으로 포함하세요. 표시가 없는(한 곳에서만 보도된) 소식은 상대적으로 덜 중요할 수 있으니, 다른 중요한 소식이 충분하면 과감히 빼세요.
- 실제로 의미 있는 소식 4~8개만 고르세요.
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
