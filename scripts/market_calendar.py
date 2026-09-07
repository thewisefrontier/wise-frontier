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
# 출처: JPX 공식 사이트(jpx.co.jp/english/corporate/about-jpx/calendar,
# "Update: Feb. 06, 2026" 명시 — 2026-09-08 브라우저로 직접 확인,
# 자동조회 도구는 403으로 막혀 있었음). exchange_calendars 오픈소스
# 라이브러리로 먼저 만든 초안과 날짜가 전부 일치해 그대로 확정했다 —
# 다만 2027-03-22(춘분 대체휴일)·2027-09-23(추분의 날, 단독)은 최초
# 라이브러리 조회 시 이름을 확신 못 해 "추정"으로 남겨뒀던 부분인데
# 공식 사이트로 명칭까지 확정했다.
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
        (date(2026, 7, 20), "바다의 날(海の日)"),
        (date(2026, 8, 11), "산의 날(山の日)"),
        (date(2026, 9, 21), "경로의 날(敬老の日)"),
        (date(2026, 9, 22), "국민휴일(브릿지 홀리데이)"),
        (date(2026, 9, 23), "추분의 날(秋分の日)"),
        (date(2026, 10, 12), "체육의 날(スポーツの日)"),
        (date(2026, 11, 3), "문화의 날(文化の日)"),
        (date(2026, 11, 23), "근로감사의 날(勤労感謝の日)"),
        (date(2026, 12, 31), "연말 휴장일"),
    ],
    2027: [
        (date(2027, 1, 1), "신정(元日)"),
        (date(2027, 1, 11), "성년의 날(成人の日)"),
        (date(2027, 2, 11), "건국기념일(建国記念の日)"),
        (date(2027, 2, 23), "일왕탄생일(天皇誕生日)"),
        (date(2027, 3, 22), "춘분의 날 대체휴일(3/21 일요일)"),
        (date(2027, 4, 29), "쇼와의 날(昭和の日)"),
        (date(2027, 5, 3), "헌법기념일(憲法記念日)"),
        (date(2027, 5, 4), "녹색의 날(みどりの日)"),
        (date(2027, 5, 5), "어린이날(こどもの日)"),
        (date(2027, 7, 19), "바다의 날(海の日)"),
        (date(2027, 8, 11), "산의 날(山の日)"),
        (date(2027, 9, 20), "경로의 날(敬老の日)"),
        (date(2027, 9, 23), "추분의 날(秋分の日)"),
        (date(2027, 10, 11), "체육의 날(スポーツの日)"),
        (date(2027, 11, 3), "문화의 날(文化の日)"),
        (date(2027, 11, 23), "근로감사의 날(勤労感謝の日)"),
        (date(2027, 12, 31), "연말 휴장일"),
    ],
}


# ── 런던·프랑크푸르트·파리 증권거래소 휴장일 ─────────────────────
# 최초 출처: exchange_calendars 라이브러리(2026-09-08, pip install
# exchange_calendars로 XLON/XFRA/XPAR 직접 조회 — quantopian/
# trading_calendars를 이어받은 커뮤니티 유지보수 오픈소스). 사용자
# 제안("github나 [캘린더 받아올 수 있는 사이트] 없어?")으로 각 거래소
# 공식 사이트 스크래핑(자동조회 도구는 403으로 막힘) 대신 채택.
#
# 이후 브라우저 도구로 각 거래소 공식 사이트를 직접 열어 대조 완료
# (2026-09-08, 전부 라이브러리 데이터와 일치해 그대로 확정):
# - 영국: londonstockexchange.com/equities-trading/business-days
#   (12/24·12/31은 "half day"일 뿐 완전 휴장이 아님을 확인 — 이미
#   반영돼 있었음)
# - 독일: cashmarket.deutsche-boerse.com "Trading Calendar and
#   Trading Hours" — 2026·2027 전부 공식 확인
# - 프랑스: live.euronext.com "2026 Holiday Calendar" — 2026만 공식
#   발표됨(2027은 아직 미발표라 라이브러리 값 유지, 연말경 재확인 필요)
# ⚠️ 그래도 매년 다음 연도분 공개 후 재확인할 것 — 특히 프랑스 2027.
UK_HOLIDAYS_BY_YEAR = {
    2026: [
        (date(2026, 1, 1), "신정"),
        (date(2026, 4, 3), "성금요일(Good Friday)"),
        (date(2026, 4, 6), "부활절 월요일(Easter Monday)"),
        (date(2026, 5, 4), "5월 초 은행휴일(Early May Bank Holiday)"),
        (date(2026, 5, 25), "봄 은행휴일(Spring Bank Holiday)"),
        (date(2026, 8, 31), "여름 은행휴일(Summer Bank Holiday)"),
        (date(2026, 12, 25), "크리스마스"),
        (date(2026, 12, 28), "박싱데이 대체휴일(Boxing Day observed)"),
    ],
    2027: [
        (date(2027, 1, 1), "신정"),
        (date(2027, 3, 26), "성금요일(Good Friday)"),
        (date(2027, 3, 29), "부활절 월요일(Easter Monday)"),
        (date(2027, 5, 3), "5월 초 은행휴일(Early May Bank Holiday)"),
        (date(2027, 5, 31), "봄 은행휴일(Spring Bank Holiday)"),
        (date(2027, 8, 30), "여름 은행휴일(Summer Bank Holiday)"),
        (date(2027, 12, 27), "크리스마스 대체휴일(Christmas observed)"),
        (date(2027, 12, 28), "박싱데이 대체휴일(Boxing Day observed)"),
    ],
}

