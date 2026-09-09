"""
scripts/style_guard.py
-----------------------
기사 문체 검증·변환 공용 로직 — 논평/칼럼체 감지, 합쇼체(-습니다) 감지·해라체
변환, 보도일 라벨 포맷, 단일 토픽 검증.

원래 gemini_writer.py와 gemini_summarizer.py 두 파일에 완전히 동일한
코드(정규식·함수)로 각각 복붙돼 있었다(2026-09-02 감사로 확인 — gemini_writer.py
쪽 주석에 "gemini_summarizer.py와 동일 로직"이라고 직접 적혀 있었는데도
실제 공용화는 안 돼 있었음). script_leak.py/category_guard.py/country_guard.py와
같은 이유로 분리한다.

verify_single_topic()만 Gemini 호출이 필요해서 call_gemini_fn을 주입받는다
(article_image.py와 같은 패턴 — 스크립트마다 GeminiClient 인스턴스·API 키
세트가 다르기 때문). 나머지는 순수 텍스트 처리라 그대로 가져다 쓰면 된다.

사용:
    from style_guard import has_column_style, has_polite_ending, to_plain_style, \
        _pub_day_label, verify_single_topic, ensure_paragraphs, parse_article_output
    ...
    verify_single_topic(title, body, call_gemini)

ensure_paragraphs()/parse_article_output()는 2026-09-08 감사("공용모듈이
필요한 시스템이 더 있는지 점검해줘")로 추가 공용화 — 각각 4개·8개 파일에
기능적으로 동일한 코드가 복붙돼 있었다(버그로 인한 드리프트는 아니고
순수 중복). 둘 다 Gemini 호출이 없는 순수 텍스트 처리라 주입이 필요 없다.

enforce_title_prefix()는 2026-09-09 감사("다른 모듈에 있는 안전장치 중에
쓸만한 것들을 모두 모아서 안전장치도 공용 모듈화하자")로 추가 공용화 —
9개 writer 파일(oil_price/opinet_price/opinet_weekly/frontier_markets/
ny_market_open×2/crypto_news/stock_news)에 제목 앞 [태그] 강제 부착
로직이 복붙돼 있었는데, 실제로는 두 계열로 드리프트해 있었다: oil_price/
opinet_price/opinet_weekly/frontier_markets는 "태그가/는 ..." 식으로
새어나온 평문 접두 표현까지 제거하는 3단계 폴백이 있었지만, crypto_news/
stock_news/ny_market_open(본편+유럽판)은 대괄호([태그]) 형태만 인식해서
Gemini가 평문으로 접두어를 흘리면 태그가 중복 부착되는 결함이 있었다.
"""

import math
import re

# ── 논평/칼럼체 검출 ────────────────────────────
BANNED_STYLE_PATTERNS = [
    r"보여줍니다", r"보여주고 있습니다", r"보여준다",
    r"도모하고 있습니다", r"도모한다",
    r"강조하고 있습니다", r"강조한다",
    r"시사합니다", r"시사한다", r"시사하며",
    r"주목됩니다", r"주목된다", r"주목받고 있습니다",
    r"평가된다", r"평가받고 있습니다", r"라는 평가다", r"라는 분석이다",
    r"필요해 보입니다", r"필요할 것으로 보입니다",
    r"지켜볼 필요가 있습니다", r"지켜봐야 할 것입니다",
    r"기대됩니다", r"기대해 볼 만합니다",
    # 화자 없는 전망/분석형 마무리 문장 (트렌드 기사에서 재발 확인, 2026-07-28)
    r"분석이 나온다", r"분석이 나옵니다", r"분석도 나온다", r"분석도 나옵니다",
    r"관측이 나온다", r"관측이 나옵니다",
    r"우려가 나온다", r"우려가 나옵니다",
    r"지속될 전망이다", r"지속될 전망입니다",
    r"이어질 전망이다", r"이어질 전망입니다",
    r"귀추가 주목된다", r"귀추가 주목됩니다",
]


def has_column_style(text: str) -> bool:
    """생성된 기사 본문에 논평/칼럼체 어미가 섞여 있는지 검사"""
    if not text:
        return False
    return any(re.search(p, text) for p in BANNED_STYLE_PATTERNS)


