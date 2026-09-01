"""
opinet_weekly_writer.py
------------------------
매주 토요일, 지난 한 주(월~금 또는 최근 7일)의 국내 평균 유가 동향을
정리한 "주간 유가 동향" 기사를 자동 생성합니다. `opinet_price_writer.py`가
매일 쌓아온 `opinet_price_history`를 바탕으로 이번 주 평균을 지난주 평균과
비교하고, 오피넷 시도별 평균가(`avgSidoPrice`)로 최고·최저 지역을 짚고,
직전에 발행된 국제유가 기사를 참고해 향후 반영 가능성을 언급합니다.

⚠️ 2026-08-18 신설 — 이력 축적이 오늘부터 시작이라 "N주 연속" 같은 표현은
실제로 계산 가능한 주 수만큼만 쓴다(`count_consecutive_weeks()`). 데이터가
1~2주뿐이면 "이번 주 대비 지난주"까지만 정직하게 서술하고, 여러 주 연속
표현은 억지로 만들지 않는다.

실행: python scripts/opinet_weekly_writer.py
권장: 주 1회(토요일) 실행
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

# articles 테이블 삽입 공용 로직(2026-09-02, article_store.py로 공용화).
try:
    from article_store import insert_final_article
except Exception:
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

# ── 설정 ────────────────────────────────────────────────────
GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
SUPABASE_URL         = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
OPINET_API_KEY       = os.getenv("OPINET_API_KEY", "")

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

try:
    from gemini_client import GeminiClient
except Exception:
    class GeminiClient:  # import 실패해도 본 기능이 죽지 않도록 폴백을 둔다
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            return None

_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)

KST = timezone(timedelta(hours=9))

PRODUCT_NAMES = {"B027": "휘발유", "D047": "경유"}
HEADLINE_PRODUCTS = ["B027", "D047"]


# ── 헬퍼 ────────────────────────────────────────────────────
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

try:
    from image_store import store_image
except Exception:
    def store_image(src_url, key_hint="", timeout=30):
        return src_url


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


# Supabase 헤더/URL 헬퍼는 article_store.py로 공용화(2026-09-02).
try:
    from article_store import sb_headers as _sb_headers
    from article_store import sb_url as _sb_articles_url
except Exception:
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_articles_url():
        return f"{SUPABASE_URL}/rest/v1/articles"


def already_published(week_end: date) -> bool:
    internal_url = f"internal://opinet_weekly_{week_end.isoformat()}"
    res = requests.get(
        f"{_sb_articles_url()}?url=eq.{internal_url}&is_published=eq.true&select=id",
        headers=_sb_headers(),
        timeout=10,
    )
    if res.status_code in (200, 206):
        return len(res.json()) > 0
    return False


# 첫 발행을 미룰 최소 이력 기간. 사용자 결정(2026-08-18): 이력이 하루이틀만
# 쌓인 채로 첫 기사가 나가면 "전주 대비 데이터 없음"만 반복하는 빈약한
# 기사가 되므로, 3주치가 쌓일 때까지는 조용히 스킵하고 이후 자동으로
# 시작한다(수동으로 다시 켜는 절차 불필요).
MIN_HISTORY_DAYS = 21


def has_enough_history(week_end: date) -> bool:
    """opinet_price_history의 최초 저장일이 MIN_HISTORY_DAYS 이상 지났는지 확인."""
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/opinet_price_history",
        headers=_sb_headers(),
        params={"select": "price_date", "order": "price_date.asc", "limit": "1"},
        timeout=10,
    )
    if res.status_code not in (200, 206):
        return False
    rows = res.json()
    if not rows:
        return False
    earliest = datetime.strptime(rows[0]["price_date"], "%Y-%m-%d").date()
    return (week_end - earliest).days >= MIN_HISTORY_DAYS


# ── 주간 이력 집계 ────────────────────────────────────────────
def _fetch_history(prodcd: str, since: date, until: date) -> list:
    """opinet_price_history에서 [since, until] 구간(포함) 데이터 조회, 날짜 오름차순."""
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/opinet_price_history",
        headers=_sb_headers(),
        params={
            "select": "price_date,price",
            "prodcd": f"eq.{prodcd}",
            "price_date": [f"gte.{since.isoformat()}", f"lte.{until.isoformat()}"],
            "order": "price_date.asc",
        },
        timeout=15,
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


def _week_avg(rows: list) -> float | None:
    if not rows:
        return None
    return sum(float(r["price"]) for r in rows) / len(rows)


def count_consecutive_weeks(prodcd: str, week_end: date, direction: str) -> int:
    """direction("상승"/"하락")과 같은 방향으로 몇 주 연속인지 센다.
    이력이 부족하면(데이터 축적 초기) 계산 가능한 만큼만 반환 — 지어내지 않는다."""
    if direction not in ("상승", "하락"):
        return 1
    weeks = 1
    cursor_end = week_end
    prev_avg = None
    for _ in range(52):  # 최대 1년치까지만 탐색
        cur_start = cursor_end - timedelta(days=6)
        cur_rows = _fetch_history(prodcd, cur_start, cursor_end)
        cur_avg = _week_avg(cur_rows)
        if cur_avg is None:
            break
        prior_end = cur_start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=6)
        prior_rows = _fetch_history(prodcd, prior_start, prior_end)
        prior_avg = _week_avg(prior_rows)
        if prior_avg is None:
            break
        this_dir = "상승" if cur_avg > prior_avg else ("하락" if cur_avg < prior_avg else "보합")
        if this_dir != direction:
            break
        weeks += 1
        cursor_end = prior_end
    return weeks


def build_weekly_summary(week_end: date) -> dict | None:
    """이번 주(week_end 포함 최근 7일) vs 지난주 평균을 유종별로 계산."""
    this_start = week_end - timedelta(days=6)
    prev_end = this_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    result = {}
    for code in HEADLINE_PRODUCTS:
        this_rows = _fetch_history(code, this_start, week_end)
        this_avg = _week_avg(this_rows)
        if this_avg is None:
            continue
        prev_rows = _fetch_history(code, prev_start, prev_end)
        prev_avg = _week_avg(prev_rows)

        diff = None
        direction = None
        weeks = 1
        if prev_avg is not None:
            diff = this_avg - prev_avg
            direction = "상승" if diff > 0 else ("하락" if diff < 0 else "보합")
            if direction in ("상승", "하락"):
                weeks = count_consecutive_weeks(code, week_end, direction)

        result[code] = {
            "name": PRODUCT_NAMES.get(code, code),
            "this_avg": this_avg,
            "prev_avg": prev_avg,
            "diff": diff,
            "direction": direction,
            "consecutive_weeks": weeks,
            "days_in_this_week": len(this_rows),
        }
    return result or None


# ── 오피넷 시도별 평균가 (지역 최고·최저) ─────────────────────
def _find_price_list(obj):
    """avgAllPrice와 동일한 방어적 파서 — PRODCD 키를 가진 리스트를 재귀 탐색."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "PRODCD" in obj[0]:
            return obj
        for item in obj:
            found = _find_price_list(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            found = _find_price_list(v)
            if found:
                return found
    return None


def fetch_regional_extremes(prodcd: str = "B027") -> dict | None:
    """시도별 평균가(avgSidoPrice)로 최고·최저 지역을 찾는다. 실패 시 None(본문에서 생략)."""
    if not OPINET_API_KEY:
        return None
    url = f"https://www.opinet.co.kr/api/avgSidoPrice.do?out=json&prodcd={prodcd}&certkey={OPINET_API_KEY}"
    try:
        res = requests.get(url, timeout=(10, 30))
        if res.status_code != 200:
            return None
        data = res.json()
    except Exception as e:
        print(f"  ⚠️ 시도별 평균가 조회 실패: {e}")
        return None

    rows = _find_price_list(data)
    if not rows:
        return None

    parsed = []
    for r in rows:
        try:
            parsed.append({"name": r.get("SIDONM", ""), "price": float(r.get("PRICE"))})
        except (TypeError, ValueError):
            continue
    if not parsed:
        return None

    parsed.sort(key=lambda x: x["price"])
    return {"lowest": parsed[0], "highest": parsed[-1]}


# ── 최근 국제유가 기사 참고 ────────────────────────────────────
def get_recent_international_context() -> str:
    """가장 최근 발행된 [국제유가] 기사 제목+리드를 짧게 가져와 프롬프트 참고자료로 쓴다.
    없어도 기사 생성 자체는 계속 진행(본문에서 그 부분만 생략)."""
    try:
        res = requests.get(
            _sb_articles_url(),
            headers=_sb_headers(),
            params={
                "select": "title_ko,summary_ko,created_at",
                "source": "eq.NewsFinal",
                "subcategory": "eq.국제유가",
                "is_published": "eq.true",
                "order": "created_at.desc",
                "limit": "1",
            },
            timeout=10,
        )
        if res.status_code in (200, 206):
            rows = res.json()
            if rows:
                a = rows[0]
                lead = (a.get("summary_ko") or "")[:300]
                return f"{a.get('title_ko','')} ({a.get('created_at','')[:10]})\n{lead}"
    except Exception:
        pass
    return ""


# ── Gemini 호출 (키 로테이션) ─────────────────────────────────
def call_gemini(prompt: str, max_tokens: int = 4000, start_tier: int = 2) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.4, timeout=(10, 30))


