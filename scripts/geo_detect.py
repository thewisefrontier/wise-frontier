"""
scripts/geo_detect.py
-------------------------
지역/국가 감지 로직의 단일 소스. gdelt_fetcher.py, site_discovery.py,
rss_fetcher.py가 전부 여기서 REGION_KEYWORDS/detect_region()/
COUNTRY_INFO/GLOBAL_COUNTRIES/detect_country()/detect_countries()를
import해 쓴다.

2026-09-08 도입 당시엔 rss_fetcher.py 걸 그대로 복사해 뗀 독립 모듈이었다
(rss_fetcher.py는 최상위 코드가 import 시점에 곧바로 실행되는 구조라
다른 스크립트가 직접 import할 수 없어서 — RSS 수집 루프 + DB 쓰기 +
save_state()). 그 뒤 두 사본이 따로 관리되며 드리프트 위험이 있었고
(call_gemini 패턴 드리프트와 같은 유형), 2026-09-10 rss_fetcher.py 쪽을
여기서 import하도록 통합해 이 파일이 유일한 원본이 됐다 — 여기만 고치면
모든 수집기에 반영된다.
"""

import re

REGION_KEYWORDS = {
    "africa": ["africa", "nigeria", "kenya", "ghana", "ethiopia", "egypt", "south africa", "allafrica", "maverick", "naira", "punch", "businessday", "businesstech", "guardian nigeria", "vanguard", "ghanaweb", "mining weekly", "engineering news"],
    "southeast_asia": ["asia", "krasia", "dealstreet", "techinasia", "vietnam", "indonesia", "thailand", "myanmar", "khmer", "malaysia", "philippine", "bangkok", "jakarta", "loop png", "pacific", "fiji", "solomon", "png"],
    "eastern_europe": ["emerging europe", "intellinews", "poland", "ukraine", "romania", "czechia", "kyiv", "warsaw", "caucasus", "azerbaijan"],
    "central_asia": ["kazakhstan", "uzbekistan", "kyrgyz", "tajik", "turkmen", "mongolia", "eurasianet", "caravanserai", "astana", "kun.uz", "kabar", "akipress"],
    "middle_east": ["iraq", "iran", "yemen", "syria", "jordan", "lebanon", "saudi", "qatar", "kuwait", "oman", "bahrain", "uae", "emirates", "al monitor", "middle east eye", "israel", "palestine", "ynet", "wafa", "globes", "israel21c", "haaretz", "jpost"],
    "south_asia": ["pakistan", "bangladesh", "nepal", "sri lanka", "dawn", "himalayan", "daily star", "india", "hindu", "business today", "economic times", "deccan chronicle"],
    "caribbean": ["haiti", "jamaica", "trinidad", "dominican", "caribbean", "haitian times", "loop caribbean", "jamaica gleaner", "caribbean news", "jamaica observer"],
    "latin_america": ["venezuela", "bolivia", "ecuador", "paraguay", "nicaragua", "salvador", "guatemala", "honduras", "news americas", "alo clandestino", "bolivia express", "telesur", "costa rica", "diario"],
    "oceania": ["australia", "new zealand", "abc news", "rnz", "stuff", "sydney", "auckland", "melbourne"]
}

def detect_region(source_name: str) -> str:
    name_lower = source_name.lower()
    for region, keywords in REGION_KEYWORDS.items():
        for kw in keywords:
            if kw in name_lower:
                return region
    return "global"

