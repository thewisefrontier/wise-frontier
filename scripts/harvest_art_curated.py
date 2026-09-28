# -*- coding: utf-8 -*-
"""비서구 화가 큐레이션 → 위키미디어 커먼즈 'Paintings by {작가}' 분류에서 퍼블릭도메인 그림을 받아
scripts/data/art_weekly_global_artworks.json 에 덧붙이고 W/A/X 순서로 다시 섞는다. 2026-09-28.

배경: 사용자 지시 "미술은 전 세계 기준(아프리카·동남아 등)". 메트·클리블랜드·시카고 목록엔 동아시아·인도만 있고
동남아·아프리카·중남미·중동은 사실상 0이라, 위키데이터 SPARQL(504·429로 불가) 대신 화가를 직접 지정.
저작권: 사후 70년 경과(1955 이전 사망) 화가만. Commons 파일 라이선스 표기가 퍼블릭도메인/CC0인 것만 받음.
※ 새 화가는 (영문명, 한글명, 국가, 국기, region) 한 줄로 추가. 커먼즈에 'Paintings by 이름' 분류가 있어야 함.

실행: python scripts/harvest_art_curated.py   (harvest_art_global.py 실행 뒤)
"""
import json
import os
import random
import re
import sys
import time

import requests

OUT = os.path.join(os.path.dirname(__file__), "data", "art_weekly_global_artworks.json")
UA = {"User-Agent": "NewsFinalBot/1.0 (https://newsfinal.co.kr)"}
API = "https://commons.wikimedia.org/w/api.php"
PER_ARTIST = 3

