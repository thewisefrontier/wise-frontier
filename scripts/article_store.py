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
import time
import requests

# 2026-09-22: 이 모듈을 공유하는 writer들이 전부 같은 위험에 노출돼 있어
# (gemini_writer.py의 같은 유형 실사고 참고 — http_retry.py) 재시도 세션으로 교체.
from http_retry import get_session
requests = get_session()

# 2026-09-24: Aiven 스탠바이 이중 쓰기 — 발행 기사(NewsFinal 최종 기사)는 전부
# 이 함수를 거치는데, 정작 Aiven 미러 코드는 db.py의 insert_article()에만
# 붙어 있었다. 그런데 db.insert_article()을 실제로 is_published=True로 부르는
# 곳은 코드 전체에 단 한 곳도 없었다(확인함) — 발행 기사는 전부 이 함수를
# 쓴다. 즉 병합된 지 하루가 지나도록 Aiven엔 발행 기사가 단 한 건도
# 미러링되지 않고 있었다. db.py의 락+timeout이 걸린 미러 함수를 그대로
# 재사용한다(중복 구현 대신 — 이 모듈이 "환경변수는 독립적으로 읽는다"는
# 원칙은 그대로 두되, Aiven 미러는 프로세스 전체에 하나만 있어야 하는
# 공유 자원이라 예외).
from db import _mirror, _AIVEN_ARTICLES_COLUMNS, _AIVEN_JSON_COLUMNS

# db.insert_article()의 미러 INSERT와 동일한 원칙(같은 id로 넣어야 자식 테이블
# 미러링 시 id가 어긋나지 않음)이지만, 여긴 payload가 writer마다 달라 컬럼이
# 고정이 아니다 — PostgREST가 돌려준 실제 저장 행(모든 컬럼에 기본값까지
# 적용된 것)을 그대로 쓰되, Aiven 스키마(migrations/aiven_init.sql)에 실제
# 존재하는 컬럼만 화이트리스트로 걸러 삽입한다(모르는 컬럼을 그대로 SQL
# 식별자로 쓰면 인젝션 위험 — payload는 내부 생성값이라 위험은 낮지만
# 방어적으로 간다). 화이트리스트는 db.py와 공유(2026-09-24, mirror_article_update
# 추가하며 중복 정의로 드리프트 날 뻔한 걸 발견해 여기로 통일).


def _mirror_final_article(row: dict) -> None:
    from psycopg2.extras import Json
    cols = [c for c in row if c in _AIVEN_ARTICLES_COLUMNS]
    if "id" not in cols:
        return
    values = tuple(Json(row[c]) if c in _AIVEN_JSON_COLUMNS and row[c] is not None else row[c] for c in cols)
    placeholders = ",".join(["%s"] * len(cols))
    _mirror(
        f"INSERT INTO articles ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
        values,
    )

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


def _supersede_older_trends(payload: dict, new_id: int) -> None:
    """같은 트렌드 주제(subcategory)의 이전 기사는 색인 제외(noindex) — 주제당
    최신 1건만 검색에 노출한다(2026-09-22: 수단 분쟁 94건·DRC 89건처럼 같은
    주제가 수십 건 쌓여 애드센스 "가치가 별로 없는 콘텐츠"로 볼 위험).
    세 트렌드 writer(trend/realtrend/extrend)가 전부 이 함수를 거치므로 여기 한 곳에서 처리."""
    sub = payload.get("subcategory") or ""
    if payload.get("source") != "NewsFinal" or not re.match(r"^(trend|realtrend|extrend)_", sub):
        return
    try:
        requests.patch(
            sb_url(),
            headers={**sb_headers(), "Prefer": "return=minimal"},
            params={"source": "eq.NewsFinal", "subcategory": f"eq.{sub}", "id": f"lt.{new_id}", "noindex": "eq.false"},
            json={"noindex": True},
            timeout=15,
        )
    except Exception as e:
        print(f"  ⚠️ 트렌드 구버전 noindex 처리 실패(무시): {e}")


