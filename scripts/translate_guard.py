"""
scripts/translate_guard.py
-----------------------------
검증된 한국어 기사 본문을 외국어로 번역/현지화한다(2026-09-03 신설 —
"국제성 있는 카테고리 기사는 만들 때 처음부터 한글 콘텐츠랑 외국어로
같이 만들면 어떨까" 요청. 2026-09-08 다국어 채널 신설로 언어 파라미터화).

⚠️ 설계: 한국어·번역을 한 번의 Gemini 호출로 동시에 생성하지 않는다.
한국어를 먼저 생성하고 각 writer의 기존 팩트체크(verify_no_fabricated_names
등)를 통과한 뒤, 그 "검증된" 한국어를 소스로 번역만 한다 — 동시 생성은
언어별로 숫자·사실이 미묘하게 갈릴 위험(예: 환율 수치가 한/영 버전에서
다르게 나옴)이 있어 사용자와 상의해 피하기로 함. 번역은 원문에 없는
사실을 새로 만들 수 없으므로 이 위험이 구조적으로 없다.

각 writer 스크립트가 자기만의 GeminiClient/키 로테이션 인스턴스를 갖고
있으므로(article_image.py·style_guard.py와 동일한 이유) call_gemini
함수를 주입받는다 — 이 모듈이 직접 Gemini 클라이언트를 만들지 않는다.

사용:
    from translate_guard import translate_article, verify_translation
    title_en, body_en = translate_article(title_ko, body_ko, call_gemini)
    title_hi, body_hi = translate_article(title_ko, body_ko, call_gemini, lang="hi")
    # 번역 실패 시 ("", "") 반환. 번역은 부가 기능이지 필수 경로가
    # 아니므로, 호출부는 실패해도 한국어 기사 저장 자체를 막으면 안 된다.

    reason = verify_translation(title_ko, body_ko, title_hi, body_hi, lang="hi")
    # 문제 없으면 "" 반환, 문제 있으면 사유 문자열(한국어) 반환.
    # 2026-09-08 사용자 지시("최소한 두세번 검증은 필요할테니까",
    # "거의 놀고 있는 엔비디아를 이용해도") — Gemini가 쓰고 Gemini가
    # 스스로 검증하면 같은 맹점이 반복되므로, 계열이 다른 모델(NVIDIA
    # nemotron, nvidia_client.py — 이미 dedup_guard.py 등에서 같은
    # 이유로 교차검증용으로 씀)로 원문·번역문을 대조한다. 팀이 힌디어/
    # 프랑스어/스페인어를 직접 못 읽어 오역을 못 잡는 리스크의 안전장치.
"""

import re

try:
    from style_guard import ensure_paragraphs
except Exception:
    def ensure_paragraphs(text: str, target: int = 3, max_sentences_per_para: int = 4) -> str:
        return text

# 번역 결과에 한글이 남아있는지 검사(2026-09-09 실사고: hi/es 번역이 원문
# 한국어를 그대로 반환하거나 일부 문구만 남긴 채 저장됨 — 사용자 지적:
# "힌디쪽에 한글 그대로 올라가거나, 본문은 한글이거나 하는 기사들이 보이는데".
# verify_translation()은 NVIDIA로 "사실 보존"만 검사하지 "번역이 실제로
# 됐는지"는 검사하지 않아 원문 그대로인 응답도 사실은 100% 일치하니 통과돼
# 버렸다 — 결정론적 정규식 검사를 별도 안전장치로 추가한다.
# ⚠️ 임계값은 0(한 글자도 허용 안 함)이다 — 처음엔 3글자까지 봐줬는데
# ("사 소스민", "구ンगजेोन 랜드"처럼 인명·고유명사 일부만 한글로 새어나온
# 사례) 실사고 샘플에서 그마저도 못 잡는 경우가 나왔다. 프롬프트 자체가
# "한글 단 한 글자도 남기지 말라"고 명시하므로 정상 번역이면 애초에 0이어야
# 맞다.
_HANGUL_RE = re.compile(r'[가-힣]')


