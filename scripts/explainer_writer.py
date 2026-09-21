"""
scripts/explainer_writer.py
-------------------------
해설 기사 초안 자동 작성 — 어드민 검토 후 발행(자동 발행 안 함).

2026-09-22 신설. 사용자 요청: "뉴스파이널 콘텐츠를 좀 더 다양하게" → 애드센스
"가치가 별로 없는 콘텐츠" 대응으로 해설 기사부터 시험 발행 → "제미나이가 해설
기사도 만들 수 있어?" → "응 검토 화면 방식으로 시작해".

설계(사람이 직접 썼을 때 품질을 만든 게 글쓰기가 아니라 검증이라는 판단):
  1. 자료: 이미 발행된 NewsFinal 기사 본문 + 배경 정보 두 갈래
     ① 뉴스파이널 기발행 관련 기사(최근 30일, related_archive) — 이미 검증해 낸 사실이라 우선
     ② fetch_background_context() 외부 배경(NVIDIA 우선 → 검색 그라운딩 폴백 + 교차검증) — 검증 필요 표시
  2. 작성: Gemini는 그 자료에 있는 사실만으로 쓴다(프롬프트는 DB prompts.explainer_rules).
  3. 검증: 본문의 모든 수치가 자료에 있는지 결정론적으로 대조(숫자 오류 방지 —
     "지연은 괜찮지만 숫자가 틀리면 절대 안돼"), 고유명사 날조·문체·단일주제 검사는
     자체 off_topic() 사용. call_gemini_article()·verify_single_topic()은 쓰지 않는다 — 앞의 것은 스트레이트
     뉴스용 문체·고유명사 검사가 해설체("~로 풀이된다")를 논평으로 오탐하고 기본 lite 모델(tier 4)이라 품질도
     낮으며, 뒤의 것은 max_tokens=5라 lite 모델이 항상 MAX_TOKENS로 실패해 "판정 불가→통과"로 사실상 꺼져
     있다(2026-09-22 로컬 시험에서 확인, 20토큰 이상이면 정상 판정).
  4. 저장: is_published=False, subcategory='이슈파이널' → 어드민 "📝 이슈 파이널 검토"에서 승인.

하루 상한(DAILY_CAP)과 기사당 1회 작성(url=internal://explainer_{원기사id})으로 물량을 묶는다.
실행: python scripts/explainer_writer.py
"""

import os
import re
import requests
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

from gemini_writer import (now_kst, load_prompt, _gemini_client)
from gemini_summarizer import fetch_background_context
from style_guard import ensure_paragraphs, has_polite_ending
from article_store import insert_final_article, sb_headers, sb_url

try:
    from nvidia_client import call_nvidia
except Exception:
    call_nvidia = None

LABEL = "[이슈 파이널]"        # 제목 말머리 — 사용자 결정(2026-09-22)
SUBCATEGORY = "이슈파이널"    # 서브카테고리(칩·어드민 필터). "_" 금지 — 있으면 내부 키로 보고 칩이 숨겨진다
DAILY_CAP = 1               # 하루(KST) 신규 초안 수
CANDIDATE_WINDOW_HOURS = 48
MIN_BASE_LEN = 1200         # 원 기사가 이보다 짧으면 해설할 재료가 부족
MAX_RETRY = 2
COPY_LIMIT = 0.3           # 원 기사 문장을 그대로 옮긴 비율 상한

_FALLBACK_RULES = """아래 [기사]와 [배경 정보]에 있는 사실만으로 해설 기사를 쓰세요. 자료에 없는 수치·인명·인용은 쓰지 마세요.
TITLE: <제목>
BODY: <본문>

[기사]
{base_title}
{base_body}

[배경 정보]
{background}

Output:"""

# 한국어 수 표기("9만3,873", "1억 2천만", "25만3725")를 하나의 값으로 환산해 대조한다. 조각 숫자로 쪼개 비교하면
# 모델이 "9만3873"을 "93만873"으로 옮겨도 조각("93", "873")이 우연히 자료에 있어 통과한다
# (2026-09-22 주간 정리 시험에서 실제로 통과했음 — 자릿수가 통째로 틀린 수치).
_NUM_SPAN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?(?:\s*[조억만천](?:\s*\d[\d,]*(?:\.\d+)?)?)*")
_UNIT = {"조": 10 ** 12, "억": 10 ** 8, "만": 10 ** 4, "천": 10 ** 3}