def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    titles = []
    for lang in ("ko", "en"):
        try:
            res = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": name, "limit": 3, "namespace": 0, "format": "json"},
                headers={"User-Agent": "NewsFinal-EntityCheck/1.0 (+https://newsfinal.co.kr)"},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if len(data) >= 2 and isinstance(data[1], list):
                    titles.extend(data[1])
        except Exception:
            continue
    return any(fuzz.token_sort_ratio(name, t) >= threshold for t in titles)


def extract_candidate_names(body: str) -> list:
    if not body:
        return []
    prompt = f"""아래 기사 본문에서 실제 존재 여부를 확인해볼 만한 구체적 고유명사를 추출하세요.
특정 인물 실명, 특정 기관·단체·기업명만 대상으로 합니다.
국가명·일반 지명(도시·나라)이나 흔한 일반명사·직함은 제외하세요.
쉼표로 구분해 나열만 하세요(설명 금지). 대상이 없으면 "없음"이라고만 답하세요.

본문:
{body[:2000]}

답변:"""
    result = call_gemini(prompt, max_tokens=150, start_tier=3)
    if not result:
        return []
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return []
    return [n.strip() for n in result.split(",") if n.strip() and len(n.strip()) >= 2][:15]


def verify_no_fabricated_names(source_prompt: str, body: str) -> str:
    if not body:
        return ""
    check_prompt = f"""아래는 기사 작성에 쓰인 원본 자료와, 그걸 바탕으로 생성된 한국어 기사 본문입니다.
기사 본문에 나오는 고유명사(인명, 기관명, 지역명)가 원본 자료에 실제로 근거하는지 확인하세요.
원본 자료에 등장하는 대상을 다른 이름으로 완전히 잘못 지어낸 경우만 찾으세요.
그런 이름이 있으면 "지어낸이름 → 원본표기" 형식으로 쉼표 구분해 나열하세요.
없으면 "없음"이라고만 답하세요.

[원본 자료]
{source_prompt[:3000]}

[생성된 기사 본문]
{body[:2000]}

답변:"""
    result = call_gemini(check_prompt, max_tokens=150, start_tier=3)
    suspect = ""
    if result:
        result = result.strip()
        if result and not ("없음" in result and len(result) <= 12):
            suspect = result

    unconfirmed = [n for n in extract_candidate_names(body) if not wikipedia_confirms(n)]
    if unconfirmed:
        note = "[위키 미확인] " + ", ".join(unconfirmed)
        suspect = (suspect + "\n" + note) if suspect else note

    return suspect