def _has_korean_leak(text: str, max_chars: int = 0) -> bool:
    return len(_HANGUL_RE.findall(text or "")) > max_chars


def _has_self_duplication(text: str, anchor_len: int = 200) -> bool:
    """Gemini가 번역 본문 전체를 통째로 두 번 반복해 응답하는 사고 방지
    (2026-09-09 실사고: id=149911 fr 번역이 같은 문단을 처음부터 끝까지
    그대로 두 번 이어붙여 저장됨). 본문 앞부분(anchor)이 뒷부분에 다시
    등장하면 자기복제로 간주한다."""
    if not text or len(text) < anchor_len * 2:
        return False
    anchor = text[:anchor_len]
    return anchor in text[anchor_len:]


LANG_NAMES = {
    "en": "English",
    "hi": "Hindi",
    "fr": "French",
    "es": "Spanish",
    # Phase 2 후보 — 리스트에 추가만 하면 translate_article()이 바로 지원:
    "ar": "Arabic",
    "ru": "Russian",
    "pt": "Portuguese",
}


def _parse_translation(text: str) -> tuple[str, str]:
    title, body = "", ""
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    if m_title:
        title = m_title.group(1).strip()
    m_body = re.search(r"BODY:\s*(.+)$", text, re.S)
    if m_body:
        body = m_body.group(1).strip()
    return title, body


def translate_article(title_ko: str, body_ko: str, call_gemini_fn, lang: str = "en",
                       max_tokens: int = 3500) -> tuple[str, str]:
    """검증된 한국어 제목·본문을 자연스러운 <lang> 뉴스 문체로 번역한다.

    직역이 아니라 그 언어권 독자에게 자연스러운 뉴스 문장으로 재구성하되,
    숫자·날짜·고유명사·사실관계는 원문 그대로 유지하도록 지시한다.
    """
    if not title_ko or not body_ko:
        return "", ""

    lang_name = LANG_NAMES.get(lang, LANG_NAMES["en"])

    prompt = f"""Translate the following Korean news article into natural, professional
{lang_name} news writing (AP style equivalent for that language). Do not translate
word-for-word — restructure sentences the way a native {lang_name} news writer
would, but keep every number, date, percentage, name, and fact EXACTLY as in
the original. Do not add commentary, opinion, or any fact not present in the
Korean original. Do not invent anything. The BODY must be written ENTIRELY in
{lang_name} — every word, including names, must be transliterated or translated;
do not leave a single Korean (Hangul) character anywhere in the output.
Preserve the paragraph structure of the original: the Korean body below is
split into paragraphs by blank lines (\\n\\n) — keep that same number of
paragraph breaks in your {lang_name} translation, do not merge everything
into one block.

Output format (follow exactly, no extra text before or after):
TITLE: <{lang_name} title>
BODY: <{lang_name} body, with paragraphs separated by a blank line>

[Korean title]
{title_ko}

[Korean body]
{body_ko[:4000]}

Output:"""

    text = call_gemini_fn(prompt, max_tokens=max_tokens)
    if not text:
        return "", ""

    title_out, body_out = _parse_translation(text)
    if not title_out or not body_out:
        return "", ""
    if _has_korean_leak(title_out) or _has_korean_leak(body_out):
        return "", ""
    if _has_self_duplication(body_out):
        return "", ""
    body_out = ensure_paragraphs(body_out)
    return title_out, body_out