# 국가 감지 — (국기, 한국어 국가명)
COUNTRY_INFO = {
    "nigeria": ("🇳🇬", "나이지리아"), "nigerian": ("🇳🇬", "나이지리아"),
    "south africa": ("🇿🇦", "남아공"), "south african": ("🇿🇦", "남아공"),
    "kenya": ("🇰🇪", "케냐"), "kenyan": ("🇰🇪", "케냐"),
    "ghana": ("🇬🇭", "가나"), "ghanaian": ("🇬🇭", "가나"),
    "ethiopia": ("🇪🇹", "에티오피아"), "ethiopian": ("🇪🇹", "에티오피아"),
    "egypt": ("🇪🇬", "이집트"), "egyptian": ("🇪🇬", "이집트"),
    "tanzania": ("🇹🇿", "탄자니아"), "tanzanian": ("🇹🇿", "탄자니아"),
    "uganda": ("🇺🇬", "우간다"), "ugandan": ("🇺🇬", "우간다"),
    "rwanda": ("🇷🇼", "르완다"), "rwandan": ("🇷🇼", "르완다"),
    "senegal": ("🇸🇳", "세네갈"), "senegalese": ("🇸🇳", "세네갈"),
    "ivory coast": ("🇨🇮", "코트디부아르"), "cote divoire": ("🇨🇮", "코트디부아르"),
    "morocco": ("🇲🇦", "모로코"), "moroccan": ("🇲🇦", "모로코"),
    "angola": ("🇦🇴", "앙골라"), "angolan": ("🇦🇴", "앙골라"),
    "mozambique": ("🇲🇿", "모잠비크"),
    "zambia": ("🇿🇲", "잠비아"), "zambian": ("🇿🇲", "잠비아"),
    "zimbabwe": ("🇿🇼", "짐바브웨"), "zimbabwean": ("🇿🇼", "짐바브웨"),
    "congo": ("🇨🇩", "콩고"), "drc": ("🇨🇩", "콩고민주공화국"),
    "cameroon": ("🇨🇲", "카메룬"),
    "sudan": ("🇸🇩", "수단"), "sudanese": ("🇸🇩", "수단"),
    "libya": ("🇱🇾", "리비아"), "libyan": ("🇱🇾", "리비아"),
    "tunisia": ("🇹🇳", "튀니지"), "tunisian": ("🇹🇳", "튀니지"),
    "mali": ("🇲🇱", "말리"),
    "somalia": ("🇸🇴", "소말리아"), "somali": ("🇸🇴", "소말리아"),
    "malawi": ("🇲🇼", "말라위"),
    "vietnam": ("🇻🇳", "베트남"), "vietnamese": ("🇻🇳", "베트남"),
    "indonesia": ("🇮🇩", "인도네시아"), "indonesian": ("🇮🇩", "인도네시아"),
    "thailand": ("🇹🇭", "태국"), "thai": ("🇹🇭", "태국"),
    "philippines": ("🇵🇭", "필리핀"), "philippine": ("🇵🇭", "필리핀"),
    "malaysia": ("🇲🇾", "말레이시아"), "malaysian": ("🇲🇾", "말레이시아"),
    "myanmar": ("🇲🇲", "미얀마"), "burmese": ("🇲🇲", "미얀마"),
    "cambodia": ("🇰🇭", "캄보디아"), "cambodian": ("🇰🇭", "캄보디아"),
    "singapore": ("🇸🇬", "싱가포르"), "singaporean": ("🇸🇬", "싱가포르"),
    "laos": ("🇱🇦", "라오스"),
    "ukraine": ("🇺🇦", "우크라이나"), "ukrainian": ("🇺🇦", "우크라이나"),
    "poland": ("🇵🇱", "폴란드"), "polish": ("🇵🇱", "폴란드"),
    "romania": ("🇷🇴", "루마니아"), "romanian": ("🇷🇴", "루마니아"),
    "czechia": ("🇨🇿", "체코"), "czech": ("🇨🇿", "체코"),
    "hungary": ("🇭🇺", "헝가리"), "hungarian": ("🇭🇺", "헝가리"),
    "georgia": ("🇬🇪", "조지아"), "georgian": ("🇬🇪", "조지아"),
    "azerbaija": ("🇦🇿", "아제르바이잔"), "azerbaijan": ("🇦🇿", "아제르바이잔"),
    "trend az": ("🇦🇿", "아제르바이잔"),
    "kazakhstan": ("🇰🇿", "카자흐스탄"),
    "uzbekistan": ("🇺🇿", "우즈베키스탄"),
    # 남아시아
    "pakistan": ("🇵🇰", "파키스탄"), "pakistani": ("🇵🇰", "파키스탄"),
    "dawn pakistan": ("🇵🇰", "파키스탄"),
    "bangladesh": ("🇧🇩", "방글라데시"), "bangladeshi": ("🇧🇩", "방글라데시"),
    "nepal": ("🇳🇵", "네팔"), "nepali": ("🇳🇵", "네팔"),
    "sri lanka": ("🇱🇰", "스리랑카"), "sri lankan": ("🇱🇰", "스리랑카"),
    "india": ("🇮🇳", "인도"), "indian": ("🇮🇳", "인도"),
    # 중동
    "uae": ("🇦🇪", "UAE"), "emirates": ("🇦🇪", "UAE"),
    "saudi arabia": ("🇸🇦", "사우디"), "saudi": ("🇸🇦", "사우디"),
    "qatar": ("🇶🇦", "카타르"), "qatari": ("🇶🇦", "카타르"),
    "kuwait": ("🇰🇼", "쿠웨이트"), "kuwaiti": ("🇰🇼", "쿠웨이트"),
    "oman": ("🇴🇲", "오만"), "omani": ("🇴🇲", "오만"),
    "bahrain": ("🇧🇭", "바레인"), "bahraini": ("🇧🇭", "바레인"),
    "iraq": ("🇮🇶", "이라크"), "iraqi": ("🇮🇶", "이라크"),
    "iran": ("🇮🇷", "이란"), "iranian": ("🇮🇷", "이란"),
    "yemen": ("🇾🇪", "예멘"), "yemeni": ("🇾🇪", "예멘"),
    "syria": ("🇸🇾", "시리아"), "syrian": ("🇸🇾", "시리아"),
    "jordan": ("🇯🇴", "요르단"), "jordanian": ("🇯🇴", "요르단"),
    "lebanon": ("🇱🇧", "레바논"), "lebanese": ("🇱🇧", "레바논"),
    "israel": ("🇮🇱", "이스라엘"), "israeli": ("🇮🇱", "이스라엘"),
    "palestine": ("🇵🇸", "팔레스타인"), "palestinian": ("🇵🇸", "팔레스타인"),
    # 카리브해
    "haiti": ("🇭🇹", "아이티"), "haitian": ("🇭🇹", "아이티"),
    "jamaica": ("🇯🇲", "자메이카"), "jamaican": ("🇯🇲", "자메이카"),
    "trinidad": ("🇹🇹", "트리니다드"), "dominican": ("🇩🇴", "도미니카"),
    # 태평양
    "papua new guinea": ("🇵🇬", "파푸아뉴기니"), "png": ("🇵🇬", "파푸아뉴기니"),
    "fiji": ("🇫🇯", "피지"), "fijian": ("🇫🇯", "피지"),
    "solomon": ("🇸🇧", "솔로몬제도"),
    "vanuatu": ("🇻🇺", "바누아투"),
    # 중앙아시아
    "kyrgyzstan": ("🇰🇬", "키르기스스탄"), "kyrgyz": ("🇰🇬", "키르기스스탄"),
    "tajikistan": ("🇹🇯", "타지키스탄"), "tajik": ("🇹🇯", "타지키스탄"),
    "turkmenistan": ("🇹🇲", "투르크메니스탄"),
    "mongolia": ("🇲🇳", "몽골"), "mongolian": ("🇲🇳", "몽골"),
    # 라틴아메리카
    "venezuela": ("🇻🇪", "베네수엘라"), "venezuelan": ("🇻🇪", "베네수엘라"),
    "bolivia": ("🇧🇴", "볼리비아"), "bolivian": ("🇧🇴", "볼리비아"),
    "ecuador": ("🇪🇨", "에콰도르"), "ecuadorian": ("🇪🇨", "에콰도르"),
    "paraguay": ("🇵🇾", "파라과이"),
    "nicaragua": ("🇳🇮", "니카라과"),
    "el salvador": ("🇸🇻", "엘살바도르"),
    "guatemala": ("🇬🇹", "과테말라"),
    "honduras": ("🇭🇳", "온두라스"),
    # 오세아니아
    "australia": ("🇦🇺", "호주"), "australian": ("🇦🇺", "호주"),
    "new zealand": ("🇳🇿", "뉴질랜드"), "new zealander": ("🇳🇿", "뉴질랜드"),
    "timor": ("🇹🇱", "동티모르"),
    # 카리브해 추가
    "barbados": ("🇧🇧", "바베이도스"), "bahamas": ("🇧🇸", "바하마"),
    "cuba": ("🇨🇺", "쿠바"), "cuban": ("🇨🇺", "쿠바"),
    "guyana": ("🇬🇾", "가이아나"), "suriname": ("🇸🇷", "수리남"),
    # 아프리카 추가
    "burkina faso": ("🇧🇫", "부르키나파소"),
    "niger": ("🇳🇪", "니제르"),
    "chad": ("🇹🇩", "차드"),
    "guinea": ("🇬🇳", "기니"),
    "sierra leone": ("🇸🇱", "시에라리온"),
    "liberia": ("🇱🇷", "라이베리아"),
    "togo": ("🇹🇬", "토고"),
    "benin": ("🇧🇯", "베냉"),
    "gabon": ("🇬🇦", "가봉"),
    "botswana": ("🇧🇼", "보츠와나"),
    "namibia": ("🇳🇦", "나미비아"),
    "eswatini": ("🇸🇿", "에스와티니"),
    "lesotho": ("🇱🇸", "레소토"),
    "eritrea": ("🇪🇷", "에리트레아"),
    "djibouti": ("🇩🇯", "지부티"),
    "mauritius": ("🇲🇺", "모리셔스"),
    "madagascar": ("🇲🇬", "마다가스카르"),
    "seychelles": ("🇸🇨", "세이셸"),
    # 주요국 (글로벌 분류용)
    "united states": ("🇺🇸", "미국"), "american": ("🇺🇸", "미국"),
    "china": ("🇨🇳", "중국"), "chinese": ("🇨🇳", "중국"),
    "japan": ("🇯🇵", "일본"), "japanese": ("🇯🇵", "일본"),
    "france": ("🇫🇷", "프랑스"), "french": ("🇫🇷", "프랑스"),
    "germany": ("🇩🇪", "독일"), "german": ("🇩🇪", "독일"),
    "united kingdom": ("🇬🇧", "영국"), "british": ("🇬🇧", "영국"),
    "russia": ("🇷🇺", "러시아"), "russian": ("🇷🇺", "러시아"),
    "turkey": ("🇹🇷", "튀르키예"), "turkish": ("🇹🇷", "튀르키예"),
    "south korea": ("🇰🇷", "한국"), "korean": ("🇰🇷", "한국"),
    "brazil": ("🇧🇷", "브라질"), "brazilian": ("🇧🇷", "브라질"),
    "mexico": ("🇲🇽", "멕시코"), "mexican": ("🇲🇽", "멕시코"),
    "colombia": ("🇨🇴", "콜롬비아"), "colombian": ("🇨🇴", "콜롬비아"),
    "argentina": ("🇦🇷", "아르헨티나"), "argentine": ("🇦🇷", "아르헨티나"),
    "chile": ("🇨🇱", "칠레"), "chilean": ("🇨🇱", "칠레"),
    "peru": ("🇵🇪", "페루"), "peruvian": ("🇵🇪", "페루"),
    "italy": ("🇮🇹", "이탈리아"), "italian": ("🇮🇹", "이탈리아"),
    "spain": ("🇪🇸", "스페인"), "spanish": ("🇪🇸", "스페인"),
    "netherlands": ("🇳🇱", "네덜란드"), "dutch": ("🇳🇱", "네덜란드"),
    "canada": ("🇨🇦", "캐나다"), "canadian": ("🇨🇦", "캐나다"),
    "portugal": ("🇵🇹", "포르투갈"), "portuguese": ("🇵🇹", "포르투갈"),
    # ── 주요국 수도·핵심 기관명 (2026-09-10 신설) ──
    # 실사고: articles_are_related()의 country_uncertain 판정(8/10 안전장치)이
    # 국가명이 본문에 안 나오면 대부분 발동하는데, 정작 "미국"/"영국" 같은
    # 국가명 대신 "Washington"/"White House" 등 지명·기관명으로만 언급되는
    # 주요국 기사가 매우 흔했다(사용자 지적: "기사 생성 건수가 하루에 100개는
    # 돼야 하는데 영 늘어나질 않네" — 최근 24h 원본의 60.3%가 country 없음).
    # COUNTRY_INFO는 그동안 프론티어/신흥국 지명은 상세히 채워져 있었는데
    # (아래 "프랑스어/현지 지명" 등) 주요국(미/영/일/중/러) 수도는 하나도
    # 없었다 — 안전장치를 느슨하게 바꾸는 대신 이 데이터 공백부터 메운다.
    "washington": ("🇺🇸", "미국"), "white house": ("🇺🇸", "미국"),
    "pentagon": ("🇺🇸", "미국"), "wall street": ("🇺🇸", "미국"),
    "capitol hill": ("🇺🇸", "미국"), "federal reserve": ("🇺🇸", "미국"),
    "london": ("🇬🇧", "영국"), "downing street": ("🇬🇧", "영국"),
    "westminster": ("🇬🇧", "영국"), "bank of england": ("🇬🇧", "영국"),
    "tokyo": ("🇯🇵", "일본"), "bank of japan": ("🇯🇵", "일본"),
    "beijing": ("🇨🇳", "중국"), "people's bank of china": ("🇨🇳", "중국"),
    "moscow": ("🇷🇺", "러시아"), "kremlin": ("🇷🇺", "러시아"),
    "paris": ("🇫🇷", "프랑스"), "berlin": ("🇩🇪", "독일"),
    "seoul": ("🇰🇷", "한국"),
    "madrid": ("🇪🇸", "스페인"), "rome": ("🇮🇹", "이탈리아"),
    "amsterdam": ("🇳🇱", "네덜란드"), "lisbon": ("🇵🇹", "포르투갈"),
    "ankara": ("🇹🇷", "튀르키예"), "istanbul": ("🇹🇷", "튀르키예"),
    "ottawa": ("🇨🇦", "캐나다"), "canberra": ("🇦🇺", "호주"),
    "brasilia": ("🇧🇷", "브라질"), "buenos aires": ("🇦🇷", "아르헨티나"),
    "santiago": ("🇨🇱", "칠레"), "bogota": ("🇨🇴", "콜롬비아"),
    "mexico city": ("🇲🇽", "멕시코"),
    # ── 프랑스어/현지 지명 ──
    "cote divoire": ("🇨🇮", "코트디부아르"), "ivory coast": ("🇨🇮", "코트디부아르"), "abidjan": ("🇨🇮", "코트디부아르"),
    "abidjan": ("🇨🇮", "코트디부아르"), "ivoirien": ("🇨🇮", "코트디부아르"),
    "dakar": ("🇸🇳", "세네갈"), "sénégal": ("🇸🇳", "세네갈"),
    "bamako": ("🇲🇱", "말리"), "malien": ("🇲🇱", "말리"),
    "ouagadougou": ("🇧🇫", "부르키나파소"), "burkinabè": ("🇧🇫", "부르키나파소"),
    "niamey": ("🇳🇪", "니제르"), "nigérien": ("🇳🇪", "니제르"),
    "ndjamena": ("🇹🇩", "차드"), "tchad": ("🇹🇩", "차드"),
    "yaoundé": ("🇨🇲", "카메룬"), "yaounde": ("🇨🇲", "카메룬"), "cameroun": ("🇨🇲", "카메룬"),
    "douala": ("🇨🇲", "카메룬"),
    "kinshasa": ("🇨🇩", "DRC"), "rdc": ("🇨🇩", "DRC"),
    "brazzaville": ("🇨🇬", "콩고공화국"),
    "bangui": ("🇨🇫", "중앙아프리카"), "centrafrique": ("🇨🇫", "중앙아프리카"),
    "libreville": ("🇬🇦", "가봉"),
    "lomé": ("🇹🇬", "토고"), "lome": ("🇹🇬", "토고"),
    "cotonou": ("🇧🇯", "베냉"), "bénin": ("🇧🇯", "베냉"),
    "conakry": ("🇬🇳", "기니"), "guinée": ("🇬🇳", "기니"),
    "antananarivo": ("🇲🇬", "마다가스카르"),
    "port-au-prince": ("🇭🇹", "아이티"), "haïti": ("🇭🇹", "아이티"),
    # ── 아랍어 지명 (로마자) ──
    "khartoum": ("🇸🇩", "수단"), "al-khartoum": ("🇸🇩", "수단"),
    "mogadishu": ("🇸🇴", "소말리아"), "muqdisho": ("🇸🇴", "소말리아"),
    "tripoli": ("🇱🇾", "리비아"),
    "tunis": ("🇹🇳", "튀니지"), "tunisie": ("🇹🇳", "튀니지"),
    "alger": ("🇩🇿", "알제리"), "algérie": ("🇩🇿", "알제리"),
    "rabat": ("🇲🇦", "모로코"), "maroc": ("🇲🇦", "모로코"),
    "riyadh": ("🇸🇦", "사우디"),
    "abu dhabi": ("🇦🇪", "UAE"), "dubai": ("🇦🇪", "UAE"),
    "baghdad": ("🇮🇶", "이라크"),
    "amman": ("🇯🇴", "요르단"),
    "beirut": ("🇱🇧", "레바논"), "beyrouth": ("🇱🇧", "레바논"),
    "sanaa": ("🇾🇪", "예멘"),
    "jerusalem": ("🇵🇸", "팔레스타인"), "tel aviv": ("🇮🇱", "이스라엘"),
    # ── 포르투갈어 지명 ──
    "luanda": ("🇦🇴", "앙골라"), "angolano": ("🇦🇴", "앙골라"),
    "maputo": ("🇲🇿", "모잠비크"), "moçambique": ("🇲🇿", "모잠비크"),
    "cabo verde": ("🇨🇻", "카보베르데"),
    # ── 동남아/인도네시아어 지명 ──
    "jakarta": ("🇮🇩", "인도네시아"),
    "kuala lumpur": ("🇲🇾", "말레이시아"),
    "manila": ("🇵🇭", "필리핀"),
    "naypyidaw": ("🇲🇲", "미얀마"), "yangon": ("🇲🇲", "미얀마"),
    "phnom penh": ("🇰🇭", "캄보디아"),
    "vientiane": ("🇱🇦", "라오스"),
    "hanoi": ("🇻🇳", "베트남"), "ho chi minh": ("🇻🇳", "베트남"),
    # ── 중앙아시아 지명 ──
    "bishkek": ("🇰🇬", "키르기스스탄"),
    "dushanbe": ("🇹🇯", "타지키스탄"),
    "ashgabat": ("🇹🇲", "투르크메니스탄"),
    "tashkent": ("🇺🇿", "우즈베키스탄"),
    "astana": ("🇰🇿", "카자흐스탄"), "almaty": ("🇰🇿", "카자흐스탄"),
    "yerevan": ("🇦🇲", "아르메니아"), "armenia": ("🇦🇲", "아르메니아"),
    "baku": ("🇦🇿", "아제르바이잔"),
    "tbilisi": ("🇬🇪", "조지아"),
    # ── 오세아니아 지명 ──
    "sydney": ("🇦🇺", "호주"), "melbourne": ("🇦🇺", "호주"),
    "auckland": ("🇳🇿", "뉴질랜드"), "wellington": ("🇳🇿", "뉴질랜드"),
}

