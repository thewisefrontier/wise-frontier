"""
scripts/backfill_value_add.py
------------------------------
기존 발행 기사 중 summary_3lines/investment_idea가 비어있는 기사에
Gemini로 "3줄 요약"과 "투자 아이디어"만 새로 생성해 채워 넣는 백필 스크립트.
본문(summary_ko)은 건드리지 않고, 기존 제목·본문을 근거로만 생성.

실행: python scripts/backfill_value_add.py
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone

# 합쇼체 방어 — gemini_writer의 변환기를 재사용한다(패턴 A).
# 이 스크립트에는 방어가 없어 백필된 summary_3lines/investment_idea에
# "-습니다" 체가 그대로 쌓이고 있었다(2026-07-29 실측).
try:
    from gemini_writer import has_polite_ending, to_plain_style
except Exception:  # import 실패해도 백필 자체는 계속돼야 한다
    def has_polite_ending(t):
        return False

    def to_plain_style(t):
        return t

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

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

BATCH_SIZE = 200     # 1회 실행당 처리 건수 (Actions 스케줄 스킵 보완용 상향, 2026-07-30)
CALL_INTERVAL = 1    # 호출 간 대기(초)
MAX_BODY_CHARS = 3000  # 본문이 너무 길면 토큰 절약을 위해 앞부분만 사용

KST = timezone(timedelta(hours=9))

_current_key_idx = 0
_exhausted_keys = {m: set() for m in GEMINI_MODELS}  # 모델별 RPD 소진 키


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def _sb_url():
    return f"{SUPABASE_URL}/rest/v1/articles"


def call_gemini(prompt: str, max_tokens: int = 500, start_tier: int = 3):
    """gemini_summarizer.py의 call_gemini()와 동일한 키 로테이션·폴백 구조."""
    global _current_key_idx
    if not GEMINI_API_KEYS:
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens},
    }
    n = len(GEMINI_API_KEYS)
    stages = [(m, _exhausted_keys[m]) for m in GEMINI_MODELS[start_tier:]]

    for model, exhausted in stages:
        available = [i for i in range(n) if i not in exhausted]
        if not available:
            continue
        ordered = sorted(available, key=lambda i: (i - _current_key_idx) % n)
        for idx in ordered:
            api_key = GEMINI_API_KEYS[idx]
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                res = requests.post(url, json=payload, timeout=(10, 30))
                if res.status_code == 200:
                    _current_key_idx = (idx + 1) % n
                    _cand = res.json()["candidates"][0]
                    # maxOutputTokens 초과로 잘린 응답을 정상 취급하면 문장이 중간에서
                    # 끊긴 채 저장된다(gemini_writer.py 실사고 id=47879와 동일 계열).
                    _finish = _cand.get("finishReason", "")
                    if _finish and _finish != "STOP":
                        print(f"  [WARN] {model} 응답 비정상 종료(finishReason={_finish}) — 폐기")
                        return None
                    return _cand["content"]["parts"][0]["text"].strip()
                elif res.status_code == 429:
                    print(f"  [429] {model} 키 {idx+1} RPD 소진")
                    exhausted.add(idx)
                    continue
                elif res.status_code == 503:
                    continue
                else:
                    print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                    return None
            except Exception as e:
                print(f"[ERROR] 호출 실패: {e}")
                continue
    return None


def fetch_batch() -> list:
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "or": "(summary_3lines.is.null,summary_3lines.eq.)",
                "order": "id.desc",
                "limit": str(BATCH_SIZE),
            },
            timeout=20,
        )
        if res.status_code in (200, 206):
            return res.json()
    except Exception as e:
        print(f"[ERROR] 대상 조회 실패: {e}")
    return []


def build_prompt(title: str, body: str) -> str:
    return f"""아래는 이미 작성·발행된 기사입니다. 본문은 절대 수정하지 말고, 이 기사를 위한 "3줄 요약"과 "투자 아이디어"만 새로 작성하세요.

제목: {title}

본문:
{body}

[3줄요약 작성 규칙]
기사 핵심을 정확히 3줄로 요약하세요. 각 줄은 "\\n"으로 구분된 완결된 문장이며, 각 줄은 40자 내외로 간결하게 쓰세요.

