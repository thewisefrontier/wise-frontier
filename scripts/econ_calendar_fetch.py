"""
econ_calendar_fetch.py
-----------------------
주 1회 실행: Gemini의 웹검색 기반 추론으로 프론티어 마켓 주요 경제 일정 초안을 조사해
econ_events 테이블에 "검수 대기"(is_verified=false) 상태로 저장.
완료 후 텔레그램으로 관리자에게 알림 발송.

⚠️ AI가 조사한 일정은 부정확할 수 있으므로 자동 게시하지 않고,
   Admin > 경제 일정 관리에서 사람이 검수 후 발행해야 함.

실행: python scripts/econ_calendar_fetch.py
권장 주기: 매주 1회 (예: 매주 일요일)
"""

import os
import re
import time
import requests
from dotenv import load_dotenv

load_dotenv()

GEMINI_MODEL = "gemini-3.1-flash-lite"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")  # 관리자 알림용 (개인 chat_id 또는 채널)

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
] if k]

_current_key_idx = 0


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def call_gemini_with_search(prompt, max_tokens=4000):
    """Gemini 웹검색 도구(google_search) 사용 — 최신 일정 정보 조사용"""
    global _current_key_idx
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens},
    }

    while _current_key_idx < len(GEMINI_API_KEYS):
        api_key = GEMINI_API_KEYS[_current_key_idx]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={api_key}"
        )
        try:
            res = requests.post(url, json=payload, timeout=(10, 60))
            if res.status_code == 200:
                data = res.json()
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts)
                return text.strip()
            elif res.status_code == 429:
                print(f"  [429] 키 {_current_key_idx+1} 한도 초과 → 전환")
                _current_key_idx += 1
            else:
                print(f"[ERROR] Gemini {res.status_code}: {res.text[:300]}")
                return None
        except requests.exceptions.Timeout:
            print(f"  [TIMEOUT] 키 {_current_key_idx+1}")
            return None
        except Exception as e:
            print(f"[ERROR] {e}")
            return None

    print("[ERROR] 모든 키 소진")
    return None


def build_calendar_prompt():
    today = time.strftime("%Y년 %m월 %d일")
    return f"""오늘은 {today}입니다. 웹검색을 활용해 향후 14일간(오늘부터 2주) 예정된
프론티어 마켓(아프리카, 동남아시아, 중앙아시아, 중동, 남아시아, 라틴아메리카, 카리브해 국가)의
주요 경제 일정을 조사하세요.

조사 대상:
- 중앙은행 기준금리 결정 회의
- 대통령/총리 선거, 국회의원 선거
- 주요 경제지표 발표 (GDP, 물가상승률, 무역수지 등 국가 차원에서 중요한 것만)
- IMF/World Bank 등 국제기구의 해당국 관련 주요 발표

[출력 규칙 — 매우 중요]
- 검수자가 사실을 확인할 수 있도록, 각 일정마다 반드시 검색 결과에서 확인한 실제 출처 URL을 포함하세요.
- 출처 URL을 명확히 확인할 수 없는 일정은 절대 포함하지 마세요. "아마 ~일 것이다" 같은 추측은 제외하세요.
- 각 일정을 한 줄씩, 아래 형식의 파이프(|)로 구분된 데이터로만 출력하세요. 다른 설명 텍스트는 쓰지 마세요.
- 형식: 날짜(YYYY-MM-DD)|시간(HH:MM 또는 빈칸)|국가|국기이모지|제목|중요도(high/medium/low)|간단설명(1문장, 없으면 빈칸)|출처URL(필수, https://로 시작하는 전체 URL)
- 예시:
2026-06-25|14:00|나이지리아|🇳🇬|중앙은행 통화정책위원회 기준금리 결정|high|인플레이션 압력에 따른 금리 동결 여부 주목|https://www.cbn.gov.ng/MonetaryPolicy/
2026-06-28||케냐|🇰🇪|대통령 선거 1차 투표|high||https://www.iebc.or.ke/
- 일정이 없으면 "NONE"만 출력하세요.
- 최대 20개까지만 출력하세요."""


