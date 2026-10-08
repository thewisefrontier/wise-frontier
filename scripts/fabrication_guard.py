"""
scripts/fabrication_guard.py
--------------------------------
생성된 기사 본문에 원본 자료에 없는 고유명사(작품명·인명·지명·기관명)나
수식어가 새로 지어내졌는지 확인하는 공용 로직.

2026-09-08 감사("공용 모듈이 필요한 시스템이 더 있는지 점검해줘")로 발견:
gemini_writer.py/econ_writer.py/frontier_markets_writer.py/daily_digest.py/
oil_price_writer.py/opinet_price_writer.py/opinet_weekly_writer.py 7개
파일에 이름만 같은 verify_no_fabricated_names()가 각자 복붙돼 있었고,
실제로 서로 다른 버전으로 갈라져 있었다 — gemini_writer.py만 2026-08-25
id=98010 실사고(수식어 날조) 이후 "[2. 수식어 날조]" 탐지가 추가됐는데
나머지 6개는 2026-08-16 id=79327(작품명 오역) 시점 버전에 멈춰 있었다.
article_image.py·style_guard.py와 같은 이유로 가장 발전된(gemini_writer.py)
버전을 기준으로 공용화한다.

call_gemini_fn 인자로 각 스크립트 자신의 call_gemini(prompt, max_tokens=...,
start_tier=...) 래퍼를 주입받는다(article_image.py·style_guard.py와 동일
패턴 — 스크립트마다 GeminiClient 인스턴스·API 키 세트가 다르기 때문).
wikipedia_confirms()는 순수 HTTP 조회라 주입이 필요 없다.

사용:
    from fabrication_guard import verify_no_fabricated_names
    suspect = verify_no_fabricated_names(source_prompt, body, call_gemini)
"""

import re

import requests

try:
    from rapidfuzz import fuzz
except Exception:
    fuzz = None

try:
    from nvidia_client import call_nvidia
except Exception:
    call_nvidia = None


def wikipedia_confirms(name: str, threshold: int = 70) -> bool:
    """이름과 충분히 비슷한 위키 문서 제목이 하나라도 있으면 True (결정론적 조회).

    "한글 음차(원어)" 괄호 병기 표기(writer_rules 표준 형식)를 통짜로 검색하면 ko/en
    어느 쪽과도 안 맞아 거의 항상 실패한다(2026-10-05 실측: "시네스트로(Sinestro)"
    통짜 검색은 ko/en 모두 매치 실패, "Sinestro"만 단독 검색하면 en 위키 정확히 매치 —
    "필리핀 자금세탁방지위원회(AMLC)"도 동일, "AMLC" 단독만 매치. 괄호 안 원어·괄호
    제거한 한글만도 각각 따로 검색해 하나라도 맞으면 확인된 것으로 본다."""
    name = (name or "").strip()
    if not name or fuzz is None:
        return False
    candidates = [name]
    m = re.search(r"\(([^)]+)\)", name)
    if m:
        inner = m.group(1).strip()
        if inner:
            candidates.append(inner)
        outer = re.sub(r"\([^)]*\)", "", name).strip()
        if outer and outer not in candidates:
            candidates.append(outer)
    for cand in candidates:
        titles = []
        for lang in ("ko", "en"):
            try:
                res = requests.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={"action": "opensearch", "search": cand, "limit": 3, "namespace": 0, "format": "json"},
                    headers={"User-Agent": "NewsFinal-EntityCheck/1.0 (+https://newsfinal.co.kr)"},
                    timeout=10,
                )
                if res.status_code == 200:
                    data = res.json()
                    if len(data) >= 2 and isinstance(data[1], list):
                        titles.extend(data[1])
            except Exception:
                continue
        if any(fuzz.token_sort_ratio(cand, t) >= threshold for t in titles):
            return True
    return False


def extract_candidate_names(body: str, call_gemini_fn) -> list:
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
    result = call_gemini_fn(prompt, max_tokens=150, start_tier=4)
    if not result:
        return []
    result = result.strip()
    if not result or ("없음" in result and len(result) <= 12):
        return []
    return [n.strip() for n in result.split(",") if n.strip() and len(n.strip()) >= 2][:15]