# ── 합쇼체(-습니다/-입니다) 탐지·변환 ────────────────────────────────
# 문장 종결부만 대상으로 하므로 인용문 내부 발언("문제없습니다"라고 말했다)은 보존된다.
_SENT_END_LA = r'(?=[.!?\n]|$)'  # 문장 종결 위치 (인용문 내부 제외용)
_POLITE_ENDING_RE = re.compile(r'(?:습니다|입니다|됩니다)[")‘’“”]*' + _SENT_END_LA)


def has_polite_ending(text: str) -> bool:
    """합쇼체 종결이 있는지 검사.
    변환기(to_plain_style)가 실제로 고칠 수 있는 패턴과 정확히 일치시킨다.
    (구 버전은 습니다/입니다/됩니다만 탐지해 '개최합니다.'·'아닙니다.'를 놓쳤음)"""
    if not text:
        return False
    return to_plain_style(text) != text


_JONG_B, _JONG_N = 17, 4  # 종성 ㅂ, ㄴ

_POLITE_CONV_RULES = [
    (re.compile(r'아닙니다' + _SENT_END_LA), '아니다'),
    (re.compile(r'입니다' + _SENT_END_LA), '이다'),
    (re.compile(r'습니다' + _SENT_END_LA), '다'),
]
_BNIDA_RE = re.compile(r'([가-힣])니다' + _SENT_END_LA)
_HANGUL_ANY_RE = re.compile(r'[가-힣]')
_LATIN_SENT_SPLIT_RE = re.compile(r'(?<=[.!?।])\s+')


def _split_sentences(text: str) -> list:
    """문장 단위 분할. 한글 본문은 기존처럼 '-다.' 어미 뒤에서만 끊어
    약어·소수점 오분할을 피하고, 그 외(번역된 en/fr/es/hi 등 라틴·데바나가리
    문자 본문)는 문장부호(.!?, 힌디 danda '।') 뒤 공백에서 끊는다.

    2026-09-10 실사고: translate_guard.py가 번역문에도 ensure_paragraphs()를
    쓰는데, 이 함수가 '-다.' 패턴만 봐서 한글이 아닌 번역 본문은 문장이 한
    개로만 잡혀(len(sentences)<2) 문단 분할이 통째로 무력화됐었다(영/불/서
    번역 기사가 전부 한 덩어리로 발행됨, 사용자 지적: "영문, 프랑스,
    스페인어 기사도 전혀 문단 정리가 안되고 있어")."""
    if _HANGUL_ANY_RE.search(text):
        return [s.strip() for s in re.split(r"(?<=다\.)\s+", text) if s.strip()]
    return [s.strip() for s in _LATIN_SENT_SPLIT_RE.split(text) if s.strip()]


def _bnida_to_nda(m) -> str:
    """'합니다'→'한다', '됩니다'→'된다' 등 종성 ㅂ + 니다 → 종성 ㄴ + 다."""
    ch = m.group(1)
    code = ord(ch) - 0xAC00
    if not (0 <= code < 11172):
        return m.group(0)
    cho, jung, jong = code // 588, (code % 588) // 28, code % 28
    if jong != _JONG_B:
        return m.group(0)
    return chr(0xAC00 + cho * 588 + jung * 28 + _JONG_N) + '다'


def to_plain_style(text: str) -> str:
    """문장 종결부의 합쇼체를 해라체(-다)로 변환."""
    if not text:
        return text
    for rx, rep in _POLITE_CONV_RULES:
        text = rx.sub(rep, text)
    return _BNIDA_RE.sub(_bnida_to_nda, text)


def _pub_day_label(raw) -> str:
    """source_published_at(UTC ISO8601) → '8월 3일'. 실패 시 빈 문자열.

    ⚠️ 값은 UTC 기준이라 현지시간과 최대 하루 어긋날 수 있다. 그럼에도 주입하는 이유:
      - country 컬럼이 원본 기사의 55.9%에서 비어 있어 국가별 오프셋 보정이 불가능하다
        (2026-08-05 실측, 8/1 이후 5,692건 기준)
      - date_guard도 발행일 당일과 전날을 모두 근거로 인정한다(±1일 수용)
      - 아무 날짜도 주지 않으면 Gemini가 요일로 도피하거나 날짜를 지어낸다.
        하루 오차는 그보다 명백히 낫다.
    소스 원문 본문에 날짜가 명시돼 있으면 그쪽이 우선이라는 규칙은 writer_rules에 있다.
    """
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