def _span_value(span: str) -> float:
    total, cur = 0.0, None
    for tok in re.findall(r"\d[\d,]*(?:\.\d+)?|[조억만천]", span):
        if tok in _UNIT:
            total += (cur if cur is not None else 1) * _UNIT[tok]
            cur = None
        else:
            if cur is not None:      # 단위 없이 이어진 수(예: 9만 3873의 3873)는 그대로 더한다
                total += cur
            cur = float(tok.replace(",", ""))
    return total + (cur or 0)


def _numbers(text: str) -> set:
    return {round(_span_value(m.group(0)), 4) for m in _NUM_SPAN_RE.finditer(text or "")}


def unsupported_numbers(body: str, facts: str) -> list:
    """본문에 있는데 자료(기사+배경)에는 없는 수치(환산값 기준). 한 자리 정수는 열거·서수 등
    오탐이 많아 제외한다.
    ponytail: 값 일치만 본다 — 자료 속 다른 맥락의 같은 값이 우연히 일치하면 못 잡는다.
    필요해지면 수치 주변 단어까지 대조하도록 강화."""
    known = _numbers(facts)
    bad = [v for v in _numbers(body) - known if v >= 10 or v != int(v)]
    return sorted(f"{v:,.0f}" if v == int(v) else f"{v:g}" for v in bad)


def unsupported_claims(body: str, facts: str) -> str:
    """계열이 다른 모델(NVIDIA)에게 "자료에서 뒷받침되지 않는 문장"만 골라내게 한다.
    수치 대조·고유명사 검사가 못 잡는 서술형 날조(예: 자료에 없는 정책 배경 한 줄)용.
    문제 없으면 "", 있으면 해당 문장들. NVIDIA 미설정·실패 시 "" (초안이 사람 검토를 거치므로 fail-open)."""
    if not call_nvidia:
        return ""
    prompt = ("아래 [자료]와 [기사]를 비교하세요. [기사]의 문장 중 [자료]에 근거가 없거나 [자료]와 다른 "
              "사실 주장(수치·인물·기관·발언·원인)이 담긴 문장만 그대로 나열하세요. 해석·분석·전망 문장은 "
              "그 근거가 [자료]에 있으면 제외하고, 근거 없이 결론만 단정하면 나열하세요. 표현을 바꿨을 뿐 "
              "자료에서 뒷받침되면 제외하세요. 문제가 없으면 정확히 OK 한 단어만 출력하세요.\n\n"
              f"[자료]\n{facts[:6000]}\n\n[기사]\n{body[:4000]}")
    try:
        resp = (call_nvidia(prompt, max_tokens=500) or "").strip()
    except Exception:
        return ""
    return "" if not resp or resp.upper().startswith("OK") else resp[:600]


def off_topic(title: str, body: str) -> str:
    """제목 사안과 무관한 문장(스포츠 결과·다른 사건 등)을 골라낸다. trend_ 기사 본문에 무관한 소식이 섞여 있어
    해설이 그걸 따라 쓰는 경우(2026-09-22 시험: 아이티 갱단 해설 끝에 축구 클럽 소식)를 막는다.
    max_tokens는 넉넉히(lite 모델은 생각 토큰까지 출력 한도에 포함돼 작으면 MAX_TOKENS로 실패). 판정 실패 시 ""(통과)."""
    prompt = ("아래 해설 기사에서 제목이 가리키는 핵심 사안과 직접 관련 없는 소식(예: 스포츠 경기 결과, 다른 나라·다른 사건)이 "
              "담긴 문장만 그대로 나열하세요. 같은 사안의 배경·영향·전망·국제사회 대응은 관련 있는 내용입니다. "
              "관련 없는 문장이 없으면 정확히 OK 한 단어만 출력하세요.\n\n"
              f"제목: {title}\n본문:\n{body[:4000]}")
    try:
        resp = (_gemini_client.call(prompt, max_tokens=800, start_tier=4, temperature=0.2, timeout=(10, 60)) or "").strip()
    except Exception:
        return ""
    return "" if not resp or resp.upper().startswith("OK") else resp[:500]


def copied_ratio(body: str, base_body: str) -> float:
    """본문 문장 중 원 기사에 그대로 들어있는 비율. 해설은 원 기사를 재구성해야 하는데
    (2026-09-22 시험 작성에서 Gemini가 원 기사를 거의 그대로 되돌려줌) 이 검사가 없으면
    수치 대조도 복사본이라 통과해 버린다."""
    norm = lambda t: re.sub(r"\s+", "", t or "")
    src = norm(base_body)
    sents = [x for x in re.split(r"(?<=[.!?다])\s+", body or "") if len(norm(x)) >= 20]
    if not sents:
        return 0.0
    return sum(1 for x in sents if norm(x) in src) / len(sents)


