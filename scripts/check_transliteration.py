"""
scripts/check_transliteration.py
--------------------------------
알려진 '이상 음역' 패턴(영문+숫자 코드가 한글 발음으로 잘못 옮겨진 경우,
예: "H-1B 비자" → "에치원비자")을 정기적으로 스캔해 자동 교정하고,
교정 내역을 텔레그램 관리자 채널로 알림.

새로 발견되는 오역 패턴은 KNOWN_FIXES 목록에 계속 추가할 것.

실행: python scripts/check_transliteration.py
"""

import os
import re
import requests
from datetime import datetime, timedelta, timezone

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_url():
    return f"{SUPABASE_URL}/rest/v1/articles"


# 알려진 오역 패턴 → 올바른 표기 (정규식, 치환값)
# ⚠️ 새 패턴 발견 시 이 목록에 계속 추가할 것. 오탐 방지를 위해
#    가능하면 "비자" 등 맥락 키워드를 포함해 좁게 잡는다.
KNOWN_FIXES = [
    # 비자 종류
    (r"에치원\s*비자|에이치원비\s*비자", "H-1B 비자"),
    (r"에치투에이\s*비자", "H-2A 비자"),
    (r"에치투비\s*비자", "H-2B 비자"),
    (r"엘원\s*비자", "L-1 비자"),
    (r"엘투\s*비자", "L-2 비자"),
    (r"오원\s*비자", "O-1 비자"),
    (r"제이원\s*비자", "J-1 비자"),
    (r"케이원\s*비자", "K-1 비자"),
    (r"이비파이브\s*비자|이비\s*5\s*비자", "EB-5 비자"),
    # 기술/통신 규격
    (r"파이브지(?=\s|$|[^\w가-힣])", "5G"),
    (r"와이파이식스", "Wi-Fi 6"),
    (r"지피티포(?=\s|$|[^\w가-힣])", "GPT-4"),
    (r"지피티파이브", "GPT-5"),
    # 화학/환경 코드
    (r"씨오투(?=\s|$|[^\w가-힣])", "CO2"),
    (r"피엠투쩜오|피엠이점오", "PM2.5"),
    # 영문 약어를 한글로 풀어 읽은 오표기
    # (음차 규칙의 예외 ③. "AI"는 "아이"가 아니라 알파벳 그대로 두는 것이 표준)
    # 실측 2026-08-03: "오픈아이" 1건 vs 정상 "오픈AI" 10건 — 같은 기사 안에서도 갈렸다
    (r"오픈아이", "오픈AI"),
    (r"엑스에이아이|엑스아이(?=\s|$|[^\w가-힣])", "xAI"),
    # 군사/항공기 모델
    (r"에프서른다섯", "F-35"),
    (r"에프십육", "F-16"),
    (r"비투\s*폭격기", "B-2 폭격기"),
]

CHECK_WINDOW_HOURS = 48  # 점검 대상: 최근 N시간 내 발행 기사 (워크플로우 주기보다 여유 있게)


# ── 문자셋 이탈 검출 ───────────────────────────────────────
# 고유명사 음역 도중 Gemini가 한글 대신 외국 문자를 뱉는 현상.
# 실사례(2026-08-05 실측, 7/1~8/5 발행분 12건):
#   "이스ام 파레스"(Issam Fares) / "가이انا"(가이아나) / "이슬라마باد"(이슬라마바드)
#   "베르나르دو"(Bernardo) / "لندن행"(런던) / "라다크리شن난"(Radhakrishnan)
#
# ⚠️ KNOWN_FIXES와 성격이 다르다. 원형을 코드가 알 수 없으므로 자동 교정이 불가능하다.
#    (`이스ام`의 `ام`이 "삼"인지 "사무"인지 판별 불가) → 검출·알림만 하고 교정은 수동.
#
# ⚠️ 그리스 문자는 제외한다. 수식·과학 기사에서 정상 사용된다(α, β, π).
#
# 한자(CJK)는 별도 처리한다. "고(故)", "대(對)중국", "시진핑(習近平)"처럼
# 괄호 안 병기는 한국 언론의 정상 관행이므로, 괄호 구간을 제거한 뒤에도
# 남는 한자만 오류로 본다. 실측(2026-08-05, 7/20 이후) 이 방식으로 오탐 0건:
#   "전离층"(전리층) / "윈드폴 稅"(세) / "카贝요"(Cabello) / "카르나타카州"(주)
#   "1万2341대"(1만) / "페塔尔"(페타르) / "駐屯 중인"(주둔) / "사회事务"(사무)
_SCRIPT_LEAK_RANGES = [
    ("아랍", r"\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF"),
    ("히브리", r"\u0590-\u05FF"),
    ("키릴", r"\u0400-\u04FF"),
    ("태국", r"\u0E00-\u0E7F"),
    ("데바나가리", r"\u0900-\u097F"),
    ("벵골", r"\u0980-\u09FF"),
    ("타밀", r"\u0B80-\u0BFF"),
]

