# -*- coding: utf-8 -*-
"""
date_guard.py — 날짜 환각 판정 공통 모듈

원문에 날짜 근거가 없는데 Gemini가 "N일(현지시간)"을 만들어내는 현상을 검출한다.
gemini_writer.py / gemini_summarizer.py / daily_digest.py 등 모든 기사 생성 경로에서
저장 직전에 호출한다.

사용법:
    from date_guard import check_date_hallucination
    bad, reason = check_date_hallucination(body, sources, base_date=now_kst().date())
    if bad:
        published = False

import 실패에도 본 기능이 죽지 않도록 소비자 쪽에서 try/except 폴백을 둘 것.

판정 규칙 (2026-08-03 실데이터 337표기 검증 기반)
  1. 원문(full_text/title_en/summary_en/source_published_at)에 그 일자 근거가 있으면 통과
  2. 과거명시어(앞서/지난/이미/당시) 또는 미래신호어(오는/예정/부터) 근접 시 통과
  3. 판정 불가(원문 텍스트 빈약 + 발행일 없음) 시 통과 — full_text 확보율 69%
  4. 과거 방향 갭 <= 3일 → 통과 (정상 범위)
  5. 미래 방향 갭 <= 3일인데 서술이 과거형 → 환각 (미래 사건을 과거로 적을 수 없다)
  6. 과거·미래 양쪽 모두 3일 초과 → 환각

⚠️ 대상은 "N일(현지시간)" 단독 표기다. "7월 23일(현지시간)" 같은 월+일 표기는 정상이므로 제외한다.
"""

import re
from datetime import date, timedelta

__all__ = ["check_date_hallucination", "extract_local_time_marks"]

# ── 상수 ──────────────────────────────────────────────────

GAP_THRESHOLD = 3          # 이 일수 이내면 정상으로 본다
MIN_SOURCE_TEXT = 200      # 원문 텍스트가 이보다 짧으면 판정 불가 → 통과
UPDATE_LOG_SEP = "[업데이트 이력]"

# 표기 추출: 앞 20자는 숫자를 포함하지 않아야 한다.
# ⚠️ .{0,20} 을 쓰면 앞자리 숫자를 먹어 d가 깨진다 (실측으로 확인된 함정)
_MARK_RE = re.compile(r"([^0-9]{0,20})([0-9]{1,2})일\(현지시간\)")

# 월+일 표기("7월 23일(현지시간)")는 대상이 아니다
_MONTH_PREFIX_RE = re.compile(r"월\s?$")

_PAST_MARKERS = ("앞서", "지난", "이미", "당시", "그해", "작년")
_FUTURE_MARKERS = ("오는", "예정", "부터", "예상", "계획")

# 과거형 종결 — 미래 날짜와 결합하면 모순이다
_PAST_TENSE_RE = re.compile(
    r"(했다|헀다|였다|었다|았다|됐다|되었다|밝혔|발표했|보도했|전했|말했|나섰|열렸|숨졌|드러났)"
)

# 다국어 월명 접두 (영어/스페인어/프랑스어/포르투갈어)
# ⚠️ 비영어 원문이 다수다. 영어 월명만 보면 놓친다.
_MON = (r"(?:jan|feb|fev|f[e\u00e9]v|mar|apr|abr|may|mai|jun|jul|jui|ago|aug|"
        r"sep|set|oct|out|nov|dec|dez|d[e\u00e9]c|ene|dic|ao[u\u00fb]t)[a-z]*")

_DATE_PATTERNS = (
    re.compile(_MON + r"\.?\s+([0-9]{1,2})(?![0-9])", re.I),                    # July 29 / julio 29
    re.compile(r"([0-9]{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?" + _MON, re.I),      # 29 July / 29 de julio
    re.compile(r"[0-9]{4}-[0-9]{1,2}-([0-9]{1,2})"),                            # ISO
    re.compile(r"[0-9]{1,2}/([0-9]{1,2})/[0-9]{4}"),                            # 유럽식 d/m/Y
    re.compile(r"([0-9]{1,2})/[0-9]{1,2}/[0-9]{4}"),                            # 미국식 m/d/Y (양쪽 다 후보)
)

# 상대 표현 → 기준일 하루 전까지 근거로 인정
_REL_YESTERDAY_RE = re.compile(
    r"(yesterday|ayer|hier|ontem|gestern|\bla v[e\u00ed]spera\b)", re.I
)
_REL_TODAY_RE = re.compile(
    r"(today|hoy|aujourd'hui|aujourdhui|hoje|heute)", re.I
)


# ── 내부 헬퍼 ─────────────────────────────────────────────

def _strip_update_log(body: str) -> str:
    """[업데이트 이력] 이후 구간은 후속 append라 생성일 기준 갭 계산이 무의미하다."""
    if not body:
        return ""
    idx = body.find(UPDATE_LOG_SEP)
    return body[:idx] if idx >= 0 else body


