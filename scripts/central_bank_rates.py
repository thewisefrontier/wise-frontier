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

이 모듈이 값을 못 찾으면(다른 나라, 또는 위 소스가 일시적으로 실패)
None을 반환한다 — econ_writer.py는 그 경우 기존 Gemini 검색 경로로
자연스럽게 폴백한다.
"""

import re
import requests
from datetime import date, datetime

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


def _fetch_fred_latest(series_id: str, timeout: int = 15) -> tuple[float, date] | None:
    """FRED 공개 CSV 엔드포인트(키 불필요)에서 시리즈의 가장 최근 값을 가져온다."""
    try:
        res = requests.get(_FRED_CSV_URL, params={"id": series_id}, timeout=timeout)
        if res.status_code != 200:
            return None
        lines = [ln.strip() for ln in res.text.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        # 마지막 줄부터 역순으로 값이 있는(결측치 "." 아닌) 최신 행을 찾는다.
        for line in reversed(lines[1:]):
            parts = line.split(",")
            if len(parts) != 2:
                continue
            d_str, v_str = parts
            if v_str in (".", ""):
                continue
            try:
                return float(v_str), datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue
        return None
    except Exception as e:
        print(f"  [WARN] FRED 조회 실패 ({series_id}): {e}")
        return None


def _fetch_boe_latest(timeout: int = 15) -> tuple[float, date] | None:
    """영란은행 공식 IADB CSV(키 불필요)에서 Bank Rate 최신값을 가져온다."""
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
        last_date, last_val = None, None
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
            last_date, last_val = d, v
        if last_val is None:
            return None
        return last_val, last_date
    except Exception as e:
        print(f"  [WARN] BOE 조회 실패: {e}")
        return None


def fetch_official_rate(country: str) -> tuple[float, date] | None:
    """국가명으로 공식 정책금리 최신값을 조회. (rate, as_of_date) 또는 None.

    지원: 미국(FRED)/유로존(FRED)/영국(BOE). 그 외(일본 포함)는 None —
    호출부(econ_writer.py)가 기존 Gemini 검색 경로로 폴백한다."""
    if country in _FRED_SERIES:
        return _fetch_fred_latest(_FRED_SERIES[country])
    if country == "영국":
        return _fetch_boe_latest()
    return None
