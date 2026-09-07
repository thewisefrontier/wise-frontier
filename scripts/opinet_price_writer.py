"""
opinet_price_writer.py
-----------------------
오피넷(한국석유공사) Open API로 전국 주유소 평균가격(휘발유·경유·LPG 등)을
조회해 국내 기름값 뉴스 기사를 자동 생성합니다.

데이터 소스: 오피넷 avgAllPrice API
  https://www.opinet.co.kr/api/avgAllPrice.do?out=json&certkey=[인증키]
무료 등록: https://www.opinet.co.kr (오픈API 이용 신청)

응답 구조(2026-08-18 공식 문서로 확정): JSON은 {"RESULT": {"OIL": [...]}}
형태이며 각 항목은 TRADE_DT/PRODCD/PRODNM/PRICE/DIFF 필드를 가진다.
_find_price_list는 이 구조를 포함해 PRODCD 키를 가진 dict 리스트를
재귀 탐색하므로, 향후 API 쪽 래퍼 변경에도 방어적으로 동작한다.

실행: python scripts/opinet_price_writer.py
"""

import math
import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv
from rapidfuzz import fuzz

try:
    from news_context import fetch_headlines
except Exception:
    def fetch_headlines(*a, **k):
        return []

load_dotenv()

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

# articles 테이블 삽입 공용 로직(2026-09-02, article_store.py로 공용화).
try:
    from article_store import insert_final_article
except Exception:
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

# ── 설정 ────────────────────────────────────────────────────
GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
SUPABASE_URL         = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
OPINET_API_KEY       = os.getenv("OPINET_API_KEY", "")  # https://www.opinet.co.kr 오픈API 무료 등록

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
    class GeminiClient:  # import 실패해도 본 기능이 죽지 않도록 폴백을 둔다
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            return None

_gemini_client = GeminiClient(GEMINI_API_KEYS, GEMINI_MODELS)

KST = timezone(timedelta(hours=9))

# 오피넷 유종 코드 (공식 문서 확인, 2026-08-18)
PRODUCT_NAMES = {
    "B027": "휘발유",
    "B034": "고급휘발유",
    "D047": "경유",
    "K015": "LPG(부탄)",
    "C004": "실내등유",
}
# 기사에서 다룰 핵심 유종(일반인 관심도 기준)
HEADLINE_PRODUCTS = ["B027", "D047", "K015"]


# ── 헬퍼 ────────────────────────────────────────────────────
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")

try:
    from image_store import store_image
except Exception:
    def store_image(src_url, key_hint="", timeout=30):
        return src_url


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


# Supabase 헤더/URL 헬퍼는 article_store.py로 공용화(2026-09-02).
try:
    from article_store import sb_headers as _sb_headers
    from article_store import sb_url as _sb_articles_url
except Exception:
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_articles_url():
        return f"{SUPABASE_URL}/rest/v1/articles"


def already_published(price_date: date) -> bool:
    """해당 날짜 국내 유가 기사가 이미 존재하는지 확인 (url 필드 기준)."""
    internal_url = f"internal://opinet_price_{price_date.isoformat()}"
    res = requests.get(
        f"{_sb_articles_url()}?url=eq.{internal_url}&is_published=eq.true&select=id",
        headers=_sb_headers(),
        timeout=10,
    )
    if res.status_code in (200, 206):
        return len(res.json()) > 0
    return False


def save_price_history(prices: dict) -> None:
    """일별 원시 평균가를 opinet_price_history에 기록.
    2026-08-18 신설 — 기사 본문(자유 텍스트)만 남기면 주간·N주 연속 비교를
    할 방법이 없어서, opinet_weekly_writer.py가 쓸 구조화 이력을 따로 쌓는다.
    (article_id, price_date) UNIQUE라 같은 날 여러 번 돌아도 중복 안 쌓임."""
    price_date = prices["date"].isoformat()
    rows = [
        {"price_date": price_date, "prodcd": code, "price": item["price"], "diff": item["diff"]}
        for code, item in prices["prices"].items()
    ]
    if not rows:
        return
    try:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/opinet_price_history",
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=rows,
            timeout=15,
        )
        if res.status_code not in (200, 201, 204):
            print(f"  ⚠️ 가격 이력 저장 실패 {res.status_code}: {res.text[:200]}")
    except Exception as e:
        print(f"  ⚠️ 가격 이력 저장 예외: {e}")


