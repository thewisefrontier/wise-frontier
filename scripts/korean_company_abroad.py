# -*- coding: utf-8 -*-
"""scripts/korean_company_abroad.py
-----------------------------------
해외에서 보도된 한국 기업 소식 판별 — 2026-10-04 신설(사용자 지시: "해외에서 일어난
한국 기업의 사건사고는 중요하게 보도한다", 이어서 "실적, 신제품, 투자, 수상 모두
포함해", 마지막으로 "한국기업 리스트를 만들어서 거기에 들어가는 건 바로 걸리도록").
즉 키워드 사전은 "포함 여부"가 아니라 "우선순위 등급"만 가른다 — 회사명(리스트)이
주체로 나오고 해외 발생이 확인되면, 구체적인 사건·실적 문구가 전혀 없어도 최소
우선순위(3)로는 반드시 걸린다. 우선순위 3단계: 1=사건사고·법적 문제(소송·리콜·결함·
과징금·조사·화재 등), 2=실적·투자·수주·제휴, 3=그 외 전부(신제품·수상 포함). 어느
층이든 국내 미보도면 gemini_writer.py의 kr_coverage 재랭킹이 추가로 가산한다(이
모듈은 그 입력만 만든다).

오탐 방지(2026-10-04 raw_candidates 실측 기반):
- LG/SK/GS/CJ/KIA처럼 흔한 약어는 단독으로 매칭하지 않는다. 실측에서 전부 걸렸다 —
  필리핀 "SK"(Sangguniang Kabataan 청년의회) 선거 기사, 나이지리아 "LG"(지방정부)
  의장 취임, 델리 "LG"(부지사) 지시, 미얀마 반군 "KIA"(Kachin Independence Army)
  고지 점령 기사. 정식 계열사명(LG Electronics, SK Hynix 등)으로만 매칭하고,
  "Kia"(기아차)는 대문자 전체(KIA=카친독립군)와 구분하기 위해 대소문자를 구분해서 매칭한다.
- 회사명이 제목 또는 리드 맨 앞(앞 30자 — 영어 기사 리드 문장의 주어 위치)에 나와야
  인정. "Shares of TSMC led gains, with Samsung, SK Hynix also rising" 같은 실측
  사례(2026-10-04)처럼 리드 뒤쪽 나열에 끼어든 경우는 30자를 넘겨 제외된다 — 완벽하진
  않지만(진짜 리드 주어인데 30자를 넘는 긴 수식어가 앞에 붙는 사례는 놓침) 실측에서
  관찰된 오탐 패턴을 정확히 걸러내면서 코드가 단순하다.
- "해외 발생" 판정은 저장된 country 필드를 신뢰하지 않는다. geo_detect.py의
  COUNTRY_INFO는 "korean"이라는 형용사 하나로도 국가를 '한국'으로 태깅하므로
  ("South Korean company"처럼 현지 발생 기사에도 거의 항상 등장), 그 필드를 그대로
  쓰면 해외 사건이 '한국' 기사로 오분류돼 선진국 게이트·국내 파이프라인에 잘못
  걸릴 위험이 있다. 대신 본문에서 geo_detect.detect_countries()로 모든 국가를 다시
  뽑아 한국이 아닌 나라가 최소 하나는 있어야 "해외"로 인정한다 — 보수적 기준이라
  기사에 현지국 지명이 전혀 안 나오는 사례는 놓칠 수 있다(한계, 재검토 필요시 개선).
"""
import re

from geo_detect import detect_countries

TIER_BONUS = {1: 20.0, 2: 8.0, 3: 3.0}

