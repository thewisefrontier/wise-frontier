"""
verify_entities.py
-------------------
이미 발행된 NewsFinal 기사를 순회하며, 본문에 등장하는 고유명사(작품명·인명·지명·
기관명 등)가 실제로 존재하는 정확한 이름인지 점검한다. 두 가지 신호를 같이 쓴다:
  1) 위키피디아 독립 조회 — 판단이 아니라 단순 추출(위험도 낮음)로 고유명사 후보를
     뽑은 뒤, 결정론적 HTTP 조회로 실존 여부를 확인. Gemini 판단을 거치지 않으므로
     Gemini가 오판(예: "없음")해도 이 신호는 별도로 남는다.
  2) Gemini 검색 그라운딩 판단 — 기존 방식. 위키 신호를 보조하는 2차 신호로 격하.
"LLM 혼자만의 판단은 위험하다"(2026-08-16 사용자 피드백)는 이유로 위키 신호를
1차로 두고, Gemini 판단에만 전적으로 의존하지 않도록 설계했다.

실사고(2026-08-16, id=79327): 영화 "Brand New Day"를 "유니온 오브 어 뉴 데이"로
완전히 잘못 옮긴 채 발행됐다. gemini_writer.py의 call_gemini_article()에 생성
시점 검증을 넣어 신규 기사는 막았지만(2026-08-16), 이미 발행된 5천여 건은 사람이
우연히 발견해야만 잡히는 상태였다. 이 스크립트는 그 백로그를 조금씩 훑어
entity_review 테이블에 점검 결과를 쌓고, 의심되는 것만 텔레그램 관리자 채널로
요약 알림한다. 완전한 탐지는 불가능하다 — "우연히 발견"을 "매일 조금씩 자동으로
훑어서 검토 목록을 쌓는다"로 바꾸는 것이 목표다.

실행: python scripts/verify_entities.py
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

BATCH_SIZE = 500       # 1회 실행당 점검 건수 — lite 티어(RPD 500)로 내려서 여유 확보
CALL_INTERVAL = 4      # 호출 간 대기(초)
MAX_BODY_CHARS = 2500
CANDIDATE_OVERFETCH = 8  # 이미 점검된 기사를 걸러내기 위한 여유분 배수

KST = timezone(timedelta(hours=9))

try:
    from gemini_client import GeminiClient
except Exception:
    class GeminiClient:  # import 실패해도 본 기능이 죽지 않도록 폴백을 둔다
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            return None

_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def call_gemini(prompt: str, max_tokens: int = 300, use_search: bool = True, start_tier: int = 3) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.1, timeout=(10, 45), use_search=use_search)


def fetch_checked_ids() -> set:
    """entity_review에 이미 점검 기록이 있는 article_id 전체 조회(페이지네이션)."""
    ids = set()
    offset = 0
    batch = 1000
    while True:
        try:
            res = requests.get(
                f"{SUPABASE_URL}/rest/v1/entity_review",
                headers={**_sb_headers(), "Range": f"{offset}-{offset+batch-1}"},
                params={"select": "article_id"},
                timeout=20,
            )
        except Exception as e:
            print(f"[ERROR] 점검 이력 조회 실패: {e}")
            break
        if res.status_code not in (200, 206):
            break
        data = res.json()
        if not data:
            break
        ids.update(r["article_id"] for r in data)
        if len(data) < batch:
            break
        offset += batch
    return ids


def fetch_candidates(checked_ids: set, limit: int) -> list:
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/articles",
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "order": "id.desc",
                "limit": str(limit * CANDIDATE_OVERFETCH),
            },
            timeout=20,
        )
        if res.status_code not in (200, 206):
            return []
        data = res.json()
    except Exception as e:
        print(f"[ERROR] 대상 조회 실패: {e}")
        return []

    out = [a for a in data if a.get("id") not in checked_ids]
    return out[:limit]


def build_extract_prompt(title: str, body: str) -> str:
    """판단이 아니라 단순 추출 — LLM 위험도가 낮은 작업이라 위키 조회 대상을 뽑는 데만 쓴다."""
    return f"""아래 기사에서 실제 존재 여부를 확인해볼 만한 구체적 고유명사를 추출하세요.
