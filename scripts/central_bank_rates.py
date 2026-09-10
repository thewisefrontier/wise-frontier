"""
central_bank_rates.py — 주요국 중앙은행 정책금리 공식 데이터 조회

사용자 신고(2026-09-08): "해외 금리 관련 기사도 거의 나오질 않고 있는데,
github 오픈소스 중에 우리쪽에서 이용할만한게 있지 않을까". 조사 결과
econ_writer.py는 금리결정 이벤트의 실제 값을 Gemini 검색(그것도 독립
2회 호출이 서로 일치해야 통과)에만 의존하는데, DB 확인 결과 나이지리아·
태국·필리핀·이집트·남아공 등 최근 발표된 금리 이벤트가 전부
actual_value=null로 남아있었다 — 사실상 이 검증이 거의 성공하지 못하고
있었고, 실패한 이벤트는 MAX_LOOKBACK_DAYS(5일)가 지나면 영구히 처리
대상에서 빠진다.

Gemini 검색 대신 각 중앙은행의 공식(또는 그에 준하는) 데이터 소스를
직접 조회한다 — 검색 결과 GitHub 라이브러리보다 각 기관의 무료 공식
API/CSV가 더 낫다고 판단(2026-09-08 실측 확인):
- 미국 연준·ECB: FRED(세인트루이스 연준) 공식 API. api_key 없이도
  쓸 수 있는 fredgraph.csv 공개 엔드포인트로 충분(차트 임베드용으로
  공식 제공되는 엔드포인트, 키 불필요).
- 영국(BOE): 영란은행 자체 공식 통계 데이터베이스(IADB) CSV API,
  키 불필요.
- 일본(BOJ): 결론적으로 못 붙였다. FRED의 일본 관련 시리즈는
  전부 유의미하게 지연되거나(월간, OECD 경유) 중단된 상태였고,
  BOJ 공식 사이트는 일별 데이터를 엑셀 파일로만 제공해 이 스크립트
  규모에서 안정적으로 파싱하기 부담스러웠다. BOJ는 당분간 기존
  econ_writer.py의 Gemini 검색 경로를 그대로 쓴다.

이 모듈이 값을 못 찾으면(다른 나라, 또는 위 소스가 일시적으로 실패,
또는 아래 "발효 지연" 문제로 판단 불가) None을 반환한다 — econ_writer.py는
그 경우 기존 Gemini 검색 경로로 자연스럽게 폴백한다.

2026-09-11 추가 — 실사고(id=156302, ECB 9/10 회의 기사가 실제 새 금리
2.50%가 아니라 회의 직전 금리 2.25%를 그대로 "결정"으로 오채택해 발행됨,
사용자 신고): ECB는 회의 당일(9/10) 발표하지만 새 금리는 통상 그 다음
영업주 초(이번엔 9/16)부터 "발효"되고, FRED의 ECBDFR 일별 시계열은
발효일이 지나야 새 값으로 바뀐다 — 즉 회의 다음날 이 모듈을 호출하면
"최신값의 날짜가 이벤트일 이후"라는 조건은 만족하지만, 실제로는 아직
안 바뀐 회의 이전 금리를 그대로 돌려주고 있었다(FRED가 일별로 옛값을
그대로 이어붙이기 때문에 "날짜만 최신"이라는 착시가 생김). 단순히
"최신 관측값의 날짜 >= 이벤트일"만으로는 이 지연을 구분할 수 없다.

수정: fetch_official_rate()가 이제 "이벤트일 직전"과 "지금 시점 최신"
두 값을 함께 비교해, 값이 실제로 달라진 경우에만(=공식 소스에 진짜
새 결정이 반영된 경우에만) 신뢰하고 (rate, as_of_date, previous_rate)를
반환한다. 두 값이 같으면(아직 발효 전이라 못 바뀐 것인지, 정말 동결
결정인지 이 데이터만으로는 구분 불가) None을 반환해 호출부가 기존
Gemini 검색 경로(실제 발표 기사를 읽고 판단)로 폴백하게 한다 — "판단
불가능한 상황에서 추측하지 않는다"는 이 프로젝트의 기존 원칙(로또/
파워볼 정보 누락 시 발행 보류 등)과 동일한 방향.
"""

import re
import requests
from datetime import date, datetime, timedelta

# 국가명 → FRED 시리즈 ID (2026-09-08 fred.stlouisfed.org에서 시리즈 설명·
# 최신값 확인 — DFEDTARU: 연준 목표금리 상단, ECBDFR: ECB 예금금리).
_FRED_SERIES = {
    "미국": "DFEDTARU",
    "유로존": "ECBDFR",
}

_FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# 영란은행(BOE) 공식 통계 데이터베이스(IADB) — 키 불필요.
# IUDBEDR: Bank Rate(정책금리) 공식 시리즈 코드(2026-09-08 확인).
_BOE_IADB_URL = "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"


