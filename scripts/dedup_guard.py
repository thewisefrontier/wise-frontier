"""
scripts/dedup_guard.py
--------------------------
일반 기사(gemini_writer.py)와 트렌드 기사(gemini_summarizer.py)가 각자
독립적으로 진화시켜온 "동일 사건 중복판정" 로직을 공용화.

2026-09-08 사용자 지적: "일반기사/트렌드기사 각각의 중복 검사 툴이 있을텐데,
그걸 합쳐서 공용 모듈로 만들면 안되나?" — 실제로 두 파일에 거의 같은 목적의
함수가 따로 있었다(gemini_writer.find_similar_article / gemini_summarizer.
find_similar_trend), 각자 다른 사고 이력으로 따로 진화해서 서로 다른 안전장치를
갖고 있었다. 예:
- gemini_writer 쪽엔 LLM 최종판정 안전망이 없어서 2026-09-06 카르그 유조선
  중복(id=135075/137073)을 놓쳤다가 나중에 급하게 추가됨.
- gemini_summarizer 쪽엔 원문 RSS 태그 기반 매칭이 있었지만 gemini_writer
  (일반 클러스터링 경로)는 아예 이 신호를 쓸 수 없었다.
이 모듈은 두 곳이 똑같이 필요로 하는 조각(태그 정규화, LLM 동일사건 판정,
태그 기반 후보 검색)을 하나로 합쳐서, 한쪽만 고치고 나머지가 안 고쳐지는
드리프트(article_store.py·gemini_client.py와 같은 문제의식)를 막는다.

⚠️ 각 파이프라인 고유의 매칭 임계값·다단계 흐름(find_similar_article의
RPC+숫자제거 2차, find_similar_trend의 리드유사도 2~3차 등)은 각자의 사고
이력에 맞춰 튜닝된 값이라 이 모듈로 옮기지 않았다 — 새 신호(태그)와 LLM
판정처럼 "완전히 같은 로직의 중복"만 공용화한다.
"""

import os
import re
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def _sb_url(table: str = "articles") -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def _sb_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


try:
    from nvidia_client import call_nvidia
except Exception:
    def call_nvidia(prompt: str, max_tokens: int = 400, temperature: float = 0.2):
        return None


# ── 원문(RSS) 태그 ───────────────────────────────────────
# country(파이프라인마다 다르게 뽑히거나 아예 안 뽑힘)와 달리 원문 언어
# 그대로 보존되고 판단이 안 섞여 안정적인 매칭 축(2026-09-08, 리퀴드 네트워크
# 해킹 트렌드 기사 중복 실사고 — country="" 사각지대 발견 후 도입).
GENERIC_TAGS = {
    "news", "latest news", "aa news", "press release", "social",
    "crypto news", "market news", "cryptocurrency market news",
    "coins", "markets", "analysis", "opinion", "sponsored",
}

# 다중 단어 태그를 단어 단위로 쪼개 비교할 때(예: "Liquid Network" vs
# "Liquid") 흔한 단어까지 겹침으로 잡으면 무관한 기사끼리도 오탐이 늘어난다.
TAG_WORD_STOPWORDS = {
    "news", "market", "markets", "update", "updates", "latest", "press",
    "release", "daily", "weekly", "report", "crypto", "cryptocurrency",
}


def article_tags(a: dict) -> set:
    """기사 dict의 source_data.tags를 정규화된 소문자 집합으로 반환(범용 버킷 제외)."""
    try:
        raw = ((a.get("source_data") or {}).get("tags")) or []
    except AttributeError:
        return set()
    return {t.strip().lower() for t in raw if t and t.strip().lower() not in GENERIC_TAGS}


def tag_word_set(tags: set) -> set:
    """태그 집합을 단어 단위 토큰 집합으로 변환(소스마다 "Liquid Network"/
    "Liquid"처럼 조금씩 다르게 태깅해도 겹치는 단어가 있으면 같은 대상으로 본다).
    최종 판정은 same_event_llm()이 한 번 더 하므로, 여기서는 후보를 놓치지
    않는 쪽을 우선한다."""
    words = set()
    for t in tags or ():
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            if len(w) >= 3 and w not in TAG_WORD_STOPWORDS:
                words.add(w)
    return words


# ── 숫자 제거 + 부분일치 허용 키워드 비교 (gemini_writer 쪽에서 이식) ──

def strip_numbers(text: str) -> str:
    """사망자 수 등 수치가 바뀌는 후속 보도를 같은 사건으로 보기 위해 숫자를 제거."""
    return re.sub(r"\d+", "", text or "")


def fuzzy_keyword_overlap(kws_a: set, kws_b: set) -> int:
    """완전 일치뿐 아니라 부분 문자열 포함 관계도 겹침으로 센다(2026-09-06
    실사고: "카르그 섬"/"카르그섬"처럼 복합 지명 표기가 갈리면 정확 일치
    기준으로는 겹치는 키워드가 적게 잡혀 중복을 놓쳤다)."""
    used_b = set()
    count = 0
    remaining_a = list(kws_a)
    remaining_b = list(kws_b)
    for wa in remaining_a:
        for wb in remaining_b:
            if wb in used_b:
                continue
            if wa in wb or wb in wa:
                count += 1
                used_b.add(wb)
                break
    return count


