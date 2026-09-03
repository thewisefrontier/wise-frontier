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

try:
    from nvidia_client import call_nvidia
except Exception:
    def call_nvidia(*a, **k):
        return None


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


# Supabase 헤더 헬퍼는 article_store.py로 공용화(2026-09-02).
try:
    from article_store import sb_headers as _sb_headers
except Exception:
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
    return f"""아래는 뉴스파이널에 이미 발행된 기사입니다. 아래 두 가지를 확인하세요.

[1. 고유명사 정확성] 본문에 등장하는 고유명사(영화·도서·게임 등 작품명, 인명, 지명,
기관명, 인용된 발언자 등)가 실제로 존재하는 정확한 이름인지 웹 검색으로 확인하세요.
정상적인 한글 음차나 공식 번역명 표기는 문제가 아닙니다 — 실제로 존재하지 않거나,
실존 대상과 이름 자체가 다르게 틀린 경우만 찾으세요.

[2. 현지화 누락] 아래 세 가지가 있으면 찾으세요(2026-08-21 id=90665·90457 실사고로 추가):
- 한글로 옮기지 않고 영문 그대로 남은 외국 기업명(비자종류·통화코드·모델명·한국 기업
  그룹 약칭·OpenAI 같은 영문 약어 포함 명칭은 정상이니 제외)
- crore(1천만)·lakh(10만) 같은 인도식 단위가 "Rs 1.34 crore"처럼 한국어 억/만 단위로
  환산되지 않고 원문 그대로 남은 경우
- 제목이 "~돌파 속 기념하는 ~의 날"처럼 영어 원문 어순을 그대로 옮긴 듯 부자연스러운 경우

[3. 수식어 날조] 실존하는 일반명사·집단명(예: "아디바시", "원주민", "노동자") 앞에 실제
보도에는 없는 수식어나 설명이 붙어 있는지 검색으로 확인하세요(예: "PreferredSource
Adivasis"처럼 명사 자체는 실존해도 그 앞의 꾸밈말이 지어낸 것인 경우 — 2026-08-25
id=98010 실사고). 검색으로 그런 수식어가 실제 보도에 쓰인 적이 있는지 확인하고, 근거를
못 찾으면 지적하세요.

문제가 있으면 항목별로 "[분류] 기사 속 표기 → 올바른 표기(또는 지적 사유)" 형식으로
쉼표 구분해 나열하세요. 분류는 [고유명사]/[미음차 기업명]/[단위 미환산]/[제목 어색함]/
[수식어 날조] 중 하나를 쓰세요. 모두 문제없으면 "없음"이라고만 답하세요.

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
    조회 실패해도 원래 suspect는 그대로 살아있어야 하므로 예외를 삼킨다.
    [단위 미환산]/[제목 어색함]/[수식어 날조]는 실존 여부 문제가 아니라 위키 조회
    대상이 아니므로 건너뛴다(2026-08-21 현지화 점검 항목 추가와 함께 도입,
    2026-08-25 수식어 날조 추가)."""
    if not suspect:
        return suspect
    try:
        pairs = _SUSPECT_PAIR_RE.findall(suspect)
        if not pairs:
            return suspect
        notes = []
        for wrong, correct in pairs[:5]:
            wrong, correct = wrong.strip(), correct.strip()
            if (wrong.startswith("[단위 미환산]") or wrong.startswith("[제목 어색함]")
                    or wrong.startswith("[수식어 날조]")):
                continue
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


# ── NVIDIA(Nemotron) 2차 교차검증 ─────────────────────────────
# "같은 모델(Gemini)이 쓰고 같은 모델이 검증하면 맹점이 그대로 반복된다"
# (2026-08-24 사용자 지적) — 다른 모델 계열로 독립 재검토를 받는다.
# 무료 티어 크레딧이 한정적이라(월 1,000) Gemini가 실제로 의심을 확정한
# 소수 건에만 쓴다. wiki_unconfirmed 단독 신호(무명 고유명사라 위키에 없을
# 뿐인 경우가 많은 노이즈 신호)는 여기 대상이 아니다 — Gemini 판단이
# 실제로 있어야만 호출한다(사용자 결정: "1차 모델을 더욱 고도화시켜서
# 최대한 나오는 걸 줄여야지").
def nvidia_cross_check(title: str, body: str, gemini_suspect: str) -> str:
    if not gemini_suspect:
        return ""
    prompt = f"""다른 AI 모델이 아래 기사에서 다음과 같은 문제를 지적했습니다. 이
지적이 실제로 타당한지 기사 본문과 대조해 독립적으로 재검토하세요. 같은 모델이
쓰고 같은 모델이 검증하면 놓치는 게 있을 수 있어 다른 모델의 시각으로 재확인하는
것입니다.

[제목]
{title}

[본문]
{body[:2000]}

[다른 모델의 지적]
{gemini_suspect}

각 지적 항목에 대해 "동의" 또는 "동의 안 함(이유)"으로만 짧게 답하세요. 전부
동의하면 "전부 동의"라고만 답하세요."""
    result = call_nvidia(prompt, max_tokens=400)
    if not result:
        return ""
    return f"[Nemotron 교차검증] {result.strip()}"


