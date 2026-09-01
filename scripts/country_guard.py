"""국가명 정규화 가드.

Gemini가 반환한 `국가`/`country` 값을 사이트가 쓰는 표준 표기로 통일한다.
검증이 없으면 같은 나라가 "한국"/"대한민국"/"South Korea" 등 서로 다른
문자열로 저장되고, 그 결과 국가명을 정확일치로 비교하는 로직(예:
find_similar_trend()의 country=eq. 필터)이 같은 사건을 다른 나라로 오판해
중복을 걸러내지 못한다.

실사고(2026-09-02): gemini_writer.py에는 이 정규화가 있었지만
gemini_summarizer.py의 트렌드 생성 3경로(run_trend_tracker/
run_realtime_trend_tracker/run_external_trend_articles)에는 없어서, 같은
"한학자 총재 징역 2년" 사건이 country="한국"과 "대한민국"으로 각각 저장돼
find_similar_trend()가 후보로 비교조차 못 하고 트렌드 기사가 중복 발행됨
(id=119633, id=120650). category_guard.py와 같은 이유("패턴 B — 공통 유틸
분리")로 별도 모듈로 뺀다 — 소비자 쪽은 try/except import 폴백으로 감싼다.
"""

COUNTRY_ALIASES = {
    "대한민국": "한국", "남한": "한국", "south korea": "한국", "korea": "한국",
    "미국": "미국", "usa": "미국", "united states": "미국",
    "중국": "중국", "china": "중국",
    "일본": "일본", "japan": "일본",
    "나이지리아": "나이지리아", "nigeria": "나이지리아",
    "케냐": "케냐", "kenya": "케냐",
    "남아프리카공화국": "남아공", "남아프리카": "남아공", "south africa": "남아공",
    "베트남": "베트남", "vietnam": "베트남",
    "인도네시아": "인도네시아", "indonesia": "인도네시아",
    "태국": "태국", "thailand": "태국",
    "필리핀": "필리핀", "philippines": "필리핀",
    "이집트": "이집트", "egypt": "이집트",
    "사우디": "사우디아라비아", "사우디아라비아": "사우디아라비아", "saudi arabia": "사우디아라비아",
    "uae": "아랍에미리트", "아랍에미리트": "아랍에미리트",
    "튀르키예": "튀르키예", "터키": "튀르키예", "turkey": "튀르키예",
    "인도": "인도", "india": "인도",
}


def normalize_country(country: str) -> str:
    """Gemini가 생성한 국가명을 표준 표기로 통일한다."""
    if not country:
        return ""
    key = country.strip().lower()
    for alias, standard in COUNTRY_ALIASES.items():
        if alias.lower() == key:
            return standard
    return country.strip()
