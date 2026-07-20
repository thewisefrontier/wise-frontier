"""
econ_calendar_fetch.py
-----------------------
공식 출처 기반 프론티어 마켓 경제 일정 자동 등록.
Gemini/AI 완전 제거 — 공식 기관 사이트 직접 크롤링 또는 공식 확인된 일정만 사용.

[전략]
1. 크롤링 가능 기관 (정적 HTML): 직접 파싱 — 나이지리아 CBN, 케냐 CBK, 인도네시아 BI
2. JS 렌더링 기관 (크롤링 불가): 연초에 공식 사이트에서 확인한 연간 일정 → STATIC_EVENTS로 관리
3. 국제기구(IMF/WB): 고정 일정이라 하드코딩

[업데이트 주기]
- 워크플로우: 매월 1일 실행 (월 1회)
- STATIC_EVENTS: 새해 초(1월)에 각국 공식 사이트 확인 후 업데이트
"""

import os
import re
import time
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

HEADERS_HTML = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0; +https://newsfinal.co.kr)",
    "Accept": "text/html,application/xhtml+xml",
}

# ── 국가별 발표 시각 매핑 ────────────────────────────────────────────
# event_time: 발표 현지시간 HH:MM
# timezone: IANA 타임존
# announcement_offset_hours: event_date 기준 발표일 오프셋(일). 당일=0, 2일차=1, 3일차=2
COUNTRY_ANNOUNCEMENT_META = {
    "나이지리아": {"event_time": "15:00", "timezone": "Africa/Lagos",        "announcement_offset_hours": 1},
    "케냐":       {"event_time": "12:00", "timezone": "Africa/Nairobi",       "announcement_offset_hours": 0},
    "인도네시아": {"event_time": "14:00", "timezone": "Asia/Jakarta",         "announcement_offset_hours": 0},
    "태국":       {"event_time": "14:00", "timezone": "Asia/Bangkok",         "announcement_offset_hours": 0},
    "필리핀":     {"event_time": "14:30", "timezone": "Asia/Manila",          "announcement_offset_hours": 0},
    "남아공":     {"event_time": "14:00", "timezone": "Africa/Johannesburg",  "announcement_offset_hours": 2},
    "이집트":     {"event_time": "13:00", "timezone": "Africa/Cairo",         "announcement_offset_hours": 0},
}

def _apply_announcement_meta(ev: dict) -> dict:
    """이벤트 딕셔너리에 국가별 발표 시각 메타 자동 주입"""
    meta = COUNTRY_ANNOUNCEMENT_META.get(ev.get("country", ""))
    if meta:
        ev["event_time"] = meta["event_time"]
        ev["timezone"] = meta["timezone"]
        ev["announcement_offset_hours"] = meta["announcement_offset_hours"]
    return ev