# ── 오피넷 데이터 수집 ────────────────────────────────────────
def _parse_trade_date(s: str) -> date | None:
    """'20260818' 또는 'YYYY-MM-DD' 형태 문자열을 date로 변환."""
    s = re.sub(r"[^0-9]", "", str(s or ""))
    if len(s) != 8:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def _find_price_list(obj):
    """JSON 응답 구조에서 PRODCD 키를 가진 dict의 리스트를 재귀 탐색한다.
    공식 문서 확인 결과(2026-08-18) {"RESULT": {"OIL": [...]}} 형태지만,
    향후 API 쪽 래퍼가 바뀌어도 방어적으로 동작하도록 재귀 탐색을 유지한다."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "PRODCD" in obj[0]:
            return obj
        for item in obj:
            found = _find_price_list(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for v in obj.values():
            found = _find_price_list(v)
            if found:
                return found
    return None


def fetch_opinet_prices() -> dict | None:
    """전국 평균 유가(유종별 리터당 원)를 조회. 실패 시 None."""
    if not OPINET_API_KEY:
        print("  [SKIP] OPINET_API_KEY 없음")
        return None

    url = f"https://www.opinet.co.kr/api/avgAllPrice.do?out=json&certkey={OPINET_API_KEY}"
    try:
        res = requests.get(url, timeout=(10, 30))
        if res.status_code != 200:
            print(f"  [ERROR] 오피넷 API {res.status_code}: {res.text[:200]}")
            return None
        data = res.json()
    except requests.exceptions.Timeout:
        print("  [ERROR] 오피넷 API 타임아웃")
        return None
    except Exception as e:
        print(f"  [ERROR] 오피넷 API 호출 실패: {e}")
        return None

    price_list = _find_price_list(data)
    if not price_list:
        print(f"  [ERROR] 오피넷 응답에서 가격 목록을 찾지 못함. 원본(앞부분): {str(data)[:500]}")
        return None

    prices = {}
    trade_dt = None
    for item in price_list:
        code = item.get("PRODCD")
        if not code:
            continue
        name = PRODUCT_NAMES.get(code, item.get("PRODNM") or code)
        try:
            price = float(item.get("PRICE"))
            diff = float(item.get("DIFF", 0) or 0)
        except (TypeError, ValueError):
            continue
        prices[code] = {"name": name, "price": price, "diff": diff}
        if not trade_dt:
            trade_dt = _parse_trade_date(item.get("TRADE_DT"))

    if not prices or not trade_dt:
        print(f"  [ERROR] 오피넷 가격 데이터 파싱 실패. 원본(앞부분): {str(data)[:500]}")
        return None

    return {"date": trade_dt, "prices": prices}


# ── Gemini 호출 (키 로테이션) ─────────────────────────────────
# 실사고(2026-08-18): start_tier=2(gemini-3.5-flash)에서 재시도까지 포함해
# 2회 연속 MAX_TOKENS로 실패했는데, 실제 필요한 본문은 646자에 불과했다
# (max_tokens=8000으로 재현 시 정상 생성 확인). Gemini 3.x는 "thinking" 토큰이
# maxOutputTokens 예산을 함께 소모하는 구조라, 비-lite 모델이 답변 생성 전에
# 내부 추론으로 예산을 다 써버리면 눈에 보이는 본문 없이 잘릴 수 있다(공식
# 문서로 정확한 thinkingConfig 필드까지는 확정 못함). 이 스크립트처럼 짧고
# 단순한 구조화 기사엔 thinking이 불필요하므로, RPD 500으로 여유도 있는
# lite 티어(start_tier=3)로 시작하도록 변경 — 사용자 제안.
def call_gemini(prompt: str, max_tokens: int = 1500, start_tier: int = 3) -> str | None:
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.4, timeout=(10, 30))


def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
    """이름과 충분히 비슷한 위키 문서 제목이 하나라도 있으면 True (결정론적 조회)."""
    name = (name or "").strip()
    if not name:
        return False
    titles = []
    for lang in ("ko", "en"):
        try:
            res = requests.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": name, "limit": 3, "namespace": 0, "format": "json"},
                headers={"User-Agent": "NewsFinal-EntityCheck/1.0 (+https://newsfinal.co.kr)"},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                if len(data) >= 2 and isinstance(data[1], list):
                    titles.extend(data[1])
        except Exception:
            continue
    return any(fuzz.token_sort_ratio(name, t) >= threshold for t in titles)


def extract_candidate_names(body: str) -> list:
    """판단이 아니라 단순 추출 — LLM 위험도가 낮은 작업이라 위키 조회 대상을 뽑는 데만 쓴다."""
    if not body:
        return []
    prompt = f"""아래 기사 본문에서 실제 존재 여부를 확인해볼 만한 구체적 고유명사를 추출하세요.
