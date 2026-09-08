"""
scripts/multilang_translate.py
-------------------------
다국어(글로벌) 채널용 배치 번역기. `domestic_kr_fetcher.py`가 모은 국내
기사(및 한국 관련 데이터저널리즘 기사)를 언어별로 번역해
`article_translations`에 저장한다.

2026-09-08 도입 — 다국어 채널 콘텐츠 전략 최종안: 프론티어/글로벌
기사가 아니라 **국내(한국) 뉴스 번역이 채널의 메인 소스**(사용자 지시:
"해외쪽에는 오히려 한국기사만 번역해서 보여주는게 더 나을 것 같은데").

독립 실행 스크립트다(gdelt_fetcher.py 등과 동일 패턴 — rss_fetcher.py는
import 시점에 파이프라인 전체가 실행되는 구조라 재사용 불가).

번역은 translate_guard.py의 translate_article()(Gemini)로 생성하고,
verify_translation()(NVIDIA, 계열이 다른 모델)로 숫자·사실 보존 여부를
검증한다 — 사용자 지시: "번역도 제미나이 번역이 우수하다지만, 다른
방법도 생각해둬. 최소한 두세번 검증은 필요할테니까", "거의 놀고 있는
엔비디아를 이용해도 괜찮을 것 같고". 검증 실패 시 1회 재시도 후 포기
(해당 언어만 스킵, 다른 언어/기사는 계속 진행).
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

from translate_guard import translate_article, verify_translation, LANG_NAMES
from gemini_client import GeminiClient

try:
    from nvidia_client import call_nvidia
except Exception:
    call_nvidia = None

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEYS = [
    os.getenv(k) for k in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
                            "GEMINI_API_KEY_4", "GEMINI_API_KEY_5")
    if os.getenv(k)
]

LANGUAGES = ["en", "hi", "fr", "es"]  # Phase 2 확장 시 이 리스트만 수정

# 사이클당 처리 기사 수 상한(사용자 지시: "전체 기사를 다 번역할 필요는 없고") —
# 언어 4개 기준 최대 10건 × 4 = 40콜/사이클, run.yml 30분 주기에 부담 없는 수준.
MAX_ARTICLES_PER_CYCLE = 10

_gemini_client = GeminiClient(GEMINI_API_KEYS) if GEMINI_API_KEYS else None


def _call_gemini(prompt: str, max_tokens: int = 3500):
    if not _gemini_client:
        return None
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=3, temperature=0.3, timeout=(10, 45))


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def fetch_candidates(limit: int = MAX_ARTICLES_PER_CYCLE) -> list:
    """번역 대상 후보 조회: DomesticKR 원본 기사 + 한국 관련 데이터저널리즘
    (오피넷 국내유가) 기사 중, 아직 article_translations에 언어 4개가
    다 안 채워진 것들. 최신순으로 limit건."""
    res = requests.get(
        _sb_url("articles"),
        headers=_sb_headers(),
        params={
            "select": "id,title_ko,summary_ko,source,subcategory",
            "or": "(source.like.DomesticKR:*,subcategory.eq.국내유가)",
            "order": "created_at.desc",
            "limit": "200",  # 넉넉히 가져와서 아래서 미번역분만 추림(limit건)
        },
        timeout=15,
    )
    if res.status_code not in (200, 206):
        print(f"[후보 조회 실패] {res.status_code} — {res.text[:200]}")
        return []
    articles = res.json() or []

    candidates = []
    for art in articles:
        if len(candidates) >= limit:
            break
        if not art.get("summary_ko") or not art.get("title_ko"):
            continue
        existing = _existing_langs(art["id"])
        missing = [l for l in LANGUAGES if l not in existing]
        if missing:
            candidates.append({**art, "missing_langs": missing})
    return candidates


def _existing_langs(article_id: int) -> set:
    res = requests.get(
        _sb_url("article_translations"),
        headers=_sb_headers(),
        params={"select": "lang", "article_id": f"eq.{article_id}"},
        timeout=10,
    )
    if res.status_code not in (200, 206):
        return set()
    return {row["lang"] for row in (res.json() or [])}


def save_translation(article_id: int, lang: str, title: str, summary: str) -> bool:
    res = requests.post(
        _sb_url("article_translations"),
        headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        json={"article_id": article_id, "lang": lang, "title": title, "summary": summary},
        timeout=15,
    )
    return res.status_code in (200, 201)


def run():
    if not _gemini_client:
        print("[SKIP] GEMINI_API_KEY 없음")
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    candidates = fetch_candidates()
    print(f"[multilang_translate] 후보 {len(candidates)}건")

    saved = 0
    for art in candidates:
        title_ko, summary_ko = art["title_ko"], art["summary_ko"]
        for lang in art["missing_langs"]:
            title_out, body_out = translate_article(title_ko, summary_ko, _call_gemini, lang=lang)
            if not title_out or not body_out:
                print(f"  ⚠️ [{lang}] 번역 실패 — id={art['id']}")
                continue

            reason = verify_translation(title_ko, summary_ko, title_out, body_out, lang, call_nvidia)
            if reason:
                # 1회 재시도
                title_out, body_out = translate_article(title_ko, summary_ko, _call_gemini, lang=lang)
                reason = verify_translation(title_ko, summary_ko, title_out, body_out, lang, call_nvidia) if title_out else reason
                if reason:
                    print(f"  ⛔ [{lang}] 검증 실패(재시도 후 포기): {reason} — id={art['id']}")
                    continue

            if save_translation(art["id"], lang, title_out, body_out):
                saved += 1
                print(f"  ✓ [{lang}] {LANG_NAMES.get(lang, lang)} 저장 — id={art['id']}")
            time.sleep(1.0)

    print(f"\n✅ multilang_translate 완료 — {saved}건 저장")


if __name__ == "__main__":
    run()