영화·도서·게임 등 작품명, 특정 인물 실명, 특정 기관·단체·기업명만 대상으로 합니다.
국가명·일반 지명(도시·나라)이나 흔한 일반명사·직함은 제외하세요.
쉼표로 구분해 나열만 하세요(설명 금지). 대상이 없으면 "없음"이라고만 답하세요.

제목: {title}

본문:
{body}

답변:"""


def extract_candidate_names(title: str, body: str) -> list:
    result = call_gemini(build_extract_prompt(title, body), max_tokens=150, use_search=False, start_tier=3)
    if not result:
        return []
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return []
    names = [n.strip() for n in result.split(",") if n.strip() and len(n.strip()) >= 2]
    return names[:15]


def build_check_prompt(title: str, body: str) -> str:
    return f"""아래는 뉴스파이널에 이미 발행된 기사입니다. 본문에 등장하는 고유명사
(영화·도서·게임 등 작품명, 인명, 지명, 기관명, 인용된 발언자 등)가 실제로 존재하는
정확한 이름인지 웹 검색으로 확인하세요.

정상적인 한글 음차나 공식 번역명 표기는 문제가 아닙니다 — 실제로 존재하지 않거나,
실존 대상과 이름 자체가 다르게 틀린 경우만 찾으세요.

문제가 있으면 "기사 속 표기 → 올바른 표기" 형식으로 쉼표 구분해 나열하세요.
모두 문제없으면 "없음"이라고만 답하세요.

제목: {title}

본문:
{body}

