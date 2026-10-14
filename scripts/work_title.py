"""
scripts/work_title.py
---------------------
작품명(그림 《》·책 『』) 확정 + 기사 반영 검사 공용 모듈.

2026-10-14 실사고(id=308600): 미술관 공개 데이터로 수집한 그림 2,210건(art_weekly_global_artworks.json)은
title_ko에 영어 원제가 그대로 들어 있고, 기사 작성 프롬프트가 "작품명을 한국어로 옮겨 넣으라"고만 해서
기사마다 Gemini가 즉석 번역했다 — 검증이 없어 "Portrait of Mrs. Francis Stanton Blake"가
"프랜시스 스탠턴 블레이크 부인 초상"(→ 부인의 초상)으로 나갔고, 영상 파생물(video-forge)에선
"블레이크 부인 초상"까지 줄었다(사용자: "작품명 중요한데 그게 제대로 안 들어간 건 문제").

- resolve_korean_title(): 한글 제목이 없으면 기사 작성 *전에* 한 번만 규칙대로 번역하고, 계열이 다른
  모델(nemotron)이 원제 대비 누락·추가를 검수한다. 두 번 다 실패하면 원제를 그대로 쓴다
  (지어낸 번역 제목보다 원제가 낫다 — 기존 프롬프트 규칙과 같은 방향).
- enforce_work_title(): 확정된 작품명이 본문(괄호 포함)·제목에 그대로 들어갔는지 코드로 검사·보정.
"""

import re

_HANGUL = re.compile("[가-힣]")


def _clean_original(title: str) -> str:
    """원제 끝의 괄호 설명(모델 실명 등, 예: "(Eugenia White Blake)")은 작품명에서 뺀다."""
    return re.sub(r"\s*\([^()]*\)\s*$", "", title or "").strip()


def _verify_translation(original: str, korean: str):
    """nemotron 검수. True=정확, False=누락/추가/오역, None=검수 불가(호출 실패).
    nemotron은 한국어로 답하게 하면 응답이 깨져(fabrication_guard.py 2026-10-10 실측) 숫자만 받는다."""
    try:
        from nvidia_client import call_nvidia
    except Exception:
        return None
    prompt = (
        "영어 작품 원제와 한국어 번역 제목을 비교하세요.\n"
        f"원제: {original}\n번역: {korean}\n"
        "원제의 인명·지명·사물·수식어가 번역에서 빠졌거나, 원제에 없는 말이 덧붙었거나, 뜻이 달라졌으면 1, "
        "빠짐없이 정확하면 0. 숫자 하나만 답하세요."
    )
    resp = call_nvidia(prompt, max_tokens=10)
    if not resp:
        return None
    digits = re.findall(r"[01]", resp)
    return None if not digits else digits[0] == "0"


def resolve_korean_title(title_ko: str, title_en: str, call_gemini_fn, kind: str = "그림") -> str:
    """기사에 쓸 한국어 작품명. 이미 한글이면 그대로, 아니면 번역+검수, 실패하면 원제."""
    if title_ko and _HANGUL.search(title_ko):
        return title_ko.strip()
    original = _clean_original(title_en or title_ko or "")
    if not original:
        return ""
    last = ""
    for attempt in range(2):
        prompt = (
            f"다음 {kind}의 원제를 한국어 작품명으로 옮기세요.\n원제: {original}\n"
            "규칙:\n"
            "- 한국에 정착된 번역 제목이 있으면 그것을 쓴다.\n"
            "- 없으면 직역한다. 원제의 인명·지명·사물·수식어를 하나도 빼거나 더하지 않는다.\n"
            "- 인명·지명은 외래어 표기법에 따라 한글로 음차한다(예: Stanton → 스탠턴).\n"
            "- \"Portrait of X\"는 \"X의 초상\", \"Mrs. X\"는 \"X 부인\" "
            "(예: Portrait of Mrs. John Smith → 존 스미스 부인의 초상).\n"
            "- 따옴표·괄호·설명 없이 한국어 제목만 한 줄로 출력한다."
        )
        if attempt and last:
            prompt += f"\n\n[다시] 직전 번역 '{last}'은 원제 요소가 빠졌거나 덧붙었습니다. 원제를 빠짐없이 다시 옮기세요."
        raw = (call_gemini_fn(prompt, max_tokens=80) or "").strip()
        ko = raw.splitlines()[0].strip().strip("\"'《》『』「」 ") if raw else ""
        if not ko or not _HANGUL.search(ko) or len(ko) > 80:
            continue
        last = ko
        verdict = _verify_translation(original, ko)
        print(f"  🏷️ 작품명 번역({attempt + 1}차): {original} → {ko} / 검수: {verdict}")
        if verdict is not False:  # 검수 호출 자체가 실패하면(None) 번역을 쓴다(이 프로젝트의 fail-open 관례)
            return ko
    print(f"  ⚠️ 작품명 번역 검수 실패 — 원제 그대로 사용: {original}")
    return original


def enforce_work_title(title: str, body: str, work_title: str, brackets: str = "《》", creator: str = ""):
    """확정 작품명이 본문(괄호 포함)과 제목에 그대로 있는지 검사·보정. 반환: (title, body, notes)."""
    if not work_title:
        return title, body, []
    o, c = brackets[0], brackets[1]
    full = f"{o}{work_title}{c}"
    notes = []
    if full not in body:
        # 첫 문단의 첫 괄호 작품명을 확정 작품명으로 교체(리드 규칙상 첫 문단 첫 작품명이 이 작품).
        first_para, sep, rest = body.partition("\n")
        pat = re.escape(o) + "[^" + re.escape(c) + "]+" + re.escape(c)
        fixed, n = re.subn(pat, full, first_para, count=1)
        if n:
            notes.append(f"본문 작품명 교정 → {full}")
            body = fixed + sep + rest
        else:
            notes.append(f"⚠️ 본문 첫 문단에 작품명 {full} 없음(자동 교정 불가)")
    if work_title not in title:
        tail = title.split(",", 1)[1].strip() if "," in title else ""
        head = f"{creator}의 {work_title}" if creator else work_title
        new_title = f"{head}, {tail}" if tail else head
        notes.append(f"제목 작품명 교정: {title} → {new_title}")
        title = new_title
    return title, body, notes


if __name__ == "__main__":
    # 회귀 체크(네트워크 없음): python scripts/work_title.py
    assert _clean_original("Portrait of Mrs. Francis Stanton Blake (Eugenia White Blake)") == \
        "Portrait of Mrs. Francis Stanton Blake"
    assert resolve_korean_title("모나리자", "Mona Lisa", lambda *a, **k: "X") == "모나리자"
    t, b, n = enforce_work_title(
        "줄리어스 르 블랑 스튜어트의 프랜시스 스탠턴 블레이크 부인 초상, 1908년 작",
        "스튜어트가 1908년에 제작한 《프랜시스 스탠턴 블레이크 부인 초상》은 월터스 미술관에 있다.\n둘째 문단 《결혼식 이후》.",
        "프랜시스 스탠턴 블레이크 부인의 초상", creator="줄리어스 르 블랑 스튜어트")
    assert "《프랜시스 스탠턴 블레이크 부인의 초상》" in b and "《결혼식 이후》" in b, b
    assert t == "줄리어스 르 블랑 스튜어트의 프랜시스 스탠턴 블레이크 부인의 초상, 1908년 작", t
    print("work_title ok")