# ── 크롤링 불가 기관 — 공식 확인된 연간 일정 (태국·필리핀·남아공·이집트·IMF·WB)
# ⚠️ 새해 초(1월)에 각국 공식 사이트 확인 후 업데이트
# 형식: (날짜, 국가코드, 국기, 국가명, 제목, 설명, 출처URL)
STATIC_EVENTS = [
    # ── 태국 BOT MPC (출처: bot.or.th/en/our-roles/monetary-policy/mpc-meeting.html)
    ("2026-08-05", "🇹🇭", "태국", "태국 중앙은행(BOT) 통화정책위원회 금리결정", "2026년 MPC 회의", "https://www.bot.or.th/en/our-roles/monetary-policy/mpc-meeting.html", "high"),
    ("2026-10-07", "🇹🇭", "태국", "태국 중앙은행(BOT) 통화정책위원회 금리결정", "2026년 MPC 회의", "https://www.bot.or.th/en/our-roles/monetary-policy/mpc-meeting.html", "high"),
    ("2026-12-02", "🇹🇭", "태국", "태국 중앙은행(BOT) 통화정책위원회 금리결정", "2026년 MPC 회의", "https://www.bot.or.th/en/our-roles/monetary-policy/mpc-meeting.html", "high"),

    # ── 필리핀 BSP (출처: bsp.gov.ph 2026 Calendar of Monetary Policy Meetings)
    ("2026-08-13", "🇵🇭", "필리핀", "필리핀 중앙은행(BSP) 통화위원회 금리결정", "Bangko Sentral ng Pilipinas 통화정책회의", "https://www.bsp.gov.ph/", "high"),
    ("2026-10-15", "🇵🇭", "필리핀", "필리핀 중앙은행(BSP) 통화위원회 금리결정", "Bangko Sentral ng Pilipinas 통화정책회의", "https://www.bsp.gov.ph/", "high"),
    ("2026-12-10", "🇵🇭", "필리핀", "필리핀 중앙은행(BSP) 통화위원회 금리결정", "Bangko Sentral ng Pilipinas 통화정책회의", "https://www.bsp.gov.ph/", "high"),

    # ── 남아공 SARB MPC (출처: resbank.co.za)
    ("2026-07-23", "🇿🇦", "남아공", "남아공 중앙은행(SARB) 통화정책위원회 금리결정", "2026년 MPC 회의 결과 발표", "https://www.resbank.co.za/en/home/what-we-do/monetary-policy/mpc-statement", "high"),
    ("2026-09-17", "🇿🇦", "남아공", "남아공 중앙은행(SARB) 통화정책위원회 금리결정", "2026년 MPC 회의 결과 발표", "https://www.resbank.co.za/en/home/what-we-do/monetary-policy/mpc-statement", "high"),
    ("2026-11-19", "🇿🇦", "남아공", "남아공 중앙은행(SARB) 통화정책위원회 금리결정", "2026년 MPC 회의 결과 발표", "https://www.resbank.co.za/en/home/what-we-do/monetary-policy/mpc-statement", "high"),

    # ── 이집트 CBE MPC (출처: cbe.org.eg)
    ("2026-07-24", "🇪🇬", "이집트", "이집트 중앙은행(CBE) 통화정책위원회 금리결정", "2026년 CBE MPC 회의", "https://www.cbe.org.eg/en/monetary-policy", "high"),
    ("2026-09-25", "🇪🇬", "이집트", "이집트 중앙은행(CBE) 통화정책위원회 금리결정", "2026년 CBE MPC 회의", "https://www.cbe.org.eg/en/monetary-policy", "high"),
    ("2026-11-26", "🇪🇬", "이집트", "이집트 중앙은행(CBE) 통화정책위원회 금리결정", "2026년 CBE MPC 회의", "https://www.cbe.org.eg/en/monetary-policy", "high"),

    # ── IMF WEO (매년 4월·10월 고정)
    ("2026-10-01", "🌐", "글로벌", "IMF 세계경제전망(WEO) 가을 보고서 발표", "Annual Meetings 계기. 프론티어 마켓 성장률 전망 포함.", "https://www.imf.org/en/Publications/WEO", "high"),

    # ── 세계은행 GEP (1월·6월 고정)
    ("2027-01-01", "🌐", "글로벌", "세계은행 세계경제전망(GEP) 보고서 발표", "Global Economic Prospects — 프론티어/이머징 마켓 경기전망 포함", "https://www.worldbank.org/en/publication/global-economic-prospects", "high"),
]