# 회사명 별칭. 값은 (정규식, 대소문자 구분 여부) 튜플 목록.
_COMPANY_PATTERNS = {
    "삼성": [(r"samsung", True)],
    "현대차": [(r"hyundai", True)],  # HD현대(조선)도 전부 "Hyundai"로 표기되므로 함께 묶는다
    "기아": [(r"\bKia\b", False)],  # 대소문자 구분 — 전체 대문자 KIA(카친독립군)와 구분
    "LG": [(r"lg electronics", True), (r"lg energy solution", True), (r"lg chem", True),
           (r"lg display", True), (r"lg uplus", True), (r"lg u\+", True),
           (r"lg cns", True), (r"lg innotek", True), (r"lg household", True)],
    "SK": [(r"sk hynix", True), (r"sk telecom", True), (r"sk innovation", True),
           (r"\bsk on\b", True), (r"sk e&s", True), (r"sk biopharm", True),
           (r"sk square", True), (r"sk ecoplant", True), (r"sk networks", True)],
    "포스코": [(r"posco", True)],
    "한화": [(r"hanwha", True)],
    "롯데": [(r"lotte", True)],
    "CJ": [(r"cj cheiljedang", True), (r"cj logistics", True), (r"cj enm", True),
           (r"cj olive\s?young", True), (r"cj cgv", True)],
    "GS": [(r"gs caltex", True), (r"gs engineering", True), (r"gs e&c", True),
           (r"gs retail", True), (r"gs energy", True)],
    "효성": [(r"hyosung", True)],
    "두산": [(r"doosan", True)],
    "한진": [(r"hanjin", True)],
    "대한항공": [(r"korean air", True)],
    "네이버": [(r"\bnaver\b", True)],
    "카카오": [(r"\bkakao\b", True)],
    "쿠팡": [(r"\bcoupang\b", True)],
    "셀트리온": [(r"celltrion", True)],
    # 금융회사 현지법인·지점(2026-10-04 추가) — 외신은 보통 모회사명+현지 법인명을 그대로
    # 쓴다("Shinhan Bank America", "Woori America Bank" 등은 아래 패턴이 부분일치로
    # 잡는다). KB/DB/IBK/KDB처럼 흔한 2~3글자 약어는 LG/SK와 같은 이유로 단독 매칭하지
    # 않고 정식명(전체 기관명 또는 "KB+업종")으로만 매칭한다.
    "KB금융": [(r"kb kookmin bank", True), (r"\bkookmin bank\b", True),
              (r"kb financial group", True), (r"kb securities", True), (r"kb insurance", True)],
    "신한금융": [(r"shinhan bank", True), (r"shinhan financial group", True),
               (r"shinhan card", True), (r"shinhan securities", True),
               (r"shinhan investment", True), (r"shinhan life", True)],
    "하나금융": [(r"hana bank", True), (r"hana financial group", True),
               (r"keb hana bank", True), (r"hana securities", True)],
    "우리금융": [(r"woori bank", True), (r"woori financial group", True),
               (r"woori investment", True), (r"woori america bank", True)],
    "NH농협": [(r"nonghyup bank", True), (r"\bnh bank\b", True),
              (r"nh investment", True), (r"nh securities", True)],
    "IBK기업은행": [(r"industrial bank of korea", True)],
    "KDB산업은행": [(r"korea development bank", True)],
    "한국수출입은행": [(r"export-import bank of korea", True), (r"\bkexim\b", True)],
    "미래에셋": [(r"mirae asset", True)],
    "한국투자증권": [(r"korea investment (&|and) securities", True), (r"korea investment holdings", True)],
    "메리츠": [(r"meritz", True)],
    "교보생명": [(r"kyobo life", True)],
    "DB손해보험": [(r"\bdb insurance\b", True)],
}

_COMPANY_RE = {
    name: [re.compile(pat, re.I if ci else 0) for pat, ci in patterns]
    for name, patterns in _COMPANY_PATTERNS.items()
}

_TIER1_KEYWORDS = (
    r"lawsuit", r"\bsue[sd]?\b", r"\bsuing\b", r"class action", r"complaint filed",
    r"\bdefect", r"\bleak(s|ed|age)?\b", r"recall(s|ed|ing)?", r"\binjur", r"\bfine[sd]?\b",
    r"penalt", r"\bprobe\b", r"investigat", r"antitrust", r"settlement", r"injunction",
    r"\bban(ned|s)?\b", r"\bfire\b", r"explosion", r"\braid(ed|s)?\b", r"\bstrike[sd]?\b",
    r"detained", r"arrest(ed|s)?", r"data breach", r"privacy violation", r"labor dispute",
    r"price.fixing", r"dumping", r"\btariff", r"unfair practice",
    r"소송", r"피소", r"집단소송", r"결함", r"리콜", r"누수", r"과징금", r"벌금",
    r"수사", r"조사", r"압수수색", r"제재", r"화재", r"폭발", r"파업", r"체포",
    r"구금", r"유출", r"반독점", r"특허\s*분쟁",
)
_TIER2_KEYWORDS = (
    r"earnings", r"quarterly (results|profit)", r"\brevenue\b", r"\binvest(s|ment|ing)?\b",
    r"new plant", r"\bfactory\b", r"joint venture", r"partnership", r"\bdeal\b",
    r"contract win", r"\border[s]?\b", r"supply agreement",
    r"실적", r"분기\s*실적", r"투자", r"공장\s*신설", r"수주", r"제휴", r"협약",
)
_TIER1_RE = [re.compile(p, re.I) for p in _TIER1_KEYWORDS]
_TIER2_RE = [re.compile(p, re.I) for p in _TIER2_KEYWORDS]


def _match_company(text: str):
    for name, patterns in _COMPANY_RE.items():
        if any(p.search(text) for p in patterns):
            return name
    return None


def _match_tier(text: str):
    """1=사건사고·법적, 2=실적·투자, 3=신제품·수상. 키워드가 전혀 안 걸려도 3(최저
    우선순위)으로 포함한다 — 2026-10-04 사용자 지시 "한국기업 리스트를 만들어서
    거기에 들어가는 건 바로 걸리도록": 회사명(리스트)+해외 발생이 확인되면 구체적인
    사건·실적 문구가 없어도(예: "~와 제휴해 디지털 지갑 서비스 도입" 같은 일반
    비즈니스 소식) 최소 우선순위로는 걸려야 한다는 뜻 — 키워드 목록은 "포함 여부"가
    아니라 "우선순위 등급"만 가른다."""
    if any(p.search(text) for p in _TIER1_RE):
        return 1
    if any(p.search(text) for p in _TIER2_RE):
        return 2
    return 3


