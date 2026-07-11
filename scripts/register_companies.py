"""
register_companies.py
----------------------
최근 '산업·기업' 카테고리 기사에서 언급된 실제 상장기업을 Gemini로 추출해
companies 테이블에 자동 등록합니다.

- 기사 하나당 한 번만 스캔합니다 (articles.company_scanned 플래그로 중복 방지 —
  Supabase에 이 컬럼을 미리 추가해 두었습니다).
- 이미 등록된 기업(프론트엔드와 동일한 id 슬러그 기준)은 건너뜁니다.
- ticker/exchange는 Gemini가 확실하다고 응답한 경우에만 채웁니다. 불확실하면
  null로 남겨두고 절대 추측하지 않습니다 — 이후 admin.html 등에서 수동 보완 가능.
- 기존에 등록된 기업 정보는 덮어쓰지 않습니다(신규만 삽입).

실행: python scripts/register_companies.py
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

GEMINI_MODEL = "gemini-3.1-flash-lite"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

GEMINI_API_KEYS = [k for k in [
    os.getenv("GEMINI_API_KEY"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
] if k]

_current_key_idx = 0
_exhausted_keys = set()

MAX_ARTICLES = 15        # 1회 실행당 스캔할 기사 수 (Gemini 호출 수 제한 목적)
CALL_INTERVAL = 10       # Gemini 호출 간격(초) — 다른 스크립트와 동일한 페이스
SCAN_WINDOW_DAYS = 3     # 최근 N일 기사만 대상

# 사이트(EXCHANGE_SUFFIX, COUNTRY_MARKET_MAP)에서 이미 쓰고 있는 거래소 코드만 허용.
# Gemini가 이 목록 밖의 값을 내놓으면 null 처리 — 임의의 거래소 코드를 만들어내지 않음.
KNOWN_EXCHANGES = {"NGX", "NSE", "JSE", "IDX", "SET", "PSE", "EGX", "HOSE"}


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _articles_url():
    return f"{SUPABASE_URL}/rest/v1/articles"

def _companies_url():
    return f"{SUPABASE_URL}/rest/v1/companies"


def slugify(name: str) -> str:
    """docs/country.html renderCompanies()의 id 생성 규칙과 동일하게 유지.
    프론트엔드 링크(/company?id=...)가 여기서 만든 id와 반드시 일치해야 함."""
    s = name.lower()
    s = re.sub(r'\s*\(.*?\)', '', s)
    s = s.strip()
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'[^a-z0-9_]', '', s)
    return s


def get_candidate_articles(limit: int) -> list:
    """산업·기업 카테고리, 아직 기업 스캔을 하지 않은 최근 기사"""
    since = (now_kst() - timedelta(days=SCAN_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M")
    res = requests.get(
        _articles_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_en,title_ko,summary_en,summary_ko,full_text,country,country_flag,category",
            "category": "eq.산업·기업",
            "company_scanned": "eq.false",
            "created_at": f"gte.{since}",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30
    )
    if res.status_code in (200, 206):
        return res.json()
    print(f"[ERROR] 기사 조회 실패: {res.status_code} — {res.text[:200]}")
    return []


def mark_scanned(article_id: int):
    requests.patch(
        f"{_articles_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={"company_scanned": True},
        timeout=15
    )


def get_existing_company_ids() -> set:
    ids = set()
    offset = 0
    batch = 1000
    while True:
        res = requests.get(
            _companies_url(),
            headers={**_sb_headers(), "Range": f"{offset}-{offset+batch-1}"},
            params={"select": "id"},
            timeout=20
        )
        if res.status_code not in (200, 206):
            break
        data = res.json()
        if not data:
            break
        ids.update(c["id"] for c in data)
        if len(data) < batch:
            break
        offset += batch
    return ids


def call_gemini(prompt: str, max_tokens: int = 500) -> str | None:
    global _current_key_idx, _exhausted_keys
    if not GEMINI_API_KEYS:
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens},
    }

    n = len(GEMINI_API_KEYS)
    available = [i for i in range(n) if i not in _exhausted_keys]
    if not available:
        print("[ERROR] 모든 키 RPD 소진")
        return None

    ordered = sorted(available, key=lambda i: (i - _current_key_idx) % n)

    for idx in ordered:
        api_key = GEMINI_API_KEYS[idx]
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={api_key}"
        )
        try:
            res = requests.post(url, json=payload, timeout=(10, 30))
            if res.status_code == 200:
                _current_key_idx = (idx + 1) % n
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            elif res.status_code == 429:
                print(f"  [429] 키 {idx+1} RPD 소진 — 블랙리스트 추가")
                _exhausted_keys.add(idx)
                continue
            elif res.status_code == 503:
                print(f"  [503] 키 {idx+1} 과부하 → 다음 키로")
                continue
            else:
                print(f"[ERROR] Gemini {res.status_code}: {res.text[:200]}")
                return None
        except requests.exceptions.Timeout:
            print(f"  [TIMEOUT] 키 {idx+1} — 다음 키로")
            continue
        except Exception as e:
            print(f"[ERROR] {e}")
            return None
    return None


def extract_companies(article: dict) -> list:
    """기사 하나에서 언급된 실제 상장기업 목록을 Gemini로 추출"""
    title = article.get("title_ko") or article.get("title_en") or ""
    body = article.get("full_text") or article.get("summary_ko") or article.get("summary_en") or ""
    country = article.get("country") or ""

    prompt = f"""아래 기사에서 실제로 증권거래소에 상장되어 있는 기업이 명시적으로 언급됐는지 확인하세요.