def verify_single_topic(title: str, body: str, call_gemini_fn) -> bool:
    """하나의 토픽만 다루는지 Gemini로 검수. 판정 실패 시 True(통과).
    call_gemini_fn은 호출 스크립트 자신의 call_gemini(prompt, max_tokens=..., start_tier=...) 래퍼."""
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

    result = call_gemini_fn(prompt, max_tokens=5, start_tier=4)
    if not result:
        return True
    return "YES" in result.upper()


def _regroup_sentences(sentences: list, target: int) -> str:
    """문장 목록을 target개 안팎의 문단으로 균등 재배분."""
    if len(sentences) < 2:
        return " ".join(sentences)
    actual_target = min(target, len(sentences) - 1)
    actual_target = max(actual_target, 2)
    n = len(sentences)
    size = math.ceil(n / actual_target)
    groups = [sentences[i:i + size] for i in range(0, n, size)]
    return "\n\n".join(" ".join(g) for g in groups)


def ensure_paragraphs(text: str, target: int = 3, max_sentences_per_para: int = 3) -> str:
    """Gemini가 프롬프트의 '문단으로 나누어 작성' 지시를 어기고
    \\n\\n 없이 한 덩어리로 응답하는 경우가 있어(강제성 없는 지시라 준수율이
    들쭉날쭉함), 코드 단에서 문장(-다.) 단위로 강제 분할하는 안전장치.
    문장이 2개 이상이면 항상 최소 2개 문단으로 분할한다(짧은 리드 문단도 포함).

    2026-09-09 확장(사용자 지적: "기사는 한 문단에 3~4줄 이상이 되면 안
    되. 보기에 좋지 않다", 실사고 id=148194 — "◆ 섹션명"으로 이미 여러
    구획(\\n\\n)이 있는 기사인데 구획 하나하나가 4~6문장을 몰아 쓴 통짜
    문단이었음): 예전엔 \\n\\n이 하나라도 있으면 통째로 손을 안 댔는데,
    그러면 "섹션은 나뉘어 있지만 섹션 안쪽은 여전히 한 문단"인 경우를 못
    잡았다. 이제 \\n\\n으로 나뉜 블록 각각을 검사해서, max_sentences_per_para를
    넘는 블록만 추가로 쪼갠다 — 이미 적당한 블록은 그대로 둔다.

    2026-09-09 추가 확장(실사고 id=150686 — 실시간 트렌드 기사가 문장마다
    \\n\\n을 넣어 한 문장짜리 문단 8개로 쪼개짐, 사용자 지적: "제목도 그렇고
    내용도 부실한데"): 이 함수는 그동안 "너무 긴 문단을 쪼개는" 방향만
    다뤘지 "너무 잘게 쪼개진 문단을 다시 묶는" 반대 방향은 없었다. 블록당
    평균 문장 수가 지나치게 낮으면(과다 분절로 판단) 전체 문장을 한 번에
    모아 target 기준으로 재배분한다.

    2026-09-10 재조정(사용자 지적, id=152113 수동 작성 기사 — "한 문단은
    2~3개 문장을 써서 4줄을 넘어가지 않도록 해. 약간 삐져나와서 5줄이 되는
    것까지는 괜찮지만 문장 여러개를 묶어서 뭉치로 문단을 만들면 읽기가
    어려워"): 기존 4는 "3~4줄"이라는 예전 기준을 문장 수로 대충 매핑한
    값이었는데, 실제로는 2~3문장이 적정선이고 4문장부터는 뭉쳐 보인다는
    게 이번에 명확해졌다. 3으로 낮춤 — 4문장짜리 블록이 들어오면 3+1로
    쪼개져 마지막 문단이 문장 1개로 짧게 남는 것도 "약간 삐져나오는 것"
    허용 범위와 부합한다."""
    if not text:
        return text

    if "\n\n" not in text:
        sentences = _split_sentences(text.strip())
        if len(sentences) < 2:
            return text  # 문장이 1개뿐이면 분할 불가
        return _regroup_sentences(sentences, target)

    # 이미 문단(블록)이 나뉜 텍스트
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    block_sentences = [_split_sentences(b) for b in blocks]
    total_sentences = sum(len(s) for s in block_sentences)

    # 과다 분절 판단: 블록이 3개 이상인데 블록당 평균 문장 수가 1.5개 이하면
    # (예: 8문장이 8블록으로, 한 문장씩) 문단이 아니라 문장 단위로 쪼개진 것 —
    # 전체를 다시 모아 target 기준으로 재배분한다.
    if len(blocks) >= 3 and total_sentences and (total_sentences / len(blocks)) <= 1.5:
        all_sentences = [s for block in block_sentences for s in block]
        return _regroup_sentences(all_sentences, target)

    # 그 외에는 블록별로 길이만 확인해 너무 긴 블록만 추가로 쪼갠다 —
    # 이미 적당한 블록은 그대로 둔다.
    out_blocks = []
    for sentences in block_sentences:
        if len(sentences) <= max_sentences_per_para:
            out_blocks.append(" ".join(sentences))
            continue
        for i in range(0, len(sentences), max_sentences_per_para):
            out_blocks.append(" ".join(sentences[i:i + max_sentences_per_para]))
    return "\n\n".join(out_blocks)