# 2026-09-03 사용자 지적: "2주간 6건이면 아예 안 쓰는 수준이나 다름없다" —
# 기존엔 Gemini가 이미 의심 표시한 기사만 엔비디아로 넘겨서(1550건 중 6건),
# Gemini 자체가 놓친 오류는 영원히 검증되지 않았다. Gemini가 "문제없음"이라고
# 판단한 기사 중 일부도 실행마다 정해진 예산 안에서 독립적으로 엔비디아에
# 넘겨 Gemini의 사각지대를 잡는다. 무료 크레딧(월 1000, 이 스크립트는 하루
# 1회 실행)을 넘지 않도록 실행당 상한을 둔다 — 여유를 크게 남겨 다른 용도와도
# 부딪히지 않게 한다.
NVIDIA_INDEPENDENT_BUDGET = 25


def build_nvidia_independent_prompt(title: str, body: str) -> str:
    # call_nvidia는 검색 그라운딩이 없다(자체 지식 기반) — Gemini의 1차 점검
    # (build_check_prompt, 웹 검색 사용)과 상호보완적인 2번째 독립 판단으로
    # 쓴다. 확신 없는 항목까지 다 걸러내려 하지 않고, 자체 지식으로 봤을 때
    # 뚜렷이 이상한 것만 짚게 한다(과다 오탐 방지).
    return f"""아래는 이미 발행된 뉴스 기사입니다. 다른 AI 모델(Gemini)이 이미 한 번
검토해 "문제없음"으로 판단한 기사인데, 같은 모델의 판단만 믿는 건 위험하므로
독립적인 2차 검토를 요청합니다.

본문에 등장하는 고유명사(인명·지명·기관명·작품명 등)나 수치·인용문 중,
당신이 아는 지식 기준으로 명백히 잘못됐거나 실존하지 않는다고 확신하는
항목이 있으면 지적하세요. 확신이 없는 항목은 지적하지 마세요(과다 지적
방지) — 검색 없이 당신의 지식만으로 판단하는 것이니 애매하면 넘어가세요.

문제가 있으면 "[분류] 기사 속 표기 → 지적 사유" 형식으로 쉼표 구분해
나열하세요. 없으면 "없음"이라고만 답하세요.

제목: {title}

본문:
{body[:2000]}

답변:"""


def nvidia_independent_check(title: str, body: str) -> str:
    result = call_nvidia(build_nvidia_independent_prompt(title, body), max_tokens=300)
    if not result:
        return ""
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return ""
    return f"[Nemotron 독립검토] {result}"


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
    nvidia_independent_used = 0
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
        gemini_suspect = parse_check_result(result)
        suspect = gemini_suspect
        if suspect:
            suspect = wiki_cross_check(suspect)
            # 3차: 다른 모델 계열(Nemotron)로 독립 재검토. wiki_unconfirmed 단독
            # 신호는 노이즈가 많아 대상에서 제외 — Gemini가 실제로 뭔가 지적했을
            # 때만 크레딧 한정적인 이 호출을 쓴다.
            nvidia_note = nvidia_cross_check(title, body, gemini_suspect)
            if nvidia_note:
                suspect = suspect + "\n" + nvidia_note
            time.sleep(2)
        elif nvidia_independent_used < NVIDIA_INDEPENDENT_BUDGET:
            # Gemini가 "문제없음"으로 판단한 기사도 예산 안에서 독립 재검토
            nvidia_independent_used += 1
            nvidia_note = nvidia_independent_check(title, body)
            if nvidia_note:
                suspect = nvidia_note
            time.sleep(2)

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
