"""
scripts/domestic_kr_writer.py
-------------------------
국내(한국) 뉴스 다중소스 클러스터링 + 뉴스파이널 자체 집필(다국어 채널 전용).

2026-09-15 신설 — 사용자 지적: "지금까지 뉴스파이널에서 구축한 기사 추출,
조합, 분석 시스템을 쓰지 않고 그냥 한국 언론사 기사를 고대로 가져다가
번역해서 올려버렸어." + "한국 언론은 다른 나라와 달리 같은 주제/소재로
비슷한 기사가 엄청 많이 나온다고." — 이전까지는 domestic_kr_fetcher.py가
크롤링한 국내 언론사 원문(full_text)을 그대로 multilang_translate.py가
번역만 했다(source LIKE 'DomesticKR:%' 원문 직접 번역). 이 스크립트가 그
사이에 들어가, gemini_writer.py와 동일한 클러스터링 로직으로 같은 사건을
다룬 여러 매체 기사를 묶어 뉴스파이널이 직접 종합·집필한 뒤에만 번역
후보가 되게 한다 — 단일 소스 기사는 이제 다국어 채널 번역 대상에서
제외된다(다출처 교차확인 없이는 신뢰할 수 없다는 판단).

발행은 절대 하지 않는다(is_published=False, source="DomesticKR-Synth") —
한국어 메인 사이트(export_articles.py, source=eq.NewsFinal 필터)와는
완전히 격리된, 다국어 채널 내부검증 전용 데이터다.

gemini_writer.py의 cluster_articles()/articles_are_related()/
call_gemini_article()/verify_single_topic()은 국제뉴스 전용 가정이 없어
그대로 재사용한다(파일 끝에 `if __name__ == "__main__":` 가드가 있어
import해도 파이프라인이 실행되지 않음 — domestic_kr_fetcher.py 자체
독스트링이 이미 확인한 안전한 재사용 패턴). 단, gemini_writer.py의
ADVANCED_ECONOMIES에 "한국"이 포함돼 있어 run()의 선진국 스킵 로직
(CLUSTER_MIN_SIZE_ADVANCED=4 미만 제외)은 국제뉴스 편집방침 전용이라
재사용하지 않는다 — 이 스크립트는 독자적인 최소 클러스터 크기만 적용한다.

독립 실행 스크립트다(gdelt_fetcher.py/domestic_kr_fetcher.py와 동일 패턴).
"""

import os
import re
import hashlib
import requests
from datetime import timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

from domestic_kr_fetcher import _is_image_source_allowed

import gemini_writer as _gw
from gemini_writer import now_kst, cluster_articles, call_gemini_article, verify_single_topic, title_keywords, load_prompt
from style_guard import ensure_paragraphs
from db import insert_article

# gemini_writer.articles_are_related()를 국내기사용으로 한 단계 더 엄격하게
# 감싸서 모듈에 되돌려 끼운다(이 프로세스 안에서만 적용 — gemini_writer.py
# 파일 자체나 국제뉴스 파이프라인에는 영향 없음, cluster_articles()가 호출
# 시점에 모듈 전역에서 이름을 다시 찾아오기 때문에 가능한 패턴).
#
# 2026-09-15 실측 발견: 원래 함수의 "제목+리드 종합 키워드 4개 이상 겹치면
# 관련"이라는 세 번째 분기가, 한국 연예 사진 기사 특유의 상투어("공개된
# 사진 속 OOO는 ~ 카메라를 바라보고 있다" 류 캡션, "입고 뽐낸" 류 제목)만
# 으로도 쉽게 채워져 서로 무관한 연예인 수십 명이 한 클러스터로 뒤섞이는
# 사고를 실제 72시간 백로그(448건)로 확인했다(최악의 경우 45명 뒤섞임).
# 제목 자체에서 겹치는 키워드가 2개 이상일 때만 관련으로 인정하도록
# 추가 게이트를 걸자 같은 데이터셋에서 정치/경제 클러스터(김승원 청문회
# 7건, AWS AI 도입 리포트 10건 등)는 그대로 살아남고, 뒤섞임 사고는
# 45명→6명으로 대폭 줄었다(완전히 0은 아님 — 잔여 사례는 "미모/각선미
# 뽐낸" 포맷 자체가 제목 키워드조차 상투어라 발생, 후속 튜닝 대상으로
# 남겨둔다).
_orig_articles_are_related = _gw.articles_are_related


