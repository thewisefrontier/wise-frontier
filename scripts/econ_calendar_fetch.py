"""
econ_calendar_fetch.py
-----------------------
공식 출처 기반 프론티어 마켓 경제 일정 자동 등록.
Gemini/AI 완전 제거 — 중앙은행 공식 사이트 등 1차 출처만 사용.

[데이터 소스 전략]
1. 중앙은행 금리결정 회의: 각국 중앙은행 공식 사이트에서 연간 일정 확인 후 하드코딩
   (JS 렌더링 기반 사이트가 많아 크롤링 불가, 연 1회 사람이 확인 후 업데이트)
2. IMF WEO 발표: IMF 공식 일정 (매년 4월·10월 고정)
3. 월드뱅크 발표: 공식 일정
4. 향후 추가 가능: 각국 중앙은행 RSS/API 있으면 자동 수집으로 전환 검토

[업데이트 주기]
- 새해 초(1월)에 각국 중앙은행 공식 사이트에서 연간 일정 확인 후 SCHEDULED_EVENTS 업데이트
- 워크플로우는 매주 일요일 실행 — 향후 2주 이내 일정만 DB에 등록

실행: python scripts/econ_calendar_fetch.py
"""

import os
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

# ── 공식 출처 확인 경제 일정 ─────────────────────────────────────────────────
# ⚠️ 새해 초마다 각국 중앙은행 공식 사이트에서 확인 후 업데이트할 것
# 출처 URL은 확인한 공식 페이지 주소를 반드시 기재
#
# 국가 코드 → (이름, 국기, 출처 URL, 중요도)
CENTRAL_BANKS = {
    "NGA": ("나이지리아", "🇳🇬", "https://www.cbn.gov.ng/MonetaryPolicy/calendar.html", "high"),
    "KEN": ("케냐",     "🇰🇪", "https://www.centralbank.go.ke/monetary-policy/",         "high"),
    "ZAF": ("남아공",   "🇿🇦", "https://www.resbank.co.za/en/home/what-we-do/monetary-policy/mpc-statement", "high"),
    "EGY": ("이집트",   "🇪🇬", "https://www.cbe.org.eg/en/monetary-policy",              "high"),
    "VNM": ("베트남",   "🇻🇳", "https://www.sbv.gov.vn/",                                "high"),
    "IDN": ("인도네시아","🇮🇩", "https://www.bi.go.id/en/default.aspx",                   "high"),
    "THA": ("태국",     "🇹🇭", "https://www.bot.or.th/en/our-roles/monetary-policy/mpc-meeting.html", "high"),
    "PHL": ("필리핀",   "🇵🇭", "https://www.bsp.gov.ph/",                                "high"),
}

# 2026년 중앙은행 금리결정 회의 일정
# 출처: 각국 중앙은행 공식 사이트 (2026년 1월 확인)
# ⚠️ 빈칸인 국가는 공식 사이트에서 확인 후 채워넣을 것
SCHEDULED_EVENTS = [
    # ── 나이지리아 CBN MPC ──
    # 출처: https://www.cbn.gov.ng/MonetaryPolicy/calendar.html (직접 확인됨)
    {"date": "2026-07-21", "country": "NGA", "title": "나이지리아 중앙은행(CBN) 통화정책위원회 금리결정", "desc": "MPC 306차 회의 (2일차 발표)", "importance": "high"},
    {"date": "2026-09-22", "country": "NGA", "title": "나이지리아 중앙은행(CBN) 통화정책위원회 금리결정", "desc": "MPC 307차 회의 (2일차 발표)", "importance": "high"},
    {"date": "2026-11-24", "country": "NGA", "title": "나이지리아 중앙은행(CBN) 통화정책위원회 금리결정", "desc": "MPC 308차 회의 (2일차 발표)", "importance": "high"},

    # ── 태국 BOT MPC ──
    # 출처: https://www.bot.or.th/en/our-roles/monetary-policy/mpc-meeting.html
    {"date": "2026-08-05", "country": "THA", "title": "태국 중앙은행(BOT) 통화정책위원회 금리결정", "desc": "2026년 MPC 회의", "importance": "high"},
    {"date": "2026-10-07", "country": "THA", "title": "태국 중앙은행(BOT) 통화정책위원회 금리결정", "desc": "2026년 MPC 회의", "importance": "high"},
    {"date": "2026-12-02", "country": "THA", "title": "태국 중앙은행(BOT) 통화정책위원회 금리결정", "desc": "2026년 MPC 회의", "importance": "high"},

    # ── IMF World Economic Outlook ──
    # 출처: https://www.imf.org/en/Publications/WEO (매년 4월·10월 고정 발표)
    {"date": "2026-10-01", "country": None, "title": "IMF 세계경제전망(WEO) 가을 보고서 발표", "desc": "IMF Annual Meetings 계기 발표. 프론티어 마켓 성장률 전망 포함.", "importance": "high", "flag": "🌐", "name_ko": "글로벌", "source_url": "https://www.imf.org/en/Publications/WEO"},

    # ── 월드뱅크 ──
    # 출처: https://www.worldbank.org/en/publication/global-economic-prospects
    {"date": "2026-01-13", "country": None, "title": "세계은행 세계경제전망(GEP) 보고서 발표", "desc": "Global Economic Prospects — 프론티어/이머징 마켓 경기전망 포함", "importance": "high", "flag": "🌐", "name_ko": "글로벌", "source_url": "https://www.worldbank.org/en/publication/global-economic-prospects"},

    # ── 필리핀 BSP ──
    # 출처: https://www.bsp.gov.ph/ (2026 Calendar of Monetary Policy Meetings 확인)
    {"date": "2026-08-13", "country": "PHL", "title": "필리핀 중앙은행(BSP) 통화위원회 금리결정", "desc": "Bangko Sentral ng Pilipinas 통화정책회의", "importance": "high"},
    {"date": "2026-10-15", "country": "PHL", "title": "필리핀 중앙은행(BSP) 통화위원회 금리결정", "desc": "Bangko Sentral ng Pilipinas 통화정책회의", "importance": "high"},
    {"date": "2026-12-10", "country": "PHL", "title": "필리핀 중앙은행(BSP) 통화위원회 금리결정", "desc": "Bangko Sentral ng Pilipinas 통화정책회의", "importance": "high"},
]


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def get_existing_keys(start_date: str) -> set:
    """중복 방지 — 이미 등록된 (날짜, 제목) 조합"""
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/econ_events",
            headers=_sb_headers(),
            params={"select": "event_date,title", "event_date": f"gte.{start_date}"},
            timeout=15
        )
        if res.status_code in (200, 206):
            return {(e["event_date"], e["title"]) for e in res.json()}
    except Exception as e:
        print(f"[경고] 기존 일정 조회 실패: {e}")
    return set()


