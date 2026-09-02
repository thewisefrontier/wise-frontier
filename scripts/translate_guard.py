"""
scripts/translate_guard.py
-----------------------------
검증된 한국어 기사 본문을 영어로 번역/현지화한다(2026-09-03 신설 —
"국제성 있는 카테고리 기사는 만들 때 처음부터 한글 콘텐츠랑 외국어로
같이 만들면 어떨까" 요청).

⚠️ 설계: 한국어·영어를 한 번의 Gemini 호출로 동시에 생성하지 않는다.
한국어를 먼저 생성하고 각 writer의 기존 팩트체크(verify_no_fabricated_names
등)를 통과한 뒤, 그 "검증된" 한국어를 소스로 번역만 한다 — 동시 생성은
언어별로 숫자·사실이 미묘하게 갈릴 위험(예: 환율 수치가 한/영 버전에서
다르게 나옴)이 있어 사용자와 상의해 피하기로 함. 번역은 원문에 없는
사실을 새로 만들 수 없으므로 이 위험이 구조적으로 없다.

각 writer 스크립트가 자기만의 GeminiClient/키 로테이션 인스턴스를 갖고
있으므로(article_image.py·style_guard.py와 동일한 이유) call_gemini
함수를 주입받는다 — 이 모듈이 직접 Gemini 클라이언트를 만들지 않는다.

사용:
    from translate_guard import translate_article
    title_en, body_en = translate_article(title_ko, body_ko, call_gemini)
    # 번역 실패 시 ("", "") 반환. 번역은 부가 기능이지 필수 경로가
    # 아니므로, 호출부는 실패해도 한국어 기사 저장 자체를 막으면 안 된다.
"""

import re


def _parse_translation(text: str) -> tuple[str, str]:
    title, body = "", ""
    m_title = re.search(r"TITLE:\s*(.+?)(?:\n|$)", text)
    if m_title:
        title = m_title.group(1).strip()
    m_body = re.search(r"BODY:\s*(.+)$", text, re.S)
    if m_body:
        body = m_body.group(1).strip()
    return title, body


def translate_article(title_ko: str, body_ko: str, call_gemini_fn, max_tokens: int = 3500) -> tuple[str, str]:
    """검증된 한국어 제목·본문을 자연스러운 영어 뉴스 문체로 번역한다.

    직역이 아니라 영어권 독자에게 자연스러운 뉴스 문장으로 재구성하되,
    숫자·날짜·고유명사·사실관계는 원문 그대로 유지하도록 지시한다.
    """
    if not title_ko or not body_ko:
        return "", ""

    prompt = f"""Translate the following Korean news article into natural, professional
English news writing (AP style). Do not translate word-for-word — restructure
sentences the way a native English news writer would, but keep every number,
date, percentage, name, and fact EXACTLY as in the original. Do not add
commentary, opinion, or any fact not present in the Korean original. Do not
invent anything.

Output format (follow exactly, no extra text before or after):
TITLE: <English title>
BODY: <English body>

[Korean title]
{title_ko}

[Korean body]
{body_ko[:4000]}

Output:"""

    text = call_gemini_fn(prompt, max_tokens=max_tokens)
    if not text:
        return "", ""

    title_en, body_en = _parse_translation(text)
    if not title_en or not body_en:
        return "", ""
    return title_en, body_en