답변:"""


def parse_check_result(result: str | None) -> str:
    """문제 없으면 빈 문자열, 의심되면 원문 그대로 반환."""
    if not result:
        return ""
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return ""
    return result


# ── 위키피디아 교차 검증 ──────────────────────────────────────
# Gemini의 검색 그라운딩 판단도 결국 또 다른 LLM 판단이라 그 자체가 틀릴 수 있다.
# 여기에 실제 위키피디아 문서 존재 여부(결정론적 조회)를 덧붙여, 검토자가
# "LLM 혼자 의심한 것"과 "실제 문서로 뒷받침되는 것"을 구분할 수 있게 한다.
# 단, "위키에 없음"이 곧 "가짜"는 아니다 — 무명 인물·소규모 기관은 원래도
# 문서가 없다. 그래서 판정을 뒤집지 않고 참고 신호로만 suspect_names에 덧붙인다.
def wikipedia_lookup(name: str, langs=("ko", "en")) -> list:
    name = (name or "").strip()
    if not name:
        return []
    titles = []
    for lang in langs:
        try:
            res = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "opensearch",
                    "search": name,
                    "limit": 3,
                    "namespace": 0,
                    "format": "json",
                },
                headers={"User-Agent": "NewsFinal-EntityCheck/1.0 (+https://newsfinal.co.kr)"},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if len(data) >= 2 and isinstance(data[1], list):
                    titles.extend(data[1])
        except Exception:
            continue
    return titles


def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
    """이름과 충분히 비슷한 위키 문서 제목이 하나라도 있으면 True."""
    titles = wikipedia_lookup(name)
    return any(fuzz.token_sort_ratio(name, t) >= threshold for t in titles)


_SUSPECT_PAIR_RE = re.compile(r"([^,→\n]+?)\s*→\s*([^,\n]+)")


def wiki_cross_check(suspect: str) -> str:
    """'지어낸이름 → 원본표기' 쌍마다 위키 조회 결과를 덧붙인 문자열 반환.
    조회 실패해도 원래 suspect는 그대로 살아있어야 하므로 예외를 삼킨다."""
    if not suspect:
        return suspect
    try:
        pairs = _SUSPECT_PAIR_RE.findall(suspect)
        if not pairs:
            return suspect
        notes = []
        for wrong, correct in pairs[:5]:
            wrong, correct = wrong.strip(), correct.strip()
            wrong_found = wikipedia_confirms(wrong)
            correct_found = wikipedia_confirms(correct)
            if wrong_found and not correct_found:
                notes.append(f"[위키: '{wrong}'만 실존 확인 — 오탐 가능성 있음]")
            elif correct_found and not wrong_found:
                notes.append(f"[위키: '{correct}' 실존 확인됨 — 지적 신뢰도 높음]")
            elif not wrong_found and not correct_found:
                notes.append(f"[위키: 둘 다 문서 없음 — 무명 고유명사일 수 있어 참고만]")
            else:
                notes.append(f"[위키: 둘 다 문서 있음 — 동명이인/이표기 가능성, 수동 확인 필요]")
        if notes:
            return suspect + "\n" + "\n".join(notes)
    except Exception as e:
        print(f"  [WARN] 위키 교차검증 실패(무시): {e}")
    return suspect


def save_review(article_id: int, status: str, suspect_names: str) -> bool:
    try:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/entity_review",
            headers=_sb_headers(),
            json={"article_id": article_id, "status": status, "suspect_names": suspect_names or None},
            timeout=15,
        )
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"[ERROR] id={article_id} 점검 결과 저장 실패: {e}")
        return False


def _send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID or not msg:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception as e:
        print(f"[WARN] 텔레그램 알림 실패: {e}")


def send_flagged_alert(flagged: list):
    if not flagged:
        return
    lines = [f"🔍 고유명사 의심 기사 {len(flagged)}건 발견 (entity_review 확인 필요)"]
    for item in flagged[:15]:
        lines.append(f"- id={item['id']}: {item['title'][:40]}\n  → {item['suspect'][:100]}")
    if len(flagged) > 15:
        lines.append(f"...외 {len(flagged) - 15}건")
    _send_telegram("\n".join(lines))


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] Supabase 설정 없음")
        return
    if not GEMINI_API_KEYS:
        print("[SKIP] Gemini API 키 없음")
        return

    checked_ids = fetch_checked_ids()
    print(f"[고유명사 점검] 기존 점검 완료 {len(checked_ids)}건")

    batch = fetch_candidates(checked_ids, BATCH_SIZE)
    print(f"[고유명사 점검] 이번 회차 대상 {len(batch)}건")

    checked = 0
    flagged = []
    for a in batch:
        title = a.get("title_ko") or ""
        body = (a.get("summary_ko") or "")[:MAX_BODY_CHARS]
        if not title or not body:
            continue

        # 1차: 위키 독립 조회 — Gemini 판단을 거치지 않고 결정론적으로 실존 확인.
        # Gemini가 "없음"이라고 오판해도 이 신호는 그와 무관하게 별도로 남는다.
        candidates = extract_candidate_names(title, body)
        time.sleep(1)
        wiki_unconfirmed = [n for n in candidates if not wikipedia_confirms(n)]

        # 2차: Gemini 검색 그라운딩 판단 (기존 방식, 보조 신호로 격하)
        result = call_gemini(build_check_prompt(title, body))
        suspect = parse_check_result(result)
        if suspect:
            suspect = wiki_cross_check(suspect)

        if wiki_unconfirmed:
            note = "[위키 독립조회 미확인 — 위키에서 실존을 확인 못한 고유명사] " + ", ".join(wiki_unconfirmed)
            suspect = (suspect + "\n" + note) if suspect else note

        status = "pending" if suspect else "clean"

        if save_review(a["id"], status, suspect):
            checked += 1
            if suspect:
                print(f"  ⚠️ id={a['id']} 의심: {suspect[:100]}")
                flagged.append({"id": a["id"], "title": title, "suspect": suspect})
            else:
                print(f"  ✅ id={a['id']} 이상 없음")

        time.sleep(CALL_INTERVAL)

    print(f"[고유명사 점검] 완료 — {checked}/{len(batch)}건 점검, {len(flagged)}건 의심")
    send_flagged_alert(flagged)


if __name__ == "__main__":
    run()