# (영문명, 한글명, 국가, 국기, region)  region: asia=아시아 버킷, global/africa=기타 버킷
ARTISTS = [
    # 동남아시아
    ("Raden Saleh", "라덴 살레", "인도네시아", "🇮🇩", "asia"),
    ("Abdullah Suriosubroto", "압둘라 수리오수브로토", "인도네시아", "🇮🇩", "asia"),
    ("Juan Luna", "후안 루나", "필리핀", "🇵🇭", "asia"),
    ("Félix Resurrección Hidalgo", "펠릭스 리수렉시온 이달고", "필리핀", "🇵🇭", "asia"),
    ("Fabián de la Rosa", "파비안 데 라 로사", "필리핀", "🇵🇭", "asia"),
    ("Khrua In Khong", "크루아 인 콩", "태국", "🇹🇭", "asia"),
    # 남아시아
    ("Raja Ravi Varma", "라자 라비 바르마", "인도", "🇮🇳", "asia"),
    ("Abanindranath Tagore", "아바닌드라나트 타고르", "인도", "🇮🇳", "asia"),
    ("Gaganendranath Tagore", "가가넨드라나트 타고르", "인도", "🇮🇳", "asia"),
    ("Amrita Sher-Gil", "암리타 셰르길", "인도", "🇮🇳", "asia"),
    ("Rabindranath Tagore", "라빈드라나트 타고르", "인도", "🇮🇳", "asia"),
    # 동아시아·중앙아시아
    ("Marzan Sharav", "마르잔 샤라브", "몽골", "🇲🇳", "asia"),
    ("Chen Cheng-po", "천청보", "대만", "🇹🇼", "asia"),
    ("Kim Hong-do", "김홍도", "한국", "🇰🇷", "asia"),
    ("Jeong Seon", "정선", "한국", "🇰🇷", "asia"),
    # 중동·서아시아·코카서스
    ("Kamal-ol-Molk", "카말 올 몰크", "이란", "🇮🇷", "global"),
    ("Kamal ol-Molk", "카말 올 몰크", "이란", "🇮🇷", "global"),
    ("Reza Abbasi", "레자 아바시", "이란", "🇮🇷", "global"),
    ("Kamaleddin Behzad", "카말레딘 베흐자드", "이란", "🇮🇷", "global"),
    ("Osman Hamdi Bey", "오스만 함디 베이", "튀르키예", "🇹🇷", "global"),
    ("Şeker Ahmed Pasha", "셰케르 아흐메트 파샤", "튀르키예", "🇹🇷", "global"),
    ("Khalil Gibran", "칼릴 지브란", "레바논", "🇱🇧", "global"),
    ("Niko Pirosmani", "니코 피로스마니", "조지아", "🇬🇪", "global"),
    # 아프리카
    ("Jan Ernst Abraham Volschenk", "얀 에른스트 아브라함 폴셴크", "남아프리카공화국", "🇿🇦", "africa"),
    ("Hugo Naudé", "휘호 나우데", "남아프리카공화국", "🇿🇦", "africa"),
    ("Frans Oerder", "프란스 우르더", "남아프리카공화국", "🇿🇦", "africa"),
    # 중남미
    ("José María Velasco", "호세 마리아 벨라스코", "멕시코", "🇲🇽", "global"),
    ("Pedro Américo", "페드루 아메리쿠", "브라질", "🇧🇷", "global"),
    ("Victor Meirelles", "비토르 메이렐리스", "브라질", "🇧🇷", "global"),
    ("Almeida Júnior", "알메이다 주니오르", "브라질", "🇧🇷", "global"),
    ("Joaquín Torres García", "호아킨 토레스 가르시아", "우루과이", "🇺🇾", "global"),
    ("Juan Manuel Blanes", "후안 마누엘 블라네스", "우루과이", "🇺🇾", "global"),
    ("Cándido López", "칸디도 로페스", "아르헨티나", "🇦🇷", "global"),
    ("Prilidiano Pueyrredón", "프릴리디아노 푸에이레돈", "아르헨티나", "🇦🇷", "global"),
    ("Armando Reverón", "아르만도 레베론", "베네수엘라", "🇻🇪", "global"),
    ("Francisco Laso", "프란시스코 라소", "페루", "🇵🇪", "global"),
    ("Pedro Lira", "페드로 리라", "칠레", "🇨🇱", "global"),
    ("Hector Hyppolite", "엑토르 이폴리트", "아이티", "🇭🇹", "global"),
    # 오세아니아
    ("Tommy McRae", "토미 맥래", "오스트레일리아", "🇦🇺", "global"),
    ("Gottfried Lindauer", "고트프리트 린다워", "뉴질랜드", "🇳🇿", "global"),
    # 네덜란드(라익스뮤지엄 소장 거장)
    ("Johannes Vermeer", "요하네스 페르메이르", "네덜란드", "🇳🇱", "europe"),
    ("Rembrandt", "렘브란트", "네덜란드", "🇳🇱", "europe"),
    ("Frans Hals", "프란스 할스", "네덜란드", "🇳🇱", "europe"),
    ("Jan Steen", "얀 스테인", "네덜란드", "🇳🇱", "europe"),
    ("Jacob van Ruisdael", "야코프 판 라위스달", "네덜란드", "🇳🇱", "europe"),
    ("Meindert Hobbema", "메인데르트 호베마", "네덜란드", "🇳🇱", "europe"),
    ("Aelbert Cuyp", "알베르트 카위프", "네덜란드", "🇳🇱", "europe"),
    ("Pieter de Hooch", "피터르 더 호흐", "네덜란드", "🇳🇱", "europe"),
    ("Gerard ter Borch", "헤라르트 테르 보르흐", "네덜란드", "🇳🇱", "europe"),
    ("Willem Kalf", "빌럼 칼프", "네덜란드", "🇳🇱", "europe"),
    ("Judith Leyster", "유디트 레이스터르", "네덜란드", "🇳🇱", "europe"),
    ("Carel Fabritius", "카렐 파브리티위스", "네덜란드", "🇳🇱", "europe"),
    ("Hendrick Avercamp", "헨드릭 아베르캄프", "네덜란드", "🇳🇱", "europe"),
    ("Vincent van Gogh", "빈센트 반 고흐", "네덜란드", "🇳🇱", "europe"),
    ("Piet Mondrian", "피터르 몬드리안", "네덜란드", "🇳🇱", "europe"),
    ("Jan Toorop", "얀 토롭", "네덜란드", "🇳🇱", "europe"),
    ("Johan Barthold Jongkind", "요한 바르톨트 용킨트", "네덜란드", "🇳🇱", "europe"),
    ("Hendrik Willem Mesdag", "헨드릭 빌럼 메스다흐", "네덜란드", "🇳🇱", "europe"),
    ("Jozef Israëls", "요제프 이스라엘스", "네덜란드", "🇳🇱", "europe"),
    ("George Hendrik Breitner", "조지 헨드릭 브라이트너", "네덜란드", "🇳🇱", "europe"),
    # 덴마크(SMK·스카겐 화가)
    ("Vilhelm Hammershøi", "빌헬름 함메르쇠이", "덴마크", "🇩🇰", "europe"),
    ("Peder Severin Krøyer", "P. S. 크뢰위에르", "덴마크", "🇩🇰", "europe"),
    ("Anna Ancher", "안나 앙케르", "덴마크", "🇩🇰", "europe"),
    ("Michael Ancher", "미카엘 앙케르", "덴마크", "🇩🇰", "europe"),
    ("Laurits Tuxen", "라우리츠 툭센", "덴마크", "🇩🇰", "europe"),
    ("Christoffer Wilhelm Eckersberg", "C. W. 에케르스베르", "덴마크", "🇩🇰", "europe"),
    ("Nicolai Abildgaard", "니콜라이 아빌드고르", "덴마크", "🇩🇰", "europe"),
    # 스웨덴(국립미술관 소장 작가)
    ("Carl Larsson", "칼 라르손", "스웨덴", "🇸🇪", "europe"),
    ("Anders Zorn", "안데르스 소른", "스웨덴", "🇸🇪", "europe"),
    ("Hilma af Klint", "힐마 아프 클린트", "스웨덴", "🇸🇪", "europe"),
    ("Bruno Liljefors", "브루노 릴리에포르스", "스웨덴", "🇸🇪", "europe"),
    ("Ernst Josephson", "에른스트 요세프손", "스웨덴", "🇸🇪", "europe"),
    ("Alexander Roslin", "알렉산데르 로슬린", "스웨덴", "🇸🇪", "europe"),
    # 노르웨이
    ("Edvard Munch", "에드바르 뭉크", "노르웨이", "🇳🇴", "europe"),
    ("Christian Krohg", "크리스티안 크로그", "노르웨이", "🇳🇴", "europe"),
    ("Johan Christian Dahl", "요한 크리스티안 달", "노르웨이", "🇳🇴", "europe"),
    ("Theodor Kittelsen", "테오도르 키텔센", "노르웨이", "🇳🇴", "europe"),
    ("Harald Sohlberg", "하랄 솔베르그", "노르웨이", "🇳🇴", "europe"),
    ("Hans Gude", "한스 구데", "노르웨이", "🇳🇴", "europe"),
    ("Erik Werenskiold", "에리크 베렌시올", "노르웨이", "🇳🇴", "europe"),
    ("Nikolai Astrup", "니콜라이 아스트루프", "노르웨이", "🇳🇴", "europe"),
    # 핀란드(아테네움 소장 작가)
    ("Akseli Gallen-Kallela", "악셀리 갈렌칼레라", "핀란드", "🇫🇮", "europe"),
    ("Albert Edelfelt", "알베르트 에델펠트", "핀란드", "🇫🇮", "europe"),
    ("Helene Schjerfbeck", "헬레네 셰르프벡", "핀란드", "🇫🇮", "europe"),
    ("Eero Järnefelt", "에에로 예르네펠트", "핀란드", "🇫🇮", "europe"),
    ("Hugo Simberg", "후고 심베리", "핀란드", "🇫🇮", "europe"),
    # 아이슬란드
    ("Þórarinn B. Þorláksson", "토라린 토를락손", "아이슬란드", "🇮🇸", "europe"),
    # 영국(예일 영국미술센터 소장 거장)·미국(필라델피아·게티 소장 거장)
    ("J. M. W. Turner", "J. M. W. 터너", "영국", "🇬🇧", "europe"),
    ("John Constable", "존 컨스터블", "영국", "🇬🇧", "europe"),
    ("William Hogarth", "윌리엄 호가스", "영국", "🇬🇧", "europe"),
    ("Thomas Gainsborough", "토머스 게인즈버러", "영국", "🇬🇧", "europe"),
    ("Joshua Reynolds", "조슈아 레이놀즈", "영국", "🇬🇧", "europe"),
    ("William Blake", "윌리엄 블레이크", "영국", "🇬🇧", "europe"),
    ("George Stubbs", "조지 스텁스", "영국", "🇬🇧", "europe"),
    ("Edwin Landseer", "에드윈 랜시어", "영국", "🇬🇧", "europe"),
    ("John Everett Millais", "존 에버릿 밀레이", "영국", "🇬🇧", "europe"),
    ("Dante Gabriel Rossetti", "단테 가브리엘 로세티", "영국", "🇬🇧", "europe"),
    ("Edward Burne-Jones", "에드워드 번존스", "영국", "🇬🇧", "europe"),
    ("John William Waterhouse", "존 윌리엄 워터하우스", "영국", "🇬🇧", "europe"),
    ("Joseph Wright of Derby", "조지프 라이트", "영국", "🇬🇧", "europe"),
    ("Thomas Lawrence", "토머스 로런스", "영국", "🇬🇧", "europe"),
    ("James McNeill Whistler", "제임스 맥닐 휘슬러", "미국", "🇺🇸", "europe"),
    ("John Singer Sargent", "존 싱어 사전트", "미국", "🇺🇸", "europe"),
    ("Thomas Eakins", "토머스 에이킨스", "미국", "🇺🇸", "europe"),
    ("Winslow Homer", "윈즐로 호머", "미국", "🇺🇸", "europe"),
    ("Mary Cassatt", "메리 커샛", "미국", "🇺🇸", "europe"),
    ("Thomas Cole", "토머스 콜", "미국", "🇺🇸", "europe"),
    ("Grant Wood", "그랜트 우드", "미국", "🇺🇸", "europe"),
    ("Edward Hopper", "에드워드 호퍼", "미국", "🇺🇸", "europe"),
    ("Georgia O'Keeffe", "조지아 오키프", "미국", "🇺🇸", "europe"),
    ("Jacob Lawrence", "제이콥 로런스", "미국", "🇺🇸", "europe"),
    ("Henry Ossawa Tanner", "헨리 오서와 태너", "미국", "🇺🇸", "europe"),
]