def _parse(text: str):
    if not text:
        return "", ""
    mt = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    mb = re.search(r"BODY:\s*(.+)$", text, re.S)
    return (mt.group(1).strip() if mt else ""), (mb.group(1).strip() if mb else "")


ARCHIVE_DAYS = 30           # 관련 기발행 기사를 찾는 기간
ARCHIVE_LIMIT = 4           # 배경으로 붙일 관련 기사 수
ARCHIVE_BODY_CHARS = 700    # 기사당 본문 발췌 길이(앞부분이 사실 요약)
# 사건 유형 동사·범용어는 "같은 사안" 신호가 못 된다(예: 나이지리아+별세만 겹쳐도 다른 사건). ponytail: 목록식 휴리스틱 —
# 오탐이 보이면 어휘를 더 넣거나 제목 임베딩 유사도로 교체.
_TITLE_STOP = {"관련", "이후", "가운데", "속에", "따른", "대한", "위한", "통해", "지난", "올해", "오늘", "발표", "전망", "논의",
               "별세", "발생", "개막", "확산", "체결", "단행", "개편", "표명", "시도", "격상", "우려", "지지", "결정", "합의", "강화", "재개"}


def _title_tokens(title: str) -> list:
    """제목에서 검색용 핵심어(말머리 제외, 2자 이상 한글·영문·숫자 혼합어). 앞쪽이 주제어인 경우가 많아 6개까지."""
    t = re.sub(r"^\s*\[[^\]]*\]\s*", "", title or "")
    toks = [x for x in re.findall(r"[가-힣A-Za-z0-9]{2,}", t) if x not in _TITLE_STOP and not x.isdigit()]
    return toks[:6]


def _body_only(text: str) -> str:
    """갱신 기사의 [업데이트] 블록을 걷어내고 원 본문만(사실 요약이 앞에 있음)."""
    t = (text or "").lstrip()
    if t.startswith("[업데이트]") and "\n────────" in t:
        t = t.split("\n────────", 1)[1].lstrip("\n")
    return t


def related_archive(base: dict) -> list:
    """뉴스파이널이 이미 발행한 관련 기사(최근 ARCHIVE_DAYS일). 이 사이트가 이미 검증해 낸 사실이라
    모델 기억(NVIDIA)·검색 결과보다 환각 위험이 낮은 배경 자료다 — "이 사안의 이전 전개"를 채운다.
    제목 핵심어 2개 이상 겹치는 것만(우연한 겹침 배제). 이슈파이널·날씨·다이제스트는 제외."""
    toks = _title_tokens(base.get("title_ko") or "")
    if not toks:
        return []
    since = (now_kst() - timedelta(days=ARCHIVE_DAYS)).strftime("%Y-%m-%d %H:%M")
    params = {
        "select": "id,title_ko,summary_ko,created_at,subcategory",
        "source": "eq.NewsFinal", "is_published": "eq.true", "id": f"neq.{base.get('id', 0)}",
        "created_at": f"gte.{since}",
        "or": "(" + ",".join(f"title_ko.ilike.*{t}*" for t in toks) + ")",
        "order": "created_at.desc", "limit": "60",
    }
    res = None
    for _ in range(2):  # 일시 오류(타임아웃 등)는 1회 재시도
        try:
            res = requests.get(sb_url(), headers=sb_headers(), params=params, timeout=30)
            if res.status_code in (200, 206):
                break
        except requests.RequestException as e:
            print(f"  ⚠️ 관련 기사 조회 예외: {e}")
    if res is None or res.status_code not in (200, 206):
        print(f"  ⚠️ 관련 기사 조회 실패 status={getattr(res, 'status_code', None)}")
        return []
    need = 2 if len(toks) >= 2 else 1
    scored = []
    for a in res.json() or []:
        sub = a.get("subcategory") or ""
        if sub.startswith(("이슈파이널", "weather", "digest_")):
            continue
        hit = sum(1 for t in toks if t in (a.get("title_ko") or ""))
        if hit >= need:
            scored.append((hit, a.get("created_at") or "", a))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [a for _, _, a in scored[:ARCHIVE_LIMIT]]


def archive_block(items: list) -> str:
    parts = []
    for a in items:
        date = (a.get("created_at") or "")[:10]
        parts.append(f"- ({date} 발행) {a.get('title_ko')}\n{_body_only(a.get('summary_ko'))[:ARCHIVE_BODY_CHARS]}")
    return "[관련 기사 — 뉴스파이널 기발행, 그 이후 상황이 바뀌었을 수 있음]\n" + "\n\n".join(parts) if parts else ""


