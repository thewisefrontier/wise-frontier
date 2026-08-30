"""
scripts/article_reviewer.py
---------------------------
발행된 자체기사(source='NewsFinal')를 정기 스캔해 편집 원칙 위반을 검수한다.

- 합쇼체(-습니다/-입니다) : 자동 변환 + update_log 기록
- 절대날짜 / 타 매체명 / 기자명·특파원 / (현지시간) 누락 : 플래그 + 관리자 알림

공통 로직은 gemini_writer.py를 import해 재사용한다
(시스템_아키텍처_현황.md 5장 "신규 모듈 작성 패턴" 참조).

제외 정책
- category='날씨' : 전면 제외. 합쇼체 81건의 톤 정책이 미결이고,
  한국 날씨 3함수는 "(현지시간)" 미표기가 정상이라 날짜 검수도 오탐이 된다.
- category='다이제스트','브리핑' : 여러 기사를 묶는 형식이라 (현지시간) 검사만 제외.

실행: python scripts/article_reviewer.py
"""

import os
import re
import requests
import feedparser
from datetime import timedelta

from gemini_writer import (
    has_polite_ending,
    to_plain_style,
    _sb_headers,
    _sb_url,
    SUPABASE_URL,
    now_kst,
)

SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

# ── 설정 (env로 조정 가능) ────────────────────────────────────────────
CHECK_WINDOW_HOURS = int(os.getenv("REVIEW_WINDOW_HOURS", "48"))
FETCH_LIMIT = int(os.getenv("REVIEW_FETCH_LIMIT", "500"))
ALERT_LIMIT = int(os.getenv("REVIEW_ALERT_LIMIT", "15"))
AUTO_FIX_POLITE = os.getenv("REVIEW_AUTO_FIX_POLITE", "1") != "0"
DRY_RUN = os.getenv("REVIEW_DRY_RUN", "0") == "1"

# 검수 전면 제외 카테고리
SKIP_CATEGORIES = {"날씨"}
# (현지시간) 검사만 제외하는 카테고리
DATE_EXEMPT_CATEGORIES = {"다이제스트", "브리핑"}

# ── 감지 패턴 ────────────────────────────────────────────────────────
# 절대날짜: "2026년 7월 15일" 형식. 날짜는 "N일(현지시간)"으로만 표기해야 한다.
ABS_DATE_RE = re.compile(r"\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일")

# 절대날짜 + "(현지시간)" 조합. "N일(현지시간)"이 들어갈 자리에 연·월이 들어간 경우로
# 원문 사진 캡션의 날짜를 보도 시점으로 오인한 사고에서 발견됐다(id=38770, "2026년 2월(현지시간)").
# 일(日)이 없으면 ABS_DATE_RE에 걸리지 않아 별도 패턴이 필요하다.
ABS_DATE_LOCALTIME_RE = re.compile(
    r"\d{4}\s*년\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?\s*\(\s*현지시간\s*\)"
)

# 보도 시점을 절대날짜로 표기한 경우. "2026년 7월 현재", "2026년 6월 말 … 보도했다" 등.
# ⚠️ 연·월 단독("2022년 11월 평화 협정")은 과거 사건 시점 특정이라 정상이므로
#    보도 동사·"현재"가 근접한 경우만 위반으로 본다(실데이터 검증: 연월 단독 497건 중 대부분 정상).
REPORTING_ABS_DATE_RE = re.compile(
    r"\d{4}\s*년\s*\d{1,2}\s*월[^0-9일\n]{0,12}(?:현재|보도|밝혔|전했)"
)

