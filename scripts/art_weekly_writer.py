"""
scripts/art_weekly_writer.py
--------------------------------
고전 명화 한 점을 소개하는 "읽을거리" 기사 자동 생성.

사용자 요청(2026-09-08): "매주 고전 미술 작품 하나를 소개하는 기사가 있으면
좋을 것 같은데" → "서양 고전미술 위주로 하되, 한국, 중국, 일본 고전 미술도
섞는 걸로" → "주말에 읽을거리로" → "작품에 대한 이야기, 작가에 대한 이야기".
2026-09-27 사용자 요청으로 주 1회 → 일 1회로 주기 변경.

- 결정론적 일간 선택: 날짜(toordinal)로 ARTWORKS를 순환 선택(같은 날 여러 번
  실행돼도 already_published()가 막고, 목록을 다 돌면 처음부터 다시 순환).
- ARTWORKS = 손수 큐레이션한 59개(파일 안에 하드코딩) + scripts/data/
  art_weekly_met_artworks.json(메트로폴리탄 미술관 Open Access, 약 350개,
  scripts/harvest_met_artworks.py로 수집)를 모듈 로드 시 이어붙인 것.
  2026-09-27 사용자 지적("59개면 너무 적다, 적어도 1년은 해야, 미술 관련
  DB나 깃허브 없어?")로 확장 — Met의 Collection API가 isPublicDomain
  플래그와 작가 사망년도를 공식 제공해 저작권 검증이 훨씬 쉽고, 이미지도
  Met가 "이 작품=이 이미지"를 확정해서 주는 URL을 그대로 쓰므로(direct_image_url)
  손수 짠 목록에서 반복됐던 "위키미디어 검색어는 맞았는데 엉뚱한 사진"류
  위험이 없다. 총 400개 이상 → 일 1회 기준 1년 넘게 안 겹침. 목록이 짧아지면
  scripts/harvest_met_artworks.py를 다시 돌리거나(부서 추가) 손수 항목을
  보탤 것.
- 이미지는 반드시 그 작품 실물이어야 하므로(일반 스톡사진으로 대체하면
  2026-09-08 파올라 페를롭 사고와 같은 문제) direct_image_url(Met 수집분)이
  있으면 그걸, 없으면 Wikimedia Commons에서 작품명으로 검색해 찾는다 — 못
  찾으면 그 날은 발행을 건너뛴다(일반 기사처럼 Pixabay 스톡사진으로 대체 안 함,
  article_image.fetch_article_image()를 안 쓰는 이유).
- 근거자료: Met 수집분은 미술관이 확정한 소장품 기록(museum_grounding —
  작가 생몰년·문화권·시대·재질·소장 경위)을 항상 존재하는 1차 근거로 쓰고,
  위키백과는 있으면 보충한다. 손수 큐레이션한 59개는 위키백과 근거자료만
  쓴다(build_daily_artwork 근처 주석 참고) — Met 수집분 중 위키백과 문서가
  아예 없는 비주류 작가(예: 조선 화가 이정)가 흔해, 위키백과에만 기대면
  그런 날마다 근거자료 부족으로 스킵될 뻔한 걸 실측으로 확인해 추가함.
- 저작권: 손수 큐레이션한 59개는 작가 사후 70년이 지나 퍼블릭도메인이
  확실한 작품만 올린다(뭉크 1944년 작고 → 2014년부터 PD 등 확인 후 포함).
  Met 수집분은 harvest_met_artworks.py가 isPublicDomain + 사후 70년(또는
  작가 불명 시 작품 제작연도 1900년 이전)을 자동 검증. 이 목록에 새 작품을
  추가할 때도 이 기준을 지킬 것.

실행: python scripts/art_weekly_writer.py
권장: 일 1회 실행 — already_published()가 같은 날 중복 발행을 막음.
⚠️ 이 스크립트 자체는 매일 실행돼도 안전(날짜 게이트가 막아줌)하지만,
실제로 매일 발행되려면 이 워크플로우를 트리거하는 외부 cron(cron-job.org)
주기도 주 1회에서 일 1회로 바꿔야 한다 — 코드만 바꾼다고 실행 빈도가
저절로 늘어나지 않는다.
"""

import json
import os
import re
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

try:
    from content_guard import is_placeholder_response
except Exception:
    def is_placeholder_response(title, body):
        return False

try:
    from json_body_guard import unwrap_json_body
except Exception:
    def unwrap_json_body(text, _depth=0):
        return None

try:
    from style_guard import has_column_style, has_polite_ending, to_plain_style, parse_article_output, ensure_paragraphs
except Exception:
    def has_column_style(text: str) -> bool:
        return False
    def has_polite_ending(text: str) -> bool:
        return False
    def to_plain_style(text: str) -> str:
        return text
    def ensure_paragraphs(text: str, target: int = 3, max_sentences_per_para: int = 4) -> str:
        return text
    def parse_article_output(text: str) -> tuple[str, str]:
        title, body = "", ""
        m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
        if m_title:
            title = m_title.group(1).strip()
        m_body = re.search(r"BODY:\s*(.+)$", text, re.S)
        if m_body:
            body = m_body.group(1).strip()
        return title, body

try:
    from article_store import insert_final_article, sb_headers as _sb_headers, sb_url as _sb_url
except Exception:
    SUPABASE_URL_FALLBACK = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_SERVICE_KEY_FALLBACK = os.getenv("SUPABASE_SERVICE_KEY", "")
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY_FALLBACK,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY_FALLBACK}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_url(table: str = "articles"):
        return f"{SUPABASE_URL_FALLBACK}/rest/v1/{table}"
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

try:
    from article_image import fetch_wikimedia_image
except Exception:
    def fetch_wikimedia_image(query: str, allow_artwork: bool = False):
        return None, None

try:
    from fabrication_guard import verify_no_fabricated_names as _fg_verify_no_fabricated_names