# ── 트렌드 기사 분량 게이트 ─────────────────────────────────
# 2026-09-22 사용자 지적(id=214590 "뭐야 이건, 기사라고 볼 수도 없는데"):
# gemini_writer.py는 분량 미달 기사를 미발행 처리하는 게이트를 갖고 있는데
# (MIN_BODY_LEN_HARD_FLOOR=700, id=145092 사고 후 추가), gemini_summarizer.py의
# 트렌드 writer 3종(trend/realtrend/extrend)에는 그게 이식되지 않아 실질 내용이
# 한 문장뿐인 기사도 그대로 발행되고 있었다(extrend는 9/15 이후 발행분 7건이
# 전부 700자 미만, 평균 352자).
#
# ⚠️ 하한을 700이 아니라 400으로 잡은 근거(2026-09-22 실측). 450~700자 구간의
# realtrend 기사들은 "26kg 금괴·8200만리라·부동산 11채"처럼 구체적 사실이 담긴
# 멀쩡한 기사였다(멀쩡 표본 최소 459자). 반면 빈약한 기사는 197·229·302·312·396자로
# 전부 400 아래에 몰려 있었다. 700을 그대로 쓰면 멀쩡한 기사를 절반이나 죽인다.
# ⚠️ 처음엔 "문장 간 재진술 유사도"와 "빈말 상투구 빈도"로 정보 밀도를 재려 했으나,
# 실제 데이터에서 좋은 기사와 나쁜 기사가 전혀 갈리지 않아(유사도 양쪽 다 33~40,
# "전해졌다" 같은 정상 표현이 상투구로 오탐) 기각했다 — 길이 말고 싸게 쓸 만한
# 판별 지표는 아직 못 찾았다. 밀도 판정이 정말 필요해지면 LLM 판정이 필요하다.
TREND_MIN_BODY_LEN = 400
_TREND_SUB_RE = re.compile(r"^(trend|realtrend|extrend)_")

# 길이만으로는 "400자는 넘겼지만 알맹이는 없는" 기사를 못 잡는다. 이 구간만
# 계열이 다른 모델(NVIDIA nemotron)에 2차 판정을 맡긴다 — Gemini가 쓴 걸 Gemini가
# 검증하면 맹점이 반복된다는 기존 원칙 그대로(nvidia_client.py 참조).
#
# 판정 대상을 400~700자로 좁힌 근거(2026-09-22 실측 13건): THIN으로 잡힌 건 딱
# 하나였고 그게 440자였다. 625·632·676·771·804자 기사는 전부 구체적 사실이 8~10개
# 들어간 정상 기사였다. 긴 기사까지 전부 판정을 돌리면 호출만 두 배가 된다.
# ⚠️ 실측 18콜 중 2콜이 503(Service temporarily overloaded)이었다. 외부 API가
# 흔들린다고 발행이 막히면 안 되므로 판정 실패 시에는 무조건 통과시킨다(fail-open).
TREND_DENSITY_CHECK_MAX_LEN = 700


def is_thin_trend_body(body: str) -> bool:
    """트렌드 기사 본문이 발행 하한 미달인지(길이 기준만)."""
    return len(body or "") < TREND_MIN_BODY_LEN


_DENSITY_PROMPT = """다음은 자동 생성된 뉴스 기사 본문이다. 이 글이 독자에게 실제 정보를 전달하는 기사인지, 아니면 사실 한두 개를 빈말과 재진술로 부풀린 껍데기인지 판정하라.

판정 기준:
- 구체적 사실(누가/무엇을/언제/어디서/수치/고유명사)이 몇 가지나 들어 있는가
- "이목이 집중됐다", "귀추가 주목된다"처럼 내용이 없는 문장의 비중
- 앞 문장을 말만 바꿔 되풀이하는 문장이 있는가
- 길이가 짧아도 구체적 사실이 여러 개면 정상 기사다.

다른 말 없이 아래 한 줄 형식으로만 답하라.
VERDICT: OK 또는 THIN | 사실수: N | 이유: (20자 이내)

본문:
{body}"""