특정 인물 실명, 특정 기관·단체·기업명만 대상으로 합니다.
국가명·일반 지명(도시·나라)이나 흔한 일반명사·직함은 제외하세요.
쉼표로 구분해 나열만 하세요(설명 금지). 대상이 없으면 "없음"이라고만 답하세요.

본문:
{body[:2000]}

답변:"""
    result = call_gemini(prompt, max_tokens=150, start_tier=3)
    if not result:
        return []
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return []
    return [n.strip() for n in result.split(",") if n.strip() and len(n.strip()) >= 2][:15]


def verify_no_fabricated_names(source_prompt: str, body: str) -> str:
    """생성된 본문에 원문 자료에 없는 고유명사가 새로 등장했는지 확인.
    oil_price_writer.py의 동명 함수와 동일 로직(실사고 2026-08-16, id=79327)."""
    if not body:
        return ""
    check_prompt = f"""아래는 기사 작성에 쓰인 원본 자료와, 그걸 바탕으로 생성된 한국어 기사 본문입니다.
기사 본문에 나오는 고유명사(인명, 기관명)가 원본 자료에 실제로 근거하는지 확인하세요.
원본 자료에 등장하는 대상을 다른 이름으로 완전히 잘못 지어낸 경우만 찾으세요.
그런 이름이 있으면 "지어낸이름 → 원본표기" 형식으로 쉼표 구분해 나열하세요.
없으면 "없음"이라고만 답하세요.

[원본 자료]
{source_prompt[:3000]}

[생성된 기사 본문]
{body[:2000]}