def _lead(a: dict, n: int = 30) -> str:
    return (a.get("summary_en") or a.get("summary_ko") or "")[:n]


def classify(cluster):
    """클러스터가 '해외에서 보도된 한국 기업 소식'이면
    {"company","tier","foreign_countries"} 반환, 아니면 None."""
    members = [a for a in cluster if not a.get("__needs_review__")]
    if not members:
        return None

    company = None
    for a in members[:3]:
        subject_text = f"{a.get('title_en') or ''} {a.get('title_ko') or ''} {_lead(a)}"
        company = _match_company(subject_text)
        if company:
            break
    if not company:
        return None

    blob = " ".join(
        f"{a.get('title_en') or ''} {a.get('title_ko') or ''} "
        f"{a.get('summary_en') or ''} {a.get('summary_ko') or ''}"
        for a in members
    )
    tier = _match_tier(blob)  # 키워드가 없어도 3(최저 우선순위)으로 떨어질 뿐, None은 안 됨

    countries = {name for _, name in detect_countries(blob)}
    foreign = sorted(countries - {"한국"})
    if not foreign:
        return None

    return {"company": company, "tier": tier, "foreign_countries": foreign}


def importance_bonus(cluster) -> float:
    info = classify(cluster)
    return TIER_BONUS[info["tier"]] if info else 0.0


def is_tier1(cluster) -> bool:
    info = classify(cluster)
    return bool(info and info["tier"] == 1)


def demo():
    incident = [
        {"source": "CBS News", "title_en": "South Korean workers detained in Georgia Hyundai plant raid",
         "summary_en": "Hundreds of South Korean nationals were detained in an immigration raid at a Hyundai battery plant in the United States."},
        {"source": "AL.com", "title_en": "Lawsuit aims to force Hyundai answers on hiring practices",
         "summary_en": "A lawsuit filed in Georgia alleges Hyundai refused to hire non-Koreans at its US plant."},
    ]
    domestic = [
        {"source": "Korea Herald", "title_en": "Posco strike continues into second day",
         "summary_en": "Posco workers in South Korea extended their walkout over wage talks."},
        {"source": "Yonhap", "title_en": "Posco union rejects wage offer",
         "summary_en": "The strike at Posco's Pohang plant in South Korea entered its second day."},
    ]
    review = [
        {"source": "Top Gear", "title_en": "Kia Tucson review: should you buy one?",
         "summary_en": "We review the new Kia Tucson's handling and interior."},
        {"source": "Auto Blog", "title_en": "Kia Tucson vs Hyundai Tucson comparison",
         "summary_en": "Comparing the two SUVs' fuel economy and trims."},
    ]
    mention_only = [
        {"source": "Reuters", "title_en": "Asian chipmakers rally on AI demand",
         "summary_en": "Shares of Taiwan's TSMC led gains, with Samsung, SK Hynix and Micron also rising in a broad sector rally."},
        {"source": "AP", "title_en": "Tech stocks climb across Asia",
         "summary_en": "Chip stocks including TSMC, Samsung and SK Hynix gained in Tuesday trading."},
    ]
    kachin = [
        {"source": "Myanmar Now", "title_en": "KIA captures hill-top outpost as fighting resumes in Mongmit",
         "summary_en": "The Kachin Independence Army seized a military outpost in northern Shan State."},
        {"source": "Irrawaddy", "title_en": "KIA advances in Mongmit township",
         "summary_en": "Fighting between the KIA and junta forces continued near Mongmit."},
    ]
    financial_branch = [
        {"source": "American Banker", "title_en": "Shinhan Bank America fined over anti-money laundering lapses",
         "summary_en": "Regulators fined Shinhan Bank America for failing to maintain adequate anti-money "
                        "laundering controls at its New York branch in the United States."},
        {"source": "Reuters", "title_en": "Shinhan Bank America settles with US regulator",
         "summary_en": "Shinhan Bank's US subsidiary agreed to a settlement over compliance failures."},
    ]

    incident_result = classify(incident)
    assert incident_result and incident_result["company"] == "현대차" and incident_result["tier"] == 1
    assert "미국" in incident_result["foreign_countries"]
    assert classify(domestic) is None, "국내(한국) 전용 사건은 제외돼야 한다"
    assert classify(review) is None, "사건사고/실적 키워드 없는 단순 리뷰는 제외돼야 한다"
    assert classify(mention_only) is None, "주체가 아니라 나열된 종목은 제외돼야 한다"
    assert classify(kachin) is None, "KIA(카친독립군)는 기아차로 오매칭되면 안 된다"
    fin_result = classify(financial_branch)
    assert fin_result and fin_result["company"] == "신한금융" and fin_result["tier"] == 1
    assert "미국" in fin_result["foreign_countries"]
    assert importance_bonus(incident) == TIER_BONUS[1]
    assert is_tier1(incident) is True
    assert is_tier1(domestic) is False
    print("ok  korean_company_abroad.demo")


if __name__ == "__main__":
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    demo()