_SCRIPT_LEAK_RE = [
    (name, re.compile(r"[" + rng + r"]"), re.compile(r".{0,14}[" + rng + r"]+.{0,14}"))
    for name, rng in _SCRIPT_LEAK_RANGES
]

# 괄호 병기 구간. 20자 상한은 긴 괄호가 통째로 지워져 오류를 놓치는 것을 막는다.
_PAREN_RE = re.compile(r"\([^)]{0,20}\)")
_CJK_PROBE = re.compile(r"[\u4E00-\u9FFF]")
_CJK_CTX = re.compile(r".{0,14}[\u4E00-\u9FFF]+.{0,14}")

# True로 바꾸면 검출 기사를 자동 미발행 처리한다.
# 기본값 False — 오류는 고유명사 몇 글자에 한정되고 기사 전체는 유효하므로,
# 알림을 받아 수동 교정하는 편이 트래픽 손실이 적다(5주 12건 규모).
UNPUBLISH_ON_SCRIPT_LEAK = False


def detect_script_leak(title: str, body: str):
    """비허용 문자셋 혼입 검출. [(스크립트명, 문맥), ...] 반환."""
    hits = []
    for field in (title or "", body or ""):
        if not field:
            continue
        for name, probe, ctx_re in _SCRIPT_LEAK_RE:
            if not probe.search(field):
                continue
            for m in ctx_re.finditer(field):
                snippet = m.group(0).replace("\n", " ").strip()
                if snippet and all(snippet != h[1] for h in hits):
                    hits.append((name, snippet))

        # 한자는 괄호 병기를 걷어낸 뒤 남는 것만 오류로 본다
        if _CJK_PROBE.search(field):
            stripped = _PAREN_RE.sub("", field)
            for m in _CJK_CTX.finditer(stripped):
                snippet = m.group(0).replace("\n", " ").strip()
                if snippet and all(snippet != h[1] for h in hits):
                    hits.append(("한자", snippet))
    return hits



def fetch_candidates() -> list:
    since = (now_kst() - timedelta(hours=CHECK_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,update_log",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "created_at": f"gte.{since}",
                "limit": "500",
            },
            timeout=30,
        )
        if res.status_code in (200, 206):
            return res.json()
    except Exception as e:
        print(f"[ERROR] 기사 조회 실패: {e}")
    return []


def apply_fixes(title: str, body: str):
    applied = []
    new_title, new_body = title or "", body or ""
    for pattern, repl in KNOWN_FIXES:
        if re.search(pattern, new_title) or re.search(pattern, new_body):
            new_title = re.sub(pattern, repl, new_title)
            new_body = re.sub(pattern, repl, new_body)
            applied.append(f"{pattern} → {repl}")
    return new_title, new_body, applied


def update_article(article_id: int, title: str, body: str, existing_log: list, applied: list):
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    new_log = (existing_log or []) + [{
        "timestamp": now_str,
        "note": f"음역 자동 교정: {'; '.join(applied)}",
    }]
    try:
        requests.patch(
            f"{_sb_url()}?id=eq.{article_id}",
            headers=_sb_headers(),
            json={"title_ko": title, "summary_ko": body, "update_log": new_log},
            timeout=15,
        )
    except Exception as e:
        print(f"[ERROR] id={article_id} 업데이트 실패: {e}")


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


