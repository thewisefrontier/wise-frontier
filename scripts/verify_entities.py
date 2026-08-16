"""
verify_entities.py
-------------------
이미 발행된 NewsFinal 기사를 순회하며, 본문에 등장하는 고유명사(작품명·인명·지명·
기관명 등)가 실제로 존재하는 정확한 이름인지 구글 검색 그라운딩으로 점검한다.

실사고(2026-08-16, id=79327): 영화 "Brand New Day"를 "유니온 오브 어 뉴 데이"로
완전히 잘못 옮긴 채 발행됐다. gemini_writer.py의 call_gemini_article()에 생성
시점 검증을 넣어 신규 기사는 막았지만(2026-08-16), 이미 발행된 5천여 건은 사람이
우연히 발견해야만 잡히는 상태였다. 이 스크립트는 그 백로그를 조금씩 훑어
entity_review 테이블에 점검 결과를 쌓고, 의심되는 것만 텔레그램 관리자 채널로
요약 알림한다. 완전한 탐지는 불가능하다 — 이것도 결국 또 다른 LLM 판단이라
놓치는 경우가 있을 수 있다. "우연히 발견"을 "매일 조금씩 자동으로 훑어서
검토 목록을 쌓는다"로 바꾸는 것이 목표다.

실행: python scripts/verify_entities.py
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

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

BATCH_SIZE = 40        # 1회 실행당 점검 건수 — 검색 그라운딩 호출은 느리고 비용이 크다
CALL_INTERVAL = 4      # 호출 간 대기(초)
MAX_BODY_CHARS = 2500
CANDIDATE_OVERFETCH = 8  # 이미 점검된 기사를 걸러내기 위한 여유분 배수

KST = timezone(timedelta(hours=9))

_current_key_idx = 0
_exhausted_keys = {m: set() for m in GEMINI_MODELS}


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def call_gemini(prompt: str, max_tokens: int = 300, use_search: bool = True, start_tier: int = 2) -> str | None:
    global _current_key_idx, _exhausted_keys
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens},
    }
    if use_search:
        payload["tools"] = [{"google_search": {}}]

    n = len(GEMINI_API_KEYS)
    model_stages = [(m, _exhausted_keys[m]) for m in GEMINI_MODELS[start_tier:]]

    for model, exhausted in model_stages:
        available = [i for i in range(n) if i not in exhausted]
        if not available:
            print(f"  [{model}] 모든 키 RPD 소진 → 다음 모델로")
            continue

        ordered = sorted(available, key=lambda i: (i - _current_key_idx) % n)

        for idx in ordered:
            api_key = GEMINI_API_KEYS[idx]
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                res = requests.post(url, json=payload, timeout=(10, 45))
                if res.status_code == 200:
                    _current_key_idx = (idx + 1) % n
                    cands = res.json().get("candidates", [])
                    if not cands:
                        return None
                    parts = cands[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts).strip()
                    return text if text else None
                elif res.status_code == 429:
                    print(f"  [429] {model} 키 {idx+1} RPD 소진 — 블랙리스트")
                    exhausted.add(idx)
                    continue
                elif res.status_code == 503:
                    print(f"  [503] {model} 키 {idx+1} 과부하 → 다음 키")
                    continue
                else:
                    print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                    return None
            except requests.exceptions.Timeout:
                print(f"  [TIMEOUT] {model} 키 {idx+1} → 다음 키")
                continue
            except Exception as e:
                print(f"[ERROR] {e}")
                continue

    print("[ERROR] 모든 모델/키 소진 또는 응답 없음")
    return None


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

        result = call_gemini(build_check_prompt(title, body))
        suspect = parse_check_result(result)
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
