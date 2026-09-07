"""
gemini_writer.py
----------------
1. 오늘 기사를 키워드+유사도 기반으로 클러스터링
2. 클러스터별 이슈 기사 생성 (신규) 또는 업데이트 (추가 기사 있을 때)
3. 브리핑은 제거 — 클러스터 이슈 기사에 집중

실행: python scripts/gemini_writer.py
"""

import os
import re
import math
import time
import json
import hashlib
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from rapidfuzz import fuzz

try:
    from date_guard import check_date_hallucination
except Exception:
    def check_date_hallucination(body, sources, base_date=None):
        return False, ""   # 폴백 — 판정 없이 통과

# 카테고리 정규화 공통 모듈. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from category_guard import normalize_category
except Exception:
    def normalize_category(raw, default="글로벌"):
        return "" if raw is None else str(raw).strip()

# 저장 시점 문자셋 혼입 하드 블록. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from script_leak import detect_script_leak
except Exception:
    def detect_script_leak(title, body):
        return []

# 저장 시점 raw JSON 본문 차단. import 실패해도 본 기능이 죽지 않도록 폴백을 둔다.
try:
    from json_body_guard import unwrap_json_body as _unwrap_json_body
except Exception:
    def _unwrap_json_body(text, _depth=0):
        return None

# articles 테이블 삽입 공용 로직(2026-09-02, 10여개 스크립트에 복붙돼 있던
# 헤더구성+POST 블록을 article_store.py로 공용화). import 실패해도 죽지
# 않도록 이 파일 자체의 _sb_headers()/_sb_url()로 폴백한다.
try:
    from article_store import insert_final_article
except Exception:
    def insert_final_article(payload: dict) -> int:
        headers = {**_sb_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        res = requests.post(_sb_url(), headers=headers, json=payload, timeout=15)
        if res.status_code in (200, 201):
            data = res.json()
            return data[0].get("id", -1) if data else -1
        return -1

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

load_dotenv()

# RPD 낮은 고품질 모델부터 순서대로 소진시키고, RPD 500인 lite 모델을 마지막 안전망으로 둔다
GEMINI_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
CALL_INTERVAL      = 10
MAX_CLUSTERS_PER_RUN = 7  # 한 번 실행당 최대 처리 클러스터 수

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
NEWSFINAL_CHANNEL = "@newsfinal"  # NewsFinal 자체기사 전용 채널

# Supabase 헤더/URL 헬퍼는 article_store.py로 공용화(2026-09-02).
try:
    from article_store import sb_headers as _sb_headers, sb_url as _sb_url
except Exception:
    def _sb_headers():
        return {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    def _sb_url():
        return f"{SUPABASE_URL}/rest/v1/articles"


def send_to_newsfinal_channel(article_id, title, body, is_update=False):
    """NewsFinal 자체기사를 텔레그램 @newsfinal 채널에 발송"""
    if not TELEGRAM_TOKEN:
        return False
    try:
        preview = (body or "").strip().replace("\n\n", "\n")[:300]
        url = f"https://newsfinal.co.kr/article?id={article_id}"
        label = "🔄 업데이트" if is_update else "📋 NewsFinal"
        msg = f"{label}\n\n*{title}*\n\n{preview}{'…' if len(body or '') > 300 else ''}\n\n[전체 기사 보기]({url})"
        res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": NEWSFINAL_CHANNEL,
                "text": msg,
                "parse_mode": "Markdown",
                # 링크 미리보기 이미지가 과도하게 크게 뜨던 문제(2026-08-18 사용자 신고)
                # → prefer_small_media로 축소. ⚠️ Bot API 문서: "prefer_small_media는
                # url이 명시적으로 지정 안 되면 무시된다" — 1차 수정 때 이걸 놓쳐서
                # 효과가 없었다(2026-08-18 재신고로 발견). url을 반드시 같이 넣을 것.
                "link_preview_options": json.dumps({"prefer_small_media": True, "url": url}),
            },
            timeout=15
        )
        data = res.json()
        if not data.get("ok"):
            print(f"  ⚠️ 텔레그램 발송 실패: {data}")
        return data.get("ok", False)
    except Exception as e:
        print(f"  ⚠️ 텔레그램 발송 예외: {e}")
        return False

# API 키 폴백 체인
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

# 영어 번역(2026-09-03) — "다른 기사도 [번역]해야지" 요청. frontier_markets_
# writer.py 등 3개 국제성 전용 스크립트에서 시작해, 사용자 확인 후 이 파일의
# 메인 클러스터링 파이프라인까지 확대(국내 전용 콘텐츠는 제외 — 이 파일은
# RSS로 수집된 해외 뉴스를 합성하는 경로라 전량 해당 없음). Gemini 호출
# 비용을 고려해(사용자 지적) 항상 lite 계열(start_tier=3)로 고정해서 부른다
# — 플래그십 모델 쿼터를 번역이 잠식하지 않도록.
try:
    from translate_guard import translate_article
except Exception:
    def translate_article(title_ko: str, body_ko: str, call_gemini_fn, max_tokens: int = 3500) -> tuple[str, str]:
        return "", ""


def _call_gemini_for_translation(prompt: str, max_tokens: int = 3500):
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=3, temperature=0.3, timeout=(10, 45))

# 프롬프트 캐시
_prompt_cache = {}

def load_prompt(name: str, fallback: str = "") -> str:
    """Supabase에서 활성 프롬프트 로드 (캐시 사용)"""
    global _prompt_cache
    if name in _prompt_cache:
        return _prompt_cache[name]
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/prompts",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            },
            params={"name": f"eq.{name}", "is_active": "eq.true", "order": "version.desc", "limit": "1"},
            timeout=10
        )
        if res.status_code in (200, 206):
            data = res.json()
            if data:
                _prompt_cache[name] = data[0]["content"]
                return _prompt_cache[name]
    except Exception as e:
        print(f"[WARN] 프롬프트 로드 실패 ({name}): {e}")
    _prompt_cache[name] = fallback
    return fallback

# 클러스터링 설정
SIMILARITY_HIGH         = 65
SIMILARITY_SAME_COUNTRY = 40
CLUSTER_MIN_SIZE        = 2

# 2026-08-26: 선진국(GLOBAL_COUNTRIES 중 프론티어마켓 편집 방향과 무관한 11개국)은
# 이미 국내외 종합매체가 넘치게 다루므로, 이 사이트는 여러 소스에서 중복 보도된
# "중요도 높은" 사건만 기사화한다(사용자 결정) — 구글뉴스 톱뉴스 피드 추가로
# 유입량 자체는 늘었지만 전부 기사화하면 프론티어마켓 포지셔닝이 흐려진다.
ADVANCED_ECONOMIES      = {"미국", "일본", "독일", "영국", "이탈리아", "스페인", "네덜란드", "캐나다", "포르투갈", "뉴질랜드", "한국"}
CLUSTER_MIN_SIZE_ADVANCED = 4

# 정부기관·국제기구·대기업 뉴스룸 등 1차 공식 소스(2026-09-04, 사용자 요청 —
# "정부기관, 대기업 해외지사도 소스로 넣으면... 그 자체로도 보도가 가능하고").
# 일반 언론 보도는 선진국 이슈일 때 교차검증(4건 이상)을 요구하지만, 이런
# 공식 소스는 발표 자체가 원천 사실이라 다른 매체가 받아쓸 때까지 기다릴
# 필요가 없다 — ADVANCED_ECONOMIES 교차검증 요건과 단독기사화의 선진국
# 제외 요건을 둘 다 우회시킨다. rss_sources 테이블의 name 컬럼과 정확히
# 일치해야 한다(articles.source에 그대로 들어옴).
OFFICIAL_SOURCE_NAMES = {
    "Federal Reserve", "Federal Reserve 통화정책", "US SEC", "ECB 유럽중앙은행",
    "Bank of England", "Pentagon DoD", "UN News", "UN Security Council",
    "EU Commission", "UK Gov News", "ASEAN", "WHO",
    "Samsung Newsroom", "SK Hynix Newsroom", "Amazon News", "Google Blog",
    "Meta Newsroom",
}


def _has_official_source(cluster) -> bool:
    return any((a.get("source") or "") in OFFICIAL_SOURCE_NAMES for a in cluster)

STOPWORDS = {
    "the","a","an","in","on","at","to","of","for","and","or","is","are",
    "was","were","has","have","been","will","with","by","from","this","that",
    "as","its","it","be","not","but","also","over","after","amid","says",
    "say","said","new","amid","following","기자","특파원","뉴스","오늘","이번",
}


# ── DB 헬퍼 (Supabase REST API) ───────────────────────────

def get_today_articles(limit=300):
    since = (now_kst() - timedelta(hours=96)).strftime("%Y-%m-%d %H:%M")
    articles = []
    offset = 0
    batch = 500
    while len(articles) < limit:
        res = requests.get(
            _sb_url(),
            headers={**_sb_headers(), "Range": f"{offset}-{offset+batch-1}"},
            params={
                "select": "id,title_ko,title_en,summary_ko,summary_en,source,category,subcategory,country,region,url,created_at,score,full_text,source_published_at",
                "sent_telegram": "eq.1",
                "source": "neq.NewsFinal",
                "created_at": f"gte.{since}",
                "order": "score.desc,created_at.desc",
            },
            timeout=30
        )
        if res.status_code not in (200, 206):
            break
        data = res.json()
        if not data:
            break
        articles.extend(data)
        if len(data) < batch:
            break
        offset += batch

    # 파킹된 토픽 기사도 클러스터링 소스로 포함
    try:
        parked_res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,title_en,summary_ko,summary_en,source,category,subcategory,country,region,url,created_at,score,full_text,source_published_at",
                "subcategory": "eq.parked_topic",
                "created_at": f"gte.{since}",
                "order": "created_at.desc",
                "limit": "200",
            },
            timeout=15
        )
        if parked_res.status_code in (200, 206):
            parked = parked_res.json()
            existing_ids = {a["id"] for a in articles}
            articles.extend(a for a in parked if a["id"] not in existing_ids)
    except Exception:
        pass

    return articles[:limit]


def get_existing_cluster(cluster_key):
    today = now_kst().strftime("%Y-%m-%d")
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "id,title_ko,summary_ko,subcategory,score",
            "source": "eq.NewsFinal",
            "subcategory": f"eq.{cluster_key}",
            "created_at": f"like.{today}%",
            "order": "created_at.desc",
            "limit": "1",
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        data = res.json()
        return data[0] if data else None
    return None


def get_article_by_id(article_id):
    """id로 기사 전체 정보(본문 포함) 조회 — 유사 기사 병합 시 기존 본문을 가져오기 위함"""
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,subcategory,score,country,category",
                "id": f"eq.{article_id}",
                "limit": "1",
            },
            timeout=15
        )
        if res.status_code in (200, 206):
            data = res.json()
            return data[0] if data else None
    except Exception as e:
        print(f"  ⚠️ get_article_by_id 실패: {e}")
    return None


def get_cluster_article_count(cluster_key):
    res = requests.get(
        _sb_url(),
        headers=_sb_headers(),
        params={
            "select": "score",
            "source": "eq.NewsFinal",
            "subcategory": f"eq.{cluster_key}",
            "order": "created_at.desc",
            "limit": "1",
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        data = res.json()
        return data[0].get("score", 0) if data else 0
    return 0


def get_today_own_articles():
    """하위 호환용 — 이제 find_similar_article이 RPC를 직접 호출하므로 빈 리스트 반환."""
    return []


def _strip_numbers(text: str) -> str:
    """제목에서 숫자(한글 수사 포함)를 제거 — 사망자 수 등 수치 변화로 인한 미탐지 방지"""
    text = re.sub(r'\d+', '', text)
    # 한글 수사 제거
    text = re.sub(r'[일이삼사오육칠팔구십백천만억]+\s*명', '명', text)
    return re.sub(r'\s+', ' ', text).strip()


def _fuzzy_keyword_overlap(kws_a: set, kws_b: set) -> int:
    """두 키워드 집합의 "공통 개수"를 정확 일치 + 부분 문자열 포함까지
    합쳐서 센다. "카르그"/"카르그섬"처럼 한쪽이 다른 쪽에 포함되는 경우도
    실질적으로 같은 개념을 가리키므로 공통으로 인정한다. 매칭된 쪽은
    중복 카운트되지 않게 소진시킨다."""
    exact = kws_a & kws_b
    count = len(exact)
    remaining_a = kws_a - exact
    remaining_b = list(kws_b - exact)
    used_b = set()
    for wa in remaining_a:
        for wb in remaining_b:
            if wb in used_b:
                continue
            if wa in wb or wb in wa:
                count += 1
                used_b.add(wb)
                break
    return count


def _same_headline_event_llm(title_a: str, body_a: str, title_b: str, body_b: str) -> bool:
    """토큰/키워드 기반 지표가 근소하게 갈릴 때만 쓰는 최종 판정 — 두 기사가
    같은 사건을 다루는지 LLM에게 직접 묻는다(gemini_summarizer.py의
    _same_event_llm과 같은 철학, gemini_writer.py 쪽엔 이 안전망이 없어서
    2026-09-06 카르그 유조선 중복(id=135075/137073)을 놓쳤다 — 두 제목이
    표현·구조가 달라 rapidfuzz 유사도가 낮게 나왔지만 실제로는 같은 사건)."""
    prompt = f"""아래 두 뉴스 기사가 완전히 같은 사건(같은 시점의 같은 구체적
사건)을 다루고 있습니까? 같은 큰 이슈의 다른 시점/다른 세부사건이면
"다름"입니다.

[기사 A 제목] {title_a}
[기사 A 본문 앞부분] {(body_a or '')[:300]}

[기사 B 제목] {title_b}
[기사 B 본문 앞부분] {(body_b or '')[:300]}

"같음" 또는 "다름"으로만 답하세요."""
    result = call_gemini(prompt, max_tokens=10, start_tier=3)
    return bool(result) and result.strip().startswith("같음")


def find_similar_article(title: str, own_articles: list, threshold: int = 70, body: str | None = None):
    """
    중복 기사 탐색 — 3단계:
    1차: DB RPC(find_duplicate_title) — pg_trgm 유사도 기반
    2차: 숫자 제거 후 같은 국가·날짜 기사와 키워드 재비교
         (사망자 수 등 수치가 바뀐 후속 보도 감지용)
    3차: 2차 기준을 근소하게 못 채운 최선의 후보를 LLM으로 최종 판정
         (body를 넘겨준 호출부에서만 동작 — 2026-09-06 실사고 대응)
    """
    if not title:
        return None, 0

    # ── 1차: RPC ──
    try:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/find_duplicate_title",
            headers=_sb_headers(),
            json={"p_title": title, "p_hours": 72, "p_threshold": 0.4},
            timeout=10,
        )
        if res.status_code in (200, 201) and res.json():
            top = res.json()[0]
            score = int(top["score"] * 100)
            if score >= threshold:
                return top, score
    except Exception as e:
        print(f"  ⚠️ [중복체크 경고] RPC 호출 실패: {e}")

    # ── 2차: 숫자 제거 + 국가+날짜+키워드 재비교 ──
    # 실사고(2026-08-13, id=67076/72477): 같은 콜롬비아 지진을 다룬 두 기사가
    # 별개 루트로 갈라졌다. 1차 RPC는 72시간 창인데 2차 폴백만 48시간이라,
    # 67076(8/11 00:59 생성)이 72477 생성 시점(8/13 04:47, 간격 51.8시간)엔
    # 이미 48시간 창을 벗어나 후보에서 빠졌다. 1차와 동일하게 72시간으로 맞춘다.
    try:
        title_stripped = _strip_numbers(title)
        today = now_kst().strftime("%Y-%m-%d")
        since_72h = (now_kst() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M")

        # 오늘 발행된 자체 기사 조회 (country 필터 없이 — 제목에서 국가명 추출 후 비교)
        res2 = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            params={
                "select": "id,title_ko,country,summary_ko",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "created_at": f"gte.{since_72h}",
                "order": "created_at.desc",
                "limit": "100",
            },
            timeout=10,
        )
        if res2.status_code not in (200, 206):
            return None, 0

        candidates = res2.json()
        title_kws = set(w for w in re.sub(r'[^\w가-힣]', ' ', title_stripped).split() if len(w) >= 2)

        best_near_miss, best_near_score = None, 0
        for cand in candidates:
            cand_title = cand.get("title_ko") or ""
            cand_stripped = _strip_numbers(cand_title)
            cand_kws = set(w for w in re.sub(r'[^\w가-힣]', ' ', cand_stripped).split() if len(w) >= 2)

            # 공통 키워드 4개 이상 + rapidfuzz 유사도 50 이상이면 중복. 정확히
            # 같은 토큰뿐 아니라 부분 문자열 포함 관계도 공통으로 친다
            # (2026-09-06 실사고, id=135075 vs 137073 "카르그 유조선 피격"
            # 중복 — "카르그 섬"(공백 있음)과 "카르그섬"(붙여씀)처럼 복합
            # 지명 표기가 갈리면 정확 일치 기준으로 겹치는 키워드가 "유조선"
            # "피격" 2개뿐이라 4개 미달로 놓쳤다. 호르무즈/카르그처럼 자주
            # 나오는 지명이 매번 붙여쓰기가 갈릴 수 있어 일반적인 패턴).
            common_count = _fuzzy_keyword_overlap(title_kws, cand_kws)
            sim = fuzz.token_sort_ratio(title_stripped, cand_stripped)
            if common_count >= 4 and sim >= 50:
                print(f"  [2차 중복감지] 숫자제거 유사도 {sim}%, 공통키워드 {common_count}개 → {cand_title[:50]}")
                return {"id": cand["id"], "title_ko": cand_title, "score": sim / 100}, sim

            # 3차: 위 기준을 근소하게 못 채운 최선의 후보 하나만 기억해뒀다가
            # LLM에게 최종 판정을 맡긴다(위 카르그 사례처럼 문장 구조 자체가
            # 달라 sim이 낮게 나오는 동일 사건은 숫자 임계값을 더 낮추는 것
            # 만으로는 다른 오탐을 늘릴 위험이 있음 — find_similar_trend()의
            # 4차 LLM 판정과 동일한 철학). 근소 기준: 공통키워드 3개 이상.
            if common_count >= 3 and common_count > best_near_score:
                best_near_miss, best_near_score = cand, common_count

        if best_near_miss and body:
            cand_title = best_near_miss.get("title_ko") or ""
            cand_body = best_near_miss.get("summary_ko") or ""
            if cand_body and _same_headline_event_llm(title, body, cand_title, cand_body):
                print(f"  [3차 중복감지-LLM] 공통키워드 {best_near_score}개, LLM 동일사건 판정 → {cand_title[:50]}")
                return {"id": best_near_miss["id"], "title_ko": cand_title, "score": 0.6}, 60

    except Exception as e:
        print(f"  ⚠️ [2차 중복체크 경고] {e}")

    return None, 0


def find_continuing_story(title: str, body_excerpt: str, country: str, hours: int = 72):
    """find_similar_article()로는 못 잡는 '같은 사안의 후속 보도'를 LLM으로 판별.

    실측(2026-08-31, 네팔·티베트 홍수): 사망자 수만 바뀌는 게 아니라 표현
    자체가 매번 달라지는("홍수"→"산사태"→"빙하호수 범람") 빠르게 전개되는
    재난은 제목 문자열 유사도로 거의 못 잡는다 — 실제 기사 13건, 78개
    쌍 비교에서 find_similar_article() 기준으로는 2쌍(2.6%)만 잡혔다.
    사용자 지시(2026-08-31): "후속 기사로 나올 수는 있는데 이전 기사를
    참조하고 가야지" — 병합(같은 글 덮어쓰기)이 아니라, 같은 나라의 최근
    발행 기사를 후보로 놓고 LLM에게 "같은 사안이 이어지는 후속 보도인지"
    직접 판별시킨다. 찾으면 그 기사를 별도의 새 기사 프롬프트에 "이전
    보도" 컨텍스트로 넣어 연속성 있게 쓰게 한다(build_issue_prompt의
    continuation_* 인자).
    """
    if not country or not title:
        return None
    since = (now_kst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(), headers=_sb_headers(),
            params={
                "select": "id,title_ko,summary_ko,created_at",
                "source": "eq.NewsFinal",
                "is_published": "eq.true",
                "country": f"eq.{country}",
                "created_at": f"gte.{since}",
                "order": "created_at.desc",
                "limit": "15",
            },
            timeout=10,
        )
        if res.status_code not in (200, 206):
            return None
        candidates = [c for c in res.json() if c.get("summary_ko")]
    except Exception as e:
        print(f"  ⚠️ [후속기사 탐지] 후보 조회 실패: {e}")
        return None

    if not candidates:
        return None

    listing = "\n".join(f"{i+1}. {c['title_ko']}" for i, c in enumerate(candidates))
    prompt = f"""아래는 새로 들어온 기사와, 같은 나라({country})에 대해 최근 {hours}시간 이내 NewsFinal이 이미 발행한 기사 목록입니다.

[새 기사]
제목: {title}
{(body_excerpt or "")[:500]}

[최근 발행된 기사 목록]
{listing}

새 기사가 위 목록 중 하나와 "같은 사안이 이어지는 후속 보도"입니까? (예: 같은 재난·같은 사건의 피해 규모나 전개 상황이 업데이트된 경우) 단순히 같은 나라라는 이유만으로 답하지 마세요. 서로 다른 사건이면 "없음"입니다.
같은 사안이면 해당 번호만 숫자로 답하세요. 아니면 "없음"이라고만 답하세요. 다른 말은 하지 마세요."""

    try:
        resp = call_gemini(prompt, max_tokens=10, start_tier=3)
        if not resp:
            return None
        m = re.match(r"^\s*(\d+)", resp.strip())
        if not m:
            return None
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
    except Exception as e:
        print(f"  ⚠️ [후속기사 탐지] 판별 실패: {e}")
    return None


def save_article(title_ko, summary_ko, cluster_key, category, region, country="", article_count=0, published=True, countries=None, image_url="", image_credit="", is_travel=False, summary_3lines="", investment_idea="", unpub_reason="", continuation_of_id=None):
    # 문자셋 혼입 감지(아랍/히브리/키릴/태국/데바나가리/벵골/타밀/한자) — 저장 차단
    _leak = detect_script_leak(title_ko, summary_ko)
    if _leak:
        print(f"  ⚠️ [문자 혼입 감지: {_leak[0][0]}] 저장 차단: {title_ko[:60]}")
        return -1

    # raw JSON 본문 차단 (2026-08-04 중첩 JSON 사고)
    _unwrapped = _unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            summary_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 저장 차단: {str(title_ko)[:60]}")
            return -1

    url = f"internal://{cluster_key}"
    now_str = now_kst().strftime("%Y-%m-%d %H:%M")

    title_en, summary_en = "", ""
    if published:
        title_en, summary_en = translate_article(title_ko, summary_ko, _call_gemini_for_translation)

    payload = {
        "title_en": title_en or title_ko,
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": summary_ko,
        "url": url,
        "source": "NewsFinal",
        "category": category,
        "subcategory": cluster_key,
        "region": region,
        "country": country,
        "country_flag": "",
        "countries": ([country] + [c for c in (countries or []) if c and c != country]) if country else (countries or []),
        "image_url": image_url,
        "image_credit": image_credit,
        "score": 1,  # 최초 게시는 항상 1, 업데이트마다 +1
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": unpub_reason or "최초 게시"}],
        "sent_telegram": 0,
        "is_published": published,
        "posted_blog": 0,
        "is_travel": bool(is_travel),
        "summary_3lines": summary_3lines,
        "investment_idea": investment_idea,
        **({"continuation_of_id": continuation_of_id} if continuation_of_id else {}),
    }
    return insert_final_article(payload)


def _generate_update_headline(delta: str) -> str:
    """이번 업데이트에서 새로 확인된 내용을 25자 내외 한 줄로 요약한다.
    독자용 "업데이트 기록" 목록에 표시할 용도(2026-09-02, 사용자 요청 —
    "단순히 '내용 업데이트'만 적지 말고 한줄 요약을 추가로 적어주면 좋을 것 같은데")."""
    if not delta:
        return ""
    prompt = f"""아래는 기사에 새로 추가된 내용입니다. 이번 업데이트에서 무엇이
새로 확인됐는지 25자 내외 한 줄로 요약하세요. 완결된 문장(예: "사망자 969명으로 늘어")
형태로 헤드라인만 출력하고, 다른 말은 절대 쓰지 마세요.

{delta[:800]}"""
    try:
        headline = call_gemini(prompt, max_tokens=40, start_tier=3)
    except Exception:
        headline = None
    if not headline:
        return ""
    headline = headline.strip().strip('"').strip("'")
    headline = re.sub(r"^(헤드라인|요약)\s*[:：]\s*", "", headline)
    return headline[:60]


def update_article(article_id, title_ko, summary_ko, note: str = "업데이트", countries=None, country="", summary_3lines=None, investment_idea=None, headline: str = ""):
    """기사 갱신(병합 업데이트) — update_log에 업데이트 기록 추가"""
    # 문자셋 혼입 감지(아랍/히브리/키릴/태국/데바나가리/벵골/타밀/한자) — 업데이트 차단
    _leak = detect_script_leak(title_ko, summary_ko)
    if _leak:
        print(f"  ⚠️ [문자 혼입 감지: {_leak[0][0]}] 업데이트 차단: {title_ko[:60]}")
        return False

    # raw JSON 본문 차단 (2026-08-04 중첩 JSON 사고)
    _unwrapped = _unwrap_json_body(summary_ko)
    if _unwrapped is not None:
        if _unwrapped:
            print("  🔧 [raw JSON 본문] 내부 body 추출 → 복구")
            summary_ko = _unwrapped
        else:
            print(f"  ⛔ [raw JSON 본문] 업데이트 차단: {str(title_ko)[:60]}")
            return False

    now_str = now_kst().strftime("%Y-%m-%d %H:%M")

    # 기존 update_log 가져오기
    try:
        res = requests.get(
            f"{_sb_url()}?id=eq.{article_id}&select=update_log",
            headers=_sb_headers(), timeout=10
        )
        existing_log = []
        if res.status_code in (200, 206):
            data = res.json()
            if data and data[0].get("update_log"):
                existing_log = data[0]["update_log"]
    except Exception:
        existing_log = []

    new_log = existing_log + [{"timestamp": now_str, "note": note, **({"headline": headline} if headline else {})}]

    # 주체국(country)을 관련국(countries)에 항상 병합 — 결함 A 재발 방지
    merged_countries = ([country] + [c for c in (countries or []) if c and c != country]) if country else (countries or [])

    res = requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={
            "title_ko": title_ko,
            # title_en은 건드리지 않는다(2026-09-03) — 예전엔 title_ko 복사값을
            # 매번 덮어썼는데, 이제 title_en에 실제 번역이 들어갈 수 있어서 그걸
            # 한국어 업데이트마다 지워버리는 꼴이 된다.
            "summary_ko": summary_ko,
            "created_at": now_str,
            "update_log": new_log,
            **( {"countries": merged_countries} if merged_countries else {} ),
            **( {"summary_3lines": summary_3lines} if summary_3lines is not None else {} ),
            **( {"investment_idea": investment_idea} if investment_idea is not None else {} ),
        },
        timeout=15
    )
    return res.status_code in (200, 204)