def unsupported_claims(body: str, facts: str) -> str:
    """계열이 다른 모델(NVIDIA)에게 "자료에서 뒷받침되지 않는 문장"만 골라내게 한다.
    수치 대조·고유명사 검사가 못 잡는 서술형 날조(예: 자료에 없는 정책 배경 한 줄)용.
    문제 없으면 "", 있으면 해당 문장들. NVIDIA 미설정·실패 시 "" (fail-open).
    2026-09-28 explainer_writer.py에서 이리로 이동 — gemini_writer(스포츠 기사 검증)도
    쓰는데 explainer_writer가 gemini_writer를 import해 순환이 생긴다."""
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


def second_review(title: str, body: str, facts: str) -> tuple:
    """발행 직전 2차 검수(2026-09-28 사용자 지시: 해설·주간 정리 기사는 "한번 더 검수하는 프로세스를 두고 바로 발행").
    글을 쓴 모델(Gemini)과 계열이 다른 NVIDIA가 독립적으로 [자료]만 보고 심사한다. 반환: (통과 여부, 사유).
    NVIDIA 미설정·실패·불분명한 응답이면 (False, 사유) — 발행하지 않고 기존처럼 검토 대기로 남긴다(fail-closed)."""
    if not call_nvidia:
        return False, "2차 검수 불가(NVIDIA 미설정)"
    if not (facts or "").strip() or not (body or "").strip():
        return False, "2차 검수 불가(근거 자료 없음)"
    prompt = ("당신은 뉴스 기사의 최종 검수자입니다. 아래 [자료]에 근거해 [기사]가 그대로 발행돼도 되는지 심사하세요. "
              "다음 중 하나라도 해당하면 불합격입니다.\n"
              "1) [자료]에 없는 사실·수치·날짜·인물·기관·인용이 들어 있다.\n"
              "2) 제목이 본문 또는 [자료]의 내용과 다르거나 과장돼 있다.\n"
              "3) 근거 없는 전망·추측을 사실처럼 단정했다.\n"
              "4) 서로 무관한 사건이 한 기사에 섞여 있다.\n"
              "5) 특정인·집단에 대한 비방·선동·홍보성 표현이 있다.\n"
              "6) 자료·검수 과정을 언급하는 메타 문구(예: '제공된 자료에 따르면', '초안')가 본문에 있다.\n"
              "7) [자료] 안에 '검증 필요'로 표시된 외부 배경 지식 블록에만 근거한 구체적 수치·날짜·인명·기관명이 있다"
              "(일반적인 배경 설명은 허용, 확인되지 않은 구체적 사실은 불합격).\n"
              "합격이면 정확히 PASS 한 단어만, 불합격이면 'FAIL: 이유 한 줄'만 출력하세요.\n\n"
              f"[자료]\n{facts[:8000]}\n\n[제목]\n{title}\n\n[기사]\n{body[:5000]}")
    try:
        resp = (call_nvidia(prompt, max_tokens=300) or "").strip()
    except Exception as e:
        return False, f"2차 검수 실패({str(e)[:40]})"
    if resp.upper().startswith("PASS"):
        return True, "2차 검수 통과"
    if resp.upper().startswith("FAIL"):
        return False, "2차 검수 불합격: " + resp[5:].strip(": ")[:200]
    return False, "2차 검수 응답 불분명"