# 타 매체명 언급 금지
# 한글 매체명 — 부분일치로 검사한다.
MEDIA_NAMES = [
    "로이터", "AP통신", "블룸버그", "신화통신", "타스통신", "타스",
    "알자지라", "가디언", "뉴욕타임스", "워싱턴포스트", "월스트리트저널",
    "파이낸셜타임스", "닛케이", "니혼게이자이",
    "연합뉴스", "뉴시스", "조선일보", "중앙일보", "동아일보", "한겨레",
    # ── 프론티어 지역·RSS 소스 매체 (2026-07-30 추가) ──
    # 실측으로 오탐이 확인된 단독 표기는 넣지 않았다:
    #   "네이션"  → 아프리카 네이션스컵(축구) 20건이 전부 오탐
    #   "뱅가드"  → "야이 청년 뱅가드"(단체명)
    #   "익스프레스" → 익스프레스웨이·아메리칸 익스프레스
    #   "펀치"·"스탠더드"·"헤럴드"·"트리뷴" → 일반명사·기업명 충돌
    # 신규 매체명 추가 시에도 반드시 DB 전수 검색으로 오탐을 먼저 확인할 것.
    "더 네이션", "프리미엄 타임스", "지오 뉴스", "지오뉴스",
    "가디언 나이지리아", "프랑스 24", "프랑스24", "유로뉴스",
    "데일리 트러스트", "비즈니스 데이", "비즈니스 리코더",
    "사우스차이나모닝포스트", "차이신", "엘 파이스", "더 힌두",
]

# 영문 약칭 — 앞뒤에 영숫자가 붙지 않은 경우만 매체명으로 본다.
# ⚠️ 경계 없이 넣으면 오탐: "RFI" → RFID(실측 3건), "AP" → APEC 등.
# ⚠️ \b는 쓸 수 없다. 파이썬은 한글도 단어문자로 취급해 "RFI와의 인터뷰"가 매칭되지 않는다.
#    → ASCII 영숫자만 배제하는 lookaround를 쓴다(뒤에 한글이 붙는 건 허용).
MEDIA_ACRONYMS = ["AFP", "BBC", "CNN", "CNBC", "SCMP", "RFI", "NDTV"]

MEDIA_RE = re.compile(
    "|".join(
        [re.escape(m) for m in MEDIA_NAMES]
        + [
            r"(?<![A-Za-z0-9])" + re.escape(m) + r"(?![A-Za-z0-9])"
            for m in MEDIA_ACRONYMS
        ]
    )
)

# 기자명·특파원 등 원문 바이라인 잔재
REPORTER_RE = re.compile(
    r"기자[가는이]\s*[^.\n]{0,40}?(보도|전했|밝혔|썼다)"
    r"|특파원"
    r"|본지\s*(취재|보도)"
)

LOCAL_TIME_TOKEN = "현지시간"

# ── 영어 미번역 감지 (2026-08-30 데스킹 툴 고도화, id=111363 사고 계기) ──────
# 괄호 안(정상적인 원어 병기)은 검사 대상에서 제외하고, 괄호 밖에 남아있는
# "대문자로 시작하는 단어 2개 이상 연속"만 미번역 의심으로 본다. 실측
# 검증(2026-08-30, 최근 발행 200건): 9건 플래그, 그중 대다수가 실제
# 인명·기관명·지명 미번역(NBC News, Ali Mahaman Lamine, Ghat Road 등)이고
# 나머지는 작품 제목류(원어 그대로 쓰는 게 허용된 예외) — 오탐률 낮음 확인.
_PAREN_RE = re.compile(r"\([^)]*\)")
_ENGLISH_RUN_RE = re.compile(
    r"\b[A-Z][a-zA-Z&'-]*(?:\s+(?:of|and|for|the|de|du|von|van|in|on)\s+[A-Za-z][a-zA-Z'-]*"
    r"|\s+[A-Z][a-zA-Z&'-]*)+\b"
)


# ── 발행 시간 상식 검사 (2026-08-30 데스킹 툴 고도화) ────────────────────
# 매일 정해진 시각에 나가야 하는 데이터 기사 유형만 대상으로 한다(일반
# 뉴스·트렌드 기사는 사건 발생 시점이 제각각이라 적용 불가). url 접두어로
# 유형을 식별 — cron-job.org 전환(2026-08-27~28) 당시 정한 실행 시간대와
# 동일하게 맞춘다. 날씨는 국가별 현지 아침 시간이 하루 종일 퍼져 있어
# SKIP_CATEGORIES로 이미 전면 제외돼 있으므로 여기 포함하지 않는다.
URL_PREFIX_EXPECTED_HOURS = {
    "internal://oil_price_": (6, 12),         # 뉴욕장 마감(KST 06~07시) 이후
    "internal://opinet_price_": (5, 10),       # 오피넷 갱신 이후
    "internal://frontier_markets_": (6, 12),   # 뉴욕장 마감 이후
}


