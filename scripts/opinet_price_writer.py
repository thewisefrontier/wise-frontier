"""
opinet_price_writer.py
-----------------------
오피넷(한국석유공사) Open API로 전국 주유소 평균가격(휘발유·경유·LPG 등)을
조회해 국내 기름값 뉴스 기사를 자동 생성합니다.

데이터 소스: 오피넷 avgAllPrice API
  https://www.opinet.co.kr/api/avgAllPrice.do?out=json&certkey=[인증키]
무료 등록: https://www.opinet.co.kr (오픈API 이용 신청)

⚠️ 2026-08-18: 오피넷 JSON 응답의 필드명(TRADE_DT/PRODCD/PRODNM/PRICE/DIFF)은
공식 문서로 확인했지만, 최상위 래퍼 키(예: RESULT.OIL 형태인지 등)는 문서로
100% 확정하지 못했다. 그래서 PRODCD 키를 가진 dict 리스트를 재귀 탐색하는
방식으로 방어적으로 파싱한다(_find_price_list). 실제 키로 첫 실행 시 응답
구조가 예상과 다르면 원본을 로그에 남기도록 해뒀다.

실행: python scripts/opinet_price_writer.py
"""

import os
import re
import time
import requests
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

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

_current_key_idx = 0
_exhausted_keys = {m: set() for m in GEMINI_MODELS}  # 모델별 RPD 소진 키

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
    오피넷 JSON 응답의 최상위 래퍼 키를 문서로 100% 확정하지 못해(2026-08-18),
    구조 변화에 방어적으로 대응하기 위함."""
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
def call_gemini(prompt: str, max_tokens: int = 1500, start_tier: int = 2) -> str | None:
    global _current_key_idx, _exhausted_keys
    if not GEMINI_API_KEYS:
        print("[ERROR] GEMINI_API_KEY 없음")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": max_tokens},
    }

    n = len(GEMINI_API_KEYS)
    model_stages = [(m, _exhausted_keys[m]) for m in GEMINI_MODELS[start_tier:]]

    for model, exhausted in model_stages:
        available = [i for i in range(n) if i not in exhausted]
        if not available:
            print(f"  [{model}] 모든 키 RPD 소진 → 다음 모델로")
            continue

        ordered = sorted(available, key=lambda i: (i - _current_key_idx) % n)

        for idx in ordered:
            api_key = GEMINI_API_KEYS[idx]
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
            try:
                res = requests.post(url, json=payload, timeout=(10, 30))
                if res.status_code == 200:
                    _current_key_idx = (idx + 1) % n
                    _cand = res.json()["candidates"][0]
                    _finish = _cand.get("finishReason", "")
                    if _finish and _finish != "STOP":
                        print(f"  [WARN] {model} 응답 비정상 종료(finishReason={_finish}) — 폐기")
                        return None
                    return _cand["content"]["parts"][0]["text"].strip()
                elif res.status_code == 429:
                    print(f"  [429] {model} 키 {idx+1} RPD 소진 → 다음 키")
                    exhausted.add(idx)
                    continue
                elif res.status_code == 503:
                    print(f"  [503] {model} 키 {idx+1} 과부하 → 다음 키")
                    continue
                else:
                    print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                    return None
            except requests.exceptions.Timeout:
                print(f"  [TIMEOUT] {model} 키 {idx+1}")
                continue
            except Exception as e:
                print(f"[ERROR] {e}")
                return None

    print("[ERROR] 모든 모델/키 소진 또는 응답 없음")
    return None


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
            f"- {item['name']}: 리터당 {item['price']:,.2f}원 "
            f"(전일比 {'+' if item['diff']>0 else ''}{item['diff']:,.2f}원, {direction})"
        )
    price_lines = "\n".join(lines)

    gasoline_price = int(round(p.get("B027", {}).get("price", 0)))
    gasoline_dir = "상승" if p.get("B027", {}).get("diff", 0) > 0 else (
        "하락" if p.get("B027", {}).get("diff", 0) < 0 else "보합"
    )

    return f"""당신은 국내 경제·생활물가 전문 기자입니다.