# ── 1. 나이지리아 CBN 크롤링 ────────────────────────────────────────────
def fetch_cbn_nigeria() -> list:
    """나이지리아 CBN MPC 일정 — 정적 HTML 테이블 파싱"""
    url = "https://www.cbn.gov.ng/MonetaryPolicy/calendar.html"
    events = []
    try:
        res = requests.get(url, headers=HEADERS_HTML, timeout=20)
        if res.status_code != 200:
            print(f"  ⚠️ CBN 크롤링 실패: {res.status_code}")
            return []

        # "Day 2" 날짜 추출 (공식 결과 발표일)
        # 형식: "Jul. 20, 2026" 또는 "Jul. 21, 2026"
        pattern = re.compile(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.\s+(\d{1,2}),\s+(\d{4})')
        matches = pattern.findall(res.text)
        month_map = {
            'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04',
            'May': '05', 'Jun': '06', 'Jul': '07', 'Aug': '08',
            'Sep': '09', 'Oct': '10', 'Nov': '11', 'Dec': '12',
        }

        # Day 2 (결과 발표일)를 추출 — 테이블에서 짝수 번째가 Day 2
        dates = []
        for m, d, y in matches:
            date_str = f"{y}-{month_map[m]}-{d.zfill(2)}"
            dates.append(date_str)

        # 짝수 인덱스(1, 3, 5...)가 Day 2
        day2_dates = dates[1::2] if len(dates) > 1 else dates

        for date_str in day2_dates:
            events.append(_apply_announcement_meta({
                "event_date": date_str,
                "event_time": None,
                "country": "나이지리아",
                "country_flag": "🇳🇬",
                "title": "나이지리아 중앙은행(CBN) 통화정책위원회 금리결정",
                "importance": "high",
                "description": "CBN MPC 회의 2일차 — 금리 결정 발표",
                "source_url": url,
                "is_verified": True,
                "source": "official_crawl",
            }))
        print(f"  ✅ CBN(나이지리아): {len(events)}건 파싱")
    except Exception as e:
        print(f"  ⚠️ CBN 크롤링 예외: {e}")
    return events


# ── 2. 케냐 CBK 크롤링 ────────────────────────────────────────────────
def fetch_cbk_kenya() -> list:
    """케냐 CBK — 'Next MPC Meeting' 페이지에서 다음 일정 파싱"""
    url = "https://www.centralbank.go.ke/mpc/"
    events = []
    try:
        res = requests.get(url, headers=HEADERS_HTML, timeout=20)
        if res.status_code != 200:
            print(f"  ⚠️ CBK 크롤링 실패: {res.status_code}")
            return []

        # "next mpc meeting" 링크 또는 날짜 텍스트 추출
        # 형식: "June 9, 2026" 또는 "Tuesday, June 9, 2026"
        pattern = re.compile(
            r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
            r'(January|February|March|April|May|June|July|August|September|October|November|December)'
            r'\s+(\d{1,2}),?\s+(\d{4})',
            re.IGNORECASE
        )
        month_map = {
            'january': '01', 'february': '02', 'march': '03', 'april': '04',
            'may': '05', 'june': '06', 'july': '07', 'august': '08',
            'september': '09', 'october': '10', 'november': '11', 'december': '12',
        }

        today = now_kst().date()
        found = set()
        for m, d, y in pattern.findall(res.text):
            date_str = f"{y}-{month_map[m.lower()]}-{d.zfill(2)}"
            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if event_date >= today and date_str not in found:
                found.add(date_str)
                events.append(_apply_announcement_meta({
                    "event_date": date_str,
                    "event_time": None,
                    "country": "케냐",
                    "country_flag": "🇰🇪",
                    "title": "케냐 중앙은행(CBK) 통화정책위원회 금리결정",
                    "importance": "high",
                    "description": "CBK MPC 회의 결과 발표",
                    "source_url": "https://www.centralbank.go.ke/mpc/",
                    "is_verified": True,
                    "source": "official_crawl",
                }))
        print(f"  ✅ CBK(케냐): {len(events)}건 파싱")
    except Exception as e:
        print(f"  ⚠️ CBK 크롤링 예외: {e}")
    return events


# ── 3. 인도네시아 BI 크롤링 ────────────────────────────────────────────
def fetch_bi_indonesia() -> list:
    """인도네시아 Bank Indonesia — 연간 RDG 일정 페이지 파싱"""
    url = "https://www.bi.go.id/en/ruang-media/agenda/rapat-dewan-gubernur/Default.aspx"
    events = []
    try:
        res = requests.get(url, headers=HEADERS_HTML, timeout=20)
        if res.status_code != 200:
            print(f"  ⚠️ BI 크롤링 실패: {res.status_code}")
            return []

        # 날짜 형식: "20-21 July 2026" 또는 "20 July 2026"
        pattern = re.compile(
            r'(\d{1,2})(?:-(\d{1,2}))?\s+'
            r'(January|February|March|April|May|June|July|August|September|October|November|December)'
            r'\s+(\d{4})',
            re.IGNORECASE
        )
        month_map = {
            'january': '01', 'february': '02', 'march': '03', 'april': '04',
            'may': '05', 'june': '06', 'july': '07', 'august': '08',
            'september': '09', 'october': '10', 'november': '11', 'december': '12',
        }

        today = now_kst().date()
        found = set()
        for d1, d2, m, y in pattern.findall(res.text):
            # 2일 회의면 마지막 날(d2)이 결과 발표일
            day = d2 if d2 else d1
            date_str = f"{y}-{month_map[m.lower()]}-{day.zfill(2)}"
            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if event_date >= today and date_str not in found:
                found.add(date_str)
                events.append(_apply_announcement_meta({
                    "event_date": date_str,
                    "event_time": None,
                    "country": "인도네시아",
                    "country_flag": "🇮🇩",
                    "title": "인도네시아 중앙은행(BI) 이사회 회의 금리결정",
                    "importance": "high",
                    "description": "Bank Indonesia Board of Governors Meeting (RDG) — 금리 결정 발표",
                    "source_url": url,
                    "is_verified": True,
                    "source": "official_crawl",
                }))
        print(f"  ✅ BI(인도네시아): {len(events)}건 파싱")
    except Exception as e:
        print(f"  ⚠️ BI 크롤링 예외: {e}")
    return events


# ── 4. 정적 일정 처리 ────────────────────────────────────────────────
def build_static_events() -> list:
    """STATIC_EVENTS에서 향후 90일 이내 + 오늘 이후 일정만 반환"""
    today = now_kst().date()
    cutoff = today + timedelta(days=90)
    events = []
    for date_str, flag, country, title, desc, source_url, importance in STATIC_EVENTS:
        try:
            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if event_date < today or event_date > cutoff:
            continue
        events.append(_apply_announcement_meta({
            "event_date": date_str,
            "event_time": None,
            "country": country,
            "country_flag": flag,
            "title": title,
            "importance": importance,
            "description": desc,
            "source_url": source_url,
            "is_verified": True,
            "source": "official_static",
        }))
    return events


# ── Supabase 헬퍼 ────────────────────────────────────────────────────
def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def get_existing_keys(start_date: str) -> set:
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
        print(f"  ⚠️ 기존 일정 조회 실패: {e}")
    return set()


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
            print(f"  ❌ 저장 예외: {e}")
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
    print(f"[경제일정] {today_str} 기준 향후 90일 이내 공식 일정 수집 중...")

    # 크롤링 (나이지리아, 케냐, 인도네시아)
    all_events = []
    all_events += fetch_cbn_nigeria()
    time.sleep(1)
    all_events += fetch_cbk_kenya()
    time.sleep(1)
    all_events += fetch_bi_indonesia()
    time.sleep(1)

    # 향후 90일 이내만 필터 (크롤링 결과도 필터)
    today = now_kst().date()
    cutoff = today + timedelta(days=90)
    all_events = [
        ev for ev in all_events
        if today <= datetime.strptime(ev["event_date"], "%Y-%m-%d").date() <= cutoff
    ]

    # 정적 일정 (크롤링 불가 기관)
    all_events += build_static_events()
    print(f"[경제일정] 전체 후보 {len(all_events)}건")

    if not all_events:
        print("[경제일정] 향후 90일 이내 등록할 일정 없음")
        return

    # 중복 제거
    existing = get_existing_keys(today_str)
    new_events = [
        ev for ev in all_events
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