def build_events_for_window(days_ahead: int = 90) -> list:
    """
    SCHEDULED_EVENTS에서 오늘 ~ days_ahead일 이내의 일정만 필터링해서 반환.
    매주 실행할 때마다 "곧 다가오는" 일정만 DB에 넣는 방식.
    """
    today = now_kst().date()
    cutoff = today + timedelta(days=days_ahead)
    result = []

    for ev in SCHEDULED_EVENTS:
        try:
            event_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
        except ValueError:
            continue

        # 오늘 이전 일정은 스킵
        if event_date < today:
            continue
        # days_ahead 이후 일정도 스킵
        if event_date > cutoff:
            continue

        # 국가 정보 채우기
        if ev.get("country") and ev["country"] in CENTRAL_BANKS:
            code = ev["country"]
            name_ko, flag, source_url, importance = CENTRAL_BANKS[code]
        else:
            # 글로벌/IMF 등 특수 항목
            name_ko = ev.get("name_ko", "글로벌")
            flag = ev.get("flag", "🌐")
            source_url = ev.get("source_url", "https://www.imf.org")
            importance = ev.get("importance", "medium")

        result.append({
            "event_date": ev["date"],
            "event_time": None,
            "country": name_ko,
            "country_flag": flag,
            "title": ev["title"],
            "importance": importance,
            "description": ev.get("desc", ""),
            "source_url": source_url,
            "is_verified": True,   # 공식 출처로 확인된 일정만 하드코딩 — 항상 즉시 게시
            "source": "official",
        })

    return result


def save_events(events: list) -> int:
    saved = 0
    for ev in events:
        try:
            res = requests.post(
                f"{SUPABASE_URL}/rest/v1/econ_events",
                headers=_sb_headers(),
                json=ev,
                timeout=15
            )
            if res.status_code in (200, 201):
                saved += 1
            else:
                print(f"  ❌ 저장 실패: {ev['title']} — {res.text[:150]}")
        except Exception as e:
            print(f"  ❌ 저장 예외: {ev['title']} — {e}")
    return saved


def send_telegram_notice(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=15
        )
    except Exception as e:
        print(f"[알림 실패] {e}")


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    today_str = now_kst().strftime("%Y-%m-%d")
    print(f"[경제일정] {today_str} 기준 향후 90일 이내 공식 일정 등록 중...")

    # 향후 90일 이내 일정만 처리
    candidates = build_events_for_window(days_ahead=90)
    print(f"[경제일정] 후보 {len(candidates)}건")

    if not candidates:
        print("[경제일정] 향후 90일 이내 등록할 일정 없음")
        return

    # 중복 제거
    existing = get_existing_keys(today_str)
    new_events = [
        ev for ev in candidates
        if (ev["event_date"], ev["title"]) not in existing
    ]

    print(f"[경제일정] 중복 제외 후 신규 {len(new_events)}건")

    if not new_events:
        print("[경제일정] 신규 일정 없음 (모두 기존 등록과 중복)")
        return

    saved = save_events(new_events)
    print(f"✅ {saved}건 저장 완료")

    if saved > 0:
        lines = [f"📅 *경제 일정 자동 등록 완료* — {saved}건"]
        for ev in new_events[:10]:
            lines.append(f"• {ev['event_date']} {ev['country_flag']} {ev['country']} — {ev['title']}")
        if len(new_events) > 10:
            lines.append(f"...외 {len(new_events)-10}건")
        send_telegram_notice("\n".join(lines))


if __name__ == "__main__":
    run()
