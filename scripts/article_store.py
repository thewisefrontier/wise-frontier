"""
scripts/article_store.py
--------------------------
articles 테이블에 최종 기사(합성 완료된 NewsFinal 기사)를 저장하는 공용
삽입 로직.

원래 gemini_writer.py/gemini_summarizer.py/econ_writer.py 등 10여 개
스크립트가 각자 거의 동일한 헤더 구성 + requests.post(.../rest/v1/articles)
블록을 복붙해서 갖고 있었다(2026-09-02 감사로 확인 — gemini_summarizer.py
한 파일 안에만 같은 로직이 3벌 있었음). script_leak.py·json_body_guard.py·
gemini_client.py와 같은 이유로 공용화한다: 한쪽만 고치고 나머지가 안 고쳐져
드리프트가 나는 사고가 이미 두 번(call_gemini 패턴, 업데이트 기록 필터)
반복됐다.

각 writer 스크립트는 여전히 자기만의 payload dict를 직접 만든다 — 스크립트마다
필드가 다르므로(econ_writer의 event_id 파생 cluster_key, weather_report의
사전 중복확인 등) 이 부분은 통일하지 않는다. 이 모듈은 완성된 payload를
받아 "삽입"만 담당한다.

⚠️ db.py의 insert_article()과는 목적이 다르다 — db.py는 클러스터링 전 RSS
원문 저장용(rss_fetcher.py 전용, is_published=False가 기본, summary_3lines/
update_log 등 최종 기사 필드가 없음)이고, 이 모듈은 합성이 끝난 최종 기사
저장용이다. 서로 대체하지 않는다.

사용:
    from article_store import insert_final_article
    art_id = insert_final_article(payload)   # 성공: 새 id, 실패: -1

각 스크립트가 이미 자체적으로 SUPABASE_URL/SUPABASE_SERVICE_KEY를 읽고
있다면(주로 GET/PATCH 등 이 모듈이 다루지 않는 다른 용도로도 씀) 그대로
둬도 된다 — 이 모듈은 자기 것을 따로 읽으므로 서로 독립적이다.
"""

import os
import re
import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# 크립토 기사 태깅(2026-09-02) — 사용자 지시: "코인 카테고리를 신설하는게
# 맞지 않을까?"에 "일단 물량 지켜본 뒤 결정하자"고 답한 뒤, "그럼 기사가
# 나오면 옮기기 쉽도록 크립토 태그를 따로 붙여놔"라는 후속 요청. 지금은
# 별도 카테고리를 만들지 않고(nav·category_guard.py·여러 프롬프트를 다
# 고쳐야 하는 구조적 변경이라 물량도 없이 하기엔 이름) "금융" 카테고리
# 그대로 두되, subcategory 끝에 "_crypto"를 붙여 나중에 물량이 쌓이면
# `subcategory like '%_crypto'`로 한 번에 찾아서 새 카테고리로 옮기기 쉽게
# 해둔다. cluster_/trend_/realtrend_ 등 접두사 기반 매칭(find_similar_trend
# 등)은 접미사 추가로 영향받지 않는다.
_CRYPTO_KEYWORDS_RE = re.compile(
    r"비트코인|이더리움|가상자산|가상화폐|암호화폐|스테이블코인|알트코인|"
    r"도지코인|리플코인|바이낸스|업비트|빗썸|코인베이스|크립토(자산|시장|화폐)?|"
    r"\bbitcoin\b|\bethereum\b|\bcrypto(currenc\w*)?\b|\bblockchain\b|"
    r"\bstablecoin\b|\baltcoin\b|\bdefi\b",
    re.I,
)


def _tag_crypto(payload: dict) -> None:
    """payload가 크립토 관련 기사면 subcategory 끝에 _crypto를 붙인다(제자리 수정)."""
    text = f"{payload.get('title_ko') or ''} {payload.get('summary_ko') or ''}"[:2000]
    if not _CRYPTO_KEYWORDS_RE.search(text):
        return
    sub = payload.get("subcategory") or ""
    if sub.endswith("_crypto"):
        return
    payload["subcategory"] = f"{sub}_crypto" if sub else "crypto"


def sb_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def sb_url(table: str = "articles") -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def insert_final_article(payload: dict) -> int:
    """완성된 payload dict를 articles 테이블에 삽입한다.

    반환: 성공 시 새 article id, 실패 시 -1. 같은 url이 이미 있으면
    무시하고 넘어간다(resolution=ignore-duplicates — 대부분의 writer
    스크립트가 url을 유니크 키로 써서 재실행 시 중복 삽입을 막는 용도).
    """
    _tag_crypto(payload)
    headers = {**sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    try:
        res = requests.post(sb_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        print(f"  ⚠️ 기사 저장 실패: {res.status_code} — {res.text[:300]}")
    except Exception as e:
        print(f"  ⚠️ 기사 저장 예외: {e}")
    return -1