답변:"""
    result = call_gemini(check_prompt, max_tokens=150, start_tier=3)
    suspect = ""
    if result:
        result = result.strip()
        if result and not ("없음" in result and len(result) <= 12):
            suspect = result

    unconfirmed = [n for n in extract_candidate_names(body) if not wikipedia_confirms(n)]
    if unconfirmed:
        note = "[위키 미확인] " + ", ".join(unconfirmed)
        suspect = (suspect + "\n" + note) if suspect else note

    return suspect


# ── 기사 프롬프트 ────────────────────────────────────────────
def build_article_prompt(prices: dict) -> str:
    pdate = prices["date"]
    p = prices["prices"]

    lines = []
    for code in HEADLINE_PRODUCTS:
        if code not in p:
            continue
        item = p[code]
        direction = "상승" if item["diff"] > 0 else ("하락" if item["diff"] < 0 else "보합")
        lines.append(
            f"- {item['name']}: 리터당 {item['price']:.2f}원 "
            f"(전일比 {'+' if item['diff']>0 else ''}{item['diff']:.2f}원, {direction})"
        )
    price_lines = "\n".join(lines)

    gasoline_price = int(round(p.get("B027", {}).get("price", 0)))
    gasoline_dir = "상승" if p.get("B027", {}).get("diff", 0) > 0 else (
        "하락" if p.get("B027", {}).get("diff", 0) < 0 else "보합"
    )

    # 2026-08-26 도입(oil_price_writer.py와 동일 패턴): 변동 배경(③)을
    # 예전엔 근거 없이 추정 서술해야 했다. 구글 뉴스에서 실제 국내 유가
    # 관련 헤드라인을 가져와 검증 가능한 근거로 쓴다.
    headlines = fetch_headlines("국내 유가 정유사 유류세", limit=5, hl="ko", gl="KR")
    if headlines:
        headline_block = "\n[관련 실제 보도 헤드라인 (참고용)]\n" + "\n".join(f"- {h}" for h in headlines)
        bg_instruction = (
            "③ 변동 배경: 위 [관련 실제 보도 헤드라인]에 나온 사건·원인(예: 국제유가 흐름, 유류세, "
            "정유사 실적·공급가, 환율)만 근거로 구체적으로 서술하세요. ⚠️ 헤드라인은 최신순 검색 결과라 "
            "오늘자가 아닐 수 있으니, 헤드라인에 나온 구체적 가격·수치는 절대 인용하지 마세요 — 가격·수치는 "
            "오직 위 [유가 데이터]만 쓰고, 헤드라인은 '왜'(원인·사건)에만 쓰세요. ⚠️ 헤드라인에 없는 "
            "정책·제도·규제(예: 최고가격제, 유류세 조정, 보조금)를 지어내지 마세요(2026-08-26 실사고 — "
            "실제로 헤드라인에 없는 \"정부 최고가격제 시행 검토\"를 지어냈고, 사실관계도 틀렸음: 최고가격제는 "
            "검토 중이 아니라 이미 시행 중). 헤드라인 중 실제로 관련된 내용이 하나도 없으면 억지로 배경을 "
            "채우지 말고 짧게 \"국제유가 흐름과 정유사 공급가에 따라 변동성을 보이고 있다\" 정도로만 "
            "간단히 쓰세요. 특정 매체를 출처로 직접 인용하지는 말고 \"~인 것으로 알려졌다\", "
            "\"~라는 분석이 나온다\"처럼 자연스럽게 녹여 쓰세요."
        )
    else:
        headline_block = ""
        bg_instruction = "③ 변동 배경: 국제유가 흐름, 정유사 공급가, 유류세, 환율 등 (사실 기반으로만, 데이터에 없는 구체적 수치를 지어내지 말 것)"

    return f"""당신은 국내 경제·생활물가 전문 기자입니다.
아래 오피넷(한국석유공사) 전국 평균 유가 데이터를 바탕으로 뉴스 기사를 작성하세요.

[유가 데이터] (출처: 오피넷/한국석유공사)
- 기준일: {pdate.month}월 {pdate.day}일
{price_lines}
{headline_block}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "[국내유가] "로 시작. 대괄호 포함 그대로 출력.
- "[국내유가] 휘발유 리터당 {gasoline_price}원…<핵심 동인>" 형태, 대괄호 포함 50자 이내
- 예: "[국내유가] 휘발유 리터당 1650원…나흘째 {gasoline_dir}"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '시사합니다'
2. 구조:
   ① 리드: "국내 주유소 휘발유 평균 판매가격이 {pdate.month}월 {pdate.day}일 기준 리터당 {gasoline_price}원을 기록했다."
   ② 경유·LPG 등 다른 유종 가격·전일 대비 수치
   {bg_instruction}
   ④ 소비자·자영업자(운수업 등) 체감 영향
