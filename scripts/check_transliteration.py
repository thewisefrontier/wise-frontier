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

from script_leak import detect_script_leak

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


# Supabase 헤더/URL 헬퍼는 article_store.py로 공용화(2026-09-02, ~14개
# 스크립트에 바이트 단위로 복붙돼 있었음).
try:
    from article_store import sb_headers as _sb_headers, sb_url as _sb_url
except Exception:
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
    # 고유명사 미음역(영문 그대로 방치) — 실측 2026-08-05: 제목 "기니, 국영 Nimba
    # 광산 회사와…"에서 회사명이 음역 안 된 채 남음(본문은 "님바 광산 회사(Nimba
    # Mining Company)"로 정상 처리돼 제목만의 문제였다).
    (r"\bNimba\b", "님바"),
    # 스페인어 직함 미음역 — 실측 2026-08-11(id=67076): "코르도바 주 Gobernadora는"
    # (콜롬비아 초코주 주지사) 처럼 스페인어 여성형 직함이 안 옮겨진 채 남음.
    # "주 Gobernadora"는 "주지사"로 통째 치환(중복 "주" 제거), 단독 등장 시엔
    # 성별 구분 없이 "주지사"로 옮긴다(뉴스 맥락상 성별 병기 불필요).
    (r"주\s*Gobernadora\b", "주지사"),
    (r"\bGobernadora\b", "주지사"),
    (r"주\s*Gobernador\b", "주지사"),
    (r"\bGobernador\b", "주지사"),
    # 영화 "Spider-Man: Brand New Day"(스파이더맨: 브랜드 뉴 데이) 반복 오역.
    # 작품 제목은 인명·지명 음차 규칙이 안 커버하던 영역이라(2026-08-16, id=79327
    # "유니온 오브 어 뉴 데이") 규칙에 추가했지만, 이후에도 다른 형태(id=81850,
    # "드" 음절 탈락)로 재발했다. 순수 한글이라 문자셋 이탈 검출로는 못 잡으므로
    # 안전망으로 등록. "브랜드 뉴 데이" 자체는 건드리지 않도록 "브랜" 뒤에
    # 공백이 바로 오는 경우(="드"가 빠진 경우)만 매칭.
    (r"브랜\s+뉴\s+데이", "브랜드 뉴 데이"),
    (r"유니온\s*오브\s*어\s*뉴\s*데이", "브랜드 뉴 데이"),
    # 이란 혁명수비대(IRGC) 오표기 — 실측 2026-09-04(id=128724, id=114801):
    # "수비대"를 "수위대"로 반복 오기. 한 글자 차이라 문자셋 이탈로도 안
    # 잡히고 사용자 제보로 발견(2회 재발 확인). "수위"는 존재하지 않는
    # 조합이라 다른 맥락과 오탐 겹칠 위험 없음.
    (r"혁명수위대", "혁명수비대"),
]

CHECK_WINDOW_HOURS = 48  # 점검 대상: 최근 N시간 내 발행 기사 (워크플로우 주기보다 여유 있게)


# ── 문자셋 이탈 검출 ───────────────────────────────────────
# detect_script_leak()은 script_leak.py로 이전(공용화) — 저장 시점 하드 블록에서도
# 동일 로직을 쓰기 위함. 이 파일은 배치 스캔 전용으로 계속 여기서 import해 쓴다.
#
# 고유명사 음역 도중 Gemini가 한글 대신 외국 문자를 뱉는 현상.
# True로 바꾸면 검출 기사를 자동 미발행 처리한다.
# 기본값 False — 오류는 고유명사 몇 글자에 한정되고 기사 전체는 유효하므로,
# 알림을 받아 수동 교정하는 편이 트래픽 손실이 적다(5주 12건 규모).
UNPUBLISH_ON_SCRIPT_LEAK = False



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