def detect_timing_flag(url: str, created_at: str) -> str | None:
    """url 유형별 상식적인 발행 시간대를 벗어났으면 사유를 반환."""
    if not url or not created_at:
        return None
    for prefix, (start_h, end_h) in URL_PREFIX_EXPECTED_HOURS.items():
        if url.startswith(prefix):
            try:
                hour = int(created_at[11:13])
            except (ValueError, IndexError):
                return None
            if not (start_h <= hour < end_h):
                return f"발행시간 이상({created_at[11:16]}, 예상 {start_h:02d}~{end_h:02d}시)"
            return None
    return None


# ── 수치 확인 (2026-08-30 데스킹 툴 고도화) ──────────────────────────────
# "소스 데이터 자체가 틀림"은 frontier_markets_writer.py의 다중검증(2026-08-30)이
# 이미 막는다. 여기서 잡는 건 다른 실패 유형 — "소스 데이터는 맞는데 Gemini가
# 본문으로 옮겨 적으며 숫자를 잘못 쓰는" 전사 오류다. 그래서 발행 후 다시
# 데이터를 조회해 비교하지 않는다(다음날 데스킹 시점엔 이미 다른 날짜
# 데이터라 비교 자체가 안 맞음) — 대신 기사 작성 시점에 실제로 썼던 원본
# 수치를 articles.source_data(JSON)에 같이 저장해두고, 그 값이 본문에
# 그대로 등장하는지만 확인한다.
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")

# 소스 데이터 전체 중 일부(예: 움직임이 작아 본문에서 생략된 지표)가 안
# 보이는 건 정상적인 문체 차이일 수 있다 — 상당 비율이 통째로 안 보일
# 때만("Gemini가 완전히 다른 데이터를 썼다" 수준) 이상으로 본다.
NUMBER_MISMATCH_MIN_ITEMS = 3      # 이보다 항목이 적으면(유가처럼 2개뿐) 신뢰도 낮아 스킵
NUMBER_MISMATCH_THRESHOLD = 0.4    # 40% 이상 안 보이면 플래그


def _walk_prices(node, path=""):
    """source_data JSON 트리에서 'price' 키를 가진 딕셔너리를 전부 찾아
    (라벨, 값) 쌍으로 반환한다. 기사 유형별로 구조가 달라도(글로벌마켓동향은
    리스트의 리스트, 유가는 wti/brent 중첩, 국내유가는 유종 코드별 딕셔너리)
    재귀 탐색이라 공통으로 동작한다."""
    found = []
    if isinstance(node, dict):
        price = node.get("price")
        if isinstance(price, (int, float)):
            label = node.get("name") or node.get("country") or path or "값"
            found.append((str(label), float(price)))
        for k, v in node.items():
            found.extend(_walk_prices(v, k))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_prices(item, path))
    return found


