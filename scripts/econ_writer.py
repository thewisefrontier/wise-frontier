"""
econ_writer.py
--------------
econ_events 테이블의 이벤트 중 actual_value가 없고,
발표 예정 시각(현지시간 기준)이 이미 지난 항목을 대상으로
Gemini 검색을 두 번 독립 호출해 결과를 검증한 뒤 actual_value 업데이트 및
기사를 자동 생성합니다.

실행: python scripts/econ_writer.py
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

# ── 설정 ────────────────────────────────────────────────────
GEMINI_MODEL_PRIMARY  = "gemini-3.5-flash-lite"
GEMINI_MODEL_FALLBACK = "gemini-3.1-flash-lite"
SUPABASE_URL         = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

_current_key_idx = 0
_exhausted_keys_primary  = set()  # RPD 소진 키 (3.5)
_exhausted_keys_fallback = set()  # RPD 소진 키 (3.1)

# 발표 시각 이후 얼마나 지나야 수집 시작하는지 (오류 방지 버퍼)
ANNOUNCEMENT_BUFFER_MINUTES = 30

# actual_value 없는 이벤트를 최대 며칠 전까지 소급 처리할지
MAX_LOOKBACK_DAYS = 5

# 두 Gemini 응답 숫자 허용 오차
RATE_TOLERANCE = 0.01


# ── Supabase 헬퍼 ────────────────────────────────────────────
def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _sb_articles_url():
    return f"{SUPABASE_URL}/rest/v1/articles"

def _sb_events_url():
    return f"{SUPABASE_URL}/rest/v1/econ_events"


# ── 발표 시각 체크 ───────────────────────────────────────────
def announcement_has_passed(event: dict) -> bool:
    """
    이벤트의 발표 예정 시각(현지시간)이 현재 UTC 기준으로 이미 지났는지 확인.
    - event_date + announcement_offset_hours(일) + event_time(HH:MM) → 현지 naive datetime
    - timezone 컬럼으로 변환 → UTC 비교
    - timezone/event_time 없으면 True(당일이면 처리 허용)
    """
    tz_str   = event.get("timezone")
    ev_time  = event.get("event_time")   # "HH:MM"
    ev_date  = event.get("event_date")   # "YYYY-MM-DD"
    offset_d = event.get("announcement_offset_hours") or 0  # 발표일 오프셋(일)

    if not tz_str or not ev_time or not ev_date:
        return True

    try:
        tz        = ZoneInfo(tz_str)
        base_date = date.fromisoformat(ev_date)
        ann_date  = base_date + timedelta(days=offset_d)
        hh, mm    = map(int, ev_time.split(":"))
        ann_local = datetime(ann_date.year, ann_date.month, ann_date.day,
                             hh, mm, tzinfo=tz)
        ann_with_buffer = ann_local + timedelta(minutes=ANNOUNCEMENT_BUFFER_MINUTES)
        return datetime.now(timezone.utc) >= ann_with_buffer.astimezone(timezone.utc)
    except Exception as e:
        print(f"    [WARN] 시각 변환 오류 ({e}) → 스킵 처리")
        return False


# ── 대상 이벤트 조회 ─────────────────────────────────────────
def get_pending_events() -> list:
    """
    actual_value IS NULL 이고 event_date가 MAX_LOOKBACK_DAYS일 이내인 이벤트 조회.
    발표 시각 경과 여부는 Python에서 필터링.
    """
    today     = now_kst().date()
    date_from = (today - timedelta(days=MAX_LOOKBACK_DAYS)).isoformat()
    date_to   = today.isoformat()

    res = requests.get(
        f"{_sb_events_url()}"
        f"?select=id,title,event_date,event_time,timezone,announcement_offset_hours,"
        f"country,importance,previous_value,forecast_value,actual_value,description"
        f"&actual_value=is.null"
        f"&event_date=gte.{date_from}"
        f"&event_date=lte.{date_to}"
        f"&order=event_date.asc",
        headers=_sb_headers(),
        timeout=15,
    )
    if res.status_code not in (200, 206):
        print(f"[ERROR] 이벤트 조회 실패 {res.status_code}")
        return []

    all_events = res.json()
    passed  = [e for e in all_events if announcement_has_passed(e)]
    skipped = len(all_events) - len(passed)
    if skipped:
        print(f"  → {skipped}건 발표 시각 미경과로 스킵")
    return passed


# ── Gemini 호출 (키 로테이션) ────────────────────────────────
def call_gemini(prompt: str, max_tokens: int = 800,
                use_search: bool = False) -> str | None:
    global _current_key_idx, _exhausted_keys_primary, _exhausted_keys_fallback

    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max_tokens,
        },
    }
    if use_search:
        payload["tools"] = [{"google_search": {}}]

    n = len(GEMINI_API_KEYS)
    model_stages = [
        (GEMINI_MODEL_PRIMARY,  _exhausted_keys_primary),
        (GEMINI_MODEL_FALLBACK, _exhausted_keys_fallback),
    ]

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
                    text  = "".join(p.get("text", "") for p in parts).strip()
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
                return None

    print("[ERROR] 모든 모델/키 소진 또는 응답 없음")
    return None


# ── 숫자 파싱 ────────────────────────────────────────────────
def parse_rate(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{1,4})?)\s*%", text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m2 = re.search(r"\b(\d{1,2}\.\d{1,4})\b", text)
    if m2:
        try:
            v = float(m2.group(1))
            if 0 <= v <= 50:
                return v
        except ValueError:
            pass
    return None


def rates_match(r1: float, r2: float) -> bool:
    return abs(r1 - r2) <= RATE_TOLERANCE


# ── 검색 프롬프트 ────────────────────────────────────────────
def build_search_prompt(event: dict) -> str:
    title      = event.get("title", "")
    country    = event.get("country", "")
    event_date = str(event.get("event_date", ""))
    prev       = event.get("previous_value") or "N/A"
    forecast   = event.get("forecast_value") or "N/A"

    return (
        f"다음 중앙은행 금리결정 이벤트의 실제 결정 금리(actual policy rate)를 검색해 알려주세요.\n\n"
        f"이벤트: {title}\n"
        f"국가: {country}\n"
        f"예정일: {event_date}\n"
        f"직전 금리: {prev}\n"
        f"예상 금리: {forecast}\n\n"
        f"오직 결정된 실제 금리 숫자(%)만 답변하세요. "
        f"결과가 아직 발표되지 않았다면 '미발표'라고만 답하세요. "
        f"다른 설명은 불필요합니다."
    )


# ── 기사 생성 프롬프트 ───────────────────────────────────────
def build_article_prompt(event: dict, actual_value: str) -> str:
    title      = event.get("title", "")
    country    = event.get("country", "")
    event_date = str(event.get("event_date", ""))
    prev       = event.get("previous_value") or "N/A"
    forecast   = event.get("forecast_value") or "N/A"
    desc       = event.get("description") or ""

    try:
        av = float(actual_value.replace("%", ""))
        pv = float(str(prev).replace("%", "")) if prev != "N/A" else None
        if pv is not None:
            direction = "인상" if av > pv else ("인하" if av < pv else "동결")
        else:
            direction = "결정"
    except Exception:
        direction = "결정"

    return f"""당신은 프론티어 마켓 전문 경제 뉴스 기자입니다.