def _fetch_fred_series(series_id: str, timeout: int = 15) -> list[tuple[date, float]] | None:
    """FRED 공개 CSV 엔드포인트(키 불필요)에서 시리즈 전체(결측치 제외)를
    (날짜, 값) 오름차순 리스트로 반환."""
    try:
        res = requests.get(_FRED_CSV_URL, params={"id": series_id}, timeout=timeout)
        if res.status_code != 200:
            return None
        lines = [ln.strip() for ln in res.text.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        out = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) != 2:
                continue
            d_str, v_str = parts
            if v_str in (".", ""):
                continue
            try:
                out.append((datetime.strptime(d_str, "%Y-%m-%d").date(), float(v_str)))
            except ValueError:
                continue
        return out or None
    except Exception as e:
        print(f"  [WARN] FRED 조회 실패 ({series_id}): {e}")
        return None


def _fetch_boe_series(timeout: int = 15) -> list[tuple[date, float]] | None:
    """영란은행 공식 IADB CSV(키 불필요)에서 Bank Rate 시계열을
    (날짜, 값) 오름차순 리스트로 반환."""
    try:
        today = date.today()
        date_from = date(today.year - 1, today.month, today.day).strftime("%d/%b/%Y")
        date_to = today.strftime("%d/%b/%Y")
        res = requests.get(
            _BOE_IADB_URL,
            params={
                "csv.x": "yes",
                "SeriesCodes": "IUDBEDR",
                "UsingCodes": "Y",
                "CSVF": "TN",
                "Datefrom": date_from,
                "Dateto": date_to,
            },
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsFinalBot/1.0; +https://newsfinal.co.kr)"},
            timeout=timeout,
        )
        if res.status_code != 200:
            return None
        lines = [ln.strip() for ln in res.text.strip().splitlines() if ln.strip()]
        if not lines:
            return None
        out = []
        for line in lines:
            parts = line.split(",")
            if len(parts) != 2:
                continue
            d_str, v_str = parts
            try:
                d = datetime.strptime(d_str.strip(), "%d %b %Y").date()
                v = float(v_str.strip())
            except ValueError:
                continue
            out.append((d, v))
        out.sort(key=lambda t: t[0])
        return out or None
    except Exception as e:
        print(f"  [WARN] BOE 조회 실패: {e}")
        return None


def _resolve_from_series(series: list[tuple[date, float]], event_date: str | None) -> tuple[float, date, float | None] | None:
    """시계열에서 최신값과, event_date 이전 마지막 관측값을 비교해 "진짜로
    바뀐 경우"에만 (최신값, 최신일, 직전값)을 반환. event_date가 없으면
    발효 지연을 검증할 기준이 없으므로 (최신값, 최신일, 시계열상 마지막으로
    달랐던 과거값)을 그대로 반환한다(기존 단순 조회 호출부 호환용)."""
    if not series:
        return None
    latest_date, latest_val = series[-1]

    if event_date is None:
        prev_val = None
        for d, v in reversed(series[:-1]):
            if v != latest_val:
                prev_val = v
                break
        return latest_val, latest_date, prev_val

    try:
        ev = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        ev = None

    if ev is not None:
        pre_val = None
        for d, v in reversed(series):
            if d < ev:
                pre_val = v
                break
        if pre_val is not None and pre_val == latest_val:
            # 이벤트일 이전 값과 "지금 최신값"이 같음 — 정말 동결인지,
            # 아직 발효 전이라 안 바뀐 것인지 이 데이터만으론 판단 불가.
            # 추측하지 않고 폴백시킨다.
            return None
        if pre_val is not None:
            return latest_val, latest_date, pre_val

    # event_date를 시계열에서 못 찾은 경우(너무 최근 등) — 기존 방식대로
    # 마지막으로 값이 달랐던 과거값을 직전값으로 사용.
    prev_val = None
    for d, v in reversed(series[:-1]):
        if v != latest_val:
            prev_val = v
            break
    return latest_val, latest_date, prev_val


def fetch_official_rate(country: str, event_date: str | None = None) -> tuple[float, date, float | None] | None:
    """국가명으로 공식 정책금리 최신값을 조회.

    반환: (최신 금리, 최신값 기준일, 직전(변경 전) 금리) 또는 None.
    직전 금리는 못 찾으면 None(그래도 최신값은 신뢰 가능).

    event_date(이벤트 발표일, "YYYY-MM-DD")를 넘기면, 그 날짜 이전 마지막
    관측값과 "지금 최신값"을 비교해 실제로 값이 바뀐 경우에만 반환한다 —
    같으면 아직 공식 소스에 반영 안 된 것일 수 있어 None을 반환해 호출부가
    Gemini 검색 등 다른 경로로 폴백하게 한다(추측 금지 원칙).

    지원: 미국(FRED)/유로존(FRED)/영국(BOE). 그 외(일본 포함)는 None —
    호출부(econ_writer.py)가 기존 Gemini 검색 경로로 폴백한다."""
    if country in _FRED_SERIES:
        series = _fetch_fred_series(_FRED_SERIES[country])
    elif country == "영국":
        series = _fetch_boe_series()
    else:
        return None
    return _resolve_from_series(series, event_date)
