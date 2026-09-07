"""
market_calendar.py — NYSE/NASDAQ 휴장일 공용 캘린더

us_market_holiday_writer.py에만 있던 걸 분리(2026-09-07) — ny_market_open_writer.py도
같은 캘린더가 필요했다. 실사고: ny_market_open_writer.py의 market_just_opened()가
주말(weekday>=5)만 걸러내고 평일 휴장일(노동절 등)은 걸러내지 못해, 2026-09-07
노동절 당일 09:30~09:59 ET 창 동안 cron 호출마다 매번 개장 데이터 수집을
시도하다 실패를 반복했다(시장이 실제로 안 열렸으니 정상적인 개장 데이터가
없음) — 매 호출이 Gemini까지 호출한 뒤에야 실패했을 가능성이 있어 API 낭비.

출처: https://www.nyse.com/markets/hours-calendars (2026-09-07 확인)
⚠️ 매년 초 NYSE 공식 캘린더에서 재확인 후 다음 연도분을 추가할 것.
"""

from datetime import date, timedelta

HOLIDAYS_BY_YEAR = {
    2026: [
        (date(2026, 1, 1), "신정(New Year's Day)"),
        (date(2026, 1, 19), "마틴 루서 킹 데이(Martin Luther King, Jr. Day)"),
        (date(2026, 2, 16), "워싱턴 탄생일(Presidents' Day)"),
        (date(2026, 4, 3), "성금요일(Good Friday)"),
        (date(2026, 5, 25), "메모리얼 데이(Memorial Day)"),
        (date(2026, 6, 19), "노예해방기념일(Juneteenth)"),
        (date(2026, 9, 7), "노동절(Labor Day)"),
        (date(2026, 11, 26), "추수감사절(Thanksgiving Day)"),
        (date(2026, 12, 25), "크리스마스(Christmas Day)"),
    ],
    2027: [
        (date(2027, 1, 1), "신정(New Year's Day)"),
        (date(2027, 1, 18), "마틴 루서 킹 데이(Martin Luther King, Jr. Day)"),
        (date(2027, 2, 15), "워싱턴 탄생일(Presidents' Day)"),
        (date(2027, 3, 26), "성금요일(Good Friday)"),
        (date(2027, 5, 31), "메모리얼 데이(Memorial Day)"),
        (date(2027, 6, 18), "노예해방기념일(Juneteenth)"),
        (date(2027, 9, 6), "노동절(Labor Day)"),
        (date(2027, 11, 25), "추수감사절(Thanksgiving Day)"),
        (date(2027, 12, 24), "크리스마스(Christmas Day)"),
    ],
}


def holiday_name(d: date) -> str | None:
    """d가 NYSE 휴장일이면 휴장 사유명을, 아니면 None을 반환.

    연도가 HOLIDAYS_BY_YEAR에 없으면(캘린더 갱신 누락) 조용히 실패하는 대신
    경고를 남기고 None을 반환한다 — 틀린 날짜를 지어내는 것보다 그냥
    정상 거래일로 취급하는 게 낫다(휴장일 오탐보다 미탐이 안전)."""
    year_holidays = HOLIDAYS_BY_YEAR.get(d.year)
    if year_holidays is None:
        print(f"  ⚠️ {d.year}년 NYSE 휴장일 캘린더 미등록 — market_calendar.HOLIDAYS_BY_YEAR 갱신 필요")
        return None
    for hd, name in year_holidays:
        if hd == d:
            return name
    return None


def is_us_market_closed(d: date) -> bool:
    """d에 뉴욕 증시가 열리지 않으면(주말 또는 NYSE 휴장일) True."""
    if d.weekday() >= 5:
        return True
    return holiday_name(d) is not None


def previous_trading_date(from_date: date) -> date:
    """from_date 이전의 가장 최근 미국 증시 영업일(주말·NYSE 휴장일 제외)을 찾는다."""
    d = from_date - timedelta(days=1)
    while is_us_market_closed(d):
        d -= timedelta(days=1)
    return d


# ── 도쿄증권거래소(JPX) 휴장일 ────────────────────────────────────
# 출처: JPX 공식 사이트(jpx.co.jp)가 자동 조회 도구에서 403으로 막혀
# 직접 확인은 못 했다 — tradinghours.com/calendarlabs.com 등 복수의
# 독립적인 거래시간 전문 집계 사이트가 교차 검증되는 선에서만 반영했다
# (2026-09-08). ⚠️ 공식 소스가 아니므로 다른 캘린더보다 신뢰도가 낮다 —
# 매년 초 가능하면 JPX 공식 사이트로 재확인할 것.
JP_HOLIDAYS_BY_YEAR = {
    2026: [
        (date(2026, 1, 1), "신정(元日)"),
        (date(2026, 1, 2), "휴장일(신년연휴)"),
        (date(2026, 1, 12), "성년의 날(成人の日)"),
        (date(2026, 2, 11), "건국기념일(建国記念の日)"),
        (date(2026, 2, 23), "일왕탄생일(天皇誕生日)"),
        (date(2026, 3, 20), "춘분의 날(春分の日)"),
        (date(2026, 4, 29), "쇼와의 날(昭和の日)"),
        (date(2026, 5, 4), "녹색의 날(みどりの日)"),
        (date(2026, 5, 5), "어린이날(こどもの日)"),
        (date(2026, 5, 6), "헌법기념일 대체휴일"),
        (date(2026, 8, 11), "산의 날(山の日)"),
        (date(2026, 9, 21), "경로의 날(敬老の日)"),
        (date(2026, 9, 22), "국민휴일(브릿지 홀리데이)"),
        (date(2026, 9, 23), "추분의 날(秋分の日)"),
        (date(2026, 10, 12), "체육의 날(スポーツの日)"),
        (date(2026, 11, 3), "문화의 날(文化の日)"),
        (date(2026, 11, 23), "근로감사의 날(勤労感謝の日)"),
        (date(2026, 12, 31), "연말 휴장일"),
    ],
}


def jp_holiday_name(d: date) -> str | None:
    """d에 도쿄증권거래소(JPX)가 휴장이면(주말 제외) 휴장 사유명을, 아니면 None을 반환."""
    year_holidays = JP_HOLIDAYS_BY_YEAR.get(d.year)
    if year_holidays is None:
        return None
    for hd, name in year_holidays:
        if hd == d:
            return name
    return None


def is_jp_market_closed(d: date) -> bool:
    """d에 도쿄증권거래소가 열리지 않으면(주말 또는 휴장일) True."""
    if d.weekday() >= 5:
        return True
    return jp_holiday_name(d) is not None


def jp_previous_trading_date(from_date: date) -> date:
    """from_date 이전의 가장 최근 일본 증시 영업일(주말·JPX 휴장일 제외)을 찾는다."""
    d = from_date - timedelta(days=1)
    while is_jp_market_closed(d):
        d -= timedelta(days=1)
    return d