def _extract_body_numbers(body: str) -> set:
    nums = set()
    for m in _NUMBER_RE.finditer(body or ""):
        try:
            nums.add(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return nums


def detect_number_mismatch(source_data, body: str) -> str | None:
    """source_data의 가격 수치 중 상당 비율이 본문에 전혀 안 보이면 플래그."""
    if not source_data:
        return None
    price_pairs = _walk_prices(source_data)
    if len(price_pairs) < NUMBER_MISMATCH_MIN_ITEMS:
        return None
    body_numbers = _extract_body_numbers(body)
    missing = [
        label for label, value in price_pairs
        if not any(abs(value - bn) < max(0.05, abs(value) * 0.001) for bn in body_numbers)
    ]
    ratio = len(missing) / len(price_pairs)
    if ratio >= NUMBER_MISMATCH_THRESHOLD:
        sample = ", ".join(missing[:3])
        return f"수치 불일치 의심({len(missing)}/{len(price_pairs)}건 본문에 없음, 예: {sample})"
    return None


# ── 유가 기사 국내 언론 교차검증 (2026-08-30) ────────────────────────────
# "다른 언론사에서 나온 유가 기사를 참조" — 사용자 결정. 다만 이 소스들을
# rss_sources(일반 수집 풀)에 넣지 않는다 — 넣으면 gemini_writer.py
# 클러스터링이 한국 국내뉴스를 정식 기사로 잘못 발행할 위험이 있어서,
# 데스킹 전용 함수로 완전히 분리한다.
#
# ⚠️ 연합뉴스는 RSS 자체에 "AI 학습 및 활용 금지" 조항이 명시돼 있어 제외.
# 아래 4곳은 실측(2026-08-30) 확인 — 정상 작동 + 그런 조항 없음.
KOREAN_OIL_REFERENCE_FEEDS = [
    ("뉴시스", "https://www.newsis.com/RSS/economy.xml"),
    ("이데일리", "http://rss.edaily.co.kr/edaily_news.xml"),
    ("한국경제", "https://www.hankyung.com/feed/economy"),
    ("머니투데이", "https://rss.mt.co.kr/mt_news.xml"),
]

_OIL_KEYWORDS = ["유가", "휘발유", "국제유가", "WTI", "브렌트", "배럴"]
_KRW_PER_LITER_RE = re.compile(r"리터당\s*([\d,]+\.?\d*)\s*원")
_USD_PER_BARREL_RE = re.compile(r"배럴당\s*([\d,]+\.?\d*)\s*달러")


def fetch_korean_oil_references(within_hours: int = 36) -> list:
    """뉴시스·이데일리·한국경제·머니투데이에서 유가 관련 최근 기사를 찾아
    [{source, title, numbers:[(단위, 값), ...]}] 형태로 반환."""
    cutoff = now_kst() - timedelta(hours=within_hours)
    results = []
    for name, url in KOREAN_OIL_REFERENCE_FEEDS:
        try:
            # User-Agent 없으면 한국경제 등 일부 매체가 빈 응답을 준다(실측 확인).
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            print(f"  [WARN] {name} 교차검증 피드 조회 실패: {e}")
            continue
        for entry in feed.entries[:30]:
            title = entry.get("title", "") or ""
            desc = entry.get("description", "") or entry.get("summary", "") or ""
            text = f"{title} {desc}"
            if not any(kw in text for kw in _OIL_KEYWORDS):
                continue
            nums = [("원/L", float(m.group(1).replace(",", "")))
                    for m in _KRW_PER_LITER_RE.finditer(text)]
            nums += [("달러/배럴", float(m.group(1).replace(",", "")))
                     for m in _USD_PER_BARREL_RE.finditer(text)]
            if nums:
                results.append({"source": name, "title": title[:60], "numbers": nums})
    return results


def detect_oil_cross_check(url: str, source_data, _fetch=fetch_korean_oil_references) -> str | None:
    """국내유가/국제유가 기사만 대상으로 국내 언론 보도와 수치를 대조.

    참고할 만한 보도 자체를 못 찾으면(휴일 등) 판단을 보류하고 플래그하지
    않는다 — "증거 없음"과 "불일치"는 다르다."""
    is_domestic = url.startswith("internal://opinet_price_")
    is_intl = url.startswith("internal://oil_price_")
    if not (is_domestic or is_intl) or not source_data:
        return None

    unit = "원/L" if is_domestic else "달러/배럴"
    our_values = []
    if is_domestic:
        for item in (source_data.get("prices") or {}).values():
            if isinstance(item, dict) and isinstance(item.get("price"), (int, float)):
                our_values.append(float(item["price"]))
    else:
        for k in ("wti", "brent"):
            v = (source_data.get(k) or {}).get("price")
            if isinstance(v, (int, float)):
                our_values.append(float(v))
    if not our_values:
        return None

    refs = _fetch()
    ref_values = [n for r in refs for (u, n) in r["numbers"] if u == unit]
    if not ref_values:
        return None

    matched = any(
        any(abs(ov - rv) <= max(0.5, ov * 0.01) for rv in ref_values)
        for ov in our_values
    )
    if not matched:
        sample_ref = ", ".join(f"{rv:.2f}" for rv in ref_values[:3])
        sample_our = ", ".join(f"{ov:.2f}" for ov in our_values[:3])
        return f"국내언론 교차검증 불일치(우리값 {sample_our} vs 언론보도 {sample_ref} {unit})"
    return None


def detect_untranslated_english(joined: str) -> list:
    """괄호 밖에 남은 2단어 이상 영문 연속 구간을 미번역 의심으로 반환."""
    stripped = _PAREN_RE.sub("", joined)
    matches = [m for m in _ENGLISH_RUN_RE.findall(stripped) if len(m) >= 8]
    # 같은 구문이 여러 번 나와도 한 번만 알림
    seen = []
    for m in matches:
        if m not in seen:
            seen.append(m)
    return seen


def fetch_candidates() -> list:
    """최근 CHECK_WINDOW_HOURS 시간 내 발행된 자체기사 조회.

    created_at은 16자 KST 텍스트("YYYY-MM-DD HH:MM")라 문자열 비교로 필터한다.
    """
    since = (now_kst() - timedelta(hours=CHECK_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,category,update_log,created_at,url,source_data",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "created_at": f"gte.{since}",
                "limit": str(FETCH_LIMIT),
            },
            timeout=30,
        )
        if res.status_code in (200, 206):
            return res.json()
        print(f"[ERROR] 기사 조회 실패: HTTP {res.status_code} {res.text[:200]}")
    except Exception as e:
        print(f"[ERROR] 기사 조회 실패: {e}")
    return []


def detect_flags(title: str, body: str, category: str, url: str = "", created_at: str = "",
                  source_data=None) -> list:
    """자동 수정하지 않고 알림만 보내는 위반 항목 탐지."""
    flags = []
    joined = f"{title}\n{body}"

    timing_flag = detect_timing_flag(url, created_at)
    if timing_flag:
        flags.append(timing_flag)

    number_flag = detect_number_mismatch(source_data, body)
    if number_flag:
        flags.append(number_flag)

    oil_cross_flag = detect_oil_cross_check(url, source_data)
    if oil_cross_flag:
        flags.append(oil_cross_flag)

    m = ABS_DATE_LOCALTIME_RE.search(joined)
    if m:
        # 가장 확실한 위반 유형이라 별도 라벨로 구분해 알림 우선순위를 높인다.
        flags.append(f"절대날짜+현지시간({m.group(0)})")
    elif ABS_DATE_RE.search(joined):
        m = ABS_DATE_RE.search(joined)
        flags.append(f"절대날짜({m.group(0)})")

    m = REPORTING_ABS_DATE_RE.search(joined)
    if m:
        flags.append(f"보도시점 절대날짜({m.group(0)[:24]})")

    m = MEDIA_RE.search(joined)
    if m:
        flags.append(f"타매체명({m.group(0)})")

    m = REPORTER_RE.search(joined)
    if m:
        flags.append(f"기자명({m.group(0)[:20]})")

    if (category or "") not in DATE_EXEMPT_CATEGORIES and LOCAL_TIME_TOKEN not in joined:
        flags.append("현지시간 누락")

    english_leftover = detect_untranslated_english(joined)
    if english_leftover:
        flags.append(f"영어 미번역 의심({', '.join(english_leftover[:3])})")

    return flags


def apply_polite_fix(title: str, body: str):
    """합쇼체 종결을 해라체로 변환. 변경분이 있으면 (새제목, 새본문, 변경필드) 반환."""
    new_title, new_body = title or "", body or ""
    changed = []

    if has_polite_ending(new_title):
        fixed = to_plain_style(new_title)
        if fixed != new_title:
            new_title = fixed
            changed.append("title_ko")

    if has_polite_ending(new_body):
        fixed = to_plain_style(new_body)
        if fixed != new_body:
            new_body = fixed
            changed.append("summary_ko")

    return new_title, new_body, changed


def patch_article(article_id: int, fields: dict, existing_log, note: str) -> bool:
    payload = dict(fields)
    payload["update_log"] = (existing_log or []) + [{
        "timestamp": now_kst().strftime("%Y-%m-%d %H:%M"),
        "note": note,
    }]
    if DRY_RUN:
        print(f"  [DRY-RUN] id={article_id} 패치 생략 — {note}")
        return True
    try:
        res = requests.patch(
            f"{_sb_url()}?id=eq.{article_id}",
            headers=_sb_headers(),
            json=payload,
            timeout=15,
        )
        if res.status_code in (200, 204):
            return True
        print(f"[ERROR] id={article_id} 업데이트 실패: HTTP {res.status_code} {res.text[:200]}")
    except Exception as e:
        print(f"[ERROR] id={article_id} 업데이트 실패: {e}")
    return False


def send_telegram_alert(fixed: list, flagged: list, scanned: int):
    if not TELEGRAM_TOKEN or not TELEGRAM_ADMIN_CHAT_ID:
        return
    if not fixed and not flagged:
        return

    lines = [f"📝 기사 검수 리포트 (최근 {CHECK_WINDOW_HOURS}시간 / {scanned}건 스캔)"]

    if fixed:
        lines.append(f"\n🔧 합쇼체 자동 변환 {len(fixed)}건")
        for item in fixed[:ALERT_LIMIT]:
            lines.append(f"- id={item['id']} {item['title'][:35]}")
        if len(fixed) > ALERT_LIMIT:
            lines.append(f"...외 {len(fixed) - ALERT_LIMIT}건")

    if flagged:
        lines.append(f"\n⚠️ 확인 필요 {len(flagged)}건")
        for item in flagged[:ALERT_LIMIT]:
            lines.append(f"- id={item['id']} [{', '.join(item['flags'])}] {item['title'][:30]}")
        if len(flagged) > ALERT_LIMIT:
            lines.append(f"...외 {len(flagged) - ALERT_LIMIT}건")

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_ADMIN_CHAT_ID, "text": "\n".join(lines)},
            timeout=15,
        )
    except Exception as e:
        print(f"[WARN] 텔레그램 알림 실패: {e}")


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] Supabase 설정 없음")
        return

    articles = fetch_candidates()
    targets = [a for a in articles if (a.get("category") or "") not in SKIP_CATEGORIES]
    skipped = len(articles) - len(targets)
    print(f"[기사 검수] 최근 {CHECK_WINDOW_HOURS}시간 {len(articles)}건 조회 "
          f"(제외 {skipped}건) → {len(targets)}건 스캔")

    fixed, flagged = [], []

    for a in targets:
        aid = a.get("id")
        title = a.get("title_ko") or ""
        body = a.get("summary_ko") or ""
        category = a.get("category") or ""

        if AUTO_FIX_POLITE:
            new_title, new_body, changed = apply_polite_fix(title, body)
            if changed:
                payload = {}
                if "title_ko" in changed:
                    payload["title_ko"] = new_title
                if "summary_ko" in changed:
                    payload["summary_ko"] = new_body
                if patch_article(aid, payload, a.get("update_log"),
                                 f"합쇼체 자동 변환({', '.join(changed)})"):
                    title, body = new_title, new_body
                    fixed.append({"id": aid, "title": title})
                    print(f"  ✅ id={aid} 합쇼체 변환 — {', '.join(changed)}")

        flags = detect_flags(title, body, category, a.get("url") or "", a.get("created_at") or "",
                              a.get("source_data"))
        if flags:
            flagged.append({"id": aid, "title": title, "flags": flags})
            print(f"  ⚠️ id={aid} {', '.join(flags)}")

    send_telegram_alert(fixed, flagged, len(targets))
    print(f"[기사 검수] 완료 — 자동변환 {len(fixed)}건, 확인필요 {len(flagged)}건")


if __name__ == "__main__":
    run()