except Exception:
    def _fg_verify_no_fabricated_names(source_prompt, body, call_gemini_fn):
        return ""


GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
] if k]

try:
    from gemini_client import GeminiClient
except Exception:
    class GeminiClient:
        def __init__(self, *a, **k):
            pass
        def call(self, *a, **k):
            return None

_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)


def call_gemini(prompt: str, max_tokens: int = 2500, start_tier: int = 4) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.4, timeout=(10, 45))


# ── 소개할 작품 목록 ──────────────────────────────────────────
# 서양 고전미술 위주 + 한국·중국·일본 고전미술 혼합(사용자 지시). wiki_query는
# Wikimedia Commons 검색어 — 사전에 전부 실측 확인(2026-09-08)해 파일이
# 실제로 존재하는 것만 올렸다. 목록이 짧아지면(=1년 넘게 반복) 여기 추가할 것.
ARTWORKS = [
    {"title_ko": "모나리자", "title_en": "Mona Lisa", "artist_ko": "레오나르도 다빈치", "artist_en": "Leonardo da Vinci", "year_label": "1503년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "Mona Lisa Leonardo da Vinci"},
    {"title_ko": "별이 빛나는 밤", "title_en": "The Starry Night", "artist_ko": "빈센트 반 고흐", "artist_en": "Vincent van Gogh", "year_label": "1889년", "country": "네덜란드", "country_flag": "🇳🇱", "region": "europe", "wiki_query": "The Starry Night Van Gogh"},
    {"title_ko": "진주 귀걸이를 한 소녀", "title_en": "Girl with a Pearl Earring", "artist_ko": "요하네스 페르메이르", "artist_en": "Johannes Vermeer", "year_label": "1665년경", "country": "네덜란드", "country_flag": "🇳🇱", "region": "europe", "wiki_query": "Girl with a Pearl Earring Vermeer"},
    {"title_ko": "비너스의 탄생", "title_en": "The Birth of Venus", "artist_ko": "산드로 보티첼리", "artist_en": "Sandro Botticelli", "year_label": "1485년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "The Birth of Venus Botticelli"},
    {"title_ko": "야간순찰", "title_en": "The Night Watch", "artist_ko": "렘브란트 판레인", "artist_en": "Rembrandt van Rijn", "year_label": "1642년", "country": "네덜란드", "country_flag": "🇳🇱", "region": "europe", "wiki_query": "The Night Watch Rembrandt"},
    {"title_ko": "시녀들(라스 메니나스)", "title_en": "Las Meninas", "artist_ko": "디에고 벨라스케스", "artist_en": "Diego Velázquez", "year_label": "1656년", "country": "스페인", "country_flag": "🇪🇸", "region": "europe", "wiki_query": "Las Meninas Velazquez"},
    {"title_ko": "튈프 박사의 해부학 강의", "title_en": "The Anatomy Lesson of Dr. Nicolaes Tulp", "artist_ko": "렘브란트 판레인", "artist_en": "Rembrandt van Rijn", "year_label": "1632년", "country": "네덜란드", "country_flag": "🇳🇱", "region": "europe", "wiki_query": "The Anatomy Lesson of Dr Nicolaes Tulp Rembrandt"},
    {"title_ko": "수련", "title_en": "Water Lilies", "artist_ko": "클로드 모네", "artist_en": "Claude Monet", "year_label": "1906년경", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "Water Lilies Monet"},
    {"title_ko": "절규", "title_en": "The Scream", "artist_ko": "에드바르 뭉크", "artist_en": "Edvard Munch", "year_label": "1893년", "country": "노르웨이", "country_flag": "🇳🇴", "region": "europe", "wiki_query": "The Scream Munch"},
    {"title_ko": "안개 바다 위의 방랑자", "title_en": "Wanderer above the Sea of Fog", "artist_ko": "카스파르 다비트 프리드리히", "artist_en": "Caspar David Friedrich", "year_label": "1818년경", "country": "독일", "country_flag": "🇩🇪", "region": "europe", "wiki_query": "Wanderer above the Sea of Fog Friedrich"},
    {"title_ko": "이삭 줍는 여인들", "title_en": "The Gleaners", "artist_ko": "장 프랑수아 밀레", "artist_en": "Jean-François Millet", "year_label": "1857년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "The Gleaners Millet painting"},
    {"title_ko": "키스", "title_en": "The Kiss", "artist_ko": "구스타프 클림트", "artist_en": "Gustav Klimt", "year_label": "1908년경", "country": "오스트리아", "country_flag": "🇦🇹", "region": "europe", "wiki_query": "The Kiss Gustav Klimt"},
    {"title_ko": "몽유도원도", "title_en": "Dream Journey to the Peach Blossom Land", "artist_ko": "안견", "artist_en": "An Gyeon", "year_label": "1447년", "country": "한국", "country_flag": "🇰🇷", "region": "asia", "wiki_query": "Dream Journey to the Peach Blossom Land An Gyeon"},
    {"title_ko": "인왕제색도", "title_en": "Inwangjesaekdo", "artist_ko": "정선", "artist_en": "Jeong Seon", "year_label": "1751년", "country": "한국", "country_flag": "🇰🇷", "region": "asia", "wiki_query": "Inwangjesaekdo Jeong Seon"},
    {"title_ko": "세한도", "title_en": "Sehando", "artist_ko": "김정희", "artist_en": "Kim Jeong-hui", "year_label": "1844년", "country": "한국", "country_flag": "🇰🇷", "region": "asia", "wiki_query": "Sehando Kim Jeong-hui"},
    {"title_ko": "미인도", "title_en": "Miindo (Portrait of a Beauty)", "artist_ko": "신윤복", "artist_en": "Shin Yun-bok", "year_label": "18세기 후반", "country": "한국", "country_flag": "🇰🇷", "region": "asia", "wiki_query": "신윤복 미인도"},
    {"title_ko": "씨름(단원풍속도첩)", "title_en": "Ssireum (Wrestling)", "artist_ko": "김홍도", "artist_en": "Kim Hong-do", "year_label": "18세기 후반", "country": "한국", "country_flag": "🇰🇷", "region": "asia", "wiki_query": "Danwon Kim Hongdo Ssireum"},
    {"title_ko": "가나가와 해변의 높은 파도 아래", "title_en": "The Great Wave off Kanagawa", "artist_ko": "가쓰시카 호쿠사이", "artist_en": "Katsushika Hokusai", "year_label": "1831년경", "country": "일본", "country_flag": "🇯🇵", "region": "asia", "wiki_query": "The Great Wave off Kanagawa Hokusai"},
    {"title_ko": "청명상하도", "title_en": "Along the River During the Qingming Festival", "artist_ko": "장택단", "artist_en": "Zhang Zeduan", "year_label": "12세기 초", "country": "중국", "country_flag": "🇨🇳", "region": "asia", "wiki_query": "Along the River During the Qingming Festival"},
    {"title_ko": "아테네 학당", "title_en": "The School of Athens", "artist_ko": "라파엘로 산치오", "artist_en": "Raphael", "year_label": "1511년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "The School of Athens Raphael"},
    {"title_ko": "아담의 창조", "title_en": "The Creation of Adam", "artist_ko": "미켈란젤로 부오나로티", "artist_en": "Michelangelo", "year_label": "1512년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "The Creation of Adam Michelangelo"},
    {"title_ko": "그랑드자트 섬의 일요일 오후", "title_en": "A Sunday Afternoon on the Island of La Grande Jatte", "artist_ko": "조르주 쇠라", "artist_en": "Georges Seurat", "year_label": "1886년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "A Sunday Afternoon on the Island of La Grande Jatte Seurat"},
    {"title_ko": "아메리칸 고딕", "title_en": "American Gothic", "artist_ko": "그랜트 우드", "artist_en": "Grant Wood", "year_label": "1930년", "country": "미국", "country_flag": "🇺🇸", "region": "global", "wiki_query": "American Gothic Grant Wood"},
    {"title_ko": "우유 따르는 여인", "title_en": "The Milkmaid", "artist_ko": "요하네스 페르메이르", "artist_en": "Johannes Vermeer", "year_label": "1658년경", "country": "네덜란드", "country_flag": "🇳🇱", "region": "europe", "wiki_query": "The Milkmaid Vermeer painting"},

    # 2026-09-27 사용자 요청("작품이 얼마나 많은데 목록에 올린 것만 쓰냐")으로
    # 20건 추가 — 각각 (1)위키미디어 커먼즈 실물 이미지 존재, (2)작가 사후
    # 70년 경과(2026년 기준 1956년 이전 사망)를 스크립트로 자동 검증 후 통과한
    # 것만 실었다. 검증 통과했어도 2건(메두사호의 뗏목·오필리아)은 최초 검색어가
    # 각각 "무명 화가의 모작"·"두개골 디테일 클로즈업"을 반환해 눈으로 직접
    # 열어봐야만 걸러졌다 — 자동검증(존재 여부)과 육안 확인(그 이미지가 맞는지)은
    # 별개라는 걸 재확인. wiki_query를 원본이 맞는 이미지로 바로잡음.
    {"title_ko": "아르놀피니 부부의 초상", "title_en": "The Arnolfini Portrait", "artist_ko": "얀 반 에이크", "artist_en": "Jan van Eyck", "year_label": "1434년", "country": "벨기에", "country_flag": "🇧🇪", "region": "europe", "wiki_query": "Arnolfini Portrait van Eyck"},
    {"title_ko": "마라의 죽음", "title_en": "The Death of Marat", "artist_ko": "자크루이 다비드", "artist_en": "Jacques-Louis David", "year_label": "1793년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "The Death of Marat David painting"},
    {"title_ko": "민중을 이끄는 자유의 여신", "title_en": "Liberty Leading the People", "artist_ko": "외젠 들라크루아", "artist_en": "Eugène Delacroix", "year_label": "1830년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "Liberty Leading the People Delacroix"},
    {"title_ko": "메두사호의 뗏목", "title_en": "The Raft of the Medusa", "artist_ko": "테오도르 제리코", "artist_en": "Théodore Géricault", "year_label": "1819년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "Radeau de la Meduse Gericault 1819"},
    {"title_ko": "오르낭의 장례식", "title_en": "A Burial at Ornans", "artist_ko": "귀스타브 쿠르베", "artist_en": "Gustave Courbet", "year_label": "1850년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "A Burial at Ornans Courbet"},
    {"title_ko": "올랭피아", "title_en": "Olympia", "artist_ko": "에두아르 마네", "artist_en": "Édouard Manet", "year_label": "1863년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "Olympia Manet painting"},
    {"title_ko": "풀밭 위의 점심 식사", "title_en": "Le Déjeuner sur l'herbe", "artist_ko": "에두아르 마네", "artist_en": "Édouard Manet", "year_label": "1863년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "Le dejeuner sur l'herbe Manet"},
    {"title_ko": "인상, 해돋이", "title_en": "Impression, Sunrise", "artist_ko": "클로드 모네", "artist_en": "Claude Monet", "year_label": "1872년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "Impression Sunrise Monet painting"},
    {"title_ko": "물랭 드 라 갈레트의 무도회", "title_en": "Bal du moulin de la Galette", "artist_ko": "피에르오귀스트 르누아르", "artist_en": "Pierre-Auguste Renoir", "year_label": "1876년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "Bal du moulin de la Galette Renoir"},
    {"title_ko": "카드놀이 하는 사람들", "title_en": "The Card Players", "artist_ko": "폴 세잔", "artist_en": "Paul Cézanne", "year_label": "1895년경", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "The Card Players Cezanne painting"},
    {"title_ko": "물랭 루주에서의 춤", "title_en": "At the Moulin Rouge", "artist_ko": "앙리 드 툴루즈로트레크", "artist_en": "Henri de Toulouse-Lautrec", "year_label": "1892년경", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "At the Moulin Rouge Toulouse-Lautrec"},
    {"title_ko": "오필리아", "title_en": "Ophelia", "artist_ko": "존 에버렛 밀레이", "artist_en": "John Everett Millais", "year_label": "1852년경", "country": "영국", "country_flag": "🇬🇧", "region": "europe", "wiki_query": "Ophelia Millais 1851 1852 Google Art Project"},
    {"title_ko": "만종", "title_en": "The Angelus", "artist_ko": "장 프랑수아 밀레", "artist_en": "Jean-François Millet", "year_label": "1859년경", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "The Angelus Millet painting"},
    {"title_ko": "비너스의 화장(로크비 비너스)", "title_en": "Venus at her Mirror (Rokeby Venus)", "artist_ko": "디에고 벨라스케스", "artist_en": "Diego Velázquez", "year_label": "1651년경", "country": "스페인", "country_flag": "🇪🇸", "region": "europe", "wiki_query": "Rokeby Venus Velazquez"},
    {"title_ko": "프리마베라(봄)", "title_en": "Primavera", "artist_ko": "산드로 보티첼리", "artist_en": "Sandro Botticelli", "year_label": "1480년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "La Primavera Botticelli 1480 Uffizi"},
    {"title_ko": "신오하시 다리와 아타케의 소나기", "title_en": "Sudden Shower over Shin-Ōhashi Bridge and Atake", "artist_ko": "우타가와 히로시게", "artist_en": "Utagawa Hiroshige", "year_label": "1857년", "country": "일본", "country_flag": "🇯🇵", "region": "asia", "wiki_query": "Sudden Shower over Shin Ohashi Hiroshige"},
    {"title_ko": "초충도", "title_en": "Chochungdo (Plants and Insects)", "artist_ko": "신사임당", "artist_en": "Shin Saimdang", "year_label": "16세기", "country": "한국", "country_flag": "🇰🇷", "region": "asia", "wiki_query": "신사임당 초충도"},
    {"title_ko": "금강전도", "title_en": "Complete View of Mt. Geumgang", "artist_ko": "정선", "artist_en": "Jeong Seon", "year_label": "1734년", "country": "한국", "country_flag": "🇰🇷", "region": "asia", "wiki_query": "정선 금강전도"},
    {"title_ko": "부춘산거도", "title_en": "Dwelling in the Fuchun Mountains", "artist_ko": "황공망", "artist_en": "Huang Gongwang", "year_label": "1350년경", "country": "중국", "country_flag": "🇨🇳", "region": "asia", "wiki_query": "Dwelling in the Fuchun Mountains"},
    {"title_ko": "한희재야연도", "title_en": "Night Revels of Han Xizai", "artist_ko": "고굉중", "artist_en": "Gu Hongzhong", "year_label": "10세기", "country": "중국", "country_flag": "🇨🇳", "region": "asia", "wiki_query": "Night Revels of Han Xizai"},

    # 2026-09-27 2차 확장분. 이번에도 자동검증 통과 후 육안으로 재확인하다가
    # "바벨탑"은 원작이 아니라 독일 오버하우젠 가스탱크 전시장의 대형 복제
    # 설치물 사진이 잡혀서 쿼리를 다시 잡았고, "죽음과 소녀"는 동명의 전혀 다른
    # 1520년작 그림(작가 Schwarz)이 먼저 잡혀 에곤 실레 이름을 명시해 바로잡음.
    # 장승업 "계산무진도"·이인문 "강산무진도"는 특정 작품명으로는 위키미디어에
    # 이미지가 없어(다른 작품의 디테일 크롭만 잡힘) 이번 배치에서 제외 — 나중에
    # 정확한 이미지가 확인되면 추가.
    {"title_ko": "최후의 만찬", "title_en": "The Last Supper", "artist_ko": "레오나르도 다빈치", "artist_en": "Leonardo da Vinci", "year_label": "1495년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "The Last Supper Leonardo da Vinci Milan"},
    {"title_ko": "이카루스의 추락이 있는 풍경", "title_en": "Landscape with the Fall of Icarus", "artist_ko": "대(大) 피터르 브뤼헐", "artist_en": "Pieter Bruegel the Elder", "year_label": "1560년경", "country": "벨기에", "country_flag": "🇧🇪", "region": "europe", "wiki_query": "Landscape with the Fall of Icarus Bruegel"},
    {"title_ko": "바벨탑", "title_en": "The Tower of Babel", "artist_ko": "대(大) 피터르 브뤼헐", "artist_en": "Pieter Bruegel the Elder", "year_label": "1563년", "country": "벨기에", "country_flag": "🇧🇪", "region": "europe", "wiki_query": "Tower of Babel Bruegel Kunsthistorisches Museum"},
    {"title_ko": "성 삼위일체", "title_en": "Holy Trinity", "artist_ko": "마사초", "artist_en": "Masaccio", "year_label": "1427년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "Masaccio Holy Trinity fresco Santa Maria Novella"},
    {"title_ko": "우르비노의 비너스", "title_en": "Venus of Urbino", "artist_ko": "티치아노 베첼리오", "artist_en": "Titian", "year_label": "1538년", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "Venus of Urbino Titian Uffizi"},
    {"title_ko": "시스티나의 성모", "title_en": "Sistine Madonna", "artist_ko": "라파엘로 산치오", "artist_en": "Raphael", "year_label": "1512년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "Sistine Madonna Raphael Dresden"},
    {"title_ko": "홀로페르네스의 목을 베는 유디트", "title_en": "Judith Beheading Holofernes", "artist_ko": "아르테미시아 젠틸레스키", "artist_en": "Artemisia Gentileschi", "year_label": "1620년경", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "Judith Beheading Holofernes Gentileschi Uffizi"},
    {"title_ko": "성 마태오의 소명", "title_en": "The Calling of Saint Matthew", "artist_ko": "카라바조", "artist_en": "Caravaggio", "year_label": "1600년", "country": "이탈리아", "country_flag": "🇮🇹", "region": "europe", "wiki_query": "The Calling of Saint Matthew Caravaggio"},
    {"title_ko": "1808년 5월 3일", "title_en": "The Third of May 1808", "artist_ko": "프란시스코 고야", "artist_en": "Francisco Goya", "year_label": "1814년", "country": "스페인", "country_flag": "🇪🇸", "region": "europe", "wiki_query": "Tres de Mayo Goya Prado"},
    {"title_ko": "그랑드 오달리스크", "title_en": "La Grande Odalisque", "artist_ko": "장오귀스트도미니크 앵그르", "artist_en": "Jean-Auguste-Dominique Ingres", "year_label": "1814년", "country": "프랑스", "country_flag": "🇫🇷", "region": "europe", "wiki_query": "La Grande Odalisque Ingres Louvre"},
    {"title_ko": "전함 테메레르", "title_en": "The Fighting Temeraire", "artist_ko": "윌리엄 터너", "artist_en": "J. M. W. Turner", "year_label": "1839년", "country": "영국", "country_flag": "🇬🇧", "region": "europe", "wiki_query": "The Fighting Temeraire Turner National Gallery"},
    {"title_ko": "건초 마차", "title_en": "The Hay Wain", "artist_ko": "존 컨스터블", "artist_en": "John Constable", "year_label": "1821년", "country": "영국", "country_flag": "🇬🇧", "region": "europe", "wiki_query": "The Hay Wain Constable National Gallery"},
    {"title_ko": "죽음과 소녀", "title_en": "Death and the Maiden", "artist_ko": "에곤 실레", "artist_en": "Egon Schiele", "year_label": "1915년", "country": "오스트리아", "country_flag": "🇦🇹", "region": "europe", "wiki_query": "Egon Schiele Death and the Maiden 1915"},
    {"title_ko": "개풍쾌청(붉은 후지산)", "title_en": "Fine Wind, Clear Morning (Red Fuji)", "artist_ko": "가쓰시카 호쿠사이", "artist_en": "Katsushika Hokusai", "year_label": "1831년경", "country": "일본", "country_flag": "🇯🇵", "region": "asia", "wiki_query": "Fine Wind Clear Morning Red Fuji Hokusai"},
    {"title_ko": "조춘도", "title_en": "Early Spring", "artist_ko": "곽희", "artist_en": "Guo Xi", "year_label": "1072년", "country": "중국", "country_flag": "🇨🇳", "region": "asia", "wiki_query": "Early Spring Guo Xi painting"},
]

# 2026-09-27 사용자 요청("적어도 1년은 해야하는데 미술 관련 DB나 깃허브 없어?")
# — 메트로폴리탄 미술관 Open Access(github.com/metmuseum/openaccess, CC0)의
# 공식 Collection API(collectionapi.metmuseum.org)에서 대표작(isHighlight)+
# 퍼블릭도메인(isPublicDomain)만, 작가 사후 70년 경과까지 자동 검증해 350건을
# 수집(scripts/harvest_met_artworks.py 실행 결과 → 이 JSON). 이미지도 Met가
# 공식으로 "이 작품=이 이미지"를 확정해서 주는 URL(direct_image_url)을 그대로
# 쓰므로, 손수 짠 목록에서 반복됐던 "검색은 맞았는데 엉뚱한 사진" 위험이 없다.
try:
    with open(os.path.join(os.path.dirname(__file__), "data", "art_weekly_global_artworks.json"), encoding="utf-8") as _f:
        ARTWORKS.extend(json.load(_f))
except Exception as e:
    print(f"  ⚠️ art_weekly_global_artworks.json 로드 실패(기본 {len(ARTWORKS)}개만 사용): {e}")


def is_unknown_artist(a: dict) -> bool:
    name = f"{a.get('artist_ko', '')} {a.get('artist_en', '')}".lower()
    return any(t in name for t in ("미상", "unknown", "anonymous", "작자", "attributed to", "workshop of", "follower of"))


def is_not_painting(a: dict) -> bool:
    """Met 수집분은 museum_grounding에 '분류: X'가 들어 있다 — 회화(Paintings)가 아니면 제외
    (2026-09-28 사용자 지시 "그림만 다루자": 도자기 그릇·필사본이 뽑힌 사고)."""
    m = re.search(r"분류: ([^ ]+)", a.get("museum_grounding", ""))
    return bool(m) and m.group(1) != "Paintings"


def get_daily_artwork(today=None) -> tuple[dict, "date"]:
    """날짜(toordinal)로 결정론적 순환 선택. 반환: (artwork, date)."""
    today = today or now_kst().date()
    idx = today.toordinal() % len(ARTWORKS)
    # 2026-09-28: 작자 미상 공예품(그릇·필사본 등)은 근거가 미술관 기록 몇 줄뿐이라 빈약한
    # 기사가 나왔다(터코이즈 볼 위드 류트 플레이어…). 작가가 특정된 작품만 쓰도록 다음 후보로 넘긴다.
    for k in range(len(ARTWORKS)):
        a = ARTWORKS[(idx + k) % len(ARTWORKS)]
        if not is_unknown_artist(a) and not is_not_painting(a):
            return a, today
    return ARTWORKS[idx], today


def already_published(pub_date) -> bool:
    internal_url = f"internal://art_weekly_{pub_date.isoformat()}"
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={"select": "id", "url": f"eq.{internal_url}", "limit": "1"},
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def _met_image_url(object_id) -> str:
    """메트 API에서 작품 1건의 이미지 주소(퍼블릭도메인일 때만). 차단·실패면 빈 문자열."""
    try:
        r = requests.get(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{object_id}",
                         timeout=20, headers={"User-Agent": "NewsFinalBot/1.0"})
        if r.status_code == 200 and r.json().get("isPublicDomain"):
            return r.json().get("primaryImage") or ""
    except Exception:
        pass
    return ""


def fetch_artwork_image(artwork: dict) -> tuple[str, str]:
    """작품 실물 이미지만 쓴다 — 못 찾으면 빈 문자열(호출부가 발행을 건너뜀).
    일반 기사처럼 Pixabay 스톡사진으로 대체하지 않는다(작품 소개 기사에
    엉뚱한 사진이 붙으면 안 됨 — 2026-09-08 파올라 페를롭 사고와 같은 문제).

    2026-09-27 추가 — direct_image_url이 있으면(메트로폴리탄 미술관 Open Access
    수집분, art_weekly_met_artworks.json) 위키미디어 검색을 아예 건너뛰고 그
    URL을 바로 쓴다. Met가 공식 API로 이미 "이 작품 = 이 이미지"를 확정해서
    주므로, 손수 짠 59개 항목에서 반복 발견된 "검색어는 맞았는데 엉뚱한 사진이
    잡히는" 부류의 위험(모작·디테일 크롭·다른 동명작 등)이 원천적으로 없다."""
    direct_url = artwork.get("direct_image_url")
    if not direct_url and artwork.get("met_object_id"):
        direct_url = _met_image_url(artwork["met_object_id"])  # 목록(CSV)엔 이미지 주소가 없어 발행 때 1건만 조회
    if direct_url:
        key = artwork.get("met_object_id") or artwork.get("wikidata") or re.sub(r"\W+", "", artwork["title_en"])[:40]
        try:
            from image_store import store_image
            stored_url = store_image(direct_url, key_hint=f"art_weekly_{key}")
        except Exception as e:
            print(f"  ⚠️ 이미지 R2 저장 실패, 원본 URL 사용: {e}")
            stored_url = direct_url
        dept = artwork.get("met_department", "")
        credit = artwork.get("image_credit") or f"이미지 출처: The Metropolitan Museum of Art (CC0 퍼블릭 도메인{f', {dept}' if dept else ''})"
        return (stored_url or direct_url), credit

    wiki_url, wiki_credit = fetch_wikimedia_image(artwork["wiki_query"], allow_artwork=True)
    if not wiki_url:
        return "", ""
    try:
        from image_store import store_image
        stored_url = store_image(wiki_url, key_hint=f"art_weekly_{artwork['title_en']}")
    except Exception as e:
        print(f"  ⚠️ 이미지 R2 저장 실패, 원본 URL 사용: {e}")
        stored_url = wiki_url
    credit = wiki_credit or "이미지 출처: Wikimedia Commons (퍼블릭 도메인)"
    return (stored_url or wiki_url), credit


# 2026-09-27 사용자 지적("미술, 미술가, 미술사 관련은 팩트체크가 중요한데") —
# 이 writer는 그동안 유가·코인 기사(oil_price_writer.py 등)와 달리 근거자료
# (grounding) 없이 순전히 Gemini 자기 지식만으로 생애·소장처 등을 썼다. 모네처럼
# 학습데이터가 풍부한 화가는 실사고 없었지만(클로드 모네 기사 실측 팩트체크
# 완료, 오류 없음), 안견·정선·신윤복·장택단처럼 학습데이터가 희박한 인물은
# 위험이 훨씬 크다. 위키백과에서 실제 근거 텍스트를 가져와 프롬프트에 주입하고
# fabrication_guard로 그 근거 밖 날조를 검사하도록 강화.
_WIKI_UA = {"User-Agent": "NewsFinal-ArtWeekly/1.0 (+https://newsfinal.co.kr)"}

try:
    from rapidfuzz import fuzz as _wiki_fuzz
except Exception:
    _wiki_fuzz = None

# 2026-09-27 dry-run 실사고: "신윤복"(artist_en="Shin Yun-bok")으로 영어 위키
# 검색 시 실제 인물 문서("Sin Yunbok", 매큔-라이샤워 표기라 철자가 다름) 대신
# 완전히 무관한 문서("Painter of the Wind", 신윤복을 소재로 한 2008년 드라마)가
# 최상위로 잡혔다 — 검색 결과를 검증 없이 그대로 믿으면 안 된다는 걸 실측으로
# 확인. fabrication_guard.py가 이미 쓰는 rapidfuzz로 검색어-제목 유사도가 너무
# 낮으면(엉뚱한 문서로 판단) 아예 매치 없음으로 처리한다.
_WIKI_TITLE_MATCH_THRESHOLD = 55


def _wiki_search_title(query: str, lang: str) -> str | None:
    try:
        res = requests.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 1},
            headers=_WIKI_UA, timeout=10,
        )
        if res.status_code != 200:
            return None
        results = res.json().get("query", {}).get("search", [])
        if not results:
            return None
        title = results[0]["title"]
        if _wiki_fuzz is not None:
            score = max(_wiki_fuzz.token_sort_ratio(query, title), _wiki_fuzz.partial_ratio(query, title))
            if score < _WIKI_TITLE_MATCH_THRESHOLD:
                print(f"  ⚠️ 위키 검색 결과 관련성 낮음(무시): '{query}' → '{title}' (유사도 {score:.0f})")
                return None
        return title
    except Exception:
        return None


def _wiki_summary(title: str, lang: str) -> str:
    try:
        import urllib.parse
        res = requests.get(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}",
            headers=_WIKI_UA, timeout=10,
        )
        if res.status_code != 200:
            return ""
        return (res.json().get("extract") or "").strip()
    except Exception:
        return ""


def fetch_wikipedia_grounding(query_ko: str, query_en: str) -> str:
    """한국어 위키백과를 우선 시도하고, 내용이 빈약하면(200자 미만) 영어
    위키백과로 보충한다. 서양 화가는 대개 한국어판도 충실하지만, 한국 고전
    화가(안견·정선 등)는 영어판이 아예 없는 경우가 많아 언어 우선순위가
    중요하다 — 반대로 하면 정보량이 줄어든다."""
    title_ko = _wiki_search_title(query_ko, "ko")
    text = _wiki_summary(title_ko, "ko") if title_ko else ""
    if len(text) < 200:
        title_en = _wiki_search_title(query_en, "en")
        text_en = _wiki_summary(title_en, "en") if title_en else ""
        if len(text_en) > len(text):
            text = text_en
    return text


def build_article_prompt(artwork: dict, grounding: str) -> str:
    return f"""당신은 프론티어 미디어 NewsFinal의 문화·예술 담당 에디터입니다.
매주 주말 "고전 명화 이야기" 코너에서 소개할 작품은 아래와 같습니다.

작품명: {artwork['title_ko']} ({artwork['title_en']})
작가: {artwork['artist_ko']} ({artwork['artist_en']})
제작 시기: {artwork['year_label']}
관련 국가: {artwork['country']}

[근거 자료 — 위키백과에서 가져온 실제 문서 발췌]
{grounding}

이 작품과 작가를 소개하는 한국어 기사를 작성하세요. 반드시 아래 두 가지를 모두 다루세요.
- 작품 이야기: 제작 배경, 소재·기법·구도의 특징, 왜 유명해졌는지, 오늘날 어디에 소장돼 있는지.
- 작가 이야기: 생애와 활동 시기, 예술적 경향, 이 작품이 작가의 생애·작품 세계에서 갖는 의미.

⚠️ 반드시 위 [근거 자료]에 실제로 나오는 사실만 재구성해서 쓰세요. 생몰년·소장처·구체적
일화·다른 인물과의 관계 등 [근거 자료]에 없는 세부사항은 지어내지 마세요. 근거 자료가
특정 항목(예: 소장처)을 다루지 않으면 그 항목은 그냥 언급하지 말고 넘어가세요. 학계에
여러 해석이 있는 부분은 "~라는 해석도 있다"처럼 하나로 단정하지 말고 여지를 두어 서술하세요.
단, "근거 자료에 따르면", "제시된 기록에 근거해" 같이 근거자료의 존재 자체를 본문에서
언급하지 마세요 — 원래 알고 있던 사실을 소개하듯 자연스럽게 서술하세요.

[문체 규칙]
- 본문은 3~4개 문단 이상으로 충분히 작성하세요(각 문단은 빈 줄로 구분).
- 모든 문장을 "-다"로 종결하세요("-습니다"/"-입니다" 같은 정중체 금지). 단, 인용구 자체는 예외.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- "~를 보여줍니다", "~라는 평가다" 같은 논평·칼럼 문체 대신 사실 서술형으로 쓰세요.
- 외국어권 인명·지명은 한글 음차로 표기하고 첫 등장 시 1회만 괄호로 원어를 병기하세요
  (예: 레오나르도 다빈치(Leonardo da Vinci)). 이미 한글 이름인 한국 인물·지명(예: 신윤복,
  안견)은 원어 병기가 필요 없습니다. 어느 경우든 같은 이름을 다시 언급할 땐 괄호 병기를
  반복하지 말고 이름만 쓰세요.
- ⚠️ 작품명을 반드시 밝히세요: 기사 제목과 본문 첫 문단에 위 '작품명'을 한국어로 옮겨 넣고, 본문 첫 등장 때
  원제를 괄호로 병기하세요(2026-09-28 프레더릭 처치 기사가 작품명 없이 작가 소개만 하고 끝난 사고).
  한국어 제목이 정착되지 않은 작품은 원제를 그대로 쓰되 지어낸 번역 제목처럼 꾸미지 마세요.
- ⚠️ 근거자료에 이름이 나오는 인물(의뢰인·소장자·기증자·스승 등)을 언급할 땐 반드시 이름을 쓰세요.
  "한 은행가", "어느 인물"처럼 이름을 빼고 뭉뚱그리지 마세요 — 이름 없이 신분만 적으면 독자가 누구인지 알 수 없습니다
  (2026-09-28 클리블랜드 기사에서 의뢰인 힌먼 B. 헐버트의 이름이 빠진 사고). 이름을 쓰기 어려울 만큼 근거가 없다면 그 문장을 넣지 마세요.

출력 형식:
TITLE: (기사 제목 — 예: "레오나르도 다빈치의 모나리자, 500년을 사로잡은 미소")
BODY: (본문)"""


def call_gemini_article(prompt: str, max_tokens: int = 2500, style_retries: int = 1) -> str | None:
    content = call_gemini(prompt, max_tokens=max_tokens)
    attempt = 0
    while content and has_column_style(content) and attempt < style_retries:
        attempt += 1
        print(f"  ⚠️ 논평/칼럼체 감지 → 재생성 시도 ({attempt}/{style_retries})")
        retried = call_gemini(
            prompt + "\n\n[재작성 지시] 방금 작성한 결과에 논평/칼럼 문체나 정중체(-습니다)가 섞였습니다. "
                     "사실 서술형으로, 모든 문장을 '-다'로 종결해 다시 작성하세요.",
            max_tokens=max_tokens,
        )
        if retried:
            content = retried
    if content and has_polite_ending(content):
        converted = to_plain_style(content)
        if converted != content:
            print("  🔧 합쇼체(-습니다) 감지 → 자동 변환 적용")
            content = converted
    return content


def insert_article(artwork: dict, title_ko: str, body_ko: str, pub_date,
                    image_url: str = "", image_credit: str = "") -> int:
    if detect_script_leak(title_ko, body_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    if is_placeholder_response(title_ko, body_ko):
        print(f"  ❌ Gemini 응답이 실제 기사가 아님(거부 응답) → 저장 차단: {title_ko[:60]}")
        return -1
    _unwrapped = unwrap_json_body(body_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            body_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 저장 차단: {title_ko[:60]}")
            return -1

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    internal_url = f"internal://art_weekly_{pub_date.isoformat()}"

    # 2026-09-09 제거(사용자 지시 — 다국어 채널이 이 번역을 재사용하지 않음).
    # title_en은 artwork["title_en"](큐레이션된 원 작품명)로 계속 폴백됨.
    title_en, summary_en = "", ""

    payload = {
        "title_en": title_en or artwork["title_en"],
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": body_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "문화·예술",
        "subcategory": "고전명화이야기",
        "region": artwork["region"],
        "country": artwork["country"],
        "country_flag": artwork["country_flag"],
        "countries": [artwork["country"]],
        "image_url": image_url,
        "image_credit": image_credit,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": f"고전 명화 이야기 — {artwork['title_ko']}({artwork['artist_ko']})"}],
        "sent_telegram": 0,
        "is_published": True,
    }
    return insert_final_article(payload)


def main():
    print(f"\n[art_weekly_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not GEMINI_API_KEYS:
        print("  [SKIP] GEMINI_API_KEY 없음")
        return
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    artwork, pub_date = get_daily_artwork()
    print(f"  → 오늘({pub_date.isoformat()}) 작품: {artwork['title_ko']} ({artwork['artist_ko']})")

    if already_published(pub_date):
        print(f"  → {pub_date.isoformat()} 고전 명화 이야기 이미 존재 → 스킵")
        return

    image_url, image_credit = fetch_artwork_image(artwork)
    if not image_url:
        print(f"  [SKIP] '{artwork['title_ko']}' 실물 이미지를 Wikimedia Commons에서 찾지 못함 — "
              f"오늘은 건너뜀(엉뚱한 대체 이미지를 쓰지 않음)")
        return
    print(f"  → 이미지 확보: {image_url[:70]}")

    # "수련"처럼 작품명만으로 검색하면 동명이인/동명 식물 등 전혀 다른 문서가
    # 잡힐 수 있어(실측: "수련" 단독 검색 → 모네 그림이 아니라 수련(식물) 문서가
    # 매칭됨) 작가명을 붙여 검색 쿼리를 명확히 한다. 작가 쪽도 "Shin Yun-bok" 같은
    # 이름만으로 검색하면 무관한 문서(동명 드라마 등)가 잡힐 수 있어 " painter"를
    # 붙여 인물 문서 쪽으로 검색 우선순위를 살짝 기울인다(그래도 최종 방어는
    # _wiki_search_title의 유사도 가드).
    artwork_wiki = fetch_wikipedia_grounding(
        f"{artwork['artist_ko']} {artwork['title_ko']}", artwork["wiki_query"])
    artist_wiki = fetch_wikipedia_grounding(artwork["artist_ko"], f"{artwork['artist_en']} painter")
    # 2026-09-27 추가 — 메트로폴리탄 미술관 Open Access 수집분(art_weekly_met_artworks.json)은
    # 위키백과 문서가 아예 없는 무명/비주류 작가가 많다(예: 조선 화가 이정) —
    # 위키백과에만 기대면 이런 항목마다 근거자료 부족으로 매일 스킵될 위험이
    # 있어, 미술관이 직접 확정한 소장품 기록(museum_grounding)을 항상 존재하는
    # 1차 근거로 우선 반영하고, 위키백과는 있으면 보충용으로 덧붙인다.
    museum_grounding = artwork.get("museum_grounding", "")
    if not museum_grounding and not artwork_wiki and not artist_wiki:
        print(f"  [SKIP] '{artwork['title_ko']}'/'{artwork['artist_ko']}' 근거자료를 "
              f"찾지 못함 — 근거 없이 발행하지 않고 오늘은 건너뜀")
        return
    grounding_parts = []
    if museum_grounding:
        grounding_parts.append(f"[박물관 공식 소장품 기록]\n{museum_grounding}")
    grounding_parts.append(f"[작품: {artwork['title_ko']}]\n{artwork_wiki or '(자료 없음)'}")
    grounding_parts.append(f"[작가: {artwork['artist_ko']}]\n{artist_wiki or '(자료 없음)'}")
    grounding = "\n\n".join(grounding_parts)
    print(f"  → 근거자료 확보(박물관기록 {len(museum_grounding)}자, 작품 위키 {len(artwork_wiki)}자, 작가 위키 {len(artist_wiki)}자)")

    prompt = build_article_prompt(artwork, grounding)
    content = call_gemini_article(prompt)
    if not content:
        print("  [ERROR] 기사 생성 실패")
        return

    # 2026-09-27 추가 — 근거자료 대비 날조 검사(oil_price_writer.py 등과 동일 패턴).
    fabricated = _fg_verify_no_fabricated_names(grounding, content, call_gemini)
    if fabricated:
        print(f"  ⚠️ 근거자료에 없는 내용 감지({fabricated}) → 재생성")
        retried = call_gemini(
            prompt + f"\n\n[재작성 지시] 다음을 근거자료에 없는 내용으로 잘못 지어냈습니다: {fabricated}. "
                     "[근거 자료]에 실제로 나온 내용만 쓰고, 확인할 수 없으면 언급하지 마세요.",
            max_tokens=2500,
        )
        if retried:
            content = retried

    title, body = parse_article_output(content)
    if not title or not body:
        print(f"  [ERROR] TITLE/BODY 파싱 실패\n{content[:300]}")
        return
    body = ensure_paragraphs(body)
    if len(body) < 400:
        print(f"  ⚠️ 본문이 너무 짧음({len(body)}자) — 스킵")
        return

    article_id = insert_article(artwork, title, body, pub_date, image_url, image_credit)
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id}): {title}")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[art_weekly_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