# 커먼즈 분류 표기가 다른 작가(악센트·하이픈 등)
CAT_ALIAS = {"Kamal-ol-Molk": "Kamal-ol-molk", "José María Velasco": "José Maria Velasco"}
NOT_PAINTING = r"photo|portrait of|stamp|map|statue|grave|house|museum|building|tomb|street|book|manuscript|newspaper|sign|coin"


def _hits(cat):
    return requests.get(API, headers=UA, timeout=40, params={
        "action": "query", "list": "search", "srnamespace": 6, "srlimit": 40, "format": "json",
        "srsearch": f'deepcategory:"{cat}" filetype:bitmap'}).json()["query"]["search"]

# 사용자 지시(2026-09-28): "저작권 상관없다, 언론은 써도 된다" → 사후 70년 안 지난 근현대 작가도 포함.
# 이미지는 영어 위키백과 작품 문서의 대표 이미지(harvest_art_extra.wiki_paintings). 'Category:Paintings by 이름'이 있는 작가만 잡힌다.
MODERN = [
    # 아프리카
    ("Mahmoud Said", "마흐무드 사이드", "이집트", "🇪🇬", "africa"),
    ("Ben Enwonwu", "벤 엔워누", "나이지리아", "🇳🇬", "africa"),
    ("Gerard Sekoto", "헤라르트 세코토", "남아프리카공화국", "🇿🇦", "africa"),
    ("Irma Stern", "이르마 스턴", "남아프리카공화국", "🇿🇦", "africa"),
    ("J. H. Pierneef", "J. H. 피르네프", "남아프리카공화국", "🇿🇦", "africa"),
    ("Skunder Boghossian", "스쿤데르 보고시안", "에티오피아", "🇪🇹", "africa"),
    ("Afewerk Tekle", "아페워르크 테클레", "에티오피아", "🇪🇹", "africa"),
    ("Ibrahim El-Salahi", "이브라힘 엘살라히", "수단", "🇸🇩", "africa"),
    ("Chéri Samba", "셰리 삼바", "콩고민주공화국", "🇨🇩", "africa"),
    ("Tshibumba Kanda-Matulu", "치붐바 칸다마툴루", "콩고민주공화국", "🇨🇩", "africa"),
    ("Twins Seven-Seven", "트윈스 세븐세븐", "나이지리아", "🇳🇬", "africa"),
    ("Aina Onabolu", "아이나 오나볼루", "나이지리아", "🇳🇬", "africa"),
    ("Mohamed Melehi", "모하메드 멜레히", "모로코", "🇲🇦", "africa"),
    # 동남아시아
    ("Affandi", "아판디", "인도네시아", "🇮🇩", "asia"),
    ("Fernando Amorsolo", "페르난도 아모르솔로", "필리핀", "🇵🇭", "asia"),
    ("Nguyễn Phan Chánh", "응우옌 판 짜인", "베트남", "🇻🇳", "asia"),
    ("Tô Ngọc Vân", "또 응옥 번", "베트남", "🇻🇳", "asia"),
    # 중남미
    ("Frida Kahlo", "프리다 칼로", "멕시코", "🇲🇽", "global"),
    ("Diego Rivera", "디에고 리베라", "멕시코", "🇲🇽", "global"),
    ("David Alfaro Siqueiros", "다비드 알파로 시케이로스", "멕시코", "🇲🇽", "global"),
    ("Rufino Tamayo", "루피노 타마요", "멕시코", "🇲🇽", "global"),
    ("Wifredo Lam", "위프레도 람", "쿠바", "🇨🇺", "global"),
    ("Fernando Botero", "페르난도 보테로", "콜롬비아", "🇨🇴", "global"),
    ("Tarsila do Amaral", "타르실라 두 아마라우", "브라질", "🇧🇷", "global"),
    ("Candido Portinari", "칸지두 포르티나리", "브라질", "🇧🇷", "global"),
    ("Oswaldo Guayasamín", "오스왈도 과야사민", "에콰도르", "🇪🇨", "global"),
    ("Roberto Matta", "로베르토 마타", "칠레", "🇨🇱", "global"),
    ("Xul Solar", "슐 솔라르", "아르헨티나", "🇦🇷", "global"),
    # 중동·남아시아·동아시아 근현대
    ("Zao Wou-Ki", "자오우키", "중국", "🇨🇳", "asia"),
    ("Xu Beihong", "쉬베이훙", "중국", "🇨🇳", "asia"),
    ("Qi Baishi", "치바이스", "중국", "🇨🇳", "asia"),
    ("M. F. Husain", "M. F. 후세인", "인도", "🇮🇳", "asia"),
    ("Jamini Roy", "자미니 로이", "인도", "🇮🇳", "asia"),
    ("Yayoi Kusama", "쿠사마 야요이", "일본", "🇯🇵", "asia"),
    ("Kim Whanki", "김환기", "한국", "🇰🇷", "asia"),
    ("Lee Jung-seob", "이중섭", "한국", "🇰🇷", "asia"),
    ("Park Soo-keun", "박수근", "한국", "🇰🇷", "asia"),
    ("Dia al-Azzawi", "디아 알아자위", "이라크", "🇮🇶", "global"),
]