def update_article_count(article_id, new_count):
    res = requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json={"score": new_count},
        timeout=15
    )
    return res.status_code in (200, 204)


# ── article_keywords (search_followup용 구조화 키워드) ──────────────────
# 2026-08-09 도입. 방문자에게는 절대 노출되지 않는 내부 전용 테이블 —
# anon에는 아무 권한도 없고, authenticated는 RLS로 admin 역할만 SELECT 가능,
# service_role(이 스크립트가 쓰는 키)만 쓸 수 있다. articles에 컬럼으로 얹지
# 않은 이유는 article.js/article.html이 articles에 select=*를 쓰기 때문 —
# 컬럼 하나라도 anon 권한이 없으면 그 select=* 전체가 permission denied로
# 깨진다. 이 함수들은 실패해도 기사 저장/업데이트 자체를 막지 않는다
# (키워드는 후속 매칭 품질을 높이는 보조 데이터일 뿐, 핵심 경로가 아니다).
def save_article_keywords(article_id, keyword_ko: str = "", keyword_en: str = "") -> bool:
    if not article_id or article_id <= 0:
        return False
    if not (keyword_ko or "").strip() and not (keyword_en or "").strip():
        return True  # 둘 다 없으면 쓸 것도 없다 — 실패가 아니라 스킵
    try:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/article_keywords",
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
            json={
                "article_id": article_id,
                "keyword_ko": (keyword_ko or "").strip()[:200],
                "keyword_en": (keyword_en or "").strip()[:200],
            },
            timeout=15,
        )
        return res.status_code in (200, 201, 204)
    except Exception as e:
        print(f"  ⚠️ id={article_id} 키워드 저장 실패(무시하고 계속): {e}")
        return False


def get_keywords_for_ids(ids: list) -> dict:
    """{article_id: {"keyword_ko": ..., "keyword_en": ...}} 매핑. 실패 시 빈 dict
    (호출부는 빈 dict를 "키워드 없음 → 검색 생략"으로 안전하게 처리해야 한다)."""
    if not ids:
        return {}
    try:
        id_list = ",".join(str(i) for i in ids)
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/article_keywords",
            headers=_sb_headers(),
            params={"article_id": f"in.({id_list})", "select": "article_id,keyword_ko,keyword_en"},
            timeout=15,
        )
        if res.status_code in (200, 206):
            return {r["article_id"]: r for r in res.json()}
    except Exception as e:
        print(f"  ⚠️ 키워드 일괄 조회 실패(무시하고 계속): {e}")
    return {}


# ── 클러스터링 ────────────────────────────────────────────

def split_multi_topic_title(title: str) -> list:
    """
    복수 주제 제목을 개별 토픽으로 분리.
    예: "우간다 군 수뇌부 갈등 및 나이지리아 채용 사기 주의보"
    → ["우간다 군 수뇌부 갈등", "나이지리아 채용 사기 주의보"]
    단일 주제면 빈 리스트 반환.
    """
    if not title:
        return []
    separators = [' 및 ', ' and ', ' & ', ' et ', '…및', ', and ', '; ']
    for sep in separators:
        if sep.lower() in title.lower():
            parts = [p.strip() for p in title.split(sep) if p.strip() and len(p.strip()) > 5]
            if len(parts) >= 2:
                return parts
    return []


# 2026-09-01 사용자 지시: "칼럼, 데스크칼럼, 기자수첩은 기사화 금지". 원문(제목/
# 본문 첫머리)에 오피니언 장르 표식이 명시적으로 붙어 있는 경우만 잡는다 —
# "opinion"/"analysis" 같은 단어는 스트레이트 뉴스에도 흔히 나오므로, 콜론·대괄호
# 등으로 장르 라벨임이 분명한 패턴만 매칭해 오탐을 줄인다. id=118436(인민일보
# 홍보 칼럼이 "중국이 밝혔다"로 오서술된 사고)은 원문에 이런 라벨이 아예 없어
# 이 필터로는 못 잡는다 — 그건 writer_rules(바이라인 감지)가 별도로 담당한다.
_OPINION_LABEL_RE = re.compile(
    r'(?:^|[\[\(【])\s*(opinion|op-ed|opeds?|column|commentary|editorial|viewpoint|'
    r'오피니언|칼럼|데스크칼럼|기자수첩|사설|기고)\s*(?:[:\]\)】\-–—|ㅣ]|$)',
    re.IGNORECASE,
)


def is_opinion_column(title: str, text: str = "") -> bool:
    """제목 또는 본문 첫머리(200자)에 오피니언/칼럼 장르 라벨이 명시된 경우만 True."""
    if title and _OPINION_LABEL_RE.search(title.strip()):
        return True
    if text and _OPINION_LABEL_RE.search(text[:200].strip()):
        return True
    return False


def is_multi_topic_title(title: str) -> bool:
    """복수 주제 제목 여부 — 분리 가능한 패턴 + 글로벌 종합 제목"""
    import re
    if not title:
        return False
    if len(split_multi_topic_title(title)) >= 2:
        return True
    if re.match(r'^글로벌\s+\S+.+(?:변화|동향|행보|흐름|속에서|격화|가속화)', title):
        return True
    if re.search(r'각국의?\s+(경제|사회|정치|행보|대응|현안)', title):
        return True
    if re.match(r'^전\s+세계\s+주요국', title):
        return True
    if '등 글로벌' in title or '등 주요 단신' in title or '등 주요 현안' in title:
        return True
    country_names = ['나이지리아','케냐','가나','에티오피아','필리핀','베트남',
                     '인도네시아','태국','이집트','우간다','탄자니아','수단',
                     '키르기스스탄','미얀마','캄보디아','인도','중국','미국',
                     '방글라데시','파키스탄','카자흐스탄','라오스','캄보디아']
    hits = [c for c in country_names if c in title]
    if len(hits) >= 3:
        return True
    return False


def is_multi_topic_body(text: str) -> bool:
    """
    본문 앞 3문단이 서로 다른 국가/주제를 다루는지 감지.
    각 문단에서 국가명을 추출해서 3개 이상 다른 국가가 나오면 복수 주제로 판단.
    """
    if not text:
        return False
    import re
    # 앞 600자만 분석
    lead = text[:600]
    paragraphs = [p.strip() for p in re.split(r'[.!?。]\s+', lead) if len(p.strip()) > 20][:6]

    country_names = ['나이지리아','케냐','가나','에티오피아','필리핀','베트남',
                     '인도네시아','태국','이집트','우간다','탄자니아','수단',
                     '키르기스스탄','미얀마','캄보디아','방글라데시','파키스탄',
                     '카자흐스탄','라오스','카메룬','코트디부아르','세네갈',
                     '잠비아','짐바브웨','앙골라','모잠비크','르완다']

    found_countries = set()
    for para in paragraphs:
        for c in country_names:
            if c in para:
                found_countries.add(c)

    # 3개 이상 다른 국가가 앞부분에 나오면 복수 주제
    return len(found_countries) >= 3


def extract_keywords(text):
    """텍스트에서 의미 있는 키워드 추출"""
    if not text:
        return set()
    text = text.lower()
    text = re.sub(r'[^\w\s가-힣]', ' ', text)
    words = text.split()
    return {w for w in words if w not in STOPWORDS and len(w) >= 3}


def title_keywords(text):
    """제목에서만 키워드 추출 — 고유명사(기관명·인명·지명) 중심"""
    if not text:
        return set()
    # 한글 2자 이상 단어 (고유명사 위주)
    text_clean = text.lower()
    text_clean = re.sub(r'[^\w\s가-힣]', ' ', text_clean)
    words = text_clean.split()
    # 불용어 제거, 한글은 2자 이상, 영문은 4자 이상
    result = set()
    for w in words:
        if w in STOPWORDS:
            continue
        # 한글 포함 단어
        if re.search(r'[가-힣]', w) and len(w) >= 2:
            result.add(w)
        # 영문 단어
        elif not re.search(r'[가-힣]', w) and len(w) >= 4:
            result.add(w)
    return result


def get_lead(text, chars=300):
    """본문 앞 2문단 추출 (약 300자)"""
    if not text:
        return ""
    # 문단 구분: 줄바꿈 또는 마침표+공백
    paragraphs = [p.strip() for p in re.split(r'\n+', text) if p.strip()]
    lead = " ".join(paragraphs[:2])
    return lead[:chars]


def articles_are_related(a, b):
    """
    두 기사가 같은 이슈인지 판단.
    제목 유사도 + 본문 앞 2문단 키워드 겹침 기반.
    국가가 다르면 즉시 제외.
    """
    title_a = (a.get("title_ko") or a.get("title_en") or "").lower()
    title_b = (b.get("title_ko") or b.get("title_en") or "").lower()

    if len(title_a) < 6 or len(title_b) < 6:
        return False

    country_a = a.get("country") or ""
    country_b = b.get("country") or ""
    diff_country = bool(country_a and country_b and country_a != country_b)

    # 국가가 명시적으로 다르면 즉시 제외 — 절대 조건
    if diff_country:
        return False

    same_category = a.get("category") == b.get("category")

    # 제목 유사도
    title_sim = fuzz.token_sort_ratio(title_a, title_b)

    # 본문 앞 2문단 키워드
    lead_a = get_lead(a.get("summary_ko") or a.get("summary_en") or "")
    lead_b = get_lead(b.get("summary_ko") or b.get("summary_en") or "")
    lead_kw_a = title_keywords(lead_a)
    lead_kw_b = title_keywords(lead_b)
    lead_common = lead_kw_a & lead_kw_b

    # 제목 핵심 키워드
    title_kw_a = title_keywords(title_a)
    title_kw_b = title_keywords(title_b)
    title_common = title_kw_a & title_kw_b

    # 국가 정보가 둘 다 없거나 한쪽만 없으면 더 엄격하게 판단.
    # 실사고(2026-08-10): diff_country는 양쪽 다 country가 있어야만 발동하는데,
    # RSS 원본은 절반 이상이 country가 비어 있다(00_공통.md §3). 한쪽만 비어도
    # "같은 국가" 경로의 느슨한 기준을 그대로 타면서 완전 무관한 기사끼리
    # 묶였다(id=63029: 미 상원 암호화폐 법안 + 러시아 키예프 공습,
    # id=62142: 우크라이나 정유소 공습 + 러시아 은어 유행, id=6273: 캐나다
    # 구리 광산 + 나이지리아 전 장관 무죄). "같은 국가라고 확신할 수 없으면"
    # 전부 엄격 기준으로 통일한다 — 오탐이 누락보다 나쁘다는 원칙.
    country_uncertain = not country_a or not country_b
    high_threshold = SIMILARITY_HIGH + 10 if country_uncertain else SIMILARITY_HIGH

    # 조건 1: 제목이 매우 유사 + 본문 앞부분도 키워드 2개 이상 공유
    if title_sim >= high_threshold and len(lead_common) >= 2:
        return True

    # 조건 2: 제목 키워드 2개 이상 공유 + 본문 앞부분 키워드 3개 이상 공유 + 같은 카테고리
    # 글로벌 기사는 키워드 요건 강화
    req_title = 3 if country_uncertain else 2
    req_lead  = 4 if country_uncertain else 3
    if len(title_common) >= req_title and len(lead_common) >= req_lead and same_category:
        return True

    # 조건 3: 제목+본문 키워드 합산 — 글로벌은 더 많이 요구
    all_common = (title_kw_a | lead_kw_a) & (title_kw_b | lead_kw_b)
    req_all = 6 if country_uncertain else 4
    if len(all_common) >= req_all and same_category:
        return True

    return False