def _sentence_after(body: str, pos: int, limit: int = 120) -> str:
    """표기 직후부터 문장 끝('다.')까지. 서술 시제 판정용."""
    tail = body[pos:pos + limit]
    m = re.search(r"다[.\n]", tail)
    return tail[:m.end()] if m else tail


def _source_days(sources, base_date):
    """원문에서 날짜 근거가 되는 '일(day)' 집합과 원문 텍스트 총 길이를 반환."""
    days = set()
    total_len = 0
    has_pubdate = False

    for s in (sources or []):
        if not isinstance(s, dict):
            continue

        # 1) 원문 발행일 — 가장 신뢰도 높은 근거
        raw_pub = s.get("source_published_at")
        if raw_pub:
            d = _parse_pub_date(raw_pub)
            if d:
                has_pubdate = True
                # 발행일 당일과 전날(전날 사건을 다음날 보도하는 경우가 흔하다)
                days.add(d.day)
                days.add((d - timedelta(days=1)).day)

        txt = " ".join(str(s.get(k) or "") for k in ("full_text", "title_en", "summary_en"))
        if not txt.strip():
            continue
        total_len += len(txt)
        txt = txt[:20000]

        for pat in _DATE_PATTERNS:
            for m in pat.finditer(txt):
                try:
                    v = int(m.group(1))
                except (ValueError, IndexError):
                    continue
                if 1 <= v <= 31:
                    days.add(v)

        if base_date:
            if _REL_TODAY_RE.search(txt):
                days.add(base_date.day)
            if _REL_YESTERDAY_RE.search(txt):
                days.add((base_date - timedelta(days=1)).day)

    return days, total_len, has_pubdate


def _parse_pub_date(raw):
    """'2026-08-02T14:00:00+00:00' 같은 값에서 date만 뽑는다."""
    s = str(raw)[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _gap_past(base_date, d):
    """d를 '가장 최근의 과거 날짜'로 해석했을 때의 일수 차이."""
    for back in range(0, 62):
        cand = base_date - timedelta(days=back)
        if cand.day == d:
            return back
    return 99


def _gap_future(base_date, d):
    """d를 '가장 가까운 미래 날짜'로 해석했을 때의 일수 차이."""
    for fwd in range(0, 62):
        cand = base_date + timedelta(days=fwd)
        if cand.day == d:
            return fwd
    return 99


# ── 공개 API ──────────────────────────────────────────────

def extract_local_time_marks(body: str):
    """본문에서 'N일(현지시간)' 단독 표기를 추출. [(d, pre, after), ...]"""
    body = _strip_update_log(body)
    out = []
    for m in _MARK_RE.finditer(body):
        pre = m.group(1)
        if _MONTH_PREFIX_RE.search(pre):     # 월+일 표기는 대상 아님
            continue
        try:
            d = int(m.group(2))
        except ValueError:
            continue
        if not (1 <= d <= 31):
            continue
        out.append((d, pre, _sentence_after(body, m.end())))
    return out


def check_date_hallucination(body, sources, base_date=None):
    """
    날짜 환각 판정.

    body      : 생성된 기사 본문(summary_ko)
    sources   : 소스 기사 dict 리스트. 단독 기사면 [a] 로 감싸서 전달
    base_date : 기사 생성 기준일(datetime.date). 기본값은 오늘

    반환 (bad: bool, reason: str)
      bad=True 이면 미발행 처리 대상
    """
    if not body:
        return False, ""

    if base_date is None:
        base_date = date.today()
    elif hasattr(base_date, "date") and not isinstance(base_date, date):
        base_date = base_date.date()

    marks = extract_local_time_marks(body)
    if not marks:
        return False, ""

    src_days, total_len, has_pubdate = _source_days(sources, base_date)

    # 판정 불가 — 원문 텍스트가 빈약하고 발행일도 없으면 통과시킨다.
    # (full_text 확보율 69%. 기술적 실패로 정상 기사를 버리면 3분의 1이 사라진다)
    if total_len < MIN_SOURCE_TEXT and not has_pubdate:
        return False, ""

    for d, pre, after in marks:
        ctx = pre + after

        if d in src_days:                                    # 1. 원문 근거 있음
            continue
        if any(k in pre for k in _PAST_MARKERS):             # 2. 과거명시어
            continue
        if any(k in ctx for k in _FUTURE_MARKERS):           # 2. 미래신호어
            continue

        gp = _gap_past(base_date, d)
        gf = _gap_future(base_date, d)

        if gp <= GAP_THRESHOLD:                              # 4. 과거 3일 이내 → 정상
            continue

        if gf <= GAP_THRESHOLD:
            # 5. 미래 날짜인데 과거형 서술 → 모순
            if _PAST_TENSE_RE.search(after):
                return True, f"날짜환각: '{d}일(현지시간)' 미래(+{gf}일)인데 과거형 서술, 원문 근거 없음"
            continue

        # 6. 양방향 모두 임계 초과
        return True, f"날짜환각: '{d}일(현지시간)' 원문 근거 없음 (과거 -{gp}일 / 미래 +{gf}일)"

    return False, ""
