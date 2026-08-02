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


def send_telegram_alert(fixed_list: list):
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID or not fixed_list:
        return
    lines = [f"🔧 이상 음역 자동 교정 {len(fixed_list)}건"]
    for item in fixed_list[:15]:
        lines.append(f"- id={item['id']}: {item['title'][:40]} ({item['applied']})")
    if len(fixed_list) > 15:
        lines.append(f"...외 {len(fixed_list) - 15}건")
    msg = "\n".join(lines)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": msg},
            timeout=15,
        )
    except Exception as e:
        print(f"[WARN] 텔레그램 알림 실패: {e}")


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] Supabase 설정 없음")
        return

    articles = fetch_candidates()
    print(f"[음역 점검] 최근 {CHECK_WINDOW_HOURS}시간 발행 기사 {len(articles)}건 스캔")

    fixed = []
    for a in articles:
        new_title, new_body, applied = apply_fixes(a.get("title_ko"), a.get("summary_ko"))
        if applied:
            update_article(a["id"], new_title, new_body, a.get("update_log"), applied)
            fixed.append({"id": a["id"], "title": new_title, "applied": "; ".join(applied)})
            print(f"  ✅ id={a['id']} 교정: {'; '.join(applied)}")

    if fixed:
        send_telegram_alert(fixed)
        print(f"[음역 점검] 완료 — {len(fixed)}건 교정, 관리자 알림 발송")
    else:
        print("[음역 점검] 완료 — 이상 없음")


if __name__ == "__main__":
    run()