def _generate(prompt: str):
    """해설 본문 생성. 고품질 모델(tier 0=가장 앞선 flash)부터, 길이가 긴 응답(생각 토큰 포함)이라 출력 한도·읽기
    타임아웃을 넉넉히 준다 — 기본값(lite, 3000토큰, 30초)에서는 MAX_TOKENS로 잘려 모든 모델이 소진됐다.
    하루 1~3회 호출이라 쿼터 부담은 없고, 소진되면 클라이언트가 다음 모델로 내려간다."""
    return _gemini_client.call(prompt, max_tokens=8000, start_tier=0, temperature=0.5, timeout=(10, 120))


def _clean_placeholders(title: str, body: str):
    """모델이 출력 형식의 자리표시자("<제목>", "<본문>")를 그대로 옮기는 경우를 벗겨낸다.
    제목이 자리표시자 자체면 빈 문자열을 돌려줘 재시도하게 한다."""
    if re.search(r"<[^>\n]{1,40}>", title or ""):
        return "", body
    body = re.sub(r"^\s*<[^>\n]{1,40}>\s*", "", body or "")
    return title, body


def write_explainer(base: dict):
    """base 기사 dict → (title, body, background) 또는 None. DB 접근은 related_archive()뿐."""
    title0 = base.get("title_ko") or ""
    body0 = base.get("summary_ko") or ""
    archive = related_archive(base)
    print(f"  관련 기발행 기사 {len(archive)}건 배경으로 사용")
    external = fetch_background_context(title0, source_context=f"{title0}\n{body0[:400]}")
    parts = [p for p in (archive_block(archive),
                         "[외부 배경 지식 — 모델 지식·검색 기반, 검증 필요]\n" + external if external else "") if p]
    if not parts:
        print("  ⚠️ 배경 정보 확보 실패 — 원 기사만으로는 해설 재료 부족, 스킵")
        return None
    background = "\n\n".join(parts)
    facts = f"{title0}\n{body0}\n{background}"

    rules = load_prompt("explainer_rules", fallback=_FALLBACK_RULES)
    prompt = rules.format(base_title=title0, base_body=body0[:3500], background=background)

    for attempt in range(MAX_RETRY + 1):
        content = _generate(prompt)
        title, body = _parse(content)
        title, body = _clean_placeholders(title, body)
        if not title or not body:
            print("  ❌ 파싱 실패(응답 없음/자리표시자 제목)")
            continue
        body = ensure_paragraphs(body)
        if has_polite_ending(body):
            print(f"  ⚠️ 합쇼체(-습니다) 감지 → 재작성({attempt + 1}/{MAX_RETRY})")
            prompt += "\n\n[재작성 지시] 모든 문장을 '-다'로 끝내세요. '-습니다/-입니다'는 쓰지 마세요."
            continue
        title = f"{LABEL} " + re.sub(r"^\s*\[[^\]]{1,12}\]\s*", "", title)  # 말머리는 코드가 보장(모델이 다른 걸 붙여도 교정)
        ratio = copied_ratio(body, body0)
        if ratio > COPY_LIMIT:
            print(f"  ⚠️ 원 기사 문장 복사 비율 {ratio:.0%} → 재작성({attempt + 1}/{MAX_RETRY})")
            prompt += ("\n\n[재작성 지시] 방금 결과가 원 기사 문장을 그대로 옮겼습니다. 원 기사를 요약·재배열하지 말고, "
                       "배경→쟁점→앞으로 볼 점 순으로 문장을 새로 구성해 해설하세요.")
            continue
        bad = unsupported_numbers(body, facts)
        if bad:
            print(f"  ⚠️ 자료에 없는 수치 감지 {bad} → 재작성({attempt + 1}/{MAX_RETRY})")
            prompt += (f"\n\n[재작성 지시] 방금 결과에 자료에 없는 수치({', '.join(bad)})가 있었습니다. "
                       "자료에 나온 수치만 사용해 다시 작성하세요.")
            continue
        claims = unsupported_claims(body, facts)
        if claims:
            print(f"  ⚠️ 자료에 근거 없는 서술 감지 → 재작성({attempt + 1}/{MAX_RETRY}): {claims[:120]}")
            prompt += ("\n\n[재작성 지시] 방금 결과에 자료에 근거 없는 서술이 있었습니다: "
                       f"{claims}\n해당 내용을 빼고, 자료에 있는 사실만으로 다시 작성하세요.")
            continue
        off = off_topic(title, body)
        if off:
            print(f"  ⚠️ 제목 사안과 무관한 문장 감지 → 재작성({attempt + 1}/{MAX_RETRY}): {off[:120]}")
            prompt += ("\n\n[재작성 지시] 방금 결과에 제목이 가리키는 핵심 사안과 무관한 내용이 있었습니다: "
                       f"{off}\n무관한 소식은 빼고 핵심 사안만 해설하세요.")
            continue
        return title, body, background
    return None