def commons_paintings(name_en, n=PER_ARTIST):
    try:
        alias = CAT_ALIAS.get(name_en, name_en)
        hits = _hits(f"Paintings by {alias}")
        main_cat = False
        if not hits:  # 'Paintings by' 분류가 없으면 작가 본 분류(하위 포함)에서 — 사진·지도 등은 아래에서 제외
            hits, main_cat = _hits(alias), True
        if not hits:
            return []
        pages = requests.get(API, headers=UA, timeout=40, params={
            "action": "query", "format": "json", "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1600,
            "titles": "|".join(h["title"] for h in hits)}).json()["query"]["pages"].values()
    except Exception as e:
        print(f"  ⚠️ {name_en} 조회 실패: {e}", file=sys.stderr)
        return []
    out, seen = [], set()
    for pg in sorted(pages, key=lambda x: x.get("index", 0)):
        ii = (pg.get("imageinfo") or [{}])[0]
        title = pg["title"][5:].rsplit(".", 1)[0]
        lic = (ii.get("extmetadata", {}).get("LicenseShortName", {}).get("value") or "").lower()
        key = re.sub(r"[^a-z0-9]", "", title.lower())[:24]
        if ("public domain" not in lic and "cc0" not in lic and "pd" not in lic) or ii.get("width", 0) < 1200 or key in seen:
            continue
        if re.search(r"detail|detalle|crop|stamp|sketch|study|estudo|estudio|drawing|dibujo|firma|busto|escuela|frame|cover|poster|postcard", title, re.I):
            continue
        if main_cat and re.search(NOT_PAINTING, title, re.I):
            continue
        clean = re.sub(rf"^{re.escape(name_en)}\s*[-–—:,]\s*", "", title).strip(" -–—")
        # 파일명 찌꺼기(Hh2-svr·DuboisOerder1928·"… p105"·"… 001")는 작품명이 아니라 사진·책 쪽인 경우가 많다
        # (2026-09-28 실측) — 두 단어 이상이고 숫자로 끝나지 않는 이름만 작품 제목으로 인정.
        if len((clean or title).split()) < 2 or re.search(r"(\d+|p\d+)$", clean or title):
            continue
        seen.add(key)
        out.append({"file": pg["title"][5:], "title": clean or title, "w": ii["width"]})
        if len(out) >= n:
            break
    return out