def is_coherent_cluster(cluster: list) -> bool:
    """
    클러스터가 실제로 같은 이슈인지 키워드 기반으로 판단.
    기사들이 공통 핵심 키워드(주체/사건/기관명)를 공유하면 단일 이슈.
    공통 키워드 없이 각자 다른 주제면 엉터리.
    Gemini 호출 없이 규칙 기반으로 처리.
    """
    # 실사고(2026-08-10): "소규모는 통과"가 <4였는데, 실제 잡탕 사고 다수가
    # 정확히 2개짜리 클러스터였다(articles_are_related가 무관한 기사 둘을
    # 잘못 묶으면 그대로 여기를 건너뛰어 발행됨 — id=63029, 62142, 6273 등).
    # 아래 로직 자체는 N=2에도 그대로 성립한다(키워드 공유 없는 쌍 = 고립 =
    # 연결비율 0 = False). 1개(=단독)만 자명하게 통과시킨다.
    if len(cluster) < 2:
        return True  # 단독 기사는 자명하게 단일 이슈

    import re

    def extract_core_kw(text):
        """제목에서 핵심 키워드 추출 (고유명사 위주)"""
        if not text:
            return set()
        text = text.lower()
        text = re.sub(r'[^\w\s가-힣]', ' ', text)
        words = text.split()
        stopwords = {
            'the','a','an','in','on','at','to','of','for','and','or','is',
            'are','was','with','by','from','as','its','new','says','said',
            '및','에서','으로','이후','위해','통해','대한','관련','주요',
            '발표','강화','확대','추진','계획','정부','시장','경제','기업',
        }
        result = set()
        for w in words:
            if w in stopwords:
                continue
            if re.search(r'[가-힣]', w) and len(w) >= 2:
                result.add(w)
            elif not re.search(r'[가-힣]', w) and len(w) >= 4:
                result.add(w)
        return result

    # 각 기사의 핵심 키워드 추출
    article_kws = []
    for a in cluster:
        if a.get("__needs_review__"):
            continue
        title = a.get("title_ko") or a.get("title_en") or ""
        kw = extract_core_kw(title)
        if kw:
            article_kws.append(kw)

    if len(article_kws) < 2:
        return True

    # 공통 키워드가 있는 기사 쌍 비율 계산
    # 절반 이상의 기사가 적어도 하나의 공통 키워드를 공유하면 단일 이슈
    n = len(article_kws)
    connected = 0
    for i in range(n):
        for j in range(i+1, n):
            if article_kws[i] & article_kws[j]:  # 공통 키워드 있으면
                connected += 1
                break  # 이 기사(i)는 연결됨
        else:
            # i번 기사가 어느 기사와도 키워드 안 겹치면 고립
            pass

    # 실제로 연결된 기사 수 계산
    has_connection = []
    for i in range(n):
        linked = any(article_kws[i] & article_kws[j] for j in range(n) if j != i)
        has_connection.append(linked)

    connected_ratio = sum(has_connection) / n

    # 절반 미만의 기사만 연결돼 있으면 엉터리 클러스터
    if connected_ratio < 0.5:
        return False
    return True


_TITLE_SOURCE_SUFFIX_RE = re.compile(r'\s+[-–|]\s+[^-–|]{2,40}$')


def _is_garbled_headline_mashup(title: str, summary: str) -> bool:
    """구글뉴스류 피드의 '요약'이 실제 요약이 아니라 제목 뒤에 다른 헤드라인이
    그대로 이어붙은 경우를 감지한다.

    실사고(2026-08-27, id=103586): 구글뉴스 독일 피드에서 온 두 항목의
    summary_en이 title_en과 거의 동일하게 시작한 뒤 다른 헤드라인 조각이
    그대로 이어붙어 있었다(예: "...Hamburger AbendblattLive-Ticker zum
    Prozess: Christina Block: „Ich..."). 번역·본문크롤링 전 상태라 이 뭉친
    텍스트가 그대로 프롬프트에 들어갔고, 맥락 없는 독일어 관용구
    ("Zepter in die Hand nehmen" = "직접 나서다")를 Gemini가 자기 배경지식
    (오이겐 블록=블록하우스 창업자)으로 채워 "경영권 전면에 나선다"는
    완전히 다른 사실을 지어냈다.
    ⚠️ title은 보통 "헤드라인 - 매체명" 형태이고 summary는 매체명 뒤에
    구분자 없이 다음 헤드라인이 바로 붙는 형태라(실측), 단순 startswith로는
    안 잡힌다 — 매체명 접미사를 뗀 핵심 헤드라인부만 퍼지 비교한다."""
    if not title or not summary:
        return False
    core = _TITLE_SOURCE_SUFFIX_RE.sub('', title).strip().lower()
    if len(core) < 15:
        core = title.strip().lower()
    s = summary.strip().lower()
    probe_len = min(len(core), len(s))
    if probe_len < 15:
        return False
    return fuzz.ratio(core[:probe_len], s[:probe_len]) >= 85 and len(s) > len(core) + 10


def _has_real_content(a: dict) -> bool:
    """full_text가 있거나, 요약이 제목의 단순 반복/뭉치기가 아닌 경우만
    '실제 내용 있음'으로 본다. 제목 하나뿐인 항목은 소스로서 신뢰할 수 없다."""
    if a.get("full_text"):
        return True
    summary = a.get("summary_ko") or a.get("summary_en") or ""
    if not summary:
        return False
    title = a.get("title_en") or a.get("title_ko") or ""
    return not _is_garbled_headline_mashup(title, summary)


def cluster_articles(articles):
    """기사를 이슈별로 클러스터링"""
    clusters = []
    used = set()

    for i, a in enumerate(articles):
        if i in used:
            continue
        cluster = [a]
        used.add(i)

        for j, b in enumerate(articles):
            if j in used or j == i:
                continue
            if articles_are_related(a, b):
                cluster.append(b)
                used.add(j)

        if len(cluster) >= CLUSTER_MIN_SIZE:
            # 클러스터 구성원 전원이 제목뿐이고 본문·실제 요약이 하나도 없으면
            # (2026-08-27 실사고, id=103586) 헤드라인만으로 세부 사실을 지어낼
            # 위험이 매우 커 검토 필요로 미발행 저장한다.
            if not any(_has_real_content(x) for x in cluster if not x.get("__needs_review__")):
                print(f"  [검토필요] 본문·요약 없이 제목뿐인 클러스터 ({len(cluster)}건) — 미발행 저장")
                cluster.append({"__needs_review__": True})
            # 다국가 혼합 클러스터 → 검토 필요로 미발행 저장
            elif not is_coherent_cluster(cluster):
                print(f"  [검토필요] 다국가 혼합 클러스터 ({len(cluster)}건) — 미발행 저장")
                cluster.append({"__needs_review__": True})
            clusters.append(cluster)

    # 큰 클러스터 우선
    clusters.sort(key=lambda c: len(c), reverse=True)
    return clusters


def make_cluster_key(cluster):
    """클러스터 고유 키 생성 — 날짜 + 대표 기사 제목 해시"""
    rep = (cluster[0].get("title_ko") or cluster[0].get("title_en") or "")[:50]
    today = now_kst().strftime("%Y%m%d")
    h = hashlib.md5(f"{today}:{rep}".encode()).hexdigest()[:8]
    return f"cluster_{today}_{h}"


# ── Gemini 호출 ───────────────────────────────────────────

# ── 프롬프트 빌더 ─────────────────────────────────────────

JSON_OUTPUT_SPEC = r"""[출력 형식]
아래 JSON 객체 하나만 출력하세요. JSON 앞뒤에 설명·인사말·마크다운 코드펜스(```)를 절대 붙이지 마세요.
{
  "title": "핵심을 담은 제목",
  "country": "기사의 핵심 주체가 되는 국가 1개. 어느 나라 기업/정부/기관이 주체인가 기준. 글로벌 기업·국제기구가 주체면 빈 문자열",
  "countries": ["기사에서 직접 당사국으로 등장하는 국가들만. 본문에 단순 언급·비교 대상으로만 나오는 나라는 제외. 최대 3개. 없으면 빈 배열"],
  "category": "경제 | 금융 | 자원·에너지 | 산업·기업 | 정치·외교 | 사회 | IT·과학 | 문화·예술 | 글로벌 중 하나",
  "is_travel": false,
  "body": "기사 본문",
  "keyword_ko": "이 기사만의 핵심 주제어 1개(한글). 나중에 이 기사의 후속 소식을 검색할 때 쓰인다",
  "keyword_en": "keyword_ko와 같은 대상의 영문 표기. 고유명사는 원문 로마자 그대로"
}
[keyword_ko/keyword_en 작성 기준]
국가명·지역명만 단독으로 쓰지 마세요 — 너무 넓어서 검색 시 완전히 무관한 기사와 섞입니다.
이 기사를 다른 기사와 구별해주는 가장 구체적인 고유명사(사건명·기업명·인물명·기관명 등)를 쓰세요.
예: "님바 광산 회사"/"Nimba Mining Company", "태풍 돌핀"/"Typhoon Dolphin". 마땅한 고유명사가
전혀 없으면(드묾) 빈 문자열로 두세요 — 억지로 국가명을 넣지 마세요.
[분야 선택 기준]
- 경제: 거시경제, 무역, GDP, 통화정책, 인플레이션 등 국가 경제 전반
- 금융: 은행, 증권, 투자, 환율, 핀테크
- 자원·에너지: 광업, 채굴, 석유, 가스, 전력, 원자재, 재생에너지
- 산업·기업: 제조업, 기업 실적, 산업 정책, 공급망
- 정치·외교: 정부, 선거, 국제관계, 정책, 외교
- 사회: 인프라, 교육, 보건, 노동, 인구
- IT·과학: 기술, 통신, 연구개발, 우주산업
- 문화·예술: 미술, 문학, 음악, 영화, 공연, 문화유산, 문화산업
- 글로벌: 특정 국가에 국한되지 않는 세계적 이슈 (단, 위 카테고리로 분류 가능하면 해당 카테고리 우선)

[is_travel 판단 기준]
이 기사가 해외여행자에게 실질적으로 유의미한 정보 — 여행경보·치안·시위·테러, 비자·입국규정, 항공노선·공항 운영, 관광지 개방/폐쇄, 감염병, 자연재해 등 — 를 담고 있으면 true, 아니면 false. 단순 경제·산업 뉴스는 false.

[JSON 작성 규칙]
- body 안의 줄바꿈은 \n, 큰따옴표는 \" 로 이스케이프하세요.
- 값이 없어도 키를 생략하지 말고 빈 문자열("") 또는 빈 배열([])을 쓰세요.
- is_travel은 문자열이 아니라 boolean(true/false)로 쓰세요.
- JSON 이외의 텍스트는 한 글자도 출력하지 마세요."""



_ACRONYM_RE = re.compile(r'\b[A-Z]{2,6}(?:/[A-Z0-9]{2,6})?\b')
# 2~3단어 타이틀케이스 조직명(예: "Assam Rifles", "Guruvayur Devaswom Board").
# 사람 이름과 헷갈리지 않도록 흔한 인명 접두 호칭이 앞에 오면 제외한다.
_TITLECASE_ORG_RE = re.compile(r'\b(?:[A-Z][a-z]+ ){1,2}[A-Z][a-z]+\b')
# 단일 단어 고유명사(예: "Guruvayur", "Imphal"). 문장 맨 앞은 그냥 대문자로
# 시작하는 흔한 단어일 뿐 고유명사 신호가 아니므로 코드에서 따로 제외한다.
_SINGLE_PROPER_RE = re.compile(r'\b[A-Z][a-z]{3,}\b')
_PERSON_TITLE_PREFIXES = {"Mr", "Mrs", "Ms", "Dr", "Prof", "President", "Prime",
                           "Minister", "Chairman", "Secretary", "Governor", "Chief"}

# 흔히 쓰여 이미 모델이 잘 아는 약어는 위키 조회에서 제외(불필요한 API 호출 방지).
_COMMON_ACRONYMS = {
    "UN", "EU", "US", "USA", "UK", "WHO", "CEO", "CFO", "CTO", "COO", "GDP",
    "IPO", "AI", "IT", "NATO", "FBI", "CIA", "NASA", "FIFA", "UEFA", "OPEC",
    "IMF", "WTO", "UNESCO", "USD", "EUR", "GBP", "JPY", "CNY", "KRW", "UAE",
    "NGO", "PM", "VP", "CEOs",
}

# 이미 모델이 잘 아는 흔한 단일 고유명사는 조회에서 제외(불필요한 API 호출 방지).
_COMMON_PROPER_NOUNS = {
    "India", "China", "Japan", "Korea", "America", "Africa", "Europe", "Asia",
    "United", "America", "Reuters", "Bloomberg", "Twitter", "Facebook",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "New", "The",
}


def wikipedia_lookup(name: str, threshold: int = 70) -> tuple | None:
    """이름과 가장 비슷한 위키 문서의 (제목, 한 줄 설명) 반환. 없으면 None.
    무료 위키 API만 쓰므로(Gemini 미사용) 쿼터 부담이 없다.
    opensearch는 설명(description)을 거의 항상 빈 문자열로 주기 때문에(실측
    2026-08-23: FARDC 검색 결과 desc가 전부 ''), 제목만 opensearch로 찾고
    한 줄 설명은 query API(extracts)로 별도 조회한다."""
    name = (name or "").strip()
    if not name:
        return None
    best_title, best_lang, best_score = None, None, 0
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
                    for title in data[1]:
                        score = fuzz.token_sort_ratio(name, title)
                        if score >= threshold and score > best_score:
                            best_title, best_lang, best_score = title, lang, score
        except Exception:
            continue

    if not best_title:
        return None

    try:
        res = requests.get(
            f"https://{best_lang}.wikipedia.org/w/api.php",
            params={"action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
                    "exsentences": 1, "redirects": 1, "titles": best_title, "format": "json"},
            headers={"User-Agent": "NewsFinal-EntityCheck/1.0 (+https://newsfinal.co.kr)"},
            timeout=10,
        )
        if res.status_code == 200:
            pages = res.json().get("query", {}).get("pages", {})
            for page in pages.values():
                extract = (page.get("extract") or "").strip()
                if extract:
                    return (best_title, extract)
    except Exception:
        pass
    return None


def extract_naming_candidates(text: str, limit: int = 4) -> list:
    """소스 원문(영문)에서 약어·타이틀케이스 조직명 후보를 규칙 기반으로 추출
    (LLM 미사용). (용어, 종류) 튜플로 반환 — 종류에 따라 위키 매칭 엄격도가
    다르다(build_naming_hints 참조).

    "R K Sharma"류 이니셜 인명은 제외한다 — 실측(2026-08-23) 결과 짧은
    이니셜 인명은 전혀 무관한 동명이인(예: "R K Sharma" → 캐나다 벤처캐피털
    리스트 "Ray Sharma")과 fuzzy-match되는 오탐이 흔했다. 이니셜 인명은
    위키 확인 없이 writer_rules의 "이니셜 생략, 성만 표기" 규칙에만 맡긴다
    (확인 불가능한 추측을 프롬프트에 끼워 넣는 것보다 안전)."""
    if not text:
        return []
    seen, found = set(), []

    for m in _ACRONYM_RE.finditer(text):
        term = m.group().strip()
        base = term.split('/')[0]
        if term in seen or base in _COMMON_ACRONYMS:
            continue
        seen.add(term)
        found.append((term, "acronym"))
        if len(found) >= limit:
            return found

    org_words = set()
    for m in _TITLECASE_ORG_RE.finditer(text):
        term = m.group().strip()
        words = term.split()
        org_words.update(words)
        if term in seen:
            continue
        prefix_before = text[max(0, m.start() - 15):m.start()].split()
        if (words[0].rstrip(".,") in _PERSON_TITLE_PREFIXES
                or (prefix_before and prefix_before[-1].rstrip(".,") in _PERSON_TITLE_PREFIXES)):
            continue
        seen.add(term)
        found.append((term, "org"))
        if len(found) >= limit:
            return found

    # 단일 단어 고유명사: 문장 맨 앞(그냥 대문자로 시작하는 흔한 단어일 뿐인
    # 경우)과, 위에서 이미 조직명의 일부로 다룬 단어는 제외한다.
    for m in _SINGLE_PROPER_RE.finditer(text):
        term = m.group().strip()
        if term in seen or term in org_words or term in _COMMON_PROPER_NOUNS:
            continue
        preceding = text[:m.start()].rstrip()
        if not preceding or preceding[-1] in ".!?":
            continue
        seen.add(term)
        found.append((term, "single"))
        if len(found) >= limit:
            break
    return found


def build_naming_hints(source_text: str) -> str:
    """약어·조직명 후보를 위키에서 조회해 프롬프트에 붙일 표기 참고 블록을
    만든다. 2026-08-23 실사고(id=94001 "에프에이알디시", id=95881
    "Assam Rifles" 음차 누락 등)로 도입 — 검색 그라운딩(Gemini) 대신 무료
    위키 API를 먼저 쓰고, 위키에 없는 후보는 그냥 건너뛴다(모델 자체 지식 +
    writer_rules 규칙에 맡김). 이 저장소에서 기사량이 가장 많은 스크립트라
    Gemini 호출을 추가하지 않는 것이 핵심.

    타이틀케이스 조직명("org")과 단일 단어 고유명사("single")는 약어보다
    애매한 문자열이라(사람 이름·흔한 단어와 형태가 겹침) 위키 매칭
    threshold를 95로 엄격하게 잡아 오탐 위험을 낮춘다 — 약어(70)보다
    훨씬 보수적이다. 특히 단일 단어는 후보군 중 가장 모호하므로 정확
    일치에 가까운 매칭만 신뢰한다."""
    hints = []
    for term, kind in extract_naming_candidates(source_text):
        threshold = 70 if kind == "acronym" else 95
        hit = wikipedia_lookup(term, threshold=threshold)
        if hit:
            title, desc = hit
            hints.append(f"- {term}: {title} — {desc}")
    if not hints:
        return ""
    return (
        "\n\n[표기 참고 - 위키피디아 확인]\n" + "\n".join(hints) +
        "\n위 정보를 참고해 정확한 한국어 표기로 쓰세요(약어는 로마자 그대로 두고 "
        "처음 등장 시 위 설명을 바탕으로 뜻을 괄호 병기)."
    )


MAX_CLUSTER_SOURCES = 12  # 2026-08-30 사용자 지적: 소스가 6개 이상인 클러스터도
# 5개까지만 본문 재료로 쓰고 나머지는 제목만 나열해 실제 내용을 버리고
# 있었다(전체 발행 기사 1,280건 중 732건이 900자 미만). 토큰 비용이
# 실제 제약이 아니므로 상한을 12로 올려 더 많은 실제 소스를 살린다
# (완전 무제한은 아님 — 극단적으로 큰 클러스터의 프롬프트 폭주 방지).