def _candidates():
    since = (now_kst() - timedelta(hours=CANDIDATE_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M")
    res = requests.get(sb_url(), headers=sb_headers(), params={
        "select": "id,title_ko,summary_ko,category,country,region,countries,image_url,image_credit,score,subcategory",
        "source": "eq.NewsFinal", "is_published": "eq.true", "created_at": f"gte.{since}",
        "or": "(subcategory.like.cluster_*,subcategory.like.econ_rate_*,subcategory.like.trend_*)",  # trend_는 이어지는 사안이라 기발행 배경이 풍부
        "order": "created_at.desc", "limit": "60",
    }, timeout=30)
    if res.status_code not in (200, 206):
        print(f"[explainer_writer] 후보 조회 실패 {res.status_code}")
        return []
    out = []
    for a in res.json() or []:
        # trend_ 기사는 본문이 짧은 대신(500~1100자) 기발행 관련 기사가 배경으로 붙어 재료가 충분하다
        min_len = 500 if (a.get("subcategory") or "").startswith("trend_") else MIN_BASE_LEN
        if len(_body_only(a.get("summary_ko"))) < min_len:
            continue
        out.append(a)
    # 클러스터 score는 소스 수가 아니라 1로 고정돼 있어 중요도 지표로 못 쓴다(2026-09-22 실측).
    # ponytail: 일단 본문이 긴(=자료가 풍부한) 순 — 검토자가 거르므로 중요도 점수는 나중에.
    out.sort(key=lambda a: len(a.get("summary_ko") or ""), reverse=True)
    return out


def _already_explained(ids: list) -> set:
    if not ids:
        return set()
    urls = ",".join(f'"internal://explainer_{i}"' for i in ids)
    res = requests.get(sb_url(), headers=sb_headers(),
                       params={"select": "url", "url": f"in.({urls})"}, timeout=15)
    return {r["url"] for r in (res.json() if res.status_code in (200, 206) else [])}


def _drafts_today() -> int:
    today = now_kst().strftime("%Y-%m-%d")
    res = requests.get(sb_url(), headers=sb_headers(), params={
        "select": "id", "source": "eq.NewsFinal", "subcategory": f"like.{SUBCATEGORY}*",
        "created_at": f"like.{today}*"}, timeout=15)
    return len(res.json()) if res.status_code in (200, 206) else DAILY_CAP


def main():
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
        print("[SKIP] SUPABASE 환경변수 없음")
        return
    if _drafts_today() >= DAILY_CAP:
        print(f"[explainer_writer] 오늘 해설 초안 {DAILY_CAP}건 이미 작성 — 스킵")
        return

    cands = _candidates()
    done = _already_explained([a["id"] for a in cands])
    cands = [a for a in cands if f"internal://explainer_{a['id']}" not in done]
    print(f"[explainer_writer] 후보 {len(cands)}건")

    for base in cands[:3]:  # 실패해도 다음 후보로 최대 3건까지만 시도
        print(f"→ 해설 시도: id={base['id']} {base['title_ko'][:50]}")
        result = write_explainer(base)
        if not result:
            continue
        title, body, background = result
        art_id = insert_final_article({
            "title_en": title, "title_ko": title, "summary_en": "", "summary_ko": body,
            "url": f"internal://explainer_{base['id']}", "source": "NewsFinal",
            "category": base.get("category") or "경제", "subcategory": SUBCATEGORY,
            "region": base.get("region") or "글로벌", "country": base.get("country") or "",
            "countries": base.get("countries") or ([base["country"]] if base.get("country") else []),
            "image_url": base.get("image_url") or "", "image_credit": base.get("image_credit") or "",
            "score": 1,
            "created_at": now_kst().strftime("%Y-%m-%d %H:%M"),
            "update_log": [{"timestamp": now_kst().strftime("%Y-%m-%d %H:%M"),
                            "note": f"해설 자동 초안(검토 대기, 원 기사 id={base['id']})"}],
            "source_data": {"explainer_of": base["id"], "background": background},
            "sent_telegram": 0, "is_published": False,
        })
        print(f"  ✅ 초안 저장 id={art_id}" if art_id > 0 else "  ❌ 저장 실패")
        if art_id > 0:
            break


if __name__ == "__main__":
    main()