def parse_events(text):
    if not text or text.strip() == "NONE":
        return []
    events = []
    skipped_no_source = 0
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 8:
            # 출처 URL 필드(8번째)가 없는 줄은 검수 불가능하므로 통째로 스킵
            skipped_no_source += 1
            continue
        try:
            date_str = parts[0].strip()
            # 날짜 형식 검증
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                continue
            source_url = parts[7].strip()
            # 출처 URL이 http(s)로 시작하지 않으면 검수 불가능 — 제외
            if not source_url.startswith("http"):
                skipped_no_source += 1
                continue
            events.append({
                "event_date": date_str,
                "event_time": parts[1].strip() or None,
                "country": parts[2].strip(),
                "country_flag": parts[3].strip(),
                "title": parts[4].strip(),
                "importance": parts[5].strip() if parts[5].strip() in ("high", "medium", "low") else "medium",
                "description": parts[6].strip() if len(parts) > 6 else "",
                "source_url": source_url,
                "is_verified": False,
                "source": "gemini",
            })
        except Exception:
            continue
    if skipped_no_source:
        print(f"[경제일정 조사] 출처 URL 없어 제외된 항목: {skipped_no_source}건")
    return events


def get_existing_titles(start_date):
    """중복 방지용 — 이미 등록된 (날짜, 제목) 조합 확인"""
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/econ_events",
        headers=_sb_headers(),
        params={"select": "event_date,title", "event_date": f"gte.{start_date}"},
        timeout=15
    )
    if res.status_code in (200, 206):
        return {(e["event_date"], e["title"]) for e in res.json()}
    return set()


def save_events(events):
    saved = 0
    for ev in events:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/econ_events",
            headers=_sb_headers(),
            json=ev,
            timeout=15
        )
        if res.status_code in (200, 201):
            saved += 1
        else:
            print(f"  ❌ 저장 실패: {ev['title']} — {res.text[:100]}")
    return saved


def send_telegram_notice(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        print("[알림 스킵] TELEGRAM_ADMIN_CHAT_ID 미설정")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_ADMIN_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=15)
    except Exception as e:
        print(f"[알림 실패] {e}")


def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    print("[경제일정 조사] Gemini 웹검색으로 향후 2주 일정 조사 중...")
    prompt = build_calendar_prompt()
    raw = call_gemini_with_search(prompt)

    if not raw:
        print("[ERROR] Gemini 응답 없음")
        send_telegram_notice("⚠️ 경제 일정 자동조사 실패 — Gemini 응답 없음. 로그를 확인해주세요.")
        return

    events = parse_events(raw)
    print(f"[경제일정 조사] {len(events)}건 파싱됨")

    if not events:
        print("[경제일정 조사] 신규 일정 없음")
        send_telegram_notice("📅 이번 주 경제 일정 자동조사: 새로 발견된 일정이 없습니다.")
        return

    today = time.strftime("%Y-%m-%d")
    existing = get_existing_titles(today)
    new_events = [e for e in events if (e["event_date"], e["title"]) not in existing]

    print(f"[경제일정 조사] 중복 제외 후 {len(new_events)}건 신규")

    if not new_events:
        send_telegram_notice("📅 이번 주 경제 일정 자동조사: 신규 일정 없음 (모두 기존 등록과 중복).")
        return

    saved = save_events(new_events)
    print(f"✅ {saved}건 저장 완료 (검수 대기 상태)")

    # 텔레그램 알림 — 검수 요청 (출처 링크 포함)
    lines = [f"📅 *이번 주 경제 일정 자동조사 완료*", f"신규 {saved}건이 검수 대기 상태로 등록됐습니다.\n"]
    for ev in new_events[:10]:
        lines.append(f"• {ev['event_date']} {ev['country_flag']} {ev['country']} — {ev['title']}")
        if ev.get('source_url'):
            lines.append(f"  [출처]({ev['source_url']})")
    if len(new_events) > 10:
        lines.append(f"...외 {len(new_events)-10}건")
    lines.append(f"\n각 일정의 출처 링크를 확인 후, Admin > 경제 일정 관리에서 검수해주세요.")
    send_telegram_notice("\n".join(lines))


if __name__ == "__main__":
    run()