3. 날짜는 "{pdate.month}월 {pdate.day}일" 형식만. "오늘", "현재", 절대연도 금지.
4. 출처: "오피넷(한국석유공사)에 따르면" 반드시 포함.
5. 분량: 500자 이상.
6. 위 ①~④는 서로 다른 문단입니다. 문단 사이에는 반드시 빈 줄을 하나씩 넣어
   구분하세요. 전체를 한 문단으로 이어 쓰지 마세요.
"""


# ── 파싱 ─────────────────────────────────────────────────────
TITLE_PREFIX = "[국내유가]"


def enforce_title_prefix(title: str) -> str:
    t = (title or "").strip()
    if not t:
        return t
    m = re.match(r"^\s*\[\s*국내\s*유가\s*\]\s*(.*)$", t)
    if m:
        t = m.group(1).strip()
    else:
        m2 = re.match(r"^국내유가(?:가|는)?\s*[,·]?\s+(.+)$", t)
        if m2 and m2.group(1)[:1] not in ("와", "과", "및"):
            t = m2.group(1).strip()
        else:
            t = re.sub(r"^국내유가\s*[,·]\s*", "", t).strip()
    return f"{TITLE_PREFIX} {t}" if t else TITLE_PREFIX


def _ensure_paragraphs(text: str, target: int = 4) -> str:
    """Gemini가 문단 구분(빈 줄) 지시를 어기고 한 덩어리로 응답하는 경우가 있어
    (2026-08-26 실사고, id=100254 — 프롬프트에 문단 구분 지시 자체가 없었음을
    확인 후 gemini_writer.py와 동일한 로직 이식) 문장(-다.) 단위로 강제 분할한다."""
    if not text or "\n\n" in text:
        return text
    sentences = [s.strip() for s in re.split(r"(?<=다\.)\s+", text.strip()) if s.strip()]
    if len(sentences) < 2:
        return text
    actual_target = min(target, len(sentences) - 1)
    actual_target = max(actual_target, 2)
    n = len(sentences)
    size = math.ceil(n / actual_target)
    groups = [sentences[i:i + size] for i in range(0, n, size)]
    return "\n\n".join(" ".join(g) for g in groups)


def parse_article_output(text: str) -> tuple[str, str]:
    title, body = "", ""
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    m_body = re.search(r"BODY:\s*([\s\S]+)", text)
    if m_title:
        title = m_title.group(1).strip()
    if m_body:
        body = _ensure_paragraphs(m_body.group(1).strip())
    return title, body


def has_column_style(text: str) -> bool:
    patterns = ["주목됩니다", "기대됩니다", "보여줍니다", "시사합니다", "중요합니다"]
    return any(p in text for p in patterns)


# ── 대표 이미지 ──────────────────────────────────────────────
_OIL_IMAGE_KEYWORDS = [
    "gas station",
    "fuel pump",
    "gasoline pump",
    "petrol station korea",
    "fuel nozzle",
    "car refueling",
]


def fetch_oil_image(price_date: date) -> str:
    """로직은 article_image.py로 공용화(2026-09-02, 4개 writer 스크립트에
    복붙돼 있었음)."""
    from article_image import fetch_seeded_pixabay_image
    return fetch_seeded_pixabay_image(
        _OIL_IMAGE_KEYWORDS, price_date.toordinal(), f"opinet_{price_date.isoformat()}"
    )


# ── 기사 삽입 ────────────────────────────────────────────────
def insert_article(title_ko: str, summary_ko: str, prices: dict, image_url: str = "") -> int:
    if detect_script_leak(title_ko, summary_ko):
        print(f"  ⚠️ [문자 혼입 감지] 저장 차단: {title_ko[:60]}")
        return -1
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")
    price_date = prices["date"].isoformat()
    internal_url = f"internal://opinet_price_{price_date}"

    payload = {
        "title_en": title_ko,
        "title_ko": title_ko,
        "summary_en": "",
        "summary_ko": summary_ko,
        "url": internal_url,
        "source": "NewsFinal",
        "category": "경제",
        "subcategory": "국내유가",
        "region": "global",
        "country": "한국",
        "country_flag": "🇰🇷",
        "image_url": image_url,
        "countries": ["한국"],
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": "국내 유가 자동 기사"}],
        "sent_telegram": 0,
        "is_published": True,
        # 2026-08-30: 데스킹 단계 본문-수치 대조용 원본 시세 저장.
        # prices["date"]는 date 객체라 JSON 직렬화가 안 되므로 문자열로 교체.
        "source_data": {**prices, "date": price_date},
    }

    return insert_final_article(payload)


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[opinet_price_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    # 2026-08-28: 외부 트리거(cron-job.org)가 30분마다 호출하게 되면서,
    # 이미 오늘자 기사가 나간 뒤에도 매번 오피넷(정부 API)을 호출하고 있던
    # 문제를 발견(사용자 지적) — oil_price_writer.py처럼 already_published()를
    # API 호출보다 먼저 체크해 불필요한 정부 API 호출을 막는다.
    today = now_kst().date()
    if already_published(today):
        print(f"  → {today} 국내 유가 기사 이미 존재 → 스킵")
        return

    prices = fetch_opinet_prices()
    if not prices:
        print("  [ERROR] 오피넷 유가 데이터 수집 실패 → 종료")
        return

    price_date = prices["date"]
    print(f"  → 데이터 기준일: {price_date.isoformat()}")

    save_price_history(prices)

    if price_date != today and already_published(price_date):
        print(f"  → {price_date} 국내 유가 기사 이미 존재 → 스킵")
        return

    print("  → Gemini로 기사 생성 중...")
    prompt = build_article_prompt(prices)
    article_text = call_gemini(prompt, max_tokens=2500)
    time.sleep(8)

    if not article_text:
        # 첫 시도가 응답 없음/잘림(MAX_TOKENS)으로 실패해도 바로 포기하지 않고
        # 한 번 더 시도한다(실사고 2026-08-18: 재시도 없이 곧장 실패 처리됨).
        print("  ⚠️ 첫 시도 실패 → 재시도")
        article_text = call_gemini(prompt, max_tokens=2500)
        time.sleep(5)

    if not article_text:
        print("  [ERROR] 기사 생성 실패")
        return

    if has_column_style(article_text):
        print("  ⚠️ 논평체 감지 → 재생성")
        article_text = call_gemini(
            prompt + "\n\n[재작성 지시] 논평/칼럼 문체가 섞였습니다. 사실 전달 중심으로만 다시 작성하세요.",
            max_tokens=2500,
        ) or article_text
        time.sleep(5)

    fabricated = verify_no_fabricated_names(prompt, article_text)
    if fabricated:
        print(f"  ⚠️ 원문에 없는 고유명사 감지({fabricated}) → 재생성")
        article_text = call_gemini(
            prompt + f"\n\n[재작성 지시] 다음 이름을 원문에 없는 표현으로 잘못 지어냈습니다: {fabricated}. "
                     "고유명사는 원본 자료에 나온 표기를 그대로 옮기고, 확신할 수 없으면 지어내지 말고 원문 표기를 그대로 쓰세요.",
            max_tokens=2500,
        ) or article_text
        time.sleep(5)

    art_title, art_body = parse_article_output(article_text)
    art_title = enforce_title_prefix(art_title)

    if not art_title or not art_body:
        print(f"  [ERROR] TITLE/BODY 파싱 실패\n{article_text[:300]}")
        return

    if len(art_body) < 400:
        print(f"  ⚠️ 본문 너무 짧음 ({len(art_body)}자) → 스킵")
        return

    print(f"  → 제목: {art_title}")
    print(f"  → 본문 {len(art_body)}자")

    image_url = fetch_oil_image(price_date)

    art_id = insert_article(art_title, art_body, prices, image_url)
    if art_id > 0:
        print(f"  ✓ 기사 삽입 완료 (articles.id={art_id})")
    else:
        print("  [ERROR] 기사 삽입 실패")

    print(f"[opinet_price_writer] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