def _domestic_articles_are_related(a: dict, b: dict) -> bool:
    if not _orig_articles_are_related(a, b):
        return False
    title_a = (a.get("title_ko") or a.get("title_en") or "").lower()
    title_b = (b.get("title_ko") or b.get("title_en") or "").lower()
    return len(title_keywords(title_a) & title_keywords(title_b)) >= 2


_gw.articles_are_related = _domestic_articles_are_related

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

SOURCE_TAG = "DomesticKR-Synth"

# 클러스터링 후보 조회 창. domestic_kr_fetcher.py가 사이클당 일부만 수집하는
# 구조라(GDELT 커서 순환) 같은 사건의 여러 매체 보도가 여러 사이클에 걸쳐
# 쌓인다 — 하루~사흘 정도는 지나야 클러스터가 찰 수 있어 넉넉히 잡는다.
CANDIDATE_WINDOW_HOURS = 72

# gemini_writer.CLUSTER_MIN_SIZE(=2)와 동일 기준. ADVANCED_ECONOMIES 게이트는
# (한국이 그 목록에 포함돼 있어) 이 파이프라인에는 적용하지 않는다 — 한국
# 언론 특성상 동일 사건에 대한 매체 수가 이미 충분히 많다(사용자 확인).
CLUSTER_MIN_SIZE = 2

MAX_CLUSTERS_PER_RUN = 5


def _sb_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def _sb_url(table="articles"):
    return f"{SUPABASE_URL}/rest/v1/{table}"


def get_domestic_candidates(hours: int = CANDIDATE_WINDOW_HOURS, limit: int = 300) -> list:
    # 2026-09-24: domestic_kr_fetcher.py의 원자재가 articles에서 raw_candidates로
    # 옮겨갔다(하루 6만 건 넘는 원자재가 발행 기사용 무거운 스키마를 쓰던 문제 —
    # articles.source like 'DomesticKR:*'는 이제 여기 없다). 이 함수가 만드는
    # "DomesticKR-Synth" 결과물은 완성품이라 여전히 articles에 그대로 쓴다.
    since = (now_kst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    res = requests.get(
        _sb_url("raw_candidates"),
        headers=_sb_headers(),
        params={
            "select": "id,title_ko,summary_ko,full_text,category,country,region,url,image_url,image_credit,created_at",
            "source": "like.DomesticKR:*",
            "created_at": f"gte.{since}",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=30,
    )
    if res.status_code not in (200, 206):
        print(f"[domestic_kr_writer] 후보 조회 실패: {res.status_code} — {res.text[:200]}")
        return []
    return res.json() or []


def make_domestic_cluster_key(cluster: list) -> str:
    rep = (cluster[0].get("title_ko") or "")[:50]
    today = now_kst().strftime("%Y%m%d")
    h = hashlib.md5(f"{today}:{rep}".encode()).hexdigest()[:8]
    return f"domestic_{today}_{h}"


def get_existing_domestic_article(cluster_key: str) -> dict | None:
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id",
            "source": f"eq.{SOURCE_TAG}",
            "subcategory": f"eq.{cluster_key}",
            "limit": "1",
        },
        timeout=15,
    )
    if res.status_code in (200, 206):
        data = res.json()
        return data[0] if data else None
    return None