아래 정보를 바탕으로 한국어 뉴스 스타일 기사를 작성하세요.

[이벤트 정보]
- 이벤트명: {title}
- 국가: {country}
- 예정일: {event_date}
- 결정 금리: {actual_value} ({direction})
- 직전 금리: {prev}
- 예상 금리: {forecast}
- 추가 설명: {desc}

[작성 규칙]
1. 제목(title)과 본문(body)을 분리해 아래 형식으로 출력하세요:
   TITLE: <제목>
   BODY: <본문>

2. 제목은 "국가명 중앙은행, 기준금리 X% 동결/인상/인하" 형태로 간결하게.
3. 본문은 700자 이상, 스트레이트 뉴스 문체(감정·논평 표현 금지).
4. 날짜는 '현지시간' 기준으로 "N일(현지시간)" 형식. 절대날짜 금지, '오늘'·'현재' 금지.
5. 금리 결정 내용, 전망치 대비 결과, 직전 금리 대비 변화를 포함.
6. 해당 국가 경제 맥락(물가, 환율, 경제성장 등)을 간략히 언급.
7. 비라틴 문자 국가명·지명은 반드시 한국어 음역.
8. 논평/칼럼 문체 금지: '~를 보여줍니다', '~기대됩니다', '~주목됩니다' 등 사용 금지.
"""


# ── 기사 삽입 ────────────────────────────────────────────────
COUNTRY_FLAG_MAP = {
    "나이지리아": "🇳🇬", "이집트": "🇪🇬", "남아공": "🇿🇦", "케냐": "🇰🇪",
    "가나": "🇬🇭", "에티오피아": "🇪🇹", "탄자니아": "🇹🇿", "모로코": "🇲🇦",
    "앙골라": "🇦🇴", "코트디부아르": "🇨🇮", "카메룬": "🇨🇲",
    "태국": "🇹🇭", "필리핀": "🇵🇭", "베트남": "🇻🇳", "인도네시아": "🇮🇩",
    "말레이시아": "🇲🇾", "방글라데시": "🇧🇩", "파키스탄": "🇵🇰", "스리랑카": "🇱🇰",
    "카자흐스탄": "🇰🇿", "우즈베키스탄": "🇺🇿", "조지아": "🇬🇪",
    "사우디아라비아": "🇸🇦", "아랍에미리트": "🇦🇪", "쿠웨이트": "🇰🇼", "카타르": "🇶🇦",
    "요르단": "🇯🇴", "이라크": "🇮🇶", "바레인": "🇧🇭", "오만": "🇴🇲",
    "튀르키예": "🇹🇷", "폴란드": "🇵🇱", "체코": "🇨🇿", "헝가리": "🇭🇺", "루마니아": "🇷🇴",
}

def insert_article(title_ko: str, summary_ko: str,
                   country: str, event_id: int) -> int:
    now_str     = now_kst().strftime("%Y-%m-%d %H:%M")
    cluster_key = f"econ_rate_{event_id}"
    flag        = COUNTRY_FLAG_MAP.get(country, "")
    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": f"internal://{cluster_key}",
        "source": "NewsFinal",
        "category": "경제",
        "subcategory": cluster_key,
        "region": "글로벌",
        "country": country,
        "country_flag": flag,
        "countries": [country],
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "금리결정 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0].get("id", -1) if data else -1
    print(f"  [ERROR] 기사 삽입 실패 {res.status_code}: {res.text[:200]}")
    return -1


# ── econ_events 업데이트 ─────────────────────────────────────
def update_event_actual(event_id: int, actual_value: str) -> bool:
    res = requests.patch(
        f"{_sb_events_url()}?id=eq.{event_id}",
        headers=_sb_headers(),
        json={"actual_value": actual_value, "is_verified": True},
        timeout=15,
    )
    return res.status_code in (200, 204)


# ── 이미 기사가 있는지 확인 ──────────────────────────────────
def article_exists(event_id: int) -> bool:
    cluster_key = f"econ_rate_{event_id}"
    res = requests.get(
        _sb_articles_url(),
        headers=_sb_headers(),
        params={"select": "id", "subcategory": f"eq.{cluster_key}", "limit": "1"},
        timeout=10,
    )
    if res.status_code in (200, 206):
        return len(res.json()) > 0
    return False


# ── 논평체 검사 ──────────────────────────────────────────────
BANNED_STYLE_PATTERNS = [
    r"보여줍니다", r"보여주고 있습니다", r"도모하고 있습니다",
    r"강조하고 있습니다", r"시사합니다", r"주목됩니다",
    r"평가된다", r"평가받고 있습니다", r"기대됩니다",
    r"지켜볼 필요가 있습니다", r"지켜봐야 할 것입니다",
]

def has_column_style(text: str) -> bool:
    return bool(text and any(re.search(p, text) for p in BANNED_STYLE_PATTERNS))


# ── TITLE / BODY 파싱 ────────────────────────────────────────
def parse_article_output(text: str) -> tuple[str, str]:
    m_title = re.search(r"TITLE:\s*(.+)", text)
    m_body  = re.search(r"BODY:\s*([\s\S]+)", text)
    title   = m_title.group(1).strip() if m_title else ""
    body    = m_body.group(1).strip()  if m_body  else ""
    return title, body


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"[econ_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    events = get_pending_events()
    if not events:
        print("[econ_writer] 처리할 이벤트 없음 — 종료")
        return

    print(f"[econ_writer] 발표 시각 경과 이벤트 {len(events)}건")

    for event in events:
        eid     = event["id"]
        title   = event.get("title", "")
        country = event.get("country", "")
        edate   = event.get("event_date", "")
        tz_str  = event.get("timezone", "")
        ev_time = event.get("event_time", "")
        offset  = event.get("announcement_offset_hours") or 0

        print(f"\n  ▶ [{eid}] {title} ({edate}, 발표 +{offset}일 {ev_time} {tz_str})")

        if article_exists(eid):
            print(f"    → 이미 기사 존재, 스킵")
            continue

        # ── Step 1: Gemini 검색 1차
        prompt = build_search_prompt(event)
        print(f"    → Gemini 1차 검색...")
        resp1 = call_gemini(prompt, max_tokens=100, use_search=True)
        time.sleep(5)

        if not resp1:
            print(f"    → 1차 응답 없음, 스킵")
            continue
        print(f"    → 1차 응답: {resp1[:80]}")

        if "미발표" in resp1:
            print(f"    → 아직 미발표, 스킵")
            continue

        rate1 = parse_rate(resp1)
        if rate1 is None:
            print(f"    → 1차 숫자 파싱 실패 ({resp1[:60]}), 스킵")
            continue

        # ── Step 2: Gemini 검색 2차 (독립 검증)
        print(f"    → Gemini 2차 검색...")
        resp2 = call_gemini(prompt, max_tokens=100, use_search=True)
        time.sleep(5)

        if not resp2:
            print(f"    → 2차 응답 없음, 스킵")
            continue
        print(f"    → 2차 응답: {resp2[:80]}")

        if "미발표" in resp2:
            print(f"    → 2차 미발표 응답, 스킵")
            continue

        rate2 = parse_rate(resp2)
        if rate2 is None:
            print(f"    → 2차 숫자 파싱 실패 ({resp2[:60]}), 스킵")
            continue

        # ── Step 3: 검증
        if not rates_match(rate1, rate2):
            print(f"    → 불일치: {rate1}% vs {rate2}% → 스킵 (다음 사이클 재시도)")
            continue

        actual_str = f"{rate1:.2f}%"
        print(f"    ✓ 검증 완료: {actual_str}")

        # ── Step 4: econ_events 업데이트
        if not update_event_actual(eid, actual_str):
            print(f"    [ERROR] econ_events 업데이트 실패")
            continue
        print(f"    ✓ econ_events 업데이트 완료")

        # ── Step 5: 기사 생성
        print(f"    → 기사 생성 중...")
        article_text = call_gemini(build_article_prompt(event, actual_str),
                                   max_tokens=1500, use_search=False)
        time.sleep(8)

        if not article_text:
            print(f"    [ERROR] 기사 생성 실패")
            continue

        if has_column_style(article_text):
            print(f"    ⚠️ 논평체 감지 → 재생성")
            retry_prompt = (
                build_article_prompt(event, actual_str)
                + "\n\n[재작성 지시] 앞서 작성한 결과에 논평/칼럼 문체가 섞였습니다. "
                  "감정·의견 표현을 완전히 배제하고 사실 전달 중심으로만 다시 작성하세요."
            )
            article_text = call_gemini(retry_prompt, max_tokens=1500, use_search=False) or article_text
            time.sleep(5)

        art_title, art_body = parse_article_output(article_text)

        if not art_title or not art_body:
            print(f"    [ERROR] TITLE/BODY 파싱 실패\n{article_text[:200]}")
            continue

        if len(art_body) < 300:
            print(f"    ⚠️ 본문 너무 짧음 ({len(art_body)}자), 스킵")
            continue

        # ── Step 6: 기사 삽입
        art_id = insert_article(art_title, art_body, country, eid)
        if art_id > 0:
            print(f"    ✓ 기사 삽입 완료 (articles.id={art_id}): {art_title}")
        else:
            print(f"    [ERROR] 기사 삽입 실패")

        time.sleep(10)

    print(f"\n[econ_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
