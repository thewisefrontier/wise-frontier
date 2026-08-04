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
     ⚠️ 근거는 월+일을 함께 대조한다. 기준일에서 EVIDENCE_WINDOW_DAYS(31일)를 벗어난
        날짜는 근거로 인정하지 않는다. (일만 대조하면 4개월 전 논문의 '19 Apr'이
        8월 기사의 '19일(현지시간)' 근거가 된다 — 실사례 id=48644)
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

# 원문 날짜 근거의 유효 범위(일). 기준일에서 이보다 멀면 근거로 인정하지 않는다.
# ⚠️ 일(day)만 대조하면 "19 Apr"이 8월 기사의 "19일(현지시간)" 근거로 잘못 인정된다.
#    (실사례: arXiv 피드가 4개월 전 논문을 재발행 — id=48644)
EVIDENCE_WINDOW_DAYS = 31

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

# 월+일을 함께 캡처한다. 일만 뽑으면 다른 달의 같은 일자를 근거로 오인한다.
_PAT_MON_DAY = re.compile(r"(?P<mon>" + _MON + r")\.?\s+(?P<day>[0-9]{1,2})(?![0-9])", re.I)
_PAT_DAY_MON = re.compile(r"(?P<day>[0-9]{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?(?P<mon>" + _MON + r")", re.I)
_PAT_ISO     = re.compile(r"(?P<y>[0-9]{4})-(?P<m>[0-9]{1,2})-(?P<day>[0-9]{1,2})(?![0-9])")
_PAT_SLASH   = re.compile(r"(?P<a>[0-9]{1,2})/(?P<b>[0-9]{1,2})/(?P<y>[0-9]{4})")  # d/m/Y·m/d/Y 양쪽 후보

# 한국어 날짜 — 소스가 자체 한국어 기사인 경로(다이제스트)에서 필수다.
# ⚠️ 이게 없으면 원기사에 "31일(현지시간)"이 그대로 있어도 근거로 인정하지 못해
#    정상 다이제스트가 매일 미발행된다 (실사례: id=50788 ← id=48206, 2026-08-04)
_PAT_KO_MD  = re.compile(r"(?P<m>[0-9]{1,2})\s*월\s*(?P<day>[0-9]{1,2})\s*일")
# 월+일 표기는 _PAT_KO_MD가 정확히 복원하므로 여기서는 제외한다.
_PAT_KO_DAY = re.compile(r"(?<![0-9])(?<!월)(?<!월 )(?P<day>[0-9]{1,2})일\(현지시간\)")

# "N일(현지시간)" 단독 표기는 월 정보가 없어 완전한 date로 복원할 수 없다.
# 소스 발행일 기준 이 창 안에서만 근거로 인정한다.
# 실측(2026-08-04, 07-25 이후 발행 자체기사 424표기): 과거 0~3일이 312건(73.6%).
# 창을 넓히면 31일 중 상당수 day가 무조건 통과해 가드가 무력화된다.
KO_DAY_BACK = 7   # 과거 방향 허용 일수
KO_DAY_FWD  = 3   # 미래 방향 허용 일수

# 다국어 월명 → 월 번호. 앞자리 우선순위 주의(juil=7 / juin=6)
_MON_PREFIX = (
    ("juil", 7), ("juin", 6), ("jan", 1), ("ene", 1), ("feb", 2), ("fev", 2), ("f\u00e9v", 2),
    ("mar", 3), ("apr", 4), ("abr", 4), ("may", 5), ("mai", 5), ("jun", 6), ("jul", 7),
    ("aug", 8), ("ago", 8), ("ao\u00fb", 8), ("aou", 8), ("sep", 9), ("set", 9),
    ("oct", 10), ("out", 10), ("nov", 11), ("dec", 12), ("dez", 12), ("d\u00e9c", 12), ("dic", 12),
)


def _mon_num(token):
    """월명 문자열 → 월 번호. 판별 불가면 None."""
    t = (token or "").lower()
    for pre, num in _MON_PREFIX:
        if t.startswith(pre):
            return num
    return None


def _pick_year(base_date, m, d):
    """연도가 없는 표기의 연도를 기준일에 가장 가까운 쪽으로 추정."""
    best = None
    for y in (base_date.year - 1, base_date.year, base_date.year + 1):
        try:
            cand = date(y, m, d)
        except ValueError:
            continue
        if best is None or abs((cand - base_date).days) < abs((best - base_date).days):
            best = cand
    return best

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


def _iter_source_dates(txt, base_date):
    """원문 텍스트에서 (월, 일)을 함께 읽어 date 객체로 복원한다."""
    if not base_date:
        return
    for pat in (_PAT_MON_DAY, _PAT_DAY_MON):
        for m in pat.finditer(txt):
            mn = _mon_num(m.group("mon"))
            try:
                dd = int(m.group("day"))
            except (ValueError, TypeError):
                continue
            if not mn or not (1 <= dd <= 31):
                continue
            cand = _pick_year(base_date, mn, dd)
            if cand:
                yield cand

    for m in _PAT_KO_MD.finditer(txt):
        try:
            mn, dd = int(m.group("m")), int(m.group("day"))
        except (ValueError, TypeError):
            continue
        if not (1 <= mn <= 12 and 1 <= dd <= 31):
            continue
        cand = _pick_year(base_date, mn, dd)
        if cand:
            yield cand

    for m in _PAT_ISO.finditer(txt):
        try:
            cand = date(int(m.group("y")), int(m.group("m")), int(m.group("day")))
        except (ValueError, TypeError):
            continue
        yield cand

    for m in _PAT_SLASH.finditer(txt):
        try:
            a, b, y = int(m.group("a")), int(m.group("b")), int(m.group("y"))
        except (ValueError, TypeError):
            continue
        for mm, dd in ((b, a), (a, b)):        # d/m/Y·m/d/Y 양쪽 후보
            try:
                yield date(y, mm, dd)
            except ValueError:
                continue


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
        pub_d = None
        if raw_pub:
            d = _parse_pub_date(raw_pub)
            if d and not (base_date and abs((d - base_date).days) > EVIDENCE_WINDOW_DAYS):
                has_pubdate = True
                pub_d = d
                # 발행일 당일과 전날(전날 사건을 다음날 보도하는 경우가 흔하다)
                days.add(d.day)
                days.add((d - timedelta(days=1)).day)

        txt = " ".join(str(s.get(k) or "") for k in ("full_text", "title_en", "summary_en"))
        if not txt.strip():
            continue
        total_len += len(txt)
        txt = txt[:20000]

        # 월+일을 함께 읽어 완전한 date로 복원한 뒤, 기준일에서 먼 날짜는 버린다.
        for cand in _iter_source_dates(txt, base_date):
            if base_date and abs((cand - base_date).days) > EVIDENCE_WINDOW_DAYS:
                continue
            days.add(cand.day)

        # 한국어 "N일(현지시간)" 단독 표기 — 월이 없어 완전 복원이 불가능하므로
        # 소스 발행일(없으면 기준일) 주변 창 안에 해당 일자가 실재할 때만 인정한다.
        ref = pub_d or base_date
        if ref:
            for m in _PAT_KO_DAY.finditer(txt):
                try:
                    dd = int(m.group("day"))
                except (ValueError, TypeError):
                    continue
                if not (1 <= dd <= 31):
                    continue
                for off in range(-KO_DAY_BACK, KO_DAY_FWD + 1):
                    if (ref + timedelta(days=off)).day == dd:
                        days.add(dd)
                        break

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