def _has_batchim(ch: str) -> bool:
    """음절 ch에 받침이 있는지(조사 가/는 vs 이/은 선택용)."""
    code = ord(ch) - 0xAC00
    if not (0 <= code < 11172):
        return False
    return code % 28 != 0


def enforce_title_prefix(title: str, prefix: str, bare_name: str, particles=None) -> str:
    """제목 앞에 [prefix] 태그를 강제 부착. 중복 부착 방지.

    이미 "[bare_name]"이 붙어 있으면 정규화만 하고, "bare_name가/는 ..."처럼
    Gemini가 평문으로 접두 표현을 흘린 경우도 감지해 제거한 뒤 재부착한다.
    조사(가/는 vs 이/은)는 particles를 넘기지 않으면 bare_name 마지막 글자
    받침 유무로 자동 판정한다.
    """
    t = (title or "").strip()
    if not t:
        return t
    spaced = r"\s*".join(re.escape(ch) for ch in bare_name)
    m = re.match(r"^\s*\[\s*" + spaced + r"\s*\]\s*(.*)$", t)
    if m:
        t = m.group(1).strip()
    else:
        if particles is None:
            particles = ("이", "은") if _has_batchim(bare_name[-1]) else ("가", "는")
        particle_group = "|".join(particles)
        # bare_name도 대괄호 검사와 같은 "글자 사이 공백 허용" 패턴(spaced)을 써야
        # "글로벌 마켓 동향"처럼 여러 단어로 된 태그명도 정확히 매칭된다.
        m2 = re.match(r"^" + spaced + r"(?:" + particle_group + r")?\s*[,·]?\s+(.+)$", t)
        if m2 and m2.group(1)[:1] not in ("와", "과", "및"):
            t = m2.group(1).strip()
        else:
            t = re.sub(r"^" + spaced + r"\s*[,·]\s*", "", t).strip()
    return f"{prefix} {t}" if t else prefix


def to_won_style_amount(n) -> str:
    """정수를 "912억5649만5759" 식 억/만 그룹핑 문자열로 변환(단위 접미사는
    호출부가 붙인다 — 원/달러/주 등 맥락에 따라 다르므로).

    2026-09-04 crypto_news_writer.py에 처음 도입(사용자 지적 — 리플 기사
    시가총액이 "91,256,495,759" 콤마 원문 그대로 나가 "가시성이 너무
    떨어진다"). 2026-09-10 stock_news_writer.py에서 거래량("32,177,880주")
    이 같은 방식으로 콤마 원문 그대로 나간 게 재발해(사용자 지적) 두 파일에
    거의 동일한 코드가 복붙돼 있던 걸 발견 — style_guard.py로 공용화.
    프롬프트에 넣는 참고자료 자체를 이 형식으로 만들어서, Gemini가 콤마
    원본을 그대로 베낄 여지를 원천 차단하는 용도로 쓴다(화폐 금액뿐 아니라
    거래량 등 큰 정수 전반에 적용 가능 — [[feedback_korean_won_man_unit_format]]
    억/만 그룹핑 규칙 참고)."""
    n = int(round(n))
    eok, rest = divmod(n, 100_000_000)
    man, remainder = divmod(rest, 10_000)
    parts = []
    if eok:
        parts.append(f"{eok}억")
    if man:
        parts.append(f"{man}만")
    if remainder or not parts:
        parts.append(f"{remainder}")
    return "".join(parts)


def parse_article_output(text: str) -> tuple[str, str]:
    """"TITLE: ...\\nBODY: ..." 형식의 Gemini 응답을 (title, body)로 분리."""
    title, body = "", ""
    if not text:
        return title, body
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    if m_title:
        title = m_title.group(1).strip()
    m_body = re.search(r"BODY:\s*(.+)$", text, re.S)
    if m_body:
        body = m_body.group(1).strip()
    return title, body