# ── 원자재 실시간 시세 그라운딩(2026-09-04) ────────────────────
# 클러스터 소스 기사들이 "6주 만에 최고치" 같은 정성적 표현만 쓰고 구체적
# 달러 수치를 안 담고 있는 경우가 흔하다(장중 로이터 마켓랩 기사 특성).
# Gemini는 없는 숫자를 지어내지 않고 그냥 생략해버려서 기사에 실제 가격이
# 전혀 안 남았다(사용자 제보, id=128194 — "국제유가가 기사에 전혀 표기가
# 안 돼 있다"). build_issue_prompt는 전 카테고리 공용 프롬프트라, 스코프를
# 클러스터 제목의 원자재 키워드 매치로 좁혀서 무관한 기사엔 영향이 없게 한다.
# (브렌트를 먼저 검사 — "유가"/"oil" 같은 일반 키워드가 브렌트 기사에도
# 흔히 같이 나오므로, 순서를 바꾸면 브렌트 기사가 WTI로 잘못 매치될 수 있음)
_COMMODITY_INFO = [
    ("BRENT",  ["브렌트", "brent"], "국제유가(브렌트유)", "달러/배럴", "BZ=F"),
    ("WTI",    ["wti", "유가", "oil price", "crude oil", "원유", "유가가"], "국제유가(WTI)", "달러/배럴", "CL=F"),
    ("NATGAS", ["천연가스", "natural gas"], "천연가스", "달러/MMBtu", "NG=F"),
    ("GOLD",   ["금값", "gold price", "금 시세"], "국제 금값", "달러/온스", "GC=F"),
]


def _detect_commodity(cluster):
    blob = " ".join(
        f"{a.get('title_ko') or ''} {a.get('title_en') or ''}" for a in cluster
    ).lower()
    for key, keywords, label, unit, symbol in _COMMODITY_INFO:
        if any(kw in blob for kw in keywords):
            return label, unit, symbol
    return None