# ── 기사 프롬프트 ────────────────────────────────────────────
def build_article_prompt(week_end: date, summary: dict, regional: dict | None, intl_context: str) -> str:
    this_start = week_end - timedelta(days=6)

    lines = []
    for code in HEADLINE_PRODUCTS:
        if code not in summary:
            continue
        s = summary[code]
        if s["prev_avg"] is None:
            lines.append(f"- {s['name']}: 이번 주 평균 리터당 {s['this_avg']:,.2f}원 (지난주 비교 데이터 아직 없음 — 이력 축적 초기)")
        else:
            weeks_note = f"{s['consecutive_weeks']}주 연속 {s['direction']}" if s["consecutive_weeks"] >= 2 else f"전주 대비 {s['direction']}"
            lines.append(
                f"- {s['name']}: 이번 주({this_start.month}/{this_start.day}~{week_end.month}/{week_end.day}) 평균 리터당 {s['this_avg']:,.2f}원, "
                f"지난주 평균 {s['prev_avg']:,.2f}원 대비 {'+' if s['diff']>0 else ''}{s['diff']:,.2f}원 ({weeks_note})"
            )
    price_lines = "\n".join(lines)

    regional_line = ""
    if regional:
        regional_line = (
            f"\n[지역별 휘발유 평균가]\n"
            f"- 최고가 지역: {regional['highest']['name']} 리터당 {regional['highest']['price']:,.2f}원\n"
            f"- 최저가 지역: {regional['lowest']['name']} 리터당 {regional['lowest']['price']:,.2f}원\n"
            f"- 지역 간 격차: 리터당 {regional['highest']['price'] - regional['lowest']['price']:,.2f}원"
        )

    intl_block = f"\n[참고 — 최근 발행된 국제유가 기사]\n{intl_context}" if intl_context else ""

    gasoline = summary.get("B027", {})
    gasoline_price = int(round(gasoline.get("this_avg", 0)))

    return f"""당신은 국내 경제·생활물가 전문 기자입니다.
아래 오피넷(한국석유공사) 데이터를 바탕으로 "주간 국내 유가 동향" 기사를 작성하세요.

[이번 주 평균 유가] (출처: 오피넷/한국석유공사)
{price_lines}
{regional_line}
{intl_block}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "[주간유가] "로 시작. 대괄호 포함 그대로 출력.
- "[주간유가] 휘발유 리터당 {gasoline_price}원…<핵심 동인>" 형태, 대괄호 포함 55자 이내

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '시사합니다'
2. 구조:
   ① 리드: 이번 주 휘발유·경유 평균가와 전주 대비 방향
   ② 유종별 상세 수치 (위 데이터에 있는 것만 — 지어내지 말 것)
   ③ 지역별 최고·최저가 비교 (데이터 있을 때만)
   ④ 국제유가 참고자료가 있으면, 국내 반영까지 통상 2~3주 걸린다는 점과 함께 향후 전망 언급 (참고자료 없으면 이 문단 생략)
3. **"지난주 비교 데이터 아직 없음"이라고 표시된 유종은 절대 "N주 연속" 같은 표현을 쓰지 말고, 이번 주 수치만 사실대로 서술하세요.** 데이터에 없는 "몇 주 연속" 숫자를 지어내면 안 됩니다.
4. 날짜는 "이번 주", "{this_start.month}월 {this_start.day}일~{week_end.day}일" 형식만. "오늘", "현재", 절대연도 금지.
5. 출처: "오피넷(한국석유공사)에 따르면" 반드시 포함.
6. 분량: 500자 이상.
"""