DE_HOLIDAYS_BY_YEAR = {
    2026: [
        (date(2026, 1, 1), "신정"),
        (date(2026, 4, 3), "성금요일(Karfreitag)"),
        (date(2026, 4, 6), "부활절 월요일(Ostermontag)"),
        (date(2026, 5, 1), "노동절(Tag der Arbeit)"),
        (date(2026, 12, 24), "크리스마스 이브"),
        (date(2026, 12, 25), "크리스마스"),
        (date(2026, 12, 31), "연말 휴장일"),
    ],
    2027: [
        (date(2027, 1, 1), "신정"),
        (date(2027, 3, 26), "성금요일(Karfreitag)"),
        (date(2027, 3, 29), "부활절 월요일(Ostermontag)"),
        (date(2027, 12, 24), "크리스마스 이브"),
        (date(2027, 12, 31), "연말 휴장일"),
    ],
}

FR_HOLIDAYS_BY_YEAR = {
    2026: [
        (date(2026, 1, 1), "신정"),
        (date(2026, 4, 3), "성금요일(Vendredi saint)"),
        (date(2026, 4, 6), "부활절 월요일(Lundi de Pâques)"),
        (date(2026, 5, 1), "노동절(Fête du Travail)"),
        (date(2026, 12, 25), "크리스마스"),
    ],
    2027: [
        (date(2027, 1, 1), "신정"),
        (date(2027, 3, 26), "성금요일(Vendredi saint)"),
        (date(2027, 3, 29), "부활절 월요일(Lundi de Pâques)"),
    ],
}


# ── 요하네스버그·인도네시아·말레이시아 증권거래소 휴장일 ───────────
# 사용자 요청(2026-09-08): "아프리카, 동남아쪽은 어떻게 소스를 못구하나?"
# — exchange_calendars 라이브러리(XJSE/XIDX/XKLS)로 조회. 각 거래소
# 공식 사이트는 Cloudflare 봇 차단(JSE)이나 접근 제한(Bursa Malaysia)에
# 막혀 직접 확인은 못 했다 — 대신 날짜를 남아공/인도네시아의 잘 알려진
# 고정 공휴일 목록과 요일 계산으로 교차검증했다(전부 일치 확인).
# ⚠️ 말레이시아는 다종교 국가라 이슬람력·힌두력 기반 이동휴일이 많아
# 라이브러리가 날짜는 정확히 계산해도(실제 거래 스케줄 기반) 절반 가까이
# 구체적 명칭을 못 붙였다 — 확신 없는 명칭을 지어내지 않고 "이동휴일
# (공휴일)"로 표기한다. 매년 초 가능하면 각 거래소 공식 캘린더로
# 명칭을 보강할 것.
ZA_HOLIDAYS_BY_YEAR = {
    2026: [
        (date(2026, 1, 1), "신정"),
        (date(2026, 4, 3), "성금요일(Good Friday)"),
        (date(2026, 4, 6), "패밀리 데이(Family Day)"),
        (date(2026, 4, 27), "자유의 날(Freedom Day)"),
        (date(2026, 5, 1), "노동절(Workers' Day)"),
        (date(2026, 6, 16), "청년의 날(Youth Day)"),
        (date(2026, 8, 10), "여성의 날 대체휴일(National Women's Day observed)"),
        (date(2026, 9, 24), "문화유산의 날(Heritage Day)"),
        (date(2026, 12, 16), "화해의 날(Day of Reconciliation)"),
        (date(2026, 12, 25), "크리스마스"),
    ],
    2027: [
        (date(2027, 1, 1), "신정"),
        (date(2027, 3, 22), "인권의 날(Human Rights Day)"),
        (date(2027, 3, 26), "성금요일(Good Friday)"),
        (date(2027, 3, 29), "패밀리 데이(Family Day)"),
        (date(2027, 4, 27), "자유의 날(Freedom Day)"),
        (date(2027, 6, 16), "청년의 날(Youth Day)"),
        (date(2027, 8, 9), "여성의 날(National Women's Day)"),
        (date(2027, 9, 24), "문화유산의 날(Heritage Day)"),
        (date(2027, 12, 16), "화해의 날(Day of Reconciliation)"),
        (date(2027, 12, 27), "굿윌 데이 대체휴일(Day of Goodwill observed)"),
    ],
}