def verify_translation(title_ko: str, body_ko: str, title_out: str, body_out: str,
                        lang: str, call_nvidia_fn=None) -> str:
    """원문 한국어와 번역 결과를 계열이 다른 모델(NVIDIA)로 대조해 숫자·
    날짜·고유명사·핵심 사실이 보존됐는지 확인한다.

    문제 없으면 "" 반환, 문제 있으면 짧은 한국어 사유 반환. NVIDIA 키가
    없거나 호출 실패하면(부가 안전장치이지 필수 경로가 아니므로) 통과로
    간주하고 "" 반환 — 검증 불가가 발행 자체를 막으면 안 된다.
    """
    if not title_out or not body_out:
        return "번역 결과 없음"

    if _has_korean_leak(title_out) or _has_korean_leak(body_out):
        return "번역 결과에 한글이 남아있음"

    if call_nvidia_fn is None:
        try:
            from nvidia_client import call_nvidia as call_nvidia_fn
        except Exception:
            return ""

    lang_name = LANG_NAMES.get(lang, lang)
    prompt = f"""Compare this Korean news article with its {lang_name} translation.
First check: is the translation actually written in {lang_name}? If any part of
it is still in Korean, or in a different language than {lang_name}, that is a
failure — report it.
Otherwise check ONLY for: numbers/statistics changed, dates changed, names/places
changed or mistranslated, facts added that aren't in the Korean original, facts
dropped that change the meaning. Ignore stylistic differences — translation
doesn't need to be word-for-word.

[Korean original]
{title_ko}
{body_ko[:2000]}

[{lang_name} translation]
{title_out}
{body_out[:2000]}

If the translation accurately preserves all facts, respond with exactly: OK
If there is a problem, respond with a brief description in Korean (one sentence,
what specifically is wrong). No other text."""

    try:
        resp = call_nvidia_fn(prompt, max_tokens=200)
    except Exception:
        return ""
    if not resp:
        return ""
    resp = resp.strip()
    if resp.upper().startswith("OK"):
        return ""
    return resp[:300]


def translate_extra_fields(summary_3lines_ko: str, investment_idea_ko: str, call_gemini_fn,
                            lang: str = "en", max_tokens: int = 1200) -> tuple[str, str]:
    """3줄요약·투자아이디어를 번역한다(2026-09-09 신설 — 사용자 지적:
    "영어로 보기... 3줄 요약은 번역이 안되는데", "투자 아이디어도 번역이
    따로 안되네". 원인은 "분리가 안 된" 게 아니라, translate_article()이
    title/body만 번역하도록 설계돼 있어 이 두 필드는 애초에 번역 대상에
    포함된 적이 없었다 — summary_3lines_en/investment_idea_en 컬럼 자체가
    없었음).

    title/body와 별도 호출인 이유: 두 필드 다 없는 기사가 있고(예: 데이터
    저널리즘 템플릿), 있어도 이미 title/body 번역이 끝난 뒤에만 필요하므로
    항상 같이 호출할 필요가 없다.

    입력 중 하나라도 없으면 그 필드는 빈 문자열로 반환(둘 다 없으면
    ("", "") 즉시 반환, Gemini 호출 자체를 안 함).
    """
    if not summary_3lines_ko and not investment_idea_ko:
        return "", ""

    lang_name = LANG_NAMES.get(lang, LANG_NAMES["en"])
    prompt = f"""Translate the following into natural, professional {lang_name} (AP style
equivalent). Keep every number, date, percentage, and name EXACTLY as in the
original. Do not add commentary or invent anything. Write entirely in
{lang_name} — do not leave any Korean text.

Output format (follow exactly, no extra text before or after):
SUMMARY3: <{lang_name} 3-line summary, lines separated by \\n>
INVESTMENT: <{lang_name} investment-idea paragraph>

[Korean 3-line summary]
{summary_3lines_ko}

[Korean investment idea]
{investment_idea_ko}

Output:"""

    text = call_gemini_fn(prompt, max_tokens=max_tokens)
    if not text:
        return "", ""

    m_s3 = re.search(r"SUMMARY3:\s*(.+?)(?:\nINVESTMENT:|$)", text, re.S)
    m_inv = re.search(r"INVESTMENT:\s*(.+)$", text, re.S)
    summary3_out = m_s3.group(1).strip() if m_s3 else ""
    investment_out = m_inv.group(1).strip() if m_inv else ""

    if not summary_3lines_ko or _has_korean_leak(summary3_out):
        summary3_out = ""
    if not investment_idea_ko or _has_korean_leak(investment_out):
        investment_out = ""
    return summary3_out, investment_out