# 실제 편집 규칙(프롬프트) 본문은 코드에 두지 않는다(2026-09-15 사용자
# 지시: "프롬프트, 기사 걸러내는 게이트 같은 것들은 매우 중요한 정보라서,
# 깃허브에 두지 말고 DB에 올려서 유출되지 않도록 해둘 것" — "그게 우리
# 노하우고 핵심이기 때문에 다른데서 코드를 가져가도 쓸 수 없도록").
# writer_rules(메인 파이프라인)와 동일하게 Supabase `prompts` 테이블에서
# 로드한다 — 여기 있는 폴백은 DB 접근 실패 시에만 쓰이는 최소 안전망이며
# 실제 튜닝된 편집 규칙이 아니다.
_DOMESTIC_RULES_FALLBACK = """여러 한국 언론사의 원문 기사를 사실관계를 종합해 하나의 독립된 뉴스
기사로 새로 작성하세요. 원문에 없는 사실은 지어내지 말고, 모든 문장을
'-다'로 종결하세요.

TITLE: <제목>
BODY: <본문>

[원문 기사 목록]
{article_list}

Output:"""


def build_domestic_prompt(cluster: list) -> str:
    parts = []
    for i, a in enumerate(cluster, 1):
        title = a.get("title_ko") or ""
        body = (a.get("full_text") or a.get("summary_ko") or "")[:1500]
        parts.append(f"[{i}] {title}\n{body}")
    rules = load_prompt("domestic_writer_rules", fallback=_DOMESTIC_RULES_FALLBACK)
    return rules.format(article_list="\n\n".join(parts))