아래 오피넷(한국석유공사) 전국 평균 유가 데이터를 바탕으로 뉴스 기사를 작성하세요.

[유가 데이터] (출처: 오피넷/한국석유공사)
- 기준일: {pdate.month}월 {pdate.day}일
{price_lines}

[출력 형식] — 반드시 이 형식 그대로:
TITLE: <제목>
BODY: <본문>

[제목]
- 반드시 "[국내유가] "로 시작. 대괄호 포함 그대로 출력.
- "[국내유가] 휘발유 리터당 {gasoline_price}원…<핵심 동인>" 형태, 대괄호 포함 50자 이내
- 예: "[국내유가] 휘발유 리터당 1,650원…나흘째 {gasoline_dir}"

[본문]
1. 뉴스 스타일. 모든 문장 "-다" 종결. 감정·논평 표현 금지.
   금지어: '주목됩니다', '기대됩니다', '보여줍니다', '시사합니다'
2. 구조:
   ① 리드: "국내 주유소 휘발유 평균 판매가격이 {pdate.month}월 {pdate.day}일 기준 리터당 {gasoline_price}원을 기록했다."
   ② 경유·LPG 등 다른 유종 가격·전일 대비 수치
   ③ 변동 배경: 국제유가 흐름, 정유사 공급가, 유류세, 환율 등 (사실 기반으로만, 데이터에 없는 구체적 수치를 지어내지 말 것)
   ④ 소비자·자영업자(운수업 등) 체감 영향
3. 날짜는 "{pdate.month}월 {pdate.day}일" 형식만. "오늘", "현재", 절대연도 금지.
4. 출처: "오피넷(한국석유공사)에 따르면" 반드시 포함.
5. 분량: 500자 이상.
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


def parse_article_output(text: str) -> tuple[str, str]:
    title, body = "", ""
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    m_body = re.search(r"BODY:\s*([\s\S]+)", text)
    if m_title:
        title = m_title.group(1).strip()
    if m_body:
        body = m_body.group(1).strip()
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
    if not PIXABAY_API_KEY:
        return ""
    seed = price_date.toordinal()
    query = _OIL_IMAGE_KEYWORDS[seed % len(_OIL_IMAGE_KEYWORDS)]
    try:
        res = requests.get(
            "https://pixabay.com/api/",
            params={
                "key": PIXABAY_API_KEY,
                "q": query,
                "image_type": "photo",
                "safesearch": "true",
                "per_page": 10,
            },
            timeout=15,
        )
        if res.status_code != 200:
            print(f"  ⚠️ Pixabay {res.status_code}: {res.text[:100]}")
            return ""
        hits = res.json().get("hits", [])
        if not hits:
            print(f"  ⚠️ Pixabay 결과 없음: {query}")
            return ""
        hit = hits[seed % len(hits)]
        raw_url = hit.get("largeImageURL", "")
        if not raw_url:
            return ""
        url = store_image(raw_url, key_hint=f"opinet_{price_date.isoformat()}")
        print(f"  🖼️ 이미지: {query} → {url[:70]}")
        return url or ""
    except Exception as e:
        print(f"  ⚠️ Pixabay 실패: {e}")
    return ""


# ── 기사 삽입 ────────────────────────────────────────────────
def insert_article(title_ko: str, summary_ko: str, prices: dict, image_url: str = "") -> int:
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
    }

    headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_sb_articles_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0].get("id", -1) if data else -1
    print(f"  [ERROR] 기사 삽입 실패 {res.status_code}: {res.text[:200]}")
    return -1


# ── 메인 ─────────────────────────────────────────────────────
def main():
    print(f"\n[opinet_price_writer] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    prices = fetch_opinet_prices()
    if not prices:
        print("  [ERROR] 오피넷 유가 데이터 수집 실패 → 종료")
        return

    price_date = prices["date"]
    print(f"  → 데이터 기준일: {price_date.isoformat()}")

    if already_published(price_date):
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