def _deterministic_source_match(name: str, source_text: str, threshold: int = 85) -> bool:
    """LLM 판단 없이, 이름(괄호 병기 원어 포함)이 원문에 실제로 등장하는지 결정론적으로 1차 확인.
    rapidfuzz로 원문 어딘가에 이 표기(주로 영문 원어)가 있는지 부분 매칭한다. 외부 벤치마크
    (Hallucination_Detection_Benchmark, 2026-10-07 조사)에서 source-grounded 방식이 LLM
    자체판단보다 recall·오탐률 모두 우수하다고 확인돼, 괄호 안 원어가 원문에 그대로 보이는
    가장 확실한 경우는 LLM 호출 없이 먼저 걸러낸다 — wikipedia_confirms()와 같은 "판단이 아닌
    결정론적 신호" 원칙(이 파일 상단 설명)을 원문 대조에도 적용."""
    if fuzz is None or not name or not (source_text or "").strip():
        return False
    m = re.search(r"\(([^)]+)\)", name)
    probe = m.group(1).strip() if m else name
    if len(probe) < 3:
        return False
    return fuzz.partial_ratio(probe, source_text) >= threshold


def names_without_source_support(names: list, source_text: str) -> list:
    """위키에서 못 찾은 이름 중 원문 자료에도 근거가 없는 것만 돌려준다(NVIDIA, 계열이 다른 모델).
    음차·번역·약칭·괄호 병기(예: 구글(Google), 유엔 안전보장이사회 = UN Security Council)는 같은 대상이면 근거 있음으로 본다.
    NVIDIA 미설정·실패·엉뚱한 응답이면 입력을 그대로 돌려준다(예전처럼 보수적으로 보류).
    걸린 이름이 있으면 한 번 더 물어 두 번 다 걸린 것만 남긴다(2026-09-29 사용자 결정): 같은 원문·이름에 [] / [칼리드…]로
    결과가 흔들렸다(정식 전체 이름 확장). 두 번째가 실패·엉뚱한 응답이면 첫 결과를 그대로 쓴다(fail-closed)."""
    if not names:
        return names
    # 결정론적 1차 필터 — 괄호 안 원어가 원문에 그대로 있으면 LLM 호출 없이 바로 통과시킨다.
    remaining = [n for n in names if not _deterministic_source_match(n, source_text)]
    if not remaining:
        return []
    first = _names_without_source_support_once(remaining, source_text)
    if not first or not call_nvidia or not (source_text or "").strip():
        return first
    second = _names_without_source_support_once(first, source_text)
    return [n for n in first if n in second]


def _names_without_source_support_once(names: list, source_text: str) -> list:
    if not names or not call_nvidia or not (source_text or "").strip():
        return names
    prompt = ("아래 [자료]는 기사 작성에 쓰인 원문(주로 외국어)이고, [이름 목록]은 그걸 바탕으로 쓴 한국어 기사에 나온 "
              "고유명사입니다(한글 음차·번역·약칭·괄호 병기 포함). 각 이름이 [자료]에 나오는 대상(같은 인물·기관·기업·매체·작품을 "
              "가리키는 다른 언어 표기 포함)으로 확인되면 제외하고, [자료]에 전혀 근거가 없는 이름만 목록에 적힌 그대로 쉼표로 "
              "나열하세요. 모두 근거가 있으면 정확히 '없음'만 답하세요.\n\n"
              f"[이름 목록]\n{', '.join(names)}\n\n[자료]\n{source_text[:6000]}")
    try:
        resp = (call_nvidia(prompt, max_tokens=300) or "").strip()
    except Exception:
        return names
    if not resp:
        return names
    if resp.startswith("없음") or ("없음" in resp and len(resp) <= 12):
        return []
    flagged = [n for n in names if n in resp]  # 목록에 있던 이름만 인정(엉뚱한 출력 방어)
    return flagged if flagged else names