# 주요국 — 글로벌 카테고리로 분류
GLOBAL_COUNTRIES = {
    "미국", "중국", "일본", "프랑스", "독일", "영국", "러시아",
    "튀르키예", "한국", "브라질", "멕시코", "콜롬비아", "아르헨티나",
    "칠레", "페루", "이스라엘", "이탈리아", "스페인", "네덜란드",
    "캐나다", "포르투갈", "호주", "뉴질랜드",
}


def detect_countries(text: str, source: str = "") -> list:
    """텍스트에서 감지된 모든 국가 반환 [(flag, name), ...]"""
    t = text.lower()
    found = {}
    sorted_keys = sorted(COUNTRY_INFO.keys(), key=len, reverse=True)
    for keyword in sorted_keys:
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, t):
            flag, name = COUNTRY_INFO[keyword]
            if name not in found:
                found[name] = flag
    return [(flag, name) for name, flag in found.items()]


def detect_country(text: str, source: str = ""):
    """제목/요약에서 국가 정보 반환 (국기, 국가명)
    - 제목/요약 우선 검색
    - 출처명은 마지막 폴백으로만 사용
    - 단어 경계 체크로 오탐 방지
    """
    def find_in_text(t):
        t_lower = t.lower()
        sorted_keys = sorted(COUNTRY_INFO.keys(), key=len, reverse=True)
        for keyword in sorted_keys:
            pattern = r'\b' + re.escape(keyword) + r'\b'
            if re.search(pattern, t_lower):
                return COUNTRY_INFO[keyword]
        return None

    result = find_in_text(text)
    if result:
        return result

    if source:
        source_lower = source.lower()
        source_specific = {
            "trend az": ("🇦🇿", "아제르바이잔"),
            "dawn pakistan": ("🇵🇰", "파키스탄"),
            "akipress": ("🇰🇬", "키르기스스탄"),
            "kabar": ("🇰🇬", "키르기스스탄"),
            "kun.uz": ("🇺🇿", "우즈베키스탄"),
            "astanatimes": ("🇰🇿", "카자흐스탄"),
        }
        for keyword, val in source_specific.items():
            if keyword in source_lower:
                return val

    return "", ""
