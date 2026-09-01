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
        _pub_day_label, verify_single_topic
    ...
    verify_single_topic(title, body, call_gemini)
"""

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

    result = call_gemini_fn(prompt, max_tokens=5, start_tier=3)
    if not result:
        return True
    return "YES" in result.upper()