# ── LLM 동일사건 최종 판정 (두 파일에 각각 있던 near-identical 함수를 통합) ──

def same_event_llm(title_a: str, body_a: str, title_b: str, body_b: str,
                    gemini_fallback=None) -> bool:
    """두 기사가 같은 실제 사건을 다루는지 LLM에게 직접 묻는다.

    "같은 모델(Gemini)이 기사를 쓰고 같은 모델이 스스로 검증하면 맹점이
    그대로 반복된다"는 문제의식(2026-08-24)에 따라 계열이 다른 Nvidia를
    우선 쓰고, 실패(키 미설정 등)하면 호출부가 넘겨준 자기 Gemini 인스턴스로
    폴백한다(각 스크립트가 자기 GEMINI_API_KEYS 쿼터를 따로 관리하므로 이
    모듈이 직접 Gemini를 호출하지 않는다 — gemini_client.py와 같은 설계).
    """
    prompt = f"""아래 두 기사가 같은 실제 사건(같은 날짜·같은 구체적 사건)을
다루고 있습니까? 단순히 같은 나라·같은 종류의 사건이지만 서로 다른 날짜의
별개 사건이면 "다름"입니다. 정확히 "같음" 또는 "다름" 한 단어만 답하세요.

[기사 A] {title_a}
{(body_a or '')[:600]}

[기사 B] {title_b}
{(body_b or '')[:600]}

답변:"""
    try:
        result = call_nvidia(prompt, max_tokens=10)
        if not result and gemini_fallback:
            result = gemini_fallback(prompt, max_tokens=10, start_tier=3)
    except Exception:
        return False
    return bool(result) and "같음" in result.strip()


def find_by_tags(title: str, body: str, tags: set, hours: int = 72, limit: int = 200,
                  order: str = "desc", gemini_fallback=None) -> dict | None:
    """공유 태그가 있는 최근 발행 NewsFinal 기사 중 LLM이 동일사건으로 확인한
    기사를 반환. 파이프라인(일반/트렌드) 구분 없이 발행된 기사 전체를 뒤진다
    — 이래야 "일반 기사로 먼저 나온 사건을 트렌드 트래커가 또 쓰는" 것처럼
    서로 다른 파이프라인끼리의 중복도 잡는다.

    order="asc"면 가장 오래된(=진행 중인 사건의 루트) 기사를 우선 반환하고
    (트렌드 병합용), "desc"면 가장 최근 기사를 우선 반환한다(단순 중복
    스킵용 — 어느 걸 반환해도 "이미 다뤘다"는 결론은 같다).
    """
    if not tags or not body:
        return None
    tag_words = tag_word_set(tags)
    if not tag_words:
        return None
    since = (_now_kst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(), headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,source_data,created_at",
                "source": "eq.NewsFinal", "is_published": "eq.true",
                "created_at": f"gte.{since}",
                "order": f"id.{order}", "limit": str(limit),
            },
            timeout=10,
        )
        if res.status_code not in (200, 206):
            return None
        for a in res.json():
            shared = tag_words & tag_word_set(article_tags(a))
            if not shared:
                continue
            existing_title = a.get("title_ko") or ""
            existing_body = a.get("summary_ko") or ""
            if not existing_title or not existing_body:
                continue
            if same_event_llm(title, body, existing_title, existing_body, gemini_fallback=gemini_fallback):
                print(f"  [dedup_guard] 태그 공유 {sorted(shared)[:3]} → 동일사건 판정 (id={a['id']}): {existing_title[:40]}")
                return a
    except Exception as e:
        print(f"  [dedup_guard] 태그 기반 조회 실패: {e}")
    return None


def find_by_llm_scan(title: str, body: str, hours: int = 8, limit: int = 20,
                      exclude_country: str | None = None, gemini_fallback=None) -> dict | None:
    """태그도 country도 못 미더울 때 쓰는 최후 안전망 — 최근 시간창 내 발행된
    기사를 전부(또는 exclude_country와 다른 나라만) LLM에게 "같은 사건인지"
    직접 물어본다. 좁은 시간창 + 적은 후보 수로 비용을 제한한다."""
    if not body:
        return None
    since = (_now_kst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(), headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,country,created_at",
                "source": "eq.NewsFinal", "is_published": "eq.true",
                "created_at": f"gte.{since}",
                "order": "id.desc", "limit": str(limit),
            },
            timeout=10,
        )
        if res.status_code not in (200, 206):
            return None
        for a in res.json():
            if exclude_country and (a.get("country") or "") == exclude_country:
                continue
            existing_title = a.get("title_ko") or ""
            existing_body = a.get("summary_ko") or ""
            if not existing_title or not existing_body:
                continue
            if same_event_llm(title, body, existing_title, existing_body, gemini_fallback=gemini_fallback):
                print(f"  [dedup_guard] LLM 좁은창 판정 → 동일사건 (id={a['id']}): {existing_title[:40]}")
                return a
    except Exception as e:
        print(f"  [dedup_guard] LLM 좁은창 조회 실패: {e}")
    return None