# ── 파싱 ─────────────────────────────────────────────────────
TITLE_PREFIX = "[주간유가]"


def enforce_title_prefix(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return t
    m = re.match(r"^\s*\[\s*주간\s*유가\s*\]\s*(.*)$", t)
    if m:
        t = m.group(1).strip()
    else:
        m2 = re.match(r"^주간유가(?:는)?\s*[,·]?\s+(.+)$", t)
        if m2 and m2.group(1)[:1] not in ("와", "과", "및"):
            t = m2.group(1).strip()
        else:
            t = re.sub(r"^주간유가\s*[,·]\s*", "", t).strip()
    return f"{TITLE_PREFIX} {t}" if t else TITLE_PREFIX


def parse_article_output(text: str) -> tuple[str, str]:
    title, body = "", ""
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    m_body = re.search(r"BODY:\s*([\s\S]+)", text)
    if m_title:
        title = m_title.group(1).strip()
    if m_body:
        body = m_body.group(1).strip()
    return title, body


def has_column_style(text: str) -> bool:
    patterns = ["주목됩니다", "기대됩니다", "보여줍니다", "시사합니다", "중요합니다"]
    return any(p in text for p in patterns)


# ── 대표 이미지 ──────────────────────────────────────────────
_IMAGE_KEYWORDS = ["gas station", "fuel pump", "gasoline price", "car refueling"]


def fetch_weekly_image(week_end: date) -> str:
    """로직은 article_image.py로 공용화(2026-09-02, 4개 writer 스크립트에
    복붙돼 있었음)."""
    from article_image import fetch_seeded_pixabay_image
    return fetch_seeded_pixabay_image(
        _IMAGE_KEYWORDS, week_end.toordinal(), f"opinet_weekly_{week_end.isoformat()}"
    )


# ── 기사 삽입 ────────────────────────────────────────────────
def insert_article(title_ko: str, summary_ko: str, week_end: date, image_url: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    internal_url = f"internal://opinet_weekly_{week_end.isoformat()}"

    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "경제",
        "subcategory": "국내유가_주간",
        "region": "global",
        "country": "한국",
        "country_flag": "🇰🇷",
        "image_url": image_url,
        "countries": ["한국"],
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "주간 국내 유가 동향 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
    }

    return insert_final_article(payload)


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[opinet_weekly_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    week_end = now_kst().date()
    print(f"  → 집계 종료일: {week_end.isoformat()}")

    if not has_enough_history(week_end):
        print(f"  → 이력이 아직 {MIN_HISTORY_DAYS}일 미만 → 첫 발행 보류 (자동으로 쌓이면 재실행 시 시작됨)")
        return

    if already_published(week_end):
        print(f"  → {week_end} 기준 주간 유가 기사 이미 존재 → 스킵")
        return

    summary = build_weekly_summary(week_end)
    if not summary:
        print("  [ERROR] 이번 주 이력 데이터 없음 → 종료 (opinet_price_writer.py가 최근 며칠 안 돈 것으로 보임)")
        return

    regional = fetch_regional_extremes()
    intl_context = get_recent_international_context()

    print("  → Gemini로 기사 생성 중...")
    prompt = build_article_prompt(week_end, summary, regional, intl_context)
    article_text = call_gemini(prompt, max_tokens=4000)
    time.sleep(8)

    if not article_text:
        print("  ⚠️ 첫 시도 실패 → 재시도")
        article_text = call_gemini(prompt, max_tokens=4000)
        time.sleep(5)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    if has_column_style(article_text):
        print("  ⚠️ 논평체 감지 → 재생성")
        article_text = call_gemini(
            prompt + "\n\n[재작성 지시] 논평/칼럼 문체가 섞였습니다. 사실 전달 중심으로만 다시 작성하세요.",
            max_tokens=4000,
        ) or article_text
        time.sleep(5)

    fabricated = verify_no_fabricated_names(prompt, article_text)
    if fabricated:
        print(f"  ⚠️ 원문에 없는 고유명사 감지({fabricated}) → 재생성")
        article_text = call_gemini(
            prompt + f"\n\n[재작성 지시] 다음 이름을 원문에 없는 표현으로 잘못 지어냈습니다: {fabricated}. "
                     "고유명사는 원본 자료에 나온 표기를 그대로 옮기고, 확신할 수 없으면 지어내지 말고 원문 표기를 그대로 쓰세요.",
            max_tokens=4000,
        ) or article_text
        time.sleep(5)

    art_title, art_body = parse_article_output(article_text)
    art_title = enforce_title_prefix(art_title)

    if not art_title or not art_body:
        print(f"  [ERROR] TITLE/BODY 파싱 실패\n{article_text[:300]}")
        return

    if len(art_body) < 400:
        print(f"  ⚠️ 본문 너무 짧음 ({len(art_body)}자) → 스킵")
        return

    print(f"  → 제목: {art_title}")
    print(f"  → 본문 {len(art_body)}자")

    image_url = fetch_weekly_image(week_end)

    art_id = insert_article(art_title, art_body, week_end, image_url)
    if art_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={art_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[opinet_weekly_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