def verify_no_fabricated_names(source_prompt: str, body: str, call_gemini_fn, wiki: bool = True, facts: str = "") -> str:
    """생성된 본문에 원문 자료에 없는 고유명사(작품명·인명·지명·기관명)가 새로 등장했는지 확인.
    두 신호를 같이 쓴다: ① 원본 자료 대조(Gemini 판단, 기존 방식) ② 위키피디아 독립 조회
    (판단이 아닌 단순 추출 + 결정론적 HTTP 조회 — Gemini가 오판해도 이 신호는 별개로 남는다.
    "LLM 혼자만의 판단은 위험하다" 2026-08-16 피드백 반영).
    실사고(2026-08-16, id=79327): 영화 "Brand New Day"를 "유니온 오브 어 뉴 데이"로
    완전히 잘못 옮김 — 작품 제목은 인명·지명과 달리 음차 규칙이 커버하지 않던 영역이라
    Gemini가 자기 사전지식으로 그럴듯한 제목을 지어냈다. 문제 없으면 빈 문자열, 의심되면
    이름 목록 반환.
    wiki=False면 ② 위키 조회(추출용 lite 호출 1회 포함)를 건너뛴다 — 무명 선수·구단처럼 위키에
    없는 게 정상인 분야용(2026-09-28 실측: 스포츠 미발행 43건 중 약 30건이 [위키 미확인] 오탐).
    그런 호출부는 unsupported_claims()로 원문 대조를 대신 건다.
    facts: 원문만 모은 텍스트(있으면 ①② 두 대조 모두에서 source_prompt 대신 쓴다). source_prompt는
    원문 뒤에 작성 규칙(1만3천자)이 붙어 있어, 그걸 그대로 자르면(예전 [:3000]) 원문 자체가
    더 긴 클러스터는 뒤쪽 소스 기사가 대조 창에서 통째로 빠진다(2026-09-29·2026-10-07) — facts를
    쓰면 규칙 텍스트 없이 원문만 [:6000]까지 확보된다."""
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
{(facts or source_prompt)[:6000]}

[생성된 기사 본문]
{body[:2000]}

답변:"""
    result = call_gemini_fn(check_prompt, max_tokens=150, start_tier=4)
    suspect = ""
    if result:
        result = result.strip()
        if result and not ("없음" in result and len(result) <= 12):
            suspect = result

    if not wiki:
        return suspect
    candidates = extract_candidate_names(body, call_gemini_fn)
    source_text = (facts or source_prompt or "").strip()
    # 2026-10-07 재설계(사용자 지적: "애초에 위키를 너무 믿고 있는게 문제") — 예전엔 위키
    # 미확인 이름만 원문 대조를 거쳐, 위키에 없으면 일단 의심부터 하고 시작하는 구조였다.
    # 프론티어 마켓 기사는 위키에 없는 실존 인물·기관·상품명이 태반이라(2026-09-28에도 같은
    # 문제로 한 차례 완화했으나 2026-10-06 실측 여전히 미발행 사유 1위, 52%) 이 전제 자체가
    # 안 맞았다. 이제 원문이 있으면 위키는 아예 보지 않고 원문 대조(names_without_source_support,
    # 계열이 다른 모델)만으로 판정한다 — "이 이름이 우리가 실제로 쓴 원문에 있는가"가 "세상에
    # 유명한 존재인가"보다 훨씬 적절한 질문이다. 위키 조회는 원문 자체가 없을 때만(드묾) 최후
    # 수단으로 돌아간다.
    if source_text:
        unconfirmed = names_without_source_support(candidates, source_text)
    else:
        unconfirmed = [n for n in candidates if not wikipedia_confirms(n)]
    if unconfirmed:
        note = "[위키 미확인] " + ", ".join(unconfirmed)
        suspect = (suspect + "\n" + note) if suspect else note

    return suspect


_TAG_NAME_RE = re.compile(r"\[(?:이름|수식어)\]\s*(.+?)\s*→")
_KO_SENT_SPLIT = re.compile(r"(?<=다\.)\s+|(?<=다\.[\"'”’])\s+")


def flagged_names(suspect: str) -> list:
    """verify_no_fabricated_names 결과에서 문제 이름/표현만 뽑는다. 못 뽑으면 빈 리스트."""
    out = []
    for line in (suspect or "").split("\n"):
        line = line.strip()
        if line.startswith("[위키 미확인]"):
            out += [n.strip() for n in line[len("[위키 미확인]"):].split(",") if n.strip()]
        else:
            out += [m.strip(" '\"") for m in _TAG_NAME_RE.findall(line)]
    return [n for n in out if 1 < len(n) <= 40]


def drop_flagged_sentences(body: str, title: str, suspect: str, min_len: int = 700, max_drop: int = 3):
    """근거 없는 이름이 든 문장만 지운 본문을 돌려준다(전체 보류 대신 발행하기 위한 보정).
    이름이 제목에 있거나, 이름을 못 뽑았거나, 지울 문장이 max_drop 초과·본문의 25% 초과·min_len 미달이면 None(=기존대로 보류)."""
    names = flagged_names(suspect)
    if not names or not body:
        return None
    keys = []
    for n in names:
        keys.append(n)
        head = n.split("(")[0].strip()
        if len(head) > 1 and head != n:
            keys.append(head)
    if any(k in (title or "") for k in keys):
        return None
    kept, dropped = [], 0
    for para in body.split("\n"):
        if not para.strip():
            kept.append(para)
            continue
        parts = _KO_SENT_SPLIT.split(para)
        good = [x for x in parts if not any(k in x for k in keys)]
        dropped += len(parts) - len(good)
        if good:
            kept.append(" ".join(good))
    new = "\n".join(kept).strip()
    if dropped == 0 or dropped > max_drop or len(new) < min_len or len(new) < len(body) * 0.75:
        return None
    # 리드 문장 자체가 지워져 다음 문장이 "그는/장관은 ~"처럼 앞선 문맥을 전제하는 채로 남으면 보정 실패로
    # 본다(2026-10-04 실사고 id=297527 — 리드 주어였던 이름이 지워져 본문이 "장관은 이번 사태가…"로 시작).
    # 이름이 리드의 둘째 문장 이후에만 있었다면(문맥은 살아있음) 정상적으로 통과한다.
    from style_guard import lead_is_dangling
    if lead_is_dangling(new):
        return None
    return new


def detect_foreign_leftover(body: str, call_gemini_fn) -> str:
    """한국어 기사 본문에 번역 안 된 외국어(스페인어·프랑스어·독일어·튀르키예어 등 라틴 문자 언어)가 남아있는지
    LLM으로 판별한다(gemini_writer.py에서 이식해 트렌드 기사 경로와 공용, 2026-09-29). 있으면 그 단어 문자열, 없으면 ""."""
    if not body:
        return ""
    prompt = f"""아래는 한국어로 작성됐어야 할 뉴스 기사입니다. 번역되지 않고 원문 언어(스페인어·프랑스어·독일어·포르투갈어·이탈리아어·튀르키예어 등) 그대로 남아있는 단어나 구절이 있는지 확인하세요.