MIN_ARTIST_GAP = 150  # 같은 작가의 다음 기사까지 최소 일수 — 위키백과 같은 문서에서 나오는 작가 소개 문단 반복 방지


def spread_artists(order, gap=MIN_ARTIST_GAP):
    """순서를 최대한 유지하되 같은 작가가 gap 안에 다시 나오지 않도록 미룬다(지연 큐 방식)."""
    out, last, waiting = [], {}, []
    def ok(a):
        return len(out) - last.get(a["artist_en"], -10**9) >= gap
    for a in order:
        # 먼저 대기 중이던 것 중 이제 가능한 것을 내보낸다
        for w in list(waiting):
            if ok(w):
                waiting.remove(w); last[w["artist_en"]] = len(out); out.append(w)
        if ok(a):
            last[a["artist_en"]] = len(out); out.append(a)
        else:
            waiting.append(a)
    out += waiting  # 남은 것은 뒤에 붙임(간격 확보 불가한 소수)
    return out


def bucket(a):
    return "W" if a["region"] == "europe" else ("A" if a["region"] == "asia" else "X")


def main():
    names = {x[0] for x in ARTISTS}
    names |= {x[0] for x in MODERN}
    base = [a for a in json.load(open(OUT, encoding="utf-8"))
            if a["artist_en"] not in names and "Walters" not in a["image_credit"]]  # 재실행해도 중복 없이
    have = {re.sub(r"[^a-z0-9]", "", a["title_en"].lower()) for a in base}
    seen_artist, added = set(), []
    for en, ko, country, flag, region in ARTISTS:
        if ko in seen_artist:
            continue
        got = commons_paintings(en)
        time.sleep(0.5)
        if got:
            seen_artist.add(ko)
        for g in got:
            if re.sub(r"[^a-z0-9]", "", g["title"].lower()) in have:
                continue
            added.append({
                "title_ko": g["title"], "title_en": g["title"], "artist_ko": ko, "artist_en": en,
                "year_label": "", "country": country, "country_flag": flag, "region": region,
                "wiki_query": f"{g['title']} {en}",
                "direct_image_url": f"https://commons.wikimedia.org/wiki/Special:FilePath/{g['file'].replace(' ', '_')}?width=1600",
                "image_credit": "이미지 출처: Wikimedia Commons (퍼블릭 도메인)",
                "museum_grounding": f"작가: {en}({country} 화가, 사후 70년 경과). 분류: Paintings 출처: 위키미디어 커먼즈 분류 'Paintings by {en}'.",
            })
        print(f"  {en}: {len(got)}점", file=sys.stderr)
    # 위키백과 작품 문서(근현대 작가) + 월터스 — harvest_art_extra
    sys.path.insert(0, os.path.dirname(__file__))
    import harvest_art_extra as ex
    from harvest_art_global import geo_of_en
    extra = ex.modern_records(MODERN)
    if "--walters" in sys.argv:
        i = sys.argv.index("--walters")
        extra += ex.from_walters(sys.argv[i + 1], sys.argv[i + 2], sys.argv[i + 3], geo_of_en)
    for a in extra:
        k = re.sub(r"[^a-z0-9]", "", a["title_en"].lower())
        if k not in have:
            have.add(k)
            added.append(a)
    print(f"덧붙일 작품 {len(added)}건(작가 {len(seen_artist)}명 + 위키·월터스)", file=sys.stderr)
    if not added:
        sys.exit("추가할 작품이 없음 — 저장하지 않음")
    items = base + added
    pools = {k: [a for a in items if bucket(a) == k] for k in "WAX"}
    rnd = random.Random(20260928)
    for p in pools.values():
        rnd.shuffle(p)
    order = []
    while any(pools.values()):
        for k in "WAWXWAWX":
            if pools[k]:
                order.append(pools[k].pop())
    order = spread_artists(order)
    print(f"최종 {len(order)}건 — " + ", ".join(f"{k}:{sum(1 for a in order if bucket(a) == k)}" for k in "WAX"), file=sys.stderr)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(order, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