[투자아이디어 작성 규칙]
"투자 아이디어"라는 이름이지만 매수/매도를 권유하는 게 아니라, 이 사안이 시장·산업에 미치는 함의를 분석하는 글입니다. 3~5문장으로 작성하세요. 다음 요소를 최대한 포함하되 실제 근거 있는 것만 쓰세요:
1. 메커니즘: 이 사건이 구체적으로 어떤 경로를 거쳐 다른 곳(시장·산업·공급망)에 영향을 미치는지
2. 규모 가늠: 본문에 나온 수치나 비율을 활용해 이 사안의 크기·비중을 가늠하게 하세요
3. 선례 비교 또는 시나리오: 과거 유사 사례의 전개·결과, 혹은 향후 전개 시나리오
4. 한국 연관성: 한국과 실제 연관(무역·공급망·원자재·환율·한국 기업 진출 등)이 있으면 어떤 품목·업종·기업이 영향받는지 구체적으로. 연관이 약하면 억지로 갖다 붙이지 말고 3번으로 대체하세요.
"~에 영향을 미칠 것으로 보인다", "주목할 필요가 있다" 같은 막연한 상투 문구는 절대 금지 — 구체적 인과관계·수치·비교를 담으세요.

아래 형식으로만 출력하세요(다른 텍스트·설명·마크다운 금지):
3줄요약: (내용)
투자아이디어: (내용)"""


_LABELS = ["3줄요약:", "투자아이디어:"]


def _extract(text: str, label: str) -> str:
    lines = text.strip().split("\n")
    start_idx = None
    first_val = ""
    for i, line in enumerate(lines):
        if line.startswith(label):
            start_idx = i
            first_val = line[len(label):].strip()
            break
    if start_idx is None:
        return ""
    collected = [first_val] if first_val else []
    for line in lines[start_idx + 1:]:
        if any(line.startswith(lbl) for lbl in _LABELS):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def parse_response(text: str):
    return _extract(text, "3줄요약:"), _extract(text, "투자아이디어:")


def update_article(article_id: int, summary_3lines: str, investment_idea: str) -> bool:
    if detect_script_leak(summary_3lines, investment_idea):
        print(f"  ⚠️ [문자 혼입 감지] id={article_id} 업데이트 차단")
        return False
    # 저장 직전 최종 방어. 프롬프트가 지켜지지 않아도 DB에는 '-다' 체만 들어간다.
    if has_polite_ending(summary_3lines) or has_polite_ending(investment_idea):
        print(f"  🔧 id={article_id} 합쇼체 감지 → 자동 변환")
        summary_3lines = to_plain_style(summary_3lines)
        investment_idea = to_plain_style(investment_idea)
    try:
        res = requests.patch(
            f"{_sb_url()}?id=eq.{article_id}",
            headers=_sb_headers(),
            json={"summary_3lines": summary_3lines, "investment_idea": investment_idea},
            timeout=15,
        )
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[ERROR] id={article_id} 업데이트 실패: {e}")
        return False


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] Supabase 설정 없음")
        return
    if not GEMINI_API_KEYS:
        print("[SKIP] Gemini API 키 없음")
        return

    batch = fetch_batch()
    print(f"[백필] 이번 회차 대상 {len(batch)}건")

    done = 0
    for a in batch:
        title = a.get("title_ko") or ""
        body = (a.get("summary_ko") or "")[:MAX_BODY_CHARS]
        if not title or not body:
            continue

        content = call_gemini(build_prompt(title, body), max_tokens=500)
        if not content:
            print(f"  ⚠️ id={a['id']} 생성 실패")
            continue

        s3, inv = parse_response(content)
        if not s3 and not inv:
            print(f"  ⚠️ id={a['id']} 파싱 실패")
            continue

        if update_article(a["id"], s3, inv):
            done += 1
            print(f"  ✅ id={a['id']} 백필 완료")

        time.sleep(CALL_INTERVAL)

    print(f"[백필] 완료 — {done}/{len(batch)}건 처리")


if __name__ == "__main__":
    run()