def _fetch_commodity_quote(symbol: str):
    """야후 파이낸스 비공식 차트 API(키 불필요) — frontier_markets_writer.py의
    fetch_yahoo_quote와 동일 패턴을 원자재 심볼(CL=F 등)로 재사용."""
    try:
        res = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d",
            timeout=10, headers={"User-Agent": "Mozilla/5.0"},
        )
        if res.status_code != 200:
            return None
        meta = (res.json().get("chart", {}).get("result") or [{}])[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        return {"price": price, "pct": meta.get("regularMarketChangePercent")}
    except Exception:
        return None


def build_commodity_grounding(cluster) -> str:
    """클러스터 제목에 원자재 키워드가 있으면 야후 실시간 시세를 프롬프트
    참고자료 블록으로 반환한다. 매치 없거나 시세 조회 실패 시 빈 문자열
    (실패해도 기존 흐름 그대로 진행 — 이 그라운딩은 있으면 좋은 보강일 뿐)."""
    detected = _detect_commodity(cluster)
    if not detected:
        return ""
    label, unit, symbol = detected
    q = _fetch_commodity_quote(symbol)
    if not q:
        return ""
    pct_str = f" (전일比 {q['pct']:+.2f}%)" if q.get("pct") is not None else ""
    return (f"\n[실시간 참고자료] {label} 현재가: {q['price']:.2f}{unit}{pct_str}. "
            f"위 원문 기사들에 구체적 수치가 없으면 이 수치를 인용하세요. "
            f"원문에 이미 다른 구체적 수치가 있으면 원문 수치를 우선하세요.\n")


def build_issue_prompt(cluster, existing_summary=None, continuation_title=None, continuation_summary=None):
    sorted_cluster = sorted(cluster, key=lambda a: bool(a.get("full_text")), reverse=True)
    main_articles = sorted_cluster[:MAX_CLUSTER_SOURCES]
    extra_titles = [a.get("title_ko") or a.get("title_en") or "" for a in sorted_cluster[MAX_CLUSTER_SOURCES:]]

    article_list = ""
    for i, a in enumerate(main_articles, 1):
        t = a.get("title_ko") or a.get("title_en") or ""
        full_text = a.get("full_text") or ""
        raw_summary = a.get("summary_ko") or a.get("summary_en") or ""
        # 요약이 실제 요약이 아니라 제목+다른 헤드라인이 뭉쳐진 것이면(구글뉴스류
        # 피드 특유의 오염) 버린다 — 실사고(2026-08-27, id=103586) 참조.
        if not full_text and _is_garbled_headline_mashup(a.get("title_en") or a.get("title_ko") or "", raw_summary):
            raw_summary = ""
        s = full_text if full_text else raw_summary
        pub = _pub_day_label(a.get("source_published_at"))
        pub_tag = f" (보도 {pub})" if pub else ""
        article_list += f"{i}. [{a.get('source','')}]{pub_tag} {t}\n"
        if s:
            article_list += f"   {s}\n\n"

    if extra_titles:
        article_list += "\n[추가 관련 기사 제목]\n"
        for t in extra_titles:
            article_list += f"- {t}\n"

    article_list += build_naming_hints(article_list)
    article_list += build_commodity_grounding(cluster)

    today_str = now_kst().strftime("%Y년 %m월 %d일")
    country = cluster[0].get("country") or ""
    category = cluster[0].get("category") or ""

    FALLBACK_RULES = """[주의사항]
- 이 작업은 요약이 아니라 기사 작성입니다. 원문의 사실관계(배경·경위·수치·인용·맥락)를 압축하지 말고 최대한 살려 완결된 기사로 풀어 쓰세요. 표준 분량은 200자 원고지 10매(대략 2,000자) 이상이며, 700자는 목표치가 아니라 최소 하한선입니다. 2,000자는 상한이 아니라 하한이라 소스에 사실관계가 더 있으면 넘어가도 됩니다. 다만 소스가 짧은 단신이면 억지로 채우지 말고, 소스에 없는 내용을 지어내 분량을 채우지도 마세요 — 분량 확보보다 환각 방지가 항상 우선입니다.
- ⚠️ 위 지시(요약하지 말고 풀어 쓰라)는 원문 문장을 순서대로 번역하듯 옮기라는 뜻이 아닙니다(2026-09-02 계기: 사용자가 참고자료로 준 이미 완성된 한국어 기사를 문장 구조만 살짝 바꿔 그대로 쓴 사례 발견). 원문에 있는 사실관계(수치·인명·날짜·인용)는 정확히 유지하되, 문장 구성·문단 순서·표현은 당신 자신의 방식으로 새로 짜서 쓰세요. 원문 문장을 하나씩 그 자리에서 한국어로 옮기지 말고, 전체 사실관계를 먼저 파악한 뒤 기사를 처음부터 다시 설계하듯 쓰세요. 소스가 1건뿐이거나 이미 한국어로 잘 쓰인 기사일수록 원문 문장 구조를 그대로 따라가기 쉬우니 더 주의하세요.
- 반드시 하나의 토픽(사건/이슈)만 다루는 기사를 작성하세요. 관련 없는 두 개 이상의 사건을 한 기사에 묶지 마세요.
- 여러 기사가 입력되더라도 가장 중요한 하나의 이슈에 집중하고, 나머지는 참고만 하세요.
- 본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
- 마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
- 매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 포함하지 마세요.
- 날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- "2026년 6월 24일 현재", "오늘", "현재" 등 절대 날짜를 본문에 쓰지 마세요. 소스 기사의 날짜 기준으로 "N일(현지시간)"으로만 표기하세요.
- 요일(월요일~일요일)을 단독으로 쓰지 마세요. 원문에 "on Wednesday"처럼 요일만 있으면 그 요일을 한국어로 옮기지 말고, 아래 보도일 규칙에 따라 날짜로 바꾸거나 시점 표현을 생략하세요. 요일은 "8일 토요일"처럼 날짜와 병기할 때만 허용됩니다.
- 각 원문 제목 옆에 "(보도 M월 D일)"이 붙어 있을 수 있습니다. 원문 본문에 사건 날짜가 명시돼 있으면 그 날짜가 우선이고, 명시된 날짜가 없을 때만 보도일을 사건 시점으로 보고 "D일(현지시간)" 형식으로 쓰세요. 보도일 표기가 없고 원문에도 날짜가 없으면 날짜를 쓰지 마세요.
- 기사 문체로 작성하세요. 논평/칼럼 문체는 금지입니다.
- 모든 인명·지명은 반드시 한글로 음차하세요. 키릴 문자, 아랍 문자, 데바나가리 등 비라틴 문자를 그대로 쓰지 마세요. 한자도 마찬가지입니다. 다만 "고(故)", "대(對)중국"처럼 괄호 안에 넣는 병기는 허용됩니다.
- ⚠️ 인명·지명에 원어를 괄호 병기할 때는 반드시 "한글 음차(원어)" 순서로 쓰세요 — "원어(한글 음차)"처럼 원어를 앞에 두지 마세요(2026-08-26 실사고, id=100504: "Rubén Rocha Moya(루벤 로차 모야)"로 영문이 먼저 나온 잘못된 예). 올바른 예: "루벤 로차 모야(Rubén Rocha Moya)".
- 영화·도서·게임 등 작품 제목은 소스 기사에 등장한 원어 표기를 정확히 확인해서만 쓰세요. 공식 한국어 제목을 확신할 수 없으면 새 표현을 지어내지 말고, 원어 제목을 괄호로 병기하거나(예: "브랜드 뉴 데이(Brand New Day)") 원어 그대로 쓰세요.
""" + JSON_OUTPUT_SPEC

    rules = load_prompt("writer_rules", fallback=FALLBACK_RULES)

    if existing_summary:
        template = load_prompt("writer_update", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
기존 기사에 새로 들어온 관련 기사들을 반영해 업데이트하세요. ({today_str})

[기존 기사]
{existing_summary}

[추가된 관련 기사]
{article_list}

새로 들어온 기사의 팩트를 기존 기사에 자연스럽게 통합해 완성도 높은 기사로 다시 써주세요.
팩트(수치, 인명, 날짜, 기관명)를 최대한 살리고, 한국어로 작성하세요.
{rules}""")
        return template.format(today_str=today_str, existing_summary=existing_summary,
                               article_list=article_list, rules=rules,
                               country=country, category=category)

    elif continuation_summary:
        # 2026-08-31: find_similar_article()가 못 잡는 "표현이 매번 바뀌는
        # 후속 재난 보도"(네팔·티베트 홍수 사례) 대응. existing_summary
        # 병합(같은 글 덮어쓰기)과 달리, 별도의 새 기사를 저장하되 이전
        # 보도를 참고해 연속성 있게 쓰게 한다(사용자 지시: "후속 기사로
        # 나올 수는 있는데 이전 기사를 참조하고 가야지").
        template = load_prompt("writer_continuation", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 진행 중인 사안에 대해 NewsFinal이 이미 보도한 이전 기사와, 새로 들어온 후속 소식입니다. ({today_str})
국가: {country} | 분야: {category}

[이전 보도 — {continuation_title}]
{continuation_summary}

[새로 들어온 후속 소식]
{article_list}

이전 보도를 이미 읽은 독자를 전제로, 새로 들어온 후속 소식을 반영한 완전히 새로운 독립 기사를 작성하세요. 이전 기사를 그대로 반복하지 말고, 이전 보도 대비 무엇이 새로 바뀌었는지(수치 변화, 새로운 전개 등)를 리드나 앞부분에서 자연스럽게 언급하세요(예: "앞서 469명으로 집계됐던 사망자 수가 750명으로 늘었다"). 이미 보도된 내용을 처음 알리는 것처럼 쓰지 마세요.
팩트(수치, 인명, 날짜, 기관명)를 최대한 살리고, 한국어로 작성하세요.
{rules}""")
        return template.format(today_str=today_str, continuation_title=continuation_title or "",
                               continuation_summary=continuation_summary, article_list=article_list,
                               rules=rules, country=country, category=category)

    elif len(main_articles) == 1 or (len(main_articles) <= 4 and len({a.get("source","") for a in main_articles}) >= 2):
        template = load_prompt("writer_single", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래 기사를 바탕으로 완성도 높은 한국어 기사를 작성하세요. ({today_str})
국가: {country} | 분야: {category}

[기사 원문]
{article_list}

원문이 여러 주제나 사건을 다루더라도 반드시 하나의 핵심 주제만 골라 작성하세요.
팩트(수치, 인명, 날짜, 기관명, 구체적 내용)를 빠짐없이 살려서 작성하세요.
원문이 프랑스어·아랍어·포르투갈어·인도네시아어 등 비영어인 경우 그대로 이해하고 한국어로 작성하세요.
원문이 길수록 기사도 충분히 길게 쓰세요. 억지로 줄이지 마세요.
한국어로만 작성하세요.
{rules}""")
        return template.format(today_str=today_str, article_list=article_list,
                               rules=rules, country=country, category=category,
                               count=len(main_articles))
    else:
        template = load_prompt("writer_cluster", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래 {count}개 기사는 같은 이슈를 다루고 있습니다. ({today_str})
국가: {country} | 분야: {category}

[관련 기사]
{article_list}

여러 기사의 팩트를 종합해 하나의 완성된 기사로 작성하세요.
각 기사의 구체적인 수치, 인명, 날짜, 기관명을 최대한 살려주세요.
원문이 풍부할수록 기사도 충분히 길게 쓰세요. 억지로 줄이지 마세요.
한국어로만 작성하세요.
{rules}""")
        return template.format(today_str=today_str, article_list=article_list,
                               rules=rules, country=country, category=category,
                               count=len(main_articles))


def call_gemini(prompt, max_tokens=1000, retry=2, start_tier=0):
    return _gemini_client.call(prompt, max_tokens=max_tokens, start_tier=start_tier,
                                temperature=0.6, timeout=(10, 30))


# 문체 검증/변환은 style_guard.py로 공용화(2026-09-02, gemini_summarizer.py와
# 완전히 동일한 코드가 각각 복붙돼 있었음). import 실패해도 죽지 않도록
# 최소 폴백을 둔다(감지 함수는 False, to_plain_style은 원문 그대로 반환).
try:
    from style_guard import has_column_style, has_polite_ending, to_plain_style, \
        _pub_day_label, verify_single_topic as _sg_verify_single_topic
    _HAS_STYLE_GUARD = True
except Exception:
    _HAS_STYLE_GUARD = False
    def has_column_style(text: str) -> bool:
        return False
    def has_polite_ending(text: str) -> bool:
        return False
    def to_plain_style(text: str) -> str:
        return text
    def _pub_day_label(raw) -> str:
        s = str(raw or "")[:10]
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
        if not m:
            return ""
        try:
            mm, dd = int(m.group(2)), int(m.group(3))
        except ValueError:
            return ""
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            return ""
        return f"{mm}월 {dd}일"


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
영화·도서·게임 등 작품명, 특정 인물 실명, 특정 기관·단체·기업명만 대상으로 합니다.
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
    """생성된 본문에 원문 자료에 없는 고유명사(작품명·인명·지명·기관명)가 새로 등장했는지 확인.
    두 신호를 같이 쓴다: ① 원본 자료 대조(Gemini 판단, 기존 방식) ② 위키피디아 독립 조회
    (판단이 아닌 단순 추출 + 결정론적 HTTP 조회 — Gemini가 오판해도 이 신호는 별개로 남는다.
    "LLM 혼자만의 판단은 위험하다" 2026-08-16 피드백 반영).
    실사고(2026-08-16, id=79327): 영화 "Brand New Day"를 "유니온 오브 어 뉴 데이"로
    완전히 잘못 옮김 — 작품 제목은 인명·지명과 달리 음차 규칙이 커버하지 않던 영역이라
    Gemini가 자기 사전지식으로 그럴듯한 제목을 지어냈다. 문제 없으면 빈 문자열, 의심되면
    이름 목록 반환."""
    if not body:
        return ""
    check_prompt = f"""아래는 기사 작성에 쓰인 원본 자료와, 그걸 바탕으로 생성된 한국어 기사 본문입니다.
다음 두 가지 유형의 오류를 확인하세요.

[1. 이름 바꿔치기] 기사 본문에 나오는 고유명사(영화·도서·게임 등 작품명, 인명, 지명,
기관명)가 원본 자료에 실제로 근거하는지 확인하세요. 정상적인 한글 음차나 공식 번역명은
문제가 아닙니다 — 원본 자료에 등장하는 대상을 다른 이름으로 완전히 잘못 지어낸 경우만
찾으세요.

[2. 수식어 날조] 실존하는 일반명사·집단명·직함(예: "아디바시", "원주민", "노동자") 앞에
원본 자료에 없는 수식어나 설명을 새로 만들어 붙인 경우를 찾으세요(예: 원문에 그냥
"Adivasis"라고만 나오는데 본문에 "PreferredSource Adivasis"처럼 없는 수식어가 붙은
경우 — 2026-08-25 id=98010 실사고). 명사 자체는 실존해도 그 앞에 붙은 꾸밈말이
원본에 없으면 이 유형입니다.

두 유형 다 있으면 "[이름] 지어낸표기 → 원본표기" 또는 "[수식어] 지어낸표기 → 원본표기
(수식어 삭제 후 표기)" 형식으로 쉼표 구분해 나열하세요. 없으면 "없음"이라고만 답하세요.

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


_ARTICLE_FENCE_RE = re.compile(r"```(?:json)?", re.I)

# 사용자 지적(2026-09-04, "기사 분량 하한이 2000자라고 해도 아직 짧은 기사들이
# 보이던데"): writer_rules v36에 "표준 분량 2,000자 이상, 700자는 최소
# 하한선"이 명시돼 있는데도(DB 프롬프트 확인 완료 — 정상 반영돼 있었음),
# 실측(9/1~ 클러스터 경로 69건)해보니 평균 1,243자에 19건(28%)이 700자
# 미만이었다. 프롬프트 지시만 있고 코드 레벨 강제가 전혀 없어서(has_column_
# style/fabricated_names/foreign_leftover와 달리 분량은 이때까지 재시도
# 대상이 아니었음) Gemini가 자주 못 지켰던 것 — 여기 재생성 루프에 합류.
# 700자를 기준으로 삼은 건 "최소 하한선"이라는 프롬프트 자체 문구를 그대로
# 따른 것(2,000자는 목표치라 강제하면 소스가 짧은 단신까지 억지로 부풀릴
# 위험이 있어 제외).
MIN_BODY_LEN_HARD_FLOOR = 700


def _extract_body_len(content: str) -> int:
    """JSON_OUTPUT_SPEC 응답에서 body 필드 길이만 추출(분량 재시도 판단용).
    JSON 파싱 실패 시 전체 텍스트 길이로 대충 추정(임계값 비교용이라 정밀할
    필요 없음)."""
    if not content:
        return 0
    try:
        cand = _ARTICLE_FENCE_RE.sub("", content.strip()).strip()
        data = json.loads(cand)
        return len(data.get("body") or "")
    except Exception:
        return len(content)


def call_gemini_article(prompt, max_tokens=1500, style_retries=1):
    """기사 본문 생성 전용 호출. 논평/칼럼체·합쇼체·원문에 없는 고유명사·번역
    누락 외국어·분량 미달 감지 시 최대 style_retries회 재생성.

    ⚠️ 2026-09-04: detect_foreign_leftover는 원래 호출부(run() 두 곳)에서
    감지 즉시 미발행 처리만 했었는데, 사용자 지적("미번역 감지 시 미발행이
    아니라 기사를 다시 쓰도록 해야 하는거 아닌가") — has_column_style/
    verify_no_fabricated_names처럼 여기 재생성 루프에 합류시켜, 감지되면
    먼저 재작성을 시도하고 그래도 안 고쳐질 때만 호출부의 미발행 처리로
    넘어가게 함. call_gemini_article 호출부 5곳(클러스터/후속보도/단독 등)
    전부에 자동 적용됨. 분량 미달 재시도도 같은 방식으로 이어서 추가."""
    content = call_gemini(prompt, max_tokens=max_tokens)
    attempt = 0
    fabricated = verify_no_fabricated_names(prompt, content) if content else ""
    foreign_leftover = detect_foreign_leftover(content) if content else ""
    body_len = _extract_body_len(content) if content else 0
    too_short = 0 < body_len < MIN_BODY_LEN_HARD_FLOOR
    while content and (has_column_style(content) or has_polite_ending(content) or fabricated or foreign_leftover or too_short) and attempt < style_retries:
        attempt += 1
        reasons = []
        if has_column_style(content) or has_polite_ending(content):
            reasons.append("논평/칼럼체" if has_column_style(content) else "합쇼체(-습니다/-입니다)")
        if fabricated:
            reasons.append(f"원문에 없는 고유명사({fabricated})")
        if foreign_leftover:
            reasons.append(f"번역 누락 외국어({foreign_leftover})")
        if too_short:
            reasons.append(f"분량 부족({body_len}자)")
        print(f"  ⚠️ {', '.join(reasons)} 감지 → 재생성 시도 ({attempt}/{style_retries})")
        retry_prompt = (
            prompt
            + "\n\n[재작성 지시] 방금 작성한 결과에 논평/칼럼 문체(예: '~를 보여줍니다', "
              "'~을 도모하고 있습니다', '~라는 평가다', '~지켜볼 필요가 있습니다' 등)이거나, "
              "'-습니다'/'-입니다' 같은 정중체(합쇼체) 종결이 섞여 있었습니다. "
              "감정·의견이 섞인 표현을 모두 배제하고, 모든 문장을 '-다'로 종결하는 "
              "스트레이트 뉴스 문체로만 다시 작성하세요."
            + (f"\n또한 다음 이름을 원문에 없는 표현으로 잘못 지어냈습니다: {fabricated}. "
               "영화·도서 등 작품 제목이나 고유명사는 원본 자료에 나온 표기를 그대로 옮기고, "
               "정확한 한국어 정식 명칭을 확신할 수 없으면 지어내지 말고 원문 표기를 그대로 쓰세요."
               if fabricated else "")
            + (f"\n또한 다음 단어가 번역되지 않고 원문 외국어(스페인어·독일어·프랑스어 등) "
               f"그대로 남아있었습니다: {foreign_leftover}. 인명·지명·기업명 등 고유명사를 "
               "제외한 이 단어들을 빠짐없이 한국어로 번역해서 본문 전체를 다시 작성하세요."
               if foreign_leftover else "")
            + (f"\n또한 방금 작성한 본문이 {body_len}자로 너무 짧습니다. 원문(소스 기사)에 "
               "아직 반영 안 된 사실관계(배경·경위·수치·인용·맥락)가 있으면 압축하지 말고 "
               "최대한 살려서 다시 쓰세요. 다만 원문 자체가 짧은 단신이라 정말 더 쓸 내용이 "
               "없다면 억지로 늘리거나 없는 내용을 지어내지 마세요."
               if too_short else "")
        )
        retried = call_gemini(retry_prompt, max_tokens=max_tokens)
        if retried:
            content = retried
            fabricated = verify_no_fabricated_names(prompt, content)
            foreign_leftover = detect_foreign_leftover(content)
            body_len = _extract_body_len(content)
            too_short = 0 < body_len < MIN_BODY_LEN_HARD_FLOOR
    if content and (has_column_style(content) or has_polite_ending(content)):
        print("  ⚠️ 재생성 후에도 논평체·합쇼체 패턴이 남아있음 (파싱 단계에서 변환)")
    if content and fabricated:
        print(f"  ⚠️ 재생성 후에도 원문에 없는 고유명사 남아있음: {fabricated} (그대로 발행 — 수동 확인 필요)")
    if content and foreign_leftover:
        print(f"  ⚠️ 재생성 후에도 번역 누락 외국어 남아있음: {foreign_leftover} (호출부에서 미발행 처리)")
    if content and too_short:
        print(f"  ⚠️ 재생성 후에도 분량 부족({body_len}자) — 원문 자체가 짧은 것으로 판단, 그대로 발행")
    return content


# 국가 → 지역 매핑
COUNTRY_TO_REGION = {
    "나이지리아": "africa", "케냐": "africa", "가나": "africa", "남아공": "africa",
    "에티오피아": "africa", "르완다": "africa", "탄자니아": "africa", "우간다": "africa",
    "이집트": "africa", "모로코": "africa", "알제리": "africa", "튀니지": "africa",
    "세네갈": "africa", "코트디부아르": "africa", "잠비아": "africa", "짐바브웨": "africa",
    "보츠와나": "africa", "나미비아": "africa", "모잠비크": "africa", "앙골라": "africa",
    "베트남": "southeast_asia", "인도네시아": "southeast_asia", "태국": "southeast_asia",
    "필리핀": "southeast_asia", "말레이시아": "southeast_asia", "캄보디아": "southeast_asia",
    "미얀마": "southeast_asia", "라오스": "southeast_asia", "동티모르": "southeast_asia",
    "카자흐스탄": "central_asia", "우즈베키스탄": "central_asia", "키르기스스탄": "central_asia",
    "타지키스탄": "central_asia", "투르크메니스탄": "central_asia",
    "사우디아라비아": "middle_east", "아랍에미리트": "middle_east", "카타르": "middle_east",
    "쿠웨이트": "middle_east", "이라크": "middle_east", "이란": "middle_east",
    "이스라엘": "middle_east", "요르단": "middle_east", "오만": "middle_east", "튀르키예": "middle_east",
    "방글라데시": "south_asia", "파키스탄": "south_asia", "스리랑카": "south_asia", "네팔": "south_asia",
    "자메이카": "caribbean", "트리니다드": "caribbean", "바베이도스": "caribbean",
    "아이티": "caribbean", "쿠바": "caribbean", "도미니카공화국": "caribbean",
    "콜롬비아": "latin_america", "페루": "latin_america", "칠레": "latin_america",
    "아르헨티나": "latin_america", "브라질": "latin_america", "멕시코": "latin_america",
    "가이아나": "latin_america", "수리남": "latin_america",
    # 중미 — 실사고(2026-08-10, id=65426): "과테말라"가 여기 없어서
    # country_to_region("과테말라")가 매핑 실패로 "global"을 반환했다.
    # 과테말라는 이 사이트에서 비중이 큰 국가라(수십 건/일) 결과적으로
    # 과테말라 기사 다수가 region="global"로 잘못 태그되고 있었을 가능성이 높다.
    "과테말라": "latin_america", "베네수엘라": "latin_america", "에콰도르": "latin_america",
    "볼리비아": "latin_america", "파라과이": "latin_america", "우루과이": "latin_america",
    "파나마": "latin_america", "코스타리카": "latin_america", "온두라스": "latin_america",
    "니카라과": "latin_america", "엘살바도르": "latin_america", "벨리즈": "latin_america",
}

def country_to_region(country: str) -> str:
    return COUNTRY_TO_REGION.get(country, "global")


# 국가명 정규화는 country_guard.py로 공용화(2026-09-02, category_guard.py와
# 같은 이유 — gemini_summarizer.py의 트렌드 생성 경로에는 이게 없어서 같은
# 사건이 "한국"/"대한민국"으로 각각 저장돼 중복 발행됨, id=119633/120650).
try:
    from country_guard import normalize_country, COUNTRY_ALIASES
except Exception:
    COUNTRY_ALIASES = {}
    def normalize_country(country: str) -> str:
        return (country or "").strip()


def update_article_fields(article_id: int, fields: dict):
    requests.patch(
        f"{_sb_url()}?id=eq.{article_id}",
        headers=_sb_headers(),
        json=fields,
        timeout=15
    )


_FENCE_RE = re.compile(r"```(?:json)?", re.I)
_LABEL_LINE_RE = re.compile(
    r"^[\s*#>\-]*\**\s*(제목|국가|관련\s*국가|분야|카테고리|여행|본문|내용|기사\s*본문)\s*\**\s*[:：]\s*(.*)$"
)
_NULLISH = ("없음", "-", "N/A", "n/a", "없다", "null", "None", "")


def _strip_leaked_labels(text: str) -> str:
    """파싱이 완전히 실패했을 때 응답에 남은 라벨 줄을 제거해 본문만 남긴다.
    ⚠️ 실사고(2026-08-26, id=99924): 이 함수는 gen_body or _strip_leaked_labels(content)
    형태로 "정상 파싱이 실패했을 때의 최후 폴백"으로 여러 호출부(cluster 업데이트
    3곳)에서 쓰인다. 원문이 "제목:"/"본문:" 라벨이 아니라 raw JSON이었던 경우
    이 함수는 라벨을 하나도 못 찾아 원문을 거의 그대로 반환했고, 그게 그대로
    summary_ko에 저장된 사고가 있었다. save_article()/update_article()의 최종
    가드가 잡아줄 거라 가정했지만 실제로는 뚫렸다(정확한 유출 지점은 라이브
    로그 없이 특정 못함) — 이 함수 자체에도 방어선을 하나 더 둬서 단일 지점
    의존을 없앤다."""
    if not text:
        return ""
    raw = _FENCE_RE.sub("", text).strip()
    _unwrapped = _unwrap_json_body(raw)
    if _unwrapped:
        return _unwrapped
    out = []
    for line in raw.split("\n"):
        m = _LABEL_LINE_RE.match(line)
        if m:
            key = m.group(1).replace(" ", "")
            rest = (m.group(2) or "").strip()
            if key in ("본문", "내용", "기사본문"):
                if rest:
                    out.append(rest)
            continue
        out.append(line)
    cleaned = "\n".join(out).strip()
    return cleaned or raw


def _coerce_countries(val, limit: int = 4) -> list:
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        parts = [str(p).strip() for p in val]
    else:
        parts = [p.strip() for p in re.split(r"[,;/·]", str(val))]
    return [p for p in parts if p and p not in _NULLISH][:limit]


def _coerce_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    s = str(val).strip().lower()
    return s.startswith("예") or s.startswith("y") or s in ("true", "1")


def _json_candidates(text: str):
    raw = _FENCE_RE.sub("", text).strip()
    yield raw
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i and (i, j + 1) != (0, len(raw)):
        yield raw[i:j + 1]


def parse_json_response(text: str):
    """Gemini JSON 응답 파싱. 성공 시 6-튜플, 실패 시 None."""
    if not text:
        return None
    for cand in _json_candidates(text):
        try:
            data = json.loads(cand)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        body = str(data.get("body") or data.get("본문") or "").strip()
        # body 필드 안에 JSON 전문이 중첩 출력되는 사고가 있어 먼저 언랩한다.
        _inner_body = _unwrap_json_body(body)
        if _inner_body:
            print("  🔧 body 중첩 JSON 감지 → 내부 본문 추출")
            body = _inner_body
        if not body:
            continue
        first = next((l for l in body.split("\n") if l.strip()), "")
        if _LABEL_LINE_RE.match(first):
            body = _strip_leaked_labels(body)
        title = str(data.get("title") or data.get("제목") or "").strip()
        country = str(data.get("country") or data.get("국가") or "").strip()
        if country in _NULLISH or country == "글로벌":
            country = ""
        category = str(data.get("category") or data.get("분야") or "").strip()
        if category in _NULLISH:
            category = ""
        raw_countries = data.get("countries")
        if raw_countries is None:
            raw_countries = data.get("관련국가")
        countries = _coerce_countries(raw_countries)
        raw_travel = data.get("is_travel")
        if raw_travel is None:
            raw_travel = data.get("여행")
        summary_3lines = str(data.get("summary_3lines") or data.get("3줄요약") or "").strip()
        investment_idea = str(data.get("investment_idea") or data.get("투자아이디어") or "").strip()
        # keyword_ko/keyword_en: article_keywords 테이블용 — 아직 프롬프트가 이 키를
        # 요구하지 않는 동안은 항상 빈 문자열이며, 하위 호출부가 빈 값을 안전하게
        # "키워드 없음 → 검색 생략"으로 처리하므로 코드 배포만으로는 아무 것도 바뀌지 않는다.
        keyword_ko = str(data.get("keyword_ko") or "").strip()
        if keyword_ko in _NULLISH:
            keyword_ko = ""
        keyword_en = str(data.get("keyword_en") or "").strip()
        if keyword_en in _NULLISH:
            keyword_en = ""
        return (title, body, country, category, countries, _coerce_bool(raw_travel),
                summary_3lines, investment_idea, keyword_ko, keyword_en)
    return None


def _parse_labeled_response(text: str):
    """레거시 라벨 형식(제목:/본문: 등) 폴백 파서."""
    title = country = category = ""
    countries = []
    is_travel = False
    raw = _FENCE_RE.sub("", text).strip()
    lines = raw.split("\n")
    body_lines = None
    for i, line in enumerate(lines):
        m = _LABEL_LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1).replace(" ", "")
        val = (m.group(2) or "").strip()
        if key == "제목":
            title = val
        elif key == "국가":
            country = "" if (val in _NULLISH or val == "글로벌") else val
        elif key == "관련국가":
            countries = _coerce_countries(val)
        elif key in ("분야", "카테고리"):
            category = "" if val in _NULLISH else normalize_category(val)
        elif key == "여행":
            is_travel = _coerce_bool(val)
        elif key in ("본문", "내용", "기사본문"):
            body_lines = ([val] if val else []) + lines[i + 1:]
            break
    body = "\n".join(body_lines).strip() if body_lines is not None else _strip_leaked_labels(raw)
    # 레거시 라벨 형식에는 keyword_ko/keyword_en이 없다 — 빈 문자열로 두면
    # 하위 호출부가 "키워드 없음"으로 안전하게 처리한다(검색 생략, 오탐 없음).
    return title, body, country, category, countries, is_travel, "", "", "", ""


def _ensure_paragraphs(text: str, target: int = 3) -> str:
    """Gemini가 문단 구분(\n\n) 지시를 어기고 한 덩어리로 응답하는 경우가 있어
    (강제성 없는 프롬프트 지시라 준수율이 들쭉날쭉함), 코드 단에서 문장(-다.)
    단위로 강제 분할하는 안전장치. 이미 \n\n이 있으면 그대로 반환.
    문장이 2개 이상이면 항상 최소 2개 문단으로 분할한다(짧은 리드 문단도 포함)."""
    if not text or "\n\n" in text:
        return text
    sentences = [s.strip() for s in re.split(r"(?<=다\.)\s+", text.strip()) if s.strip()]
    if len(sentences) < 2:
        return text  # 문장이 1개뿐이면 분할 불가
    actual_target = min(target, len(sentences) - 1)
    actual_target = max(actual_target, 2)
    n = len(sentences)
    size = math.ceil(n / actual_target)
    groups = [sentences[i:i + size] for i in range(0, n, size)]
    return "\n\n".join(" ".join(g) for g in groups)


# ── 절대날짜 후처리 ─────────────────────────────────────────────────────
# 원칙: 모든 날짜는 사건 발생지 현지시간 기준 "N일(현지시간)".
# 프롬프트 지시(writer_rules)만으론 준수율이 낮아 "2026년 7월 29일 보도했다" 형태가
# 계속 새어나오므로 코드 단에서 최종 차단한다.
#
# ⚠️ 무조건 변환은 금지. 실측(2026-07-30, 발행 자체기사 중 연월일 표기 830건 전수):
#     보도일 기준 0~3일 전 : 606건 → 전부 위반 (보도 시점을 절대날짜로 쓴 것)
#     미래(예정일)         :  54건 → 전부 정당 (배당 지급일·결선 투표일·행사 기간)
#     4일 이상 과거        : 170건 → 전부 정당 (과거 사건 시점 특정. "2015년 7월 18일 구속")
#   따라서 "보도 시점 근처"만 변환하고 과거·미래는 손대지 않는다.
#   창을 넓히면 과거 사건 시점이 소실돼 기사가 망가진다.
_ABS_DATE_RE = re.compile(r"(?<![0-9])(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# 연도 없는 "8월 3일" 형태. 연도는 기사 기준일의 연도로 가정한다.
# 연말·연초 경계에서는 gap이 음수(미래)로 나와 자동으로 축약 대상에서 빠진다.
_ABS_MD_RE = re.compile(r"(?<![0-9])(?<!년)(?<!년 )(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# "8월 4일 12:31" = 라이브 업데이트 스탬프(_update_stamp). 절대 축약하지 않는다.
_TIME_TAIL_RE = re.compile(r"^\s*\d{1,2}:\d{2}")
ABS_DATE_WINDOW_DAYS = int(os.getenv("ABS_DATE_WINDOW_DAYS", "3"))


def _normalize_recent_abs_dates(text: str, base=None, window_days: int = None,
                                add_local_time: bool = True) -> str:
    """보도 시점 근처(0~window_days일 전)의 절대날짜만 "N일(현지시간)"으로 축약."""
    if not text or ("년" not in text and "월" not in text):
        return text
    if window_days is None:
        window_days = ABS_DATE_WINDOW_DAYS
    if window_days < 0:
        return text
    base_date = (base or now_kst()).date()
    # "(현지시간)"은 관례상 한 필드 내 첫 언급에만 붙인다.
    # 이미 본문 어딘가에 표기가 있으면 새로 붙이지 않는다(검수 통과 조건도 충족).
    state = {"marked": "(현지시간)" in text}

    def _sub(m):
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return m.group(0)          # 2월 30일 등 비정상 날짜는 원문 유지
        delta = (base_date - d).days
        if delta < 0 or delta > window_days:
            return m.group(0)          # 미래 예정일·과거 사건은 그대로 둔다
        # 뒤에 이미 "(현지시간)"이 붙어 있으면 중복 삽입하지 않는다.
        tail = text[m.end():m.end() + 6]
        if tail.startswith("(현지시간)") or state["marked"] or not add_local_time:
            suffix = ""
        else:
            suffix = "(현지시간)"
            state["marked"] = True
        return f"{d.day}일{suffix}"

    out = _ABS_DATE_RE.sub(_sub, text)

    def _sub_md(m):
        """연도 없는 "N월 N일" → 보도 시점 근처면 "N일(현지시간)"."""
        try:
            d = datetime(base_date.year, int(m.group(1)), int(m.group(2))).date()
        except ValueError:
            return m.group(0)
        tail = out[m.end():m.end() + 8]
        if _TIME_TAIL_RE.match(tail):
            return m.group(0)      # 업데이트 스탬프는 손대지 않는다
        delta = (base_date - d).days
        if delta < 0 or delta > window_days:
            return m.group(0)      # 미래·과거 사건 시점은 월 정보가 필요하다
        if tail.startswith("(현지시간)") or state["marked"] or not add_local_time:
            suffix = ""
        else:
            suffix = "(현지시간)"
            state["marked"] = True
        return f"{d.day}일{suffix}"

    return _ABS_MD_RE.sub(_sub_md, out)


def _plainify_parsed(parsed):
    """파싱 결과의 텍스트 필드에 합쇼체 → 해라체 변환을 강제 적용.
    프롬프트 지시·재생성이 모두 실패해도 DB에는 '-다' 체만 저장되도록 하는 최종 안전장치."""
    title, body, country, category, countries, is_travel, summary3, investment, keyword_ko, keyword_en = parsed

    # 보도 시점 절대날짜 → "N일(현지시간)" 축약 (과거·미래 날짜는 불변)
    _before = (title, body, summary3, investment)
    title, body, summary3, investment = (_normalize_recent_abs_dates(t) for t in _before)
    if (title, body, summary3, investment) != _before:
        print("  🔧 보도 시점 절대날짜 감지 → 'N일(현지시간)' 축약 적용")

    if any(has_polite_ending(t) for t in (title, body, summary3, investment)):
        print("  🔧 합쇼체 감지 → 자동 변환 적용(-습니다 → -다)")
        title = to_plain_style(title)
        body = to_plain_style(body)
        summary3 = to_plain_style(summary3)
        investment = to_plain_style(investment)
    return title, body, country, category, countries, is_travel, summary3, investment, keyword_ko, keyword_en


def parse_title_and_body(text):
    """Gemini 응답 파싱. 1순위 JSON, 실패 시 레거시 라벨 파서로 폴백.
    반환 10-튜플의 마지막 두 값(keyword_ko, keyword_en)은 프롬프트가 해당 키를
    아직 요구하지 않는 동안은 항상 빈 문자열이다 — 호출부는 이를 "키워드 없음"으로
    안전하게 처리해야 한다(검색 생략이 오탐보다 낫다)."""
    if not text:
        return "", "", "", "", [], False, "", "", "", ""
    parsed = parse_json_response(text)
    if parsed:
        return _plainify_parsed(parsed)
    print("  ⚠️ JSON 파싱 실패 → 레거시 라벨 파서로 폴백")
    return _plainify_parsed(_parse_labeled_response(text))


# ── 기업 자동 감지·등록 ────────────────────────────────────────────────

def get_company_by_id(company_id: str) -> dict | None:
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/companies",
            headers=_sb_headers(),
            params={"id": f"eq.{company_id}", "limit": "1"},
            timeout=10
        )
        if res.status_code in (200, 206):
            data = res.json()
            return data[0] if data else None
    except Exception as e:
        print(f"  ⚠️ 기업 조회 실패: {e}")
    return None


def save_company(company_id: str, name: str, name_ko: str, country: str,
                 country_flag: str, exchange: str, ticker: str, sector: str,
                 description: str, founded_year: int = None,
                 headquarters: str = None, website: str = None) -> bool:
    try:
        payload = {
            "id": company_id,
            "name": name,
            "name_ko": name_ko,
            "country": country,
            "country_flag": country_flag,
            "exchange": exchange,
            "ticker": ticker,
            "sector": sector,
            "description": description,
            "founded_year": founded_year,
            "headquarters": headquarters,
            "website": website,
            "is_published": True,
            "created_at": now_kst().strftime("%Y-%m-%d %H:%M"),
            "updated_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/companies",
            headers=_sb_headers(),
            json=payload,
            timeout=15
        )
        if res.status_code in (200, 201):
            return True
        print(f"  ⚠️ 기업 저장 실패: HTTP {res.status_code} - {res.text[:300]}")
        return False
    except Exception as e:
        print(f"  ⚠️ 기업 저장 실패(예외): {e}")
    return False


def generate_update_note(existing_summary: str, new_summary: str) -> str:
    prompt = f"""기존 기사와 업데이트된 기사를 비교해서, 이번 업데이트에서 새롭게 추가되거나 변경된 핵심 내용을 15자 이내 한 줄로 요약하세요.
예시: "현지 당국 공식 발표 추가", "사망자 수 214명으로 업데이트", "정부 대응 방안 발표"
마크다운 없이 텍스트만 출력하세요.

기존 내용: {(existing_summary or '')[:300]}
새 내용: {(new_summary or '')[:300]}"""
    result = call_gemini(prompt, max_tokens=50, start_tier=3)
    return (result or "업데이트").strip().replace('\n', ' ')[:30]


# 2026-09-01: 위키미디어 커먼즈+Pixabay 이미지 검색 로직을 article_image.py로
# 공용화(다른 writer 스크립트도 쓸 수 있도록 -- script_leak.py/gemini_client.py와
# 같은 이유). 호출부(수십 곳) 코드는 안 바뀌도록 같은 시그니처의 얇은 wrapper만 남긴다.
from article_image import fetch_article_image as _fetch_article_image_base
from image_store import store_image


def fetch_article_image(title: str, body: str, entity: str = "") -> tuple:
    return _fetch_article_image_base(title, body, entity, call_gemini)


def detect_and_register_companies(title: str, body: str, country: str):
    if not title and not body:
        return

    prompt = f"""아래 뉴스 기사에서 언급된 주요 기업(상장사 또는 대형 민간기업)을 최대 3개 추출하세요.
프론티어 마켓(아프리카, 동남아시아, 중동, 동유럽 등) 기업만 대상으로 합니다.
글로벌 대기업(애플, 구글, 삼성 등)은 제외합니다.

기사 제목: {title}
기사 본문: {body[:800]}
기사 국가: {country}

각 기업에 대해 아래 JSON 형식으로만 응답하세요 (마크다운, 추가 설명 없이):
[
  {{
    "id": "영문_소문자_언더스코어_ID (예: safaricom, dangote_cement)",
    "name": "공식 영문 기업명",
    "name_ko": "한국어 기업명",
    "exchange": "거래소 약칭 (예: NSE, NGX, IDX, SET, PSE, EGX, HOSE, JSE)",
    "ticker": "티커 심볼 (모르면 빈 문자열)",
    "sector": "업종 (예: 통신, 은행, 에너지, 부동산)",
    "description": "한국 투자자를 위한 3문장 이내 기업 소개. 설립연도, 핵심 사업, 시장 내 위상 포함. 투자 권유 없이 사실만.",
    "founded_year": 설립연도_숫자_또는_null,
    "headquarters": "본사 도시, 국가 (예: 나이로비, 케냐)"
  }}
]
추출할 기업이 없으면 빈 배열 []을 반환하세요."""

    raw = call_gemini(prompt, max_tokens=800, start_tier=2)
    if not raw:
        return

    try:
        import json
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            return
        companies = json.loads(match.group())
        if not isinstance(companies, list):
            return

        for comp in companies[:3]:
            company_id = comp.get("id", "").strip().lower().replace(" ", "_")
            if not company_id or not comp.get("name"):
                continue

            existing = get_company_by_id(company_id)
            if existing:
                continue

            comp_country = country or ""
            flag_map = {
                "나이지리아": "🇳🇬", "케냐": "🇰🇪", "남아공": "🇿🇦", "남아프리카공화국": "🇿🇦",
                "베트남": "🇻🇳", "인도네시아": "🇮🇩", "태국": "🇹🇭", "필리핀": "🇵🇭",
                "이집트": "🇪🇬", "가나": "🇬🇭", "에티오피아": "🇪🇹", "탄자니아": "🇹🇿",
                "방글라데시": "🇧🇩", "파키스탄": "🇵🇰", "카자흐스탄": "🇰🇿",
                "몽골": "🇲🇳",
            }
            country_flag = flag_map.get(comp_country, "🌍")

            ok = save_company(
                company_id=company_id,
                name=comp.get("name", ""),
                name_ko=comp.get("name_ko", ""),
                country=comp_country,
                country_flag=country_flag,
                exchange=comp.get("exchange", ""),
                ticker=comp.get("ticker", ""),
                sector=comp.get("sector", ""),
                description=comp.get("description", ""),
                founded_year=comp.get("founded_year"),
                headquarters=comp.get("headquarters", ""),
            )
            if ok:
                print(f"  🏢 새 기업 등록: {comp.get('name')} ({company_id})")

    except Exception as e:
        print(f"  ⚠️ 기업 감지 파싱 오류: {e}")


# ── 메인 실행 ─────────────────────────────────────────────

def verify_single_topic(title: str, body: str) -> bool:
    """style_guard.verify_single_topic()에 이 파일의 call_gemini를 주입해서 위임."""
    if _HAS_STYLE_GUARD:
        return _sg_verify_single_topic(title, body, call_gemini)
    if not title or not body:
        return True
    prompt = f"""아래 기사가 하나의 명확한 토픽(사건/이슈/기업/정책)만 다루는지 판단하세요.
서로 다른 국가나 전혀 관련 없는 사건 여러 개를 한 기사에 묶은 경우 "NO"라고만 답하세요.
특히 기사 뒷부분 문단에 제목·앞문단과 무관한 다른 사건이 붙어 있으면(예: 영화 흥행 기사 뒤에 스포츠 경기 내용) 반드시 "NO"라고 답하세요.
하나의 토픽이면 "YES"라고만 답하세요.

제목: {title}
본문 전체:
{body[:2500]}

답변 (YES 또는 NO만):"""
    result = call_gemini(prompt, max_tokens=5, start_tier=3)
    if not result:
        return True
    return "YES" in result.upper()


def detect_foreign_leftover(body: str) -> str:
    """한국어 기사 본문에 번역 안 된 외국어(스페인어·프랑스어·독일어·포르투갈어
    등 라틴 문자 언어) 단어가 남아있는지 LLM으로 판별한다.

    script_leak.py는 비라틴 문자(키릴·아랍·한자 등)만 잡는다. 라틴 문자로
    쓰인 외국어는 스크립트로 구분이 안 돼서 그 가드를 그대로 통과한다
    (2026-09-01 실사고, id=118435: 과테말라 매체의 스페인어 원문을 옮기다가
    "trayectoria"(경력)를 그대로 남기고, 심지어 무관한 독일어 "Jahrzehnte"
    (decades)까지 섞여 나옴 — 사용자 제보로 발견). 발견하면 발견된 단어
    문자열을, 없으면 빈 문자열을 반환한다."""
    if not body:
        return ""
    prompt = f"""아래는 한국어로 작성됐어야 할 뉴스 기사입니다. 번역되지 않고 원문 언어(스페인어·프랑스어·독일어·포르투갈어·이탈리아어 등) 그대로 남아있는 단어나 구절이 있는지 확인하세요.

⚠️ 다음은 오류가 아닙니다: 영어 인명·기업명·지명 등 고유명사(한글 음차와 함께 괄호 병기된 원어 포함), 비자 종류·통화코드·모델명 등 관용적으로 로마자를 유지하는 표현, 작품 제목의 원어 병기.
⚠️ 오류인 것: 그 외 일반 명사·형용사·부사 등이 한국어로 번역되지 않고 스페인어·독일어·프랑스어 등 외국어 단어 그대로 남아있는 경우.

본문:
{body[:2500]}

번역 안 된 외국어 단어가 있으면 그 단어들만 쉼표로 나열하세요. 없으면 "없음"이라고만 답하세요."""
    result = call_gemini(prompt, max_tokens=60, start_tier=3)
    if not result:
        return ""
    result = result.strip()
    if not result or result[:10].replace(" ", "").startswith("없음"):
        return ""
    return result[:200]


def park_multi_topic_articles(articles: list) -> int:
    parked = 0
    for a in articles:
        title_en = a.get("title_en") or a.get("title_ko") or ""
        parts = split_multi_topic_title(title_en)
        if not parts:
            continue

        full_text = a.get("full_text") or a.get("summary_en") or a.get("summary_ko") or ""
        country   = a.get("country") or ""
        category  = a.get("category") or "글로벌"
        region    = a.get("region") or "global"

        print(f"  [파킹] 복수 주제 분리: {title_en[:60]}")
        for part in parts:
            try:
                safe_kw = part[:20].replace("'", "").replace('"', '').replace('(', '').replace(')', '')
                check = requests.get(
                    f"{SUPABASE_URL}/rest/v1/articles",
                    headers=_sb_headers(),
                    params={
                        "select": "id",
                        "subcategory": "eq.parked_topic",
                        "title_en": f"ilike.*{safe_kw}*",
                        "limit": "1",
                    },
                    timeout=10
                )
                if check.status_code in (200, 206) and check.json():
                    print(f"    → 이미 파킹됨: {part[:50]}")
                    continue
            except Exception:
                pass

            now_str = now_kst().strftime("%Y-%m-%d %H:%M")
            payload = {
                "title_en":    part,
                "title_ko":    part,
                "summary_en":  full_text[:500],
                "summary_ko":  "",
                "full_text":   full_text,
                "url":         a.get("url", f"parked://{part[:30]}"),
                "source":      a.get("source", ""),
                "category":    category,
                "subcategory": "parked_topic",
                "region":      region,
                "country":     country,
                "country_flag":"",
                "countries":   ([country] + [c for c in (a.get("countries") or []) if c and c != country]) if country else (a.get("countries") or []),
                "score":       0,
                "created_at":  a.get("created_at", now_str),
                "first_published_at": now_str,
                "update_log":  [{"timestamp": now_str, "note": f"복수주제 분리 파킹 (원제: {title_en[:60]})"}],
                "sent_telegram": 0,
                "is_published":  False,
                "posted_blog":   0,
            }
            try:
                res = requests.post(
                    f"{SUPABASE_URL}/rest/v1/articles",
                    headers=_sb_headers(),
                    json=payload,
                    timeout=15
                )
                if res.status_code in (200, 201):
                    data = res.json()
                    art_id = data[0].get("id", -1) if data else -1
                    print(f"    → 파킹 완료 (id={art_id}): {part[:60]}")
                    parked += 1
                else:
                    print(f"    → 파킹 실패: {res.status_code}")
            except Exception as e:
                print(f"    → 파킹 예외: {e}")

    return parked


# ── 라이브 기사 능동적 업데이트 ──────────────────────────────

# 라이브 업데이트 대상 subcategory 접두 (트렌드 기사 전용).
# 트렌드는 사건이 며칠에 걸쳐 전개되므로 후속 정보를 능동 검색해 반영하는 것이
# 기사의 핵심이다. 반대로 일반 기사(cluster_/solo_)는 후속 반영이 필요 없고,
# 정기 자동생성물(weather_/국제유가/digest_)은 외부 API 실측값이 본문이라
# Gemini가 덮어쓰면 실측이 창작으로 대체된다.
# (실사고 id=47879: 기상청 기반 한국 날씨 기사가 "역대 최고기온 42.5도 경신
#  반영"이라는 근거 없는 내용으로 전면 교체됨)
LIVE_UPDATE_PREFIXES = ("trend_", "realtrend_", "extrend_")


def get_stale_live_articles() -> list:
    since     = (now_kst() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
    not_after = (now_kst() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    try:
        res = requests.get(
            _sb_url(),
            headers=_sb_headers(),
            # dict를 쓰면 같은 키("created_at")가 뒤엣것으로 덮어써져 48시간 하한이
            # 통째로 사라진다. 튜플 리스트로 보내야 PostgREST가 두 조건을 AND로 묶는다.
            # select에 subcategory를 포함해야 아래 접두 필터가 실제로 동작한다.
            #
            # ⚠️ 기존에는 score=eq.1 로 걸렀는데, 트렌드 기사는 score=2로 저장되어
            # 정작 대상이어야 할 트렌드가 통째로 빠지고 일반·자동생성 기사만
            # 돌고 있었다(2026-08-04 실측: trend_/realtrend_ 216건 전부 score=2,
            # weather_ 105건이 score=1이라 대상에 포함). score 조건을 버리고
            # subcategory 접두로 직접 지정한다.
            params=[
                ("select", "id,title_ko,summary_ko,country,category,score,subcategory,update_log"),
                ("source", "eq.NewsFinal"),
                ("is_published", "eq.true"),
                ("subcategory", "like.*trend_*"),
                ("created_at", f"gte.{since}"),
                ("created_at", f"lte.{not_after}"),
                ("order", "created_at.desc"),
                ("limit", "20"),
            ],
            timeout=15
        )
        if res.status_code in (200, 206):
            # PostgREST의 like.*trend_* 는 앞뒤 와일드카드라 오탐 여지가 있어
            # 접두를 코드에서 다시 정확히 검증한다.
            articles = [a for a in res.json()
                        if (a.get("subcategory") or "").startswith(LIVE_UPDATE_PREFIXES)]
            kw_map = get_keywords_for_ids([a["id"] for a in articles])
            for a in articles:
                kw = kw_map.get(a["id"]) or {}
                a["keyword_ko"] = kw.get("keyword_ko") or ""
                a["keyword_en"] = kw.get("keyword_en") or ""
            return articles
    except Exception as e:
        print(f"  [라이브 업데이트] 조회 실패: {e}")
    return []


# search_followup() 폴백 키워드 추출에서 건너뛸 국가·대륙·지역명(2026-09-07,
# id=137672/138097 실사고). COUNTRY_TO_REGION의 국가명 전부 + 대륙/지역 표현.
_FOLLOWUP_KW_BLOCKLIST = set(COUNTRY_TO_REGION.keys()) | {
    "아프리카", "아시아", "유럽", "중동", "중남미", "북미", "동아시아", "오세아니아",
    "동남아시아", "중앙아시아", "남아시아", "카리브해", "라틴아메리카", "글로벌",
    "한국", "중국", "일본", "미국", "러시아", "인도", "영국", "프랑스", "독일",
}


def search_followup(title: str, country: str, keyword_ko: str = "", keyword_en: str = "") -> list:
    """keyword_ko/keyword_en(article_keywords 테이블, 2026-08-09 도입)이 있으면 그것만
    쓴다 - Gemini가 생성 시점에 핵심 주제를 직접 답한 구조화 데이터라, 제목 문자열을
    매번 다시 추측하는 것보다 안전하다. 키워드가 없는 기사(이 필드 도입 이전 생성분)는
    title 파싱 구식 로직으로 폴백하되, 두 차례 실사고(id=63237, 61698 - 국가명만으로
    검색해 완전 무관한 내용이 붙음) 이후 확정된 원칙을 지킨다: 쓸 만한 키워드가 없으면
    검색 자체를 생략한다. 놓치는 게 오매칭보다 낫다."""
    import urllib.parse
    since = (now_kst() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")

    if keyword_ko or keyword_en:
        kw = keyword_ko.strip()
        eng_kw = keyword_en.strip()
    else:
        # 폴백: 구조화 키워드가 없는 기사용 title 파싱 휴리스틱.
        # 제목은 "국가명, 본문" 형식이라 첫 단어가 거의 항상 국가명이다. 국가명만으로
        # 검색하면 무관한 기사가 붙는다(id=63237). 국가명 다음 실제 핵심어를 쓴다.
        #
        # ⚠️ 2026-09-07 실사고(id=137672, 138097): 트렌드 트래커(gemini_summarizer.py)가
        # 만든 제목은 쉼표가 아예 없는 일반 문장형("이란 서부 도로에서 연료 탱크로리
        # 폭발해...", "아프리카 각국서 엠폭스 확산...")이라 위 쉼표 분리가 무력화되고,
        # kw_list[0]이 그대로 "이란"/"아프리카" 같은 국가·대륙명이 되어버렸다. "이란"으로
        # 검색해 완전 무관한 "이란 연료가격 인상" 기사가, "아프리카"로 검색해 케냐
        # 소상공인 금지 등 무관한 기사가 그대로 "후속 정보"로 붙었다. 쉼표 유무와
        # 무관하게 국가명·대륙명 자체는 항상 건너뛰고 그다음 실제 단어를 쓴다.
        body_part = title.split(",", 1)[1] if "," in title else title
        kw_list = [w for w in body_part.replace(",", "").replace("\xb7", " ").split() if len(w) >= 2]
        kw_list = [w for w in kw_list if w not in _FOLLOWUP_KW_BLOCKLIST]
        kw = kw_list[0] if kw_list else ""
        # 한글 전용 제목은 영문 단어가 없다. country 폴백은 무관한 GDELT 결과를
        # 불러온다(id=61698). 영문 키워드가 없으면 GDELT 검색을 건너뛴다.
        eng_words = [w for w in title.split() if not any("가" <= c <= "힣" for c in w)]
        eng_kw = " ".join(eng_words[:3]) if eng_words else ""

    results = []

    if kw:
        try:
            res = requests.get(
                _sb_url(),
                headers=_sb_headers(),
                params={
                    "select": "title_en,title_ko,summary_en,summary_ko,full_text,source",
                    "source": "neq.NewsFinal",
                    "created_at": f"gte.{since}",
                    "or": f"(title_ko.ilike.*{kw}*,title_en.ilike.*{kw}*)",
                    "order": "created_at.desc",
                    "limit": "8",
                },
                timeout=15
            )
            if res.status_code in (200, 206):
                results.extend(res.json())
        except Exception:
            pass

    if eng_kw:
        try:
            gres = requests.get(
                f"https://api.gdeltproject.org/api/v2/doc/doc"
                f"?query={urllib.parse.quote(eng_kw)}&mode=artlist&maxrecords=5&timespan=2d&format=json",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=12
            )
            if gres.status_code == 200:
                for a in gres.json().get("articles", []):
                    results.append({
                        "title_en": a.get("title", ""),
                        "summary_en": a.get("title", ""),
                        "source": a.get("domain", "GDELT"),
                    })
        except Exception:
            pass

    return results


# ── 라이브 업데이트: 상단 블록 구조 ──────────────────────────────────
#
#   [업데이트]
#   ■ 8월 4일 12:31 — 최신
#   ■ 8월 3일 09:10 — 그 이전
#   ────────
#   (원 본문 — 최초 생성 이후 불변)
#
# BBC·CNN 라이브 기사와 같은 배치다. 최신이 맨 위에 오므로 본문의 낡은 수치가
# 아래 남아도 독자는 갱신된 값을 먼저 읽는다. 덕분에 **본문 전면 재작성이
# 아예 필요 없어진다** — Gemini가 이미 검수를 통과한 문장을 다시 쓸 일이
# 없으므로 누락·변형·창작 리스크가 구조적으로 제거된다.
#
# ⚠️ 원 본문은 어떤 경로로도 덮어쓰지 않는다. 이 불변식을 깨면 위 이점이 전부 사라진다.

UPDATE_HEAD = "[업데이트]"
UPDATE_SEP  = "────────"

# 구형(하단) 이력 구분자. gemini_summarizer가 붙여 온 기존 발행분과의 읽기 호환용.
# 신규 append는 전부 상단 블록으로 가지만, 이미 붙어 있는 하단 이력은 보존한다.
HISTORY_SEP = "────────\n[업데이트 이력]"

MIN_DELTA_LEN     = 60    # 이보다 짧으면 새 내용으로 치지 않는다
DELTA_DUP_OVERLAP = 0.65  # 문장 겹침률이 이 이상이면 재탕으로 보고 폐기
                          # (실측: 조사·어미만 바꾼 환언 0.69 / 무관한 문장 0.18 이하)

# 한 기사에 붙는 "후속 정보 추가" 최대 횟수. update_article()이 매번 created_at을
# 지금 시각으로 갱신해서 get_stale_live_articles()의 "3~48시간 전" 창에 계속 다시
# 걸리므로, 이 캡이 없으면 무한히 반복 적용될 수 있다(실사고 id=61698: 매 회차마다
# 걸려 24시간 동안 5차례 연속 붙음 — search_followup 매칭 버그와 별개로, 매칭이
# 완벽해도 한 기사가 끝없이 늘어나는 걸 막을 장치가 없었다).
MAX_LIVE_UPDATES_PER_ARTICLE = 3

# 업데이트 항목의 머리표·타임스탬프. 내용 비교 전에 떼어내지 않으면
# 같은 문장인데도 접두 때문에 겹침률이 떨어져 중복이 새어 나간다.
# (summarizer가 붙인 구형 항목은 타임스탬프가 없으므로 선택 그룹으로 둔다)
_HIST_PREFIX_RE = re.compile(
    r"^■\s*(?:\d{1,2}월\s*\d{1,2}일\s*\d{1,2}:\d{2}\s*(?:업데이트)?\s*[—\-–]\s*)?"
)


def _split_article(summary: str) -> tuple[str, str, str]:
    """(상단 업데이트 블록, 원 본문, 구형 하단 이력)으로 분리한다.
    없는 구간은 빈 문자열. 어느 것도 없으면 전부 본문으로 취급한다."""
    s = (summary or "").lstrip()
    updates = ""
    if s.startswith(UPDATE_HEAD):
        parts = s.split("\n" + UPDATE_SEP, 1)
        if len(parts) == 2:
            updates = parts[0].rstrip()
            # 구분선 줄의 나머지를 버리고 다음 줄부터 본문
            s = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
            s = s.lstrip("\n")

    parts = s.split(HISTORY_SEP, 1)
    body   = parts[0].rstrip()
    legacy = (HISTORY_SEP + parts[1]) if len(parts) > 1 else ""
    return updates, body, legacy


def _compose_article(updates: str, body: str, legacy: str) -> str:
    """_split_article()의 역연산."""
    out = ""
    if updates:
        out += updates.rstrip() + "\n" + UPDATE_SEP + "\n\n"
    out += (body or "").rstrip()
    if legacy:
        out += "\n\n" + legacy
    return out


def _update_stamp() -> str:
    d = now_kst()
    return f"{d.month}월 {d.day}일 {d.strftime('%H:%M')}"


def _prepend_update(updates: str, delta: str) -> str:
    """새 항목을 블록 맨 위(헤더 바로 아래)에 넣는다. 최신이 위.

    실사고(2026-08-13, id=72477): delta가 짧은 한 줄이던 시절엔 안 보였는데,
    라이브 업데이트를 "완결된 기사문"으로 바꾼 뒤(2026-08-09) 각 항목이 여러
    문단이 되면서 새 항목과 그 다음(기존) 항목 사이에 빈 줄이 없어 문단이
    그대로 붙어버리는 문제가 드러났다. lines[1:]를 이어붙일 때 빈 문자열을
    하나 끼워 새 항목 끝과 기존 항목 시작 사이에 반드시 빈 줄이 들어가게 한다.
    """
    entry = f"■ {_update_stamp()} — {delta}"
    if updates:
        lines = updates.split("\n")
        return "\n".join([lines[0], entry, ""] + lines[1:])
    return UPDATE_HEAD + "\n" + entry


def _norm_sent(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s or "")


def _char_ngrams(s: str, n: int = 3) -> set:
    s = _norm_sent(s)
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else set()


def _overlap(a: set, b: set) -> float:
    """작은 쪽 기준 포함률. 길이가 다른 두 서술의 '같은 말인가'를 자카드보다 잘 잡는다."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _split_sents(text: str) -> list:
    return [x for x in re.split(r"(?<=다\.)\s*|\n+", text or "") if x.strip()]


def _dedupe_delta(delta: str, existing: str) -> str:
    """기존 본문·업데이트에 이미 있는 내용을 문장 단위로 delta에서 제거한다.
    완전일치뿐 아니라 조사·어미만 바꾼 재탕도 겹침률로 걸러낸다.
    delta 자체의 문장 간 중복도 같이 제거한다.

    ⚠️ 한계: 문자 3-gram 기반이라 어휘를 통째로 바꾼 환언은 못 잡는다
       (예: "사임 의사를 밝혔다" vs "사임하겠다는 뜻을 나타냈다" → 겹침률 0).
    """
    existing = "\n".join(_HIST_PREFIX_RE.sub("", ln.strip())
                         for ln in (existing or "").split("\n"))
    seen_norm, seen_ng = set(), []
    for x in _split_sents(existing):
        ns = _norm_sent(x)
        if not ns:
            continue
        seen_norm.add(ns)
        seen_ng.append(_char_ngrams(x))

    kept = []
    for x in _split_sents(delta):
        ns = _norm_sent(x)
        if not ns or ns in seen_norm:
            continue
        ng = _char_ngrams(x)
        if any(_overlap(ng, g) >= DELTA_DUP_OVERLAP for g in seen_ng):
            continue
        kept.append(x.strip())
        seen_norm.add(ns)
        seen_ng.append(ng)
    return " ".join(kept).strip()


def update_live_articles():
    if not GEMINI_API_KEYS:
        return

    stale = get_stale_live_articles()
    if not stale:
        print("[라이브 업데이트] 대상 없음")
        return

    print(f"\n[라이브 업데이트] 대상 {len(stale)}건 → 최대 5건 처리")
    updated = 0

    for a in stale[:5]:
        title   = a.get("title_ko") or ""
        summary = a.get("summary_ko") or ""
        country = a.get("country") or ""
        keyword_ko = a.get("keyword_ko") or ""
        keyword_en = a.get("keyword_en") or ""
        art_id  = a["id"]

        prior_live_updates = sum(
            1 for l in (a.get("update_log") or [])
            if (l or {}).get("note") == "후속 정보 추가"
        )
        if prior_live_updates >= MAX_LIVE_UPDATES_PER_ARTICLE:
            print(f"  → {title[:50]}")
            print(f"     [SKIP] 후속 정보 추가 {prior_live_updates}회로 상한 도달 — 더 이상 붙이지 않음")
            continue

        # 원 본문은 어떤 경우에도 Gemini에 넘기지 않고 덮어쓰지도 않는다.
        updates, base_body, legacy = _split_article(summary)

        print(f"  → {title[:50]}")
        followups = search_followup(title, country, keyword_ko, keyword_en)
        if not followups:
            print(f"     후속 없음")
            continue

        # search_followup()이 full_text(원문 전문)까지 조회해오는데도 여태 안 쓰고
        # summary만, 그것도 200자로 잘라 넘겼다 — 재료 자체가 부족하니 압축 요약밖에
        # 못 나온 것도 당연하다. full_text가 있으면 그걸 우선 쓰고, 길이 상한도
        # 모델 컨텍스트 기준으로 충분히 넉넉하게 잡는다(5건 합쳐도 15000자 안쪽).
        followup_text = ""
        for f in followups[:5]:
            t = f.get("title_ko") or f.get("title_en") or ""
            b = f.get("full_text") or f.get("summary_ko") or f.get("summary_en") or ""
            followup_text += f"- {t}\n  {b[:3000]}\n"

        applied = "\n".join(x for x in (updates, legacy) if x)
        applied_block = f"\n[이미 반영된 업데이트]\n{applied}\n" if applied else ""

        # 증분만 생성한다. 본문 재작성 경로는 존재하지 않는다.
        # BBC·CNN 라이브 기사처럼 상단에 쌓이는 구조 자체는 유지하되(위치는 그대로),
        # 각 업데이트 항목은 압축 요약이 아니라 그 자체로 완결된 기사문이어야 한다.
        # 실사고(2026-08-09): "2~4문장" 상한과 후속 정보 200자 절단 때문에 매번
        # 짧은 요약 블럿만 생성돼 "기사가 아니라 요약이 올라온다"는 지적을 받음.
        delta_prompt = f"""아래 기사에 이어 붙일 '새로 확인된 내용'을 기사문으로 쓰세요.
BBC·CNN 라이브 업데이트처럼, 이 항목 하나만 읽어도 무슨 일이 있었는지 충분히
이해되는 완결된 기사 문단이어야 합니다. 사실을 나열만 하는 압축 요약이 아니라,
후속 정보에 담긴 배경·경위·수치·전망까지 살려서 서술하세요.

[현재 기사]
제목: {title}
내용: {base_body}
{applied_block}
[후속 정보]
{followup_text}

규칙:
- 현재 기사와 이미 반영된 업데이트에 있는 내용은 절대 반복하지 마세요.
- 새로 확인된 사실이 없으면 정확히 "업데이트 불필요" 한 줄만 출력하세요.
- 후속 정보에 담긴 사실 관계를 빠짐없이 살려 쓰세요. 문장 수를 인위적으로 줄이지 말고,
  근거가 있는 만큼 충분히 서술하세요(보통 3문단 이상 분량이 나옵니다). 논평·마크다운·헤더·소제목 금지.
- 수치가 갱신됐으면 갱신된 값을 명시하세요(예: "사망자는 30명으로 늘었다").
- 모든 문장을 "-다"로 종결하세요. "-습니다", "-입니다" 등 정중체는 쓰지 마세요.
- 날짜는 후속 정보에 명시된 "N일(현지시간)" 형식만 쓰세요. 날짜 근거가 없으면 생략하고 추측하지 마세요.
- 고유명사는 한글 음차로 쓰되, 명칭에 든 영문 약어는 알파벳 그대로 두세요(오픈AI, xAI, UN, EU).
  영문+숫자 코드(H-1B, 5G)와 한국 기업 약칭(SK, LG)도 그대로 둡니다.

새로 확인된 내용:"""

        delta = call_gemini_article(delta_prompt, max_tokens=1500)
        if not delta or "업데이트 불필요" in delta:
            print("     업데이트 불필요")
            continue

        delta = delta.strip()
        if delta.startswith("새로 확인된 내용:"):
            delta = delta.split(":", 1)[1].strip()
        delta = _strip_leaked_labels(delta)
        # 실사고(2026-08-13, id=72477): Gemini가 우리 쪽 "■ 날짜 — " 표기 형식을
        # 델타 본문 맨 앞에 스스로 흉내내 넣는 경우가 있다. _prepend_update()가
        # 이미 자체 "■ {stamp} — "를 앞에 붙이므로 이중으로 찍힌다
        # ("■ 8월 13일 16:25 — ■ 12일(현지시간) — ..."). 앞머리에 남은
        # "■ ... — " 패턴은 무조건 우리 것이 아니라 Gemini가 흉내낸 잔재이므로 제거.
        delta = re.sub(r"^■\s*[^—\n]{0,30}—\s*", "", delta)
        delta = _normalize_recent_abs_dates(delta)
        if has_polite_ending(delta):
            print("     🔧 합쇼체 감지 → 자동 변환 적용")
            delta = to_plain_style(delta)

        # 중복 방어 — 같은 내용이 매 실행마다 append되는 것을 막는다.
        # 비교 대상은 본문 + 기존 업데이트 전체(summary)다.
        # _dedupe_delta는 문장을 공백으로 다시 이어붙이므로(단락 구분 소실),
        # 여러 문단 분량 기사문을 만든 뒤에는 _ensure_paragraphs로 단락을 복원한다.
        delta = _dedupe_delta(delta, summary)
        if len(delta) < MIN_DELTA_LEN:
            print(f"     새 내용 부족({len(delta)}자) → 생략")
            continue
        delta = _ensure_paragraphs(delta)

        note = "후속 정보 추가"
        headline = _generate_update_headline(delta)
        new_body = _compose_article(_prepend_update(updates, delta), base_body, legacy)

        if update_article(art_id, title, new_body, note=note, headline=headline):
            update_article_count(art_id, 2)
            print(f"     ✅ {note}")
            updated += 1

        time.sleep(CALL_INTERVAL)

    print(f"[라이브 업데이트] {updated}건 완료")

def run():
    if not GEMINI_API_KEYS:
        print("[SKIP] GEMINI_API_KEY 없음")
        return

    print("\n[클러스터링] 오늘 기사 분석 중...")
    all_articles = get_today_articles(limit=300)

    opinion_skipped = [
        a for a in all_articles
        if is_opinion_column(a.get("title_en") or a.get("title_ko") or "", a.get("full_text") or "")
    ]
    if opinion_skipped:
        print(f"  [제외] 칼럼/오피니언 장르 라벨 {len(opinion_skipped)}건 → 기사화 대상에서 제외")
        skip_ids = {a["id"] for a in opinion_skipped}
        all_articles = [a for a in all_articles if a["id"] not in skip_ids]

    clusters = cluster_articles(all_articles)
    print(f"  → {len(all_articles)}건 중 {len(clusters)}개 클러스터 발견\n")

    today_own_articles = get_today_own_articles()

    generated = 0
    updated   = 0
    processed = 0

    for i, cluster in enumerate(clusters):
        if processed >= MAX_CLUSTERS_PER_RUN:
            print(f"[STOP] 이번 실행 최대 처리 수 도달 ({MAX_CLUSTERS_PER_RUN}개) — 다음 실행에 계속")
            break

        country     = cluster[0].get("country") or ""
        category    = cluster[0].get("category") or ""
        titles      = [a.get("title_ko") or a.get("title_en") or "" for a in cluster]
        cluster_key = make_cluster_key(cluster)
        cur_count   = len(cluster)

        print(f"[클러스터 {i+1}/{len(clusters)}] {country} / {category} — {cur_count}건")
        for t in titles:
            print(f"  - {t[:60]}")

        existing      = get_existing_cluster(cluster_key)
        prev_count    = get_cluster_article_count(cluster_key)

        if existing:
            if cur_count <= prev_count:
                print(f"  [SKIP] 새 기사 없음 ({cur_count}건 동일)\n")
                continue

            print(f"  → 기존 기사 업데이트 ({prev_count}건 → {cur_count}건)")
            prompt  = build_issue_prompt(cluster, existing["summary_ko"])
            has_full = any(a.get("full_text") for a in cluster)
            content = call_gemini_article(prompt, max_tokens=4000 if has_full else 1500)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment, gen_keyword_ko, gen_keyword_en = parse_title_and_body(content)
                gen_body = _ensure_paragraphs(gen_body)
                new_title = gen_title if gen_title else titles[0][:50]
                note = generate_update_note(existing["summary_ko"], gen_body or _strip_leaked_labels(content))
                update_article(existing["id"], new_title, gen_body or _strip_leaked_labels(content), note=note, countries=gen_countries if gen_countries else None, country=gen_country or "", summary_3lines=gen_summary3 or None, investment_idea=gen_investment or None)
                update_article_count(existing["id"], prev_count + 1)
                save_article_keywords(existing["id"], gen_keyword_ko, gen_keyword_en)
                if gen_country or gen_category or gen_travel:
                    update_fields = {}
                    if gen_country:
                        norm_country = normalize_country(gen_country)
                        update_fields["country"] = norm_country
                        update_fields["region"] = country_to_region(norm_country)
                    if gen_category:
                        update_fields["category"] = gen_category
                        if gen_category == "글로벌" and not gen_country:
                            update_fields["region"] = "global"
                    if gen_travel:
                        update_fields["is_travel"] = True
                    if update_fields:
                        update_article_fields(existing["id"], update_fields)
                print(f"  ✅ 업데이트 완료: {new_title}\n")
                updated += 1
            else:
                print(f"  ❌ 업데이트 실패\n")

        else:
            if cur_count < CLUSTER_MIN_SIZE:
                print(f"  [SKIP] 기사 부족 ({cur_count}건)\n")
                continue

            if (country in ADVANCED_ECONOMIES and cur_count < CLUSTER_MIN_SIZE_ADVANCED
                    and not _has_official_source(cluster)):
                print(f"  [SKIP] 선진국({country}) 저중복 이슈 ({cur_count}건 < {CLUSTER_MIN_SIZE_ADVANCED}건) — 프론티어마켓 편집방향상 제외\n")
                continue

            probe_title = titles[0][:80] if titles else ""
            similar_existing, sim_score = find_similar_article(probe_title, today_own_articles) if probe_title else (None, 0)

            if similar_existing:
                print(f"  → 유사 기존 기사 발견 (유사도 {sim_score}%) → 병합 업데이트: {similar_existing.get('title_ko','')[:40]}")
                existing_full = get_article_by_id(similar_existing["id"])
                existing_summary = existing_full.get("summary_ko") if existing_full else None

                prompt = build_issue_prompt(cluster, existing_summary) if existing_summary else build_issue_prompt(cluster)
                has_full = any(a.get("full_text") for a in cluster)
                content = call_gemini_article(prompt, max_tokens=4000 if has_full else 1500)

                if content:
                    gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment, gen_keyword_ko, gen_keyword_en = parse_title_and_body(content)
                    gen_body = _ensure_paragraphs(gen_body)
                    new_title = gen_title if gen_title else probe_title
                    note = generate_update_note(existing_summary, gen_body or _strip_leaked_labels(content))
                    update_article(similar_existing["id"], new_title, gen_body or _strip_leaked_labels(content), note=note, countries=gen_countries if gen_countries else None, country=gen_country or "", summary_3lines=gen_summary3 or None, investment_idea=gen_investment or None)
                    prev_count = existing_full.get("score", 0) if existing_full else 0
                    update_article_count(similar_existing["id"], max(prev_count, cur_count) + 1)
                    save_article_keywords(similar_existing["id"], gen_keyword_ko, gen_keyword_en)
                    if gen_country or gen_category or gen_travel:
                        update_fields = {}
                        if gen_country:
                            norm_country = normalize_country(gen_country)
                            update_fields["country"] = norm_country
                            update_fields["region"] = country_to_region(norm_country)
                        if gen_category:
                            update_fields["category"] = gen_category
                            if gen_category == "글로벌" and not gen_country:
                                update_fields["region"] = "global"
                        if gen_travel:
                            update_fields["is_travel"] = True
                        if update_fields:
                            update_article_fields(similar_existing["id"], update_fields)
                    print(f"  ✅ 병합 완료: {new_title}\n")
                    updated += 1
                    send_to_newsfinal_channel(similar_existing["id"], new_title, gen_body or _strip_leaked_labels(content), is_update=True)
                else:
                    print(f"  ❌ 병합 실패\n")
                time.sleep(CALL_INTERVAL)
                processed += 1
                continue

            body_excerpt = " ".join(
                (a.get("full_text") or a.get("summary_ko") or a.get("summary_en") or "")[:300]
                for a in cluster[:2]
            )
            continuing = find_continuing_story(probe_title, body_excerpt, country) if probe_title else None

            if continuing:
                print(f"  → 후속 보도로 판단(이전: {continuing['title_ko'][:40]}) → 참조해서 새 기사 작성")
                prompt = build_issue_prompt(cluster, continuation_title=continuing["title_ko"],
                                            continuation_summary=continuing["summary_ko"])
            else:
                print(f"  → 신규 이슈 기사 생성")
                prompt = build_issue_prompt(cluster)
            has_full = any(a.get("full_text") for a in cluster)
            content = call_gemini_article(prompt, max_tokens=4000 if has_full else 1500)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment, gen_keyword_ko, gen_keyword_en = parse_title_and_body(content)
                gen_body = _ensure_paragraphs(gen_body)
                full_title = gen_title if gen_title else titles[0][:50]

                final_country = normalize_country(gen_country or country)
                final_category = gen_category or category or "종합"
                final_region = country_to_region(final_country) if final_country else (cluster[0].get("region") or "global")
                if final_category == "글로벌" and not final_country:
                    final_region = "global"

                # published 기본값. 아래 조건들이 하나도 걸리지 않는 정상 경로에서
                # 미정의 상태가 되어 UnboundLocalError가 나던 버그를 막는다.
                published = True

                needs_review = any(a.get("__needs_review__") for a in cluster)
                if needs_review:
                    published = False
                    final_subcategory = "needs_review"
                else:
                    final_subcategory = cluster_key

                similar, sim_score = find_similar_article(full_title, today_own_articles, body=gen_body)
                if similar:
                    print(f"  ⚠️ 유사 기사 재발견 (유사도 {sim_score}%) → 미발행으로 저장: {similar.get('title_ko','')[:40]}")
                    published = False

                _dg_reason = ""
                if published and not verify_single_topic(full_title, gen_body or _strip_leaked_labels(content)):
                    print(f"  ❌ 검수 실패 (복수 토픽) → 미발행으로 저장: {full_title[:50]}")
                    published = False
                    _dg_reason = "복수 토픽 혼입 — 무관한 사건이 한 기사에 묶임"

                if published:
                    _dg_bad, _dg_reason = check_date_hallucination(
                        gen_body or _strip_leaked_labels(content), cluster, now_kst().date())
                    if _dg_bad:
                        print(f"  ⚠️ [{_dg_reason}] → 미발행으로 저장")
                        published = False

                if published:
                    _fl = detect_foreign_leftover(gen_body or _strip_leaked_labels(content))
                    if _fl:
                        print(f"  ⚠️ [번역 안 된 외국어 잔존: {_fl}] → 미발행으로 저장")
                        published = False
                        _dg_reason = f"번역 누락 — 외국어 잔존: {_fl}"

                image_url, image_credit = fetch_article_image(
                    full_title, gen_body or _strip_leaked_labels(content), gen_keyword_en
                ) if published else ("", "")

                article_id = save_article(
                    unpub_reason  = _dg_reason,
                    title_ko      = full_title,
                    summary_ko    = gen_body or _strip_leaked_labels(content),
                    cluster_key   = final_subcategory if 'final_subcategory' in dir() else cluster_key,
                    category      = final_category,
                    region        = final_region,
                    country       = final_country,
                    summary_3lines = gen_summary3,
                    investment_idea = gen_investment,
                    article_count = cur_count,
                    published     = published,
                    countries     = gen_countries,
                    image_url     = image_url,
                    image_credit  = image_credit,
                    is_travel     = gen_travel,
                    continuation_of_id = continuing["id"] if (continuing and published) else None,
                )
                if article_id > 0:
                    status = "✅ 저장 완료" if published else "📋 미발행 저장"
                    print(f"  {status} (id={article_id}): {full_title}\n")
                    save_article_keywords(article_id, gen_keyword_ko, gen_keyword_en)
                    if published:
                        today_own_articles.append({"id": article_id, "title_ko": full_title})
                        generated += 1
                        send_to_newsfinal_channel(article_id, full_title, gen_body or _strip_leaked_labels(content), is_update=False)
                        detect_and_register_companies(full_title, gen_body or _strip_leaked_labels(content), final_country)
                else:
                    print(f"  ⚠️ 저장 실패\n")
            else:
                print(f"  ❌ 생성 실패\n")

        time.sleep(CALL_INTERVAL)
        processed += 1

    # ── 단독 기사화 ──
    # 공식 소스(OFFICIAL_SOURCE_NAMES)는 선진국 제외 요건을 우회 — 백악관/
    # 연준/삼성 뉴스룸 같은 1차 발표는 그 자체로 원천 사실이라 다른 매체가
    # 받아쓸 때까지(선진국 요건상 사실상 무기한) 기다릴 필요가 없다.
    #
    # 2026-09-07 도입(사용자 지적: "미술쪽 기사는 좀 올라왔나?" → 소스는
    # 추가했는데 발행 0건). 원인 두 가지를 같이 고친다:
    # (b) Dezeen·designboom은 크롤러가 본문(full_text)을 아예 못 가져온다
    #     (사이트 구조상 RSS 요약이 사실상 유일한 재료) — 이런 소스는 RSS
    #     요약(summary_en)만으로도 단독기사화를 허용한다.
    # (a) 그렇게 자격을 얻어도 매 실행 상위 5건에 물량이 훨씬 많은 프론티어
    #     마켓 뉴스가 항상 먼저 채워 순번이 영영 안 온다 — 문화·예술 소스에
    #     최소 쿼터를 별도로 확보한다.
    CULTURE_SOURCE_NAMES = {
        "Variety", "IndieWire", "Pitchfork", "Playbill", "ArchDaily", "Dezeen", "designboom",
        "My Modern Met", "Artlyst", "Creative Boom", "Booooooom", "Art in America", "Juxtapoz",
        "BBC Culture", "Guardian Culture", "LitHub", "Paris Review",
    }
    CULTURE_QUOTA = 2  # 단독기사 슬롯(5개) 중 문화·예술 소스에 배정하는 최소 개수

    def _has_enough_material(a):
        ft = a.get("full_text") or ""
        if len(ft) >= 1000:
            return True
        if not ft and (a.get("source") or "") in CULTURE_SOURCE_NAMES:
            return len((a.get("summary_en") or "")) >= 200
        return False

    solo_candidates = [
        a for a in all_articles
        if _has_enough_material(a)
        and not is_multi_topic_title(a.get("title_en","") or a.get("title_ko",""))
        and not is_multi_topic_body(a.get("full_text","") or a.get("summary_en",""))
        and ((a.get("country") or "") not in ADVANCED_ECONOMIES
             or (a.get("source") or "") in OFFICIAL_SOURCE_NAMES
             or (a.get("source") or "") in CULTURE_SOURCE_NAMES)
    ]

    multi_topic_skipped = [
        a for a in all_articles
        if len(a.get("full_text") or "") >= 1000
        and is_multi_topic_title(a.get("title_en","") or a.get("title_ko",""))
    ]
    if multi_topic_skipped:
        print(f"  [파킹] 복수 주제 제목 {len(multi_topic_skipped)}건 → DB 파킹")
        parked_count = park_multi_topic_articles(multi_topic_skipped)
        print(f"  [파킹] {parked_count}개 토픽 파킹 완료")

    print(f"\n[단독 기사] 원문 충분한 기사 {len(solo_candidates)}건")

    _culture_pool = [a for a in solo_candidates if (a.get("source") or "") in CULTURE_SOURCE_NAMES]
    _other_pool = [a for a in solo_candidates if (a.get("source") or "") not in CULTURE_SOURCE_NAMES]
    _picked_culture = _culture_pool[:CULTURE_QUOTA]
    solo_selected = _picked_culture + _other_pool[:5 - len(_picked_culture)]
    if _picked_culture:
        print(f"  [쿼터] 문화·예술 소스 {len(_picked_culture)}건 확보")

    solo_generated = 0
    for a in solo_selected:
        if processed >= MAX_CLUSTERS_PER_RUN + 5:
            break

        title = a.get("title_ko") or a.get("title_en") or ""
        url = f"solo_{a.get('id')}"
        cluster_key = f"solo_{now_kst().strftime('%Y%m%d')}_{hashlib.md5(title.encode()).hexdigest()[:8]}"

        existing = get_existing_cluster(cluster_key)
        if existing:
            continue

        print(f"  → 단독 기사 생성: {title[:60]}")

        rules = load_prompt("writer_rules", fallback="""[주의사항]
- 반드시 하나의 토픽(사건/이슈)만 다루는 기사를 작성하세요.
- 본문 앞에 [도시명], [날짜] 같은 전문 형식 헤더를 붙이지 마세요.
- 마크다운 문법(**굵게**, ##제목 등)을 사용하지 마세요.
- 매체 홍보성 내용(구독 유도, 텔레그램 채널 안내 등)은 포함하지 마세요.
- 날짜 표기는 "2일(현지시간)" 형식으로 간결하게 쓰세요.
- "2026년 6월 24일 현재", "오늘", "현재" 등 절대 날짜를 본문에 쓰지 마세요.
- 기사 문체로 작성하세요. 논평/칼럼 문체는 금지입니다.
- 모든 인명·지명은 반드시 한글로 음차하세요. 키릴 문자, 아랍 문자 등 비라틴 문자를 그대로 쓰지 마세요.
- 영화·도서·게임 등 작품 제목은 소스 기사에 등장한 원어 표기를 정확히 확인해서만 쓰세요. 공식 한국어 제목을 확신할 수 없으면 새 표현을 지어내지 말고, 원어 제목을 괄호로 병기하거나(예: "브랜드 뉴 데이(Brand New Day)") 원어 그대로 쓰세요.
""" + JSON_OUTPUT_SPEC)

        similar_existing, pre_sim_score = find_similar_article(title, today_own_articles) if title else (None, 0)

        if similar_existing:
            print(f"  → 유사 기존 기사 발견 (유사도 {pre_sim_score}%) → 병합 업데이트: {similar_existing.get('title_ko','')[:40]}")
            existing_full = get_article_by_id(similar_existing["id"])
            existing_summary = existing_full.get("summary_ko") if existing_full else None

            merge_template = """당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
기존 기사에 새로 들어온 관련 기사를 반영해 업데이트하세요. ({today_str})

[기존 기사]
{existing_summary}

[새로 들어온 원문 — {source}]
{full_text}

새로 들어온 기사의 팩트를 기존 기사에 자연스럽게 통합해 완성도 높은 기사로 다시 써주세요.
팩트(수치, 인명, 날짜, 기관명)를 최대한 살리고, 한국어로 작성하세요.
{rules}

{json_spec}"""

            prompt = merge_template.format(
                today_str=now_kst().strftime('%Y년 %m월 %d일'),
                existing_summary=existing_summary or "",
                source=a.get('source', ''),
                full_text=a.get('full_text', ''),
                rules=rules,
                json_spec=JSON_OUTPUT_SPEC,
            )
            content = call_gemini_article(prompt, max_tokens=4000)

            if content:
                gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment, gen_keyword_ko, gen_keyword_en = parse_title_and_body(content)
                gen_body = _ensure_paragraphs(gen_body)
                new_title = gen_title if gen_title else title[:50]
                existing_sum = existing_full.get("summary_ko") if existing_full else None
                note = generate_update_note(existing_sum, gen_body or _strip_leaked_labels(content))
                update_article(similar_existing["id"], new_title, gen_body or _strip_leaked_labels(content), note=note, countries=gen_countries if gen_countries else None, country=gen_country or "", summary_3lines=gen_summary3 or None, investment_idea=gen_investment or None)
                prev_count = existing_full.get("score", 0) if existing_full else 0
                update_article_count(similar_existing["id"], prev_count + 1)
                save_article_keywords(similar_existing["id"], gen_keyword_ko, gen_keyword_en)
                if gen_country or gen_category or gen_travel:
                    update_fields = {}
                    if gen_country:
                        norm_country = normalize_country(gen_country)
                        update_fields["country"] = norm_country
                        update_fields["region"] = country_to_region(norm_country)
                    if gen_category:
                        update_fields["category"] = gen_category
                        if gen_category == "글로벌" and not gen_country:
                            update_fields["region"] = "global"
                    if gen_travel:
                        update_fields["is_travel"] = True
                    if update_fields:
                        update_article_fields(similar_existing["id"], update_fields)
                print(f"  ✅ 단독 병합 완료: {new_title}\n")
                updated += 1
                send_to_newsfinal_channel(similar_existing["id"], new_title, gen_body or _strip_leaked_labels(content), is_update=True)
            else:
                print(f"  ❌ 단독 병합 실패\n")
            time.sleep(CALL_INTERVAL)
            continue

        template = load_prompt("writer_solo", fallback="""당신은 프론티어 미디어 NewsFinal의 수석 에디터입니다.
아래는 {source}의 원문 기사입니다. ({today_str})
국가: {country} | 분야: {category}

[원문]
{full_text}

원문의 팩트(수치, 인명, 날짜, 기관명, 구체적 내용)를 빠짐없이 살려서 한국어 기사로 작성하세요.
원문이 길면 기사도 충분히 길게 쓰세요. 억지로 줄이지 마세요.
{rules}""")

        prompt = template.format(
            source=a.get('source', ''),
            today_str=now_kst().strftime('%Y년 %m월 %d일'),
            country=a.get('country', ''),
            category=a.get('category', ''),
            full_text=a.get('full_text', ''),
            rules=rules,
        )

        content = call_gemini_article(prompt, max_tokens=4000)
        if content:
            gen_title, gen_body, gen_country, gen_category, gen_countries, gen_travel, gen_summary3, gen_investment, gen_keyword_ko, gen_keyword_en = parse_title_and_body(content)
            gen_body = _ensure_paragraphs(gen_body)
            full_title = gen_title if gen_title else title[:50]

            final_country = normalize_country(gen_country or a.get("country") or "")
            final_category = gen_category or a.get("category") or "종합"
            final_region = country_to_region(final_country) if final_country else (a.get("region") or "global")
            if final_category == "글로벌" and not final_country:
                final_region = "global"

            if not verify_single_topic(full_title, gen_body or _strip_leaked_labels(content)):
                print(f"  ❌ 검수 실패 (복수 토픽) — 파킹: {full_title[:50]}")
                park_multi_topic_articles([{"title_en": full_title, "full_text": gen_body or _strip_leaked_labels(content),
                    "country": final_country, "category": final_category, "region": final_region}])
                time.sleep(CALL_INTERVAL)
                continue

            similar, sim_score = find_similar_article(full_title, today_own_articles, body=gen_body)
            if similar:
                print(f"  ⚠️ 유사 기사 재발견 (유사도 {sim_score}%) → 미발행으로 저장")
                published = False
            else:
                published = True

            _dg_reason = ""
            if published:
                _dg_bad, _dg_reason = check_date_hallucination(
                    gen_body or _strip_leaked_labels(content), [a], now_kst().date())
                if _dg_bad:
                    print(f"  ⚠️ [{_dg_reason}] → 미발행으로 저장")
                    published = False

            if published:
                _fl = detect_foreign_leftover(gen_body or _strip_leaked_labels(content))
                if _fl:
                    print(f"  ⚠️ [번역 안 된 외국어 잔존: {_fl}] → 미발행으로 저장")
                    published = False
                    _dg_reason = f"번역 누락 — 외국어 잔존: {_fl}"

            image_url, image_credit = "", ""
            if published:
                # 2026-09-07 도입(사용자 지적: "미술쪽 글은 이미지가 중요...
                # 특정 그림을 다루는 글은 그 그림의 사진을 넣어야 한다는 거야.
                # 반드시"). ArchDaily·Dezeen·My Modern Met 등은 그 기사가
                # 다루는 실제 대상(신축 건물·신작) 사진을 RSS 자체에 이미
                # 신고 있다(rss_fetcher.py의 extract_rss_image()가 수집).
                # 이런 대상은 애초에 Wikimedia/Pixabay 일반 검색으로는 나올
                # 수 없어(공개 라이선스 저장소에 있을 리 없는 신작·신축물)
                # 출처가 준 실제 사진을 최우선으로 쓴다.
                _src_image = a.get("image_url") or ""
                if _src_image:
                    _stored = store_image(_src_image, key_hint=f"rss_{a.get('id')}")
                    if _stored:
                        image_url = _stored
                        image_credit = f"사진: {a.get('source', '')}"
                if not image_url:
                    image_url, image_credit = fetch_article_image(
                        full_title, gen_body or _strip_leaked_labels(content), gen_keyword_en
                    )

            article_id = save_article(
                unpub_reason=_dg_reason,
                title_ko=full_title,
                summary_ko=gen_body or _strip_leaked_labels(content),
                cluster_key=cluster_key,
                category=final_category,
                region=final_region,
                country=final_country,
                summary_3lines=gen_summary3,
                investment_idea=gen_investment,
                article_count=1,
                published=published,
                countries=gen_countries,
                image_url=image_url,
                image_credit=image_credit,
                is_travel=gen_travel,
            )
            if article_id > 0:
                status = "✅ 단독 저장" if published else "📋 단독 미발행"
                print(f"  {status} (id={article_id}): {full_title}\n")
                save_article_keywords(article_id, gen_keyword_ko, gen_keyword_en)
                if published:
                    today_own_articles.append({"id": article_id, "title_ko": full_title})
                    solo_generated += 1
                    send_to_newsfinal_channel(article_id, full_title, gen_body or _strip_leaked_labels(content), is_update=False)
                    detect_and_register_companies(full_title, gen_body or _strip_leaked_labels(content), final_country)
        time.sleep(CALL_INTERVAL)

    print(f"✅ 완료 — 클러스터 {generated}건 생성 / {updated}건 업데이트 / 단독 {solo_generated}건 생성")

    # 라이브 기사 능동적 업데이트
    update_live_articles()


if __name__ == "__main__":
    run()