def send_telegram_alert(fixed_list: list):
    if not fixed_list:
        return
    lines = [f"🔧 이상 음역 자동 교정 {len(fixed_list)}건"]
    for item in fixed_list[:15]:
        lines.append(f"- id={item['id']}: {item['title'][:40]} ({item['applied']})")
    if len(fixed_list) > 15:
        lines.append(f"...외 {len(fixed_list) - 15}건")
    _send_telegram("\n".join(lines))


def send_script_leak_alert(leak_list: list):
    """문자셋 이탈은 자동 교정이 불가능하므로 수동 교정용 문맥을 함께 보낸다."""
    if not leak_list:
        return
    head = "⚠️ 문자셋 이탈 %d건 (수동 교정 필요)" % len(leak_list)
    if UNPUBLISH_ON_SCRIPT_LEAK:
        head += " — 자동 미발행 처리함"
    lines = [head]
    for item in leak_list[:15]:
        lines.append(f"- id={item['id']} [{item['scripts']}] {item['ctx']}")
    if len(leak_list) > 15:
        lines.append(f"...외 {len(leak_list) - 15}건")
    lines.append("https://newsfinal.co.kr/article?id=" + str(leak_list[0]["id"]))
    _send_telegram("\n".join(lines))


def unpublish_article(article_id: int, existing_log: list, reason: str):
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    new_log = (existing_log or []) + [{"timestamp": now_str, "note": reason}]
    try:
        requests.patch(
            f"{_sb_url()}?id=eq.{article_id}",
            headers=_sb_headers(),
            json={"is_published": False, "update_log": new_log},
            timeout=15,
        )
    except Exception as e:
        print(f"[ERROR] id={article_id} 미발행 처리 실패: {e}")



def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] Supabase 설정 없음")
        return

    articles = fetch_candidates()
    print(f"[음역 점검] 최근 {CHECK_WINDOW_HOURS}시간 발행 기사 {len(articles)}건 스캔")

    fixed = []
    leaked = []
    for a in articles:
        new_title, new_body, applied = apply_fixes(a.get("title_ko"), a.get("summary_ko"))
        if applied:
            update_article(a["id"], new_title, new_body, a.get("update_log"), applied)
            fixed.append({"id": a["id"], "title": new_title, "applied": "; ".join(applied)})
            print(f"  ✅ id={a['id']} 교정: {'; '.join(applied)}")

        # 문자셋 이탈 — 교정본 기준으로 검사한다(치환이 문자를 걷어냈을 수 있다)
        hits = detect_script_leak(new_title, new_body)
        if hits:
            scripts = ",".join(sorted({h[0] for h in hits}))
            ctx = " | ".join(h[1] for h in hits[:3])
            leaked.append({"id": a["id"], "scripts": scripts, "ctx": ctx})
            print(f"  ⚠️ id={a['id']} 문자셋 이탈[{scripts}]: {ctx}")
            if UNPUBLISH_ON_SCRIPT_LEAK:
                log = a.get("update_log")
                if applied:      # 방금 update_article이 로그를 한 줄 늘렸다
                    log = (log or []) + [{"timestamp": now_kst().strftime("%Y-%m-%d %H:%M"),
                                          "note": f"음역 자동 교정: {'; '.join(applied)}"}]
                unpublish_article(a["id"], log, f"문자셋 이탈 미발행[{scripts}]: {ctx[:120]}")

    if fixed:
        send_telegram_alert(fixed)
        print(f"[음역 점검] 완료 — {len(fixed)}건 교정, 관리자 알림 발송")
    else:
        print("[음역 점검] 완료 — 이상 없음")

    if leaked:
        send_script_leak_alert(leaked)
        print(f"[문자셋 점검] {len(leaked)}건 검출, 관리자 알림 발송")
    else:
        print("[문자셋 점검] 이상 없음")


if __name__ == "__main__":
    run()