ID_HOLIDAYS_BY_YEAR = {
    2026: [
        (date(2026, 1, 1), "신정"),
        (date(2026, 2, 17), "음력설(Imlek/Chinese New Year)"),
        (date(2026, 4, 3), "성금요일(Good Friday)"),
        (date(2026, 5, 1), "노동절(Labor Day)"),
        (date(2026, 5, 14), "예수승천일(Ascension Day)"),
        (date(2026, 6, 1), "판차실라의 날(Pancasila Day)"),
        (date(2026, 8, 17), "독립기념일(Independence Day)"),
        (date(2026, 12, 25), "크리스마스"),
        (date(2026, 12, 31), "연말 휴장일"),
    ],
    2027: [
        (date(2027, 1, 1), "신정"),
        (date(2027, 3, 26), "성금요일(Good Friday)"),
        (date(2027, 5, 6), "예수승천일(Ascension Day)"),
        (date(2027, 6, 1), "판차실라의 날(Pancasila Day)"),
        (date(2027, 8, 17), "독립기념일(Independence Day)"),
        (date(2027, 12, 31), "연말 휴장일"),
    ],
}

MY_HOLIDAYS_BY_YEAR = {
    2026: [
        (date(2026, 1, 1), "신정"),
        (date(2026, 2, 2), "연방 직할구의 날(Federal Territory Day)"),
        (date(2026, 2, 17), "이동휴일(공휴일, 음력설 추정)"),
        (date(2026, 2, 18), "이동휴일(공휴일, 음력설 추정)"),
        (date(2026, 3, 23), "이동휴일(공휴일)"),
        (date(2026, 5, 1), "노동절(Labour Day)"),
        (date(2026, 5, 27), "이동휴일(공휴일)"),
        (date(2026, 6, 1), "이동휴일(공휴일)"),
        (date(2026, 6, 17), "이동휴일(공휴일)"),
        (date(2026, 8, 25), "이동휴일(공휴일)"),
        (date(2026, 8, 31), "독립기념일(National Day)"),
        (date(2026, 9, 16), "말레이시아의 날(Malaysia Day)"),
        (date(2026, 11, 9), "이동휴일(공휴일)"),
        (date(2026, 12, 25), "크리스마스"),
    ],
    2027: [
        (date(2027, 1, 1), "신정"),
        (date(2027, 1, 22), "이동휴일(공휴일)"),
        (date(2027, 2, 1), "연방 직할구의 날(Federal Territory Day)"),
        (date(2027, 2, 8), "이동휴일(공휴일, 음력설 추정)"),
        (date(2027, 2, 24), "이동휴일(공휴일)"),
        (date(2027, 3, 9), "이동휴일(공휴일)"),
        (date(2027, 3, 10), "이동휴일(공휴일)"),
        (date(2027, 5, 17), "이동휴일(공휴일)"),
        (date(2027, 5, 20), "이동휴일(공휴일)"),
        (date(2027, 6, 7), "이동휴일(공휴일)"),
        (date(2027, 8, 31), "독립기념일(National Day)"),
        (date(2027, 9, 16), "말레이시아의 날(Malaysia Day)"),
        (date(2027, 10, 28), "이동휴일(공휴일)"),
    ],
}


def _make_holiday_helpers(holidays_by_year: dict):
    def holiday_name_fn(d: date) -> str | None:
        year_holidays = holidays_by_year.get(d.year)
        if year_holidays is None:
            return None
        for hd, name in year_holidays:
            if hd == d:
                return name
        return None

    def is_closed_fn(d: date) -> bool:
        if d.weekday() >= 5:
            return True
        return holiday_name_fn(d) is not None

    def previous_trading_fn(from_date: date) -> date:
        d = from_date - timedelta(days=1)
        while is_closed_fn(d):
            d -= timedelta(days=1)
        return d

    return holiday_name_fn, is_closed_fn, previous_trading_fn


uk_holiday_name, is_uk_market_closed, uk_previous_trading_date = _make_holiday_helpers(UK_HOLIDAYS_BY_YEAR)
de_holiday_name, is_de_market_closed, de_previous_trading_date = _make_holiday_helpers(DE_HOLIDAYS_BY_YEAR)
fr_holiday_name, is_fr_market_closed, fr_previous_trading_date = _make_holiday_helpers(FR_HOLIDAYS_BY_YEAR)
jp_holiday_name, is_jp_market_closed, jp_previous_trading_date = _make_holiday_helpers(JP_HOLIDAYS_BY_YEAR)
za_holiday_name, is_za_market_closed, za_previous_trading_date = _make_holiday_helpers(ZA_HOLIDAYS_BY_YEAR)
id_holiday_name, is_id_market_closed, id_previous_trading_date = _make_holiday_helpers(ID_HOLIDAYS_BY_YEAR)
my_holiday_name, is_my_market_closed, my_previous_trading_date = _make_holiday_helpers(MY_HOLIDAYS_BY_YEAR)