⚠️ 이 검사는 영어가 아닌 외국어(스페인어·프랑스어·독일어·포르투갈어·이탈리아어·튀르키예어 등)만 대상입니다. 영어 단어·구절은 설령 한글 번역이나 병기 없이 혼자 쓰였어도 절대 오류로 보지 마세요(인명·기업명·지명·책/영화/음악 등 작품 제목·비자 종류·통화코드·모델명 포함 — 한국 뉴스에서 영어 고유명사·제목은 원어 그대로 쓰는 것이 정상입니다).
⚠️ 오류인 것: 영어가 아닌 외국어의 일반 명사·형용사·부사 등이 한국어로 번역되지 않고 스페인어·독일어·프랑스어·튀르키예어 등 그 외국어 단어 그대로 남아있는 경우.

본문:
{body[:2500]}

번역 안 된 외국어 단어가 있으면 그 단어들만 쉼표로 나열하세요. 없으면 "없음"이라고만 답하세요."""
    result = call_gemini_fn(prompt, max_tokens=60, start_tier=4)
    if not result:
        return ""
    result = result.strip()
    if not result or result[:10].replace(" ", "").startswith("없음"):
        return ""
    return result[:200]


def scrub_extras(suspect: str, *texts):
    """문장을 지운 기사의 3줄요약·투자아이디어에 같은 이름이 남아 있으면 그 항목을 비운다."""
    keys = [n.split("(")[0].strip() for n in flagged_names(suspect)]
    return tuple("" if t and any(k and k in t for k in keys) else t for t in texts)