def judge_thin_by_llm(body: str) -> tuple:
    """NVIDIA 교차 판정. 반환 (thin_여부, 사유문자열).

    판정 불가(키 없음·503·형식 이탈)면 (False, "")로 통과시킨다 — 외부 API
    상태가 발행을 막으면 안 된다."""
    try:
        from nvidia_client import call_nvidia   # 페이싱·429/503 재시도는 클라이언트가 담당
    except Exception:
        return False, ""
    out = call_nvidia(_DENSITY_PROMPT.format(body=body), max_tokens=80, temperature=0.0)
    if not out:
        return False, ""
    head = out.strip().splitlines()[0][:120]
    if re.search(r"VERDICT\s*:\s*THIN", head, re.I):
        return True, head
    return False, head   # OK거나 형식을 벗어났으면 통과(fail-open)


def _gate_thin_trend(payload: dict) -> None:
    """분량 하한 미달 트렌드 기사는 발행하지 않고 어드민 검토로 돌린다.
    버리지는 않는다 — is_published=False로 저장돼 데스킹 툴(docs/admin.html)에 뜬다.
    세 트렌드 writer가 전부 insert_final_article()을 거치므로 여기 한 곳에서 처리한다
    (_supersede_older_trends와 같은 이유)."""
    if payload.get("source") != "NewsFinal":
        return
    if not _TREND_SUB_RE.match(payload.get("subcategory") or ""):
        return
    if not payload.get("is_published"):
        return  # 이미 다른 사유로 미발행(다주제 혼입·날짜 환각 등)이면 그 사유를 유지
    body = payload.get("summary_ko") or ""
    body_len = len(body)
    if body_len >= TREND_MIN_BODY_LEN:
        # 길이는 통과했지만 알맹이가 없을 수 있는 구간만 교차 판정한다.
        if body_len >= TREND_DENSITY_CHECK_MAX_LEN:
            return
        thin, why = judge_thin_by_llm(body)
        if not thin:
            return
        payload["is_published"] = False
        note = f"밀도 부족 미발행(NVIDIA 교차판정) — {why}"
        log = payload.get("update_log")
        if isinstance(log, list) and log and isinstance(log[0], dict):
            log[0]["note"] = note
        print(f"  ⚠️ [밀도 부족 {body_len}자] → 미발행 저장(어드민 검토 대기): {why}")
        return
    payload["is_published"] = False
    note = (f"분량 부족 미발행 — 트렌드 신호가 빈약해 실질 내용 없음"
            f"({body_len}자, 하한 {TREND_MIN_BODY_LEN}자)")
    log = payload.get("update_log")
    if isinstance(log, list) and log and isinstance(log[0], dict):
        log[0]["note"] = note   # 미발행 사유는 update_log[0].note에 적는다(save_article과 동일 관례)
    print(f"  ⚠️ [분량 부족 {body_len}자] → 미발행 저장(어드민 검토 대기)")


def insert_final_article(payload: dict) -> int:
    """완성된 payload dict를 articles 테이블에 삽입한다.

    반환: 성공 시 새 article id, 실패 시 -1. 같은 url이 이미 있으면
    무시하고 넘어간다(resolution=ignore-duplicates — 대부분의 writer
    스크립트가 url을 유니크 키로 써서 재실행 시 중복 삽입을 막는 용도).
    """
    _tag_crypto(payload)
    _gate_thin_trend(payload)
    headers = {**sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    try:
        res = requests.post(sb_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            new_id = data[0].get("id", -1) if data else -1
            if new_id > 0:
                _supersede_older_trends(payload, new_id)
                if data[0].get("is_published"):
                    _mirror_final_article(data[0])
            return new_id
        print(f"  ⚠️ 기사 저장 실패: {res.status_code} — {res.text[:300]}")
    except Exception as e:
        print(f"  ⚠️ 기사 저장 예외: {e}")
    return -1