[기사]
제목: {title}
국가: {country}
본문: {body[:2000]}

규칙:
- 이 기사가 명확히 다루는, 실제 상장기업만 포함하세요. 스쳐 지나가듯 언급된 기업, 비상장기업, 정부기관, 공공기관은 제외하세요.
- 확실하지 않은 정보(티커, 거래소)는 절대 지어내지 말고 null로 두세요.
- 상장기업이 없으면 빈 배열 []만 반환하세요.

JSON 배열로만 응답하세요 (마크다운, 설명 문구 없이):
[
  {{
    "name": "기업 공식 영문명",
    "sector": "업종 (한국어, 예: 은행/통신/에너지 등 짧게)",
    "ticker": "확실히 아는 경우에만 티커 심볼, 모르면 null",
    "exchange": "확실히 아는 경우에만 거래소 코드(NGX/NSE/JSE/IDX/SET/PSE/EGX/HOSE 중 하나), 모르면 null"
  }}
]"""

    raw = call_gemini(prompt, max_tokens=500)
    if not raw:
        return []

    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        return []
    try:
        companies = json.loads(match.group())
        return companies if isinstance(companies, list) else []
    except Exception:
        return []


def upsert_company(company: dict, country: str, country_flag: str) -> bool:
    name = (company.get("name") or "").strip()
    if not name:
        return False
    cid = slugify(name)
    if not cid:
        return False

    exchange = company.get("exchange")
    if exchange not in KNOWN_EXCHANGES:
        exchange = None
    ticker = company.get("ticker") or None

    payload = {
        "id": cid,
        "name": name,
        "country": country,
        "country_flag": country_flag,
        "exchange": exchange,
        "ticker": ticker,
        "sector": company.get("sector") or None,
        "is_published": True,
        "updated_at": now_kst().strftime("%Y-%m-%d %H:%M"),
    }
    # 이미 존재하는 id면 건드리지 않음 (신규만 삽입) — 수동 큐레이션 데이터 보호
    res = requests.post(
        _companies_url(),
        headers={**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=payload,
        timeout=15
    )
    return res.status_code in (200, 201)


def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음 — register_companies 건너뜀")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] Supabase 환경변수 없음")
        return

    articles = get_candidate_articles(MAX_ARTICLES)
    print(f"[기업 등록] 스캔 대상 기사 {len(articles)}건")
    if not articles:
        return

    existing_ids = get_existing_company_ids()
    print(f"[기업 등록] 기존 등록 기업 {len(existing_ids)}개")

    registered = 0
    for i, article in enumerate(articles):
        companies = extract_companies(article)
        for c in companies:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            cid = slugify(name)
            if not cid or cid in existing_ids:
                continue
            ok = upsert_company(c, article.get("country") or "", article.get("country_flag") or "")
            if ok:
                existing_ids.add(cid)
                registered += 1
                print(f"  ✅ 신규 등록: {name} ({cid})")
            else:
                print(f"  ❌ 등록 실패: {name}")

        mark_scanned(article["id"])
        if i < len(articles) - 1:
            time.sleep(CALL_INTERVAL)

    print(f"[기업 등록] 완료 — {registered}개 신규 등록")


if __name__ == "__main__":
    run()
