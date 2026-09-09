"""
scripts/art_weekly_writer.py
--------------------------------
매주 고전 명화 한 점을 소개하는 "주말 읽을거리" 기사 자동 생성.

사용자 요청(2026-09-08): "매주 고전 미술 작품 하나를 소개하는 기사가 있으면
좋을 것 같은데" → "서양 고전미술 위주로 하되, 한국, 중국, 일본 고전 미술도
섞는 걸로" → "주말에 읽을거리로" → "작품에 대한 이야기, 작가에 대한 이야기".

- 결정론적 주간 선택: ISO 연도·주차로 ARTWORKS를 순환 선택(같은 주에 여러 번
  실행돼도 already_published()가 막고, 목록을 다 돌면 처음부터 다시 순환).
- 이미지는 반드시 그 작품 실물이어야 하므로(일반 스톡사진으로 대체하면
  2026-09-08 파올라 페를롭 사고와 같은 문제) Wikimedia Commons에서 작품명으로
  직접 찾고, 못 찾으면 그 주는 발행을 건너뛴다 — 일반 기사처럼 Pixabay
  일반 스톡사진으로 대체하지 않는다(article_image.fetch_article_image()를
  안 쓰는 이유).
- 저작권: ARTWORKS에는 작가 사후 70년이 지나 퍼블릭도메인이 확실한 작품만
  올린다(뭉크 1944년 작고 → 2014년부터 PD, 클림트 1918년 작고 → 1988년부터
  PD 등 확인 후 포함). 이 목록에 새 작품을 추가할 때도 이 기준을 지킬 것.

실행: python scripts/art_weekly_writer.py
권장: 주 1회(토요일 오전) 실행 — already_published()가 같은 주 중복 발행을 막음.
"""

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
    def fetch_wikimedia_image(query: str):
        return None, None


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


def call_gemini(prompt: str, max_tokens: int = 2500, start_tier: int = 0) -> str | None:
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
]


def get_weekly_artwork(today=None) -> tuple[dict, int, int]:
    """ISO 연도·주차로 결정론적 순환 선택. 반환: (artwork, iso_year, iso_week)."""
    today = today or now_kst().date()
    iso_year, iso_week, _ = today.isocalendar()
    idx = (iso_year * 100 + iso_week) % len(ARTWORKS)
    return ARTWORKS[idx], iso_year, iso_week


def already_published(iso_year: int, iso_week: int) -> bool:
    internal_url = f"internal://art_weekly_{iso_year}W{iso_week:02d}"
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={"select": "id", "url": f"eq.{internal_url}", "limit": "1"},
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def fetch_artwork_image(artwork: dict) -> tuple[str, str]:
    """작품 실물 이미지만 쓴다 — 못 찾으면 빈 문자열(호출부가 발행을 건너뜀).
    일반 기사처럼 Pixabay 스톡사진으로 대체하지 않는다(작품 소개 기사에
    엉뚱한 사진이 붙으면 안 됨 — 2026-09-08 파올라 페를롭 사고와 같은 문제)."""
    wiki_url, wiki_credit = fetch_wikimedia_image(artwork["wiki_query"])
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


def build_article_prompt(artwork: dict) -> str:
    return f"""당신은 프론티어 미디어 NewsFinal의 문화·예술 담당 에디터입니다.
매주 주말 "고전 명화 이야기" 코너에서 소개할 작품은 아래와 같습니다.

작품명: {artwork['title_ko']} ({artwork['title_en']})
작가: {artwork['artist_ko']} ({artwork['artist_en']})
제작 시기: {artwork['year_label']}
관련 국가: {artwork['country']}

이 작품과 작가를 소개하는 한국어 기사를 작성하세요. 반드시 아래 두 가지를 모두 다루세요.
- 작품 이야기: 제작 배경, 소재·기법·구도의 특징, 왜 유명해졌는지, 오늘날 어디에 소장돼 있는지.
- 작가 이야기: 생애와 활동 시기, 예술적 경향, 이 작품이 작가의 생애·작품 세계에서 갖는 의미.
확인된 역사적 사실만 다루고 불확실한 내용은 지어내지 마세요. 학계에 여러 해석이 있는 부분은
"~라는 해석도 있다"처럼 하나로 단정하지 말고 여지를 두어 서술하세요.

[문체 규칙]
- 본문은 3~4개 문단 이상으로 충분히 작성하세요(각 문단은 빈 줄로 구분).
- 모든 문장을 "-다"로 종결하세요("-습니다"/"-입니다" 같은 정중체 금지). 단, 인용구 자체는 예외.
- 마크다운 문법, 헤더, 홍보 문구 금지.
- "~를 보여줍니다", "~라는 평가다" 같은 논평·칼럼 문체 대신 사실 서술형으로 쓰세요.
- 인명·지명 등 고유명사는 한글 음차로 표기하고 첫 등장 시 괄호로 원어를 병기하세요
  (예: 레오나르도 다빈치(Leonardo da Vinci)).

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


def insert_article(artwork: dict, title_ko: str, body_ko: str, iso_year: int, iso_week: int,
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
    internal_url = f"internal://art_weekly_{iso_year}W{iso_week:02d}"

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

    artwork, iso_year, iso_week = get_weekly_artwork()
    print(f"  → 이번 주({iso_year}년 {iso_week}주차) 작품: {artwork['title_ko']} ({artwork['artist_ko']})")

    if already_published(iso_year, iso_week):
        print(f"  → {iso_year}W{iso_week:02d} 고전 명화 이야기 이미 존재 → 스킵")
        return

    image_url, image_credit = fetch_artwork_image(artwork)
    if not image_url:
        print(f"  [SKIP] '{artwork['title_ko']}' 실물 이미지를 Wikimedia Commons에서 찾지 못함 — "
              f"이번 주는 건너뜀(엉뚱한 대체 이미지를 쓰지 않음)")
        return
    print(f"  → 이미지 확보: {image_url[:70]}")

    prompt = build_article_prompt(artwork)
    content = call_gemini_article(prompt)
    if not content:
        print("  [ERROR] 기사 생성 실패")
        return

    title, body = parse_article_output(content)
    if not title or not body:
        print(f"  [ERROR] TITLE/BODY 파싱 실패\n{content[:300]}")
        return
    body = ensure_paragraphs(body)
    if len(body) < 400:
        print(f"  ⚠️ 본문이 너무 짧음({len(body)}자) — 스킵")
        return

    article_id = insert_article(artwork, title, body, iso_year, iso_week, image_url, image_credit)
    if article_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={article_id}): {title}")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[art_weekly_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