def _parse_domestic_output(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    m_body = re.search(r"BODY:\s*(.+)$", text, re.S)
    title = m_title.group(1).strip() if m_title else ""
    body = m_body.group(1).strip() if m_body else ""
    return title, body


_WATERMARK_CHECK_PROMPT = (
    "Look at this image. Does it have a visible text watermark, logo, or brand "
    "mark overlaid on the photo itself (e.g. a news outlet name, wire service "
    "name, or photo agency mark printed in a corner or across the image)? "
    "Answer with exactly one word: YES or NO."
)


def _image_has_watermark(image_url: str) -> bool:
    """2026-09-16 사용자 지적: "텐아시아 로고가 사진에 박혀 잇네. 저것도
    쓰면 안돼" — 사진 픽셀 자체에 워터마크가 찍혀 있으면(본문 텍스트에
    캡션으로 안 남는 경우가 많아 기존 정규식 검사로는 못 잡음) 텍스트
    검사와 별개로 Gemini 비전 호출로 직접 확인한다. 클러스터 대표 이미지
    선정 시(실행당 최대 MAX_CLUSTERS_PER_RUN=5건)만 호출해 비용을 낮춘다.
    호출 실패 시에는 판단 불가로 통과시키지 않고 안전하게 "워터마크 있음"
    으로 간주해 스킵한다(사진 없는 것보다 저작권 위험이 더 크므로 fail-closed).
    """
    try:
        res = requests.get(image_url, timeout=10)
        if res.status_code != 200 or not res.content:
            return True
        mime = res.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        answer = _gw._gemini_client.call(
            _WATERMARK_CHECK_PROMPT, max_tokens=10, temperature=0,
            image_bytes=res.content, image_mime=mime,
        )
        if not answer:
            return True
        return answer.strip().upper().startswith("YES")
    except Exception as e:
        print(f"  ⚠️ 워터마크 검사 실패({e}) — 안전하게 사용 안 함")
        return True


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    candidates = get_domestic_candidates()
    print(f"[domestic_kr_writer] 후보 {len(candidates)}건 (최근 {CANDIDATE_WINDOW_HOURS}시간)")
    if not candidates:
        return

    for a in candidates:
        if not a.get("category"):
            a["category"] = "사회"

    clusters = cluster_articles(candidates)
    print(f"  → {len(clusters)}개 클러스터 발견")

    processed = 0
    written = 0
    for cluster in clusters:
        if processed >= MAX_CLUSTERS_PER_RUN:
            print(f"[STOP] 이번 실행 최대 처리 수 도달 ({MAX_CLUSTERS_PER_RUN}개) — 다음 실행에 계속")
            break

        if any(a.get("__needs_review__") for a in cluster):
            real_members = [a for a in cluster if not a.get("__needs_review__")]
            print(f"  [SKIP] 검토필요 클러스터(다주제 혼합 의심, {len(real_members)}건) — "
                  f"{real_members[0].get('title_ko','')[:40] if real_members else ''}")
            continue

        if len(cluster) < CLUSTER_MIN_SIZE:
            continue

        cluster_key = make_domestic_cluster_key(cluster)
        if get_existing_domestic_article(cluster_key):
            continue  # 이미 이 클러스터로 집필됨 — v1은 업데이트 없이 스킵

        processed += 1
        titles = [x.get("title_ko", "")[:50] for x in cluster]
        print(f"[클러스터 {processed}] {len(cluster)}건:")
        for t in titles:
            print(f"  - {t}")

        prompt = build_domestic_prompt(cluster)
        content = call_gemini_article(prompt, max_tokens=2500)
        if not content:
            print("  ❌ 생성 실패\n")
            continue

        title, body = _parse_domestic_output(content)
        if not title or not body:
            print("  ❌ 파싱 실패\n")
            continue

        body = ensure_paragraphs(body)

        if not verify_single_topic(title, body):
            print("  ⚠️ 단일주제 검증 실패 — 미발행 스킵\n")
            continue

        # 사진 출처 가드를 여기서도 다시 확인한다(도메인 차단은
        # domestic_kr_fetcher.py 수집 시점에도 걸리지만, 그 가드가 생기기
        # 전에 이미 저장된 과거 이미지도 있을 수 있어 선택 시점에 한 번 더
        # 걸러 이중 방어한다).
        image_url = None
        image_credit = None
        for x in cluster:
            if not x.get("image_url"):
                continue
            xdomain = urlparse(x.get("url") or "").netloc
            if not _is_image_source_allowed(xdomain, x.get("full_text") or ""):
                continue
            # 텍스트 캡션에 안 걸려도 사진 픽셀 자체에 매체 로고/워터마크가
            # 찍혀 있는 경우가 있다(2026-09-16 사용자 지적: "텐아시아
            # 로고가 사진에 박혀 잇네") — 최종 후보에 대해서만 비전 검사.
            if _image_has_watermark(x["image_url"]):
                print(f"  ⚠️ 워터마크 감지로 이미지 제외: {xdomain}")
                continue
            image_url = x["image_url"]
            # 사용자 지적: "적어도 출처를 내가 알 수 있어야 할 것 같은데" →
            # "캡션에서 실제 크레딧 문구를 뽑아 표시하도록" — domestic_kr_
            # fetcher.py가 수집 시점에 캡션+크레딧을 이미 image_credit에
            # 저장해뒀으면 그걸 그대로 쓰고(도메인만 아는 것보다 정확함),
            # 옛날 방식으로 도메인만 들어있는 값이면 그대로 폴백한다.
            image_credit = x.get("image_credit") or xdomain
            break
        category = cluster[0].get("category") or "사회"

        article_id = insert_article(
            title_en="", title_ko=title,
            summary_en="", summary_ko=body,
            url=f"internal://{cluster_key}",
            source=SOURCE_TAG,
            category=category, subcategory=cluster_key,
            region="korea", country="한국", country_flag="🇰🇷",
            score=len(cluster),
            full_text=body,
            countries=["한국"],
            is_published=False,
            image_url=image_url,
            image_credit=image_credit,
        )
        if article_id > 0:
            written += 1
            print(f"  ✅ 저장 (id={article_id}): {title}\n")
        else:
            print("  ❌ 저장 실패\n")

    print(f"\n✅ domestic_kr_writer 완료 — {written}건 신규 집필 (클러스터 {len(clusters)}개 중 {processed}개 처리)")


if __name__ == "__main__":
    run()
