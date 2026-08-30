"""
scripts/script_leak.py
-----------------------
고유명사 음역 도중 Gemini가 한글 대신 외국 문자를 뱉는 현상(스크립트 혼입) 검출.
check_transliteration.py가 배치로 쓰던 로직을 공용 모듈로 뽑아, 각 writer
스크립트의 저장 시점 하드 블록에서도 그대로 재사용한다.

실사례(2026-08-05 실측, 7/1~8/5 발행분 12건):
  "이스ام 파레스"(Issam Fares) / "가이انا"(가이아나) / "이슬라마باد"(이슬라마바드)
  "베르나르دو"(Bernardo) / "لندن행"(런던) / "라다크리شن난"(Radhakrishnan)

⚠️ 원형을 코드가 알 수 없으므로 자동 교정이 불가능하다(`이스ام`의 `ام`이
   "삼"인지 "사무"인지 판별 불가) → 검출만 하고 교정은 KNOWN_FIXES/수동.
⚠️ 그리스 문자는 제외한다. 수식·과학 기사에서 정상 사용된다(α, β, π).

일본어(히라가나·가타카나)는 2026-08-24 실사고(id=95929, "이란"의 "란"이
가타카나 "ラン"으로 새어나와 "이ラン(이란)"으로 저장)로 추가됐다. 최초
설계(2026-08-19) 당시 참고한 12건 실사례가 전부 아랍·키릴 계열이라
일본어 문자셋은 처음부터 감시 대상에서 빠져 있었다.

같은 날(2026-08-24) 사용자 지시로 "가능한 수준의 언어를 전부" 넣어
프론티어 마켓 보도 대상국 문자셋(미얀마·캄보디아·라오스·스리랑카·에티오피아·
몽골·아르메니아·조지아·인도 각지방 문자 등)까지 선제적으로 확장했다 —
이번에도 특정 사고 하나를 보고서야 부랴부랴 추가하는 대신, 상위 클래스
(비한글·비라틴·비그리스 문자 전체)를 겨냥한다([[feedback_generalize_pattern_fixes]]).
오탐 검토를 코드 리뷰로 대체할 수 없으니 육안으로 이국 문자를 직접
타이핑해 붙여넣는 실수를 막기 위해, 새로 추가한 문자셋은 코드포인트
숫자(chr())로 범위를 생성한다 — 기존 항목(리터럴 문자 붙여넣기)은 이미
실전 검증된 값이라 그대로 둔다.

한자(CJK)는 별도 처리한다. "고(故)", "대(對)중국", "시진핑(習近平)"처럼
괄호 안 병기는 한국 언론의 정상 관행이므로, 괄호 구간을 제거한 뒤에도
남는 한자만 오류로 본다. 실측(2026-08-05, 7/20 이후) 이 방식으로 오탐 0건.

⚠️ 2026-08-31 발견: 위 실측이 "2글자 이상 한자 덩어리"(스크립트 혼입·
이름 미음역) 사례로만 검증된 탓에, 정작 "美/中/日/英"처럼 한국 언론이
괄호 없이 흔히 쓰는 국가명 한 글자 축약까지 전부 오류로 잡고 있었다.
lotto_writer.py의 파워볼 기사가 제목의 "美 파워볼" 때문에 저장 시점마다
100% 차단되어 단 한 건도 발행되지 못한 채 방치된 것으로 확인(사용자
제보: "파워볼 기사는 우리쪽에서 한번도 나간 적이 없다"). 실제 스크립트
혼입(이름 음역 중 외국 문자 새어나옴)은 항상 2글자 이상 이어지므로,
괄호 밖에 남는 한자가 1글자뿐이면 의도된 국가명 축약으로 보고 넘어가고
2글자 이상 연속될 때만 오류로 본다.
"""

import re

_SCRIPT_LEAK_RANGES = [
    ("아랍", "؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿"),
    ("히브리", "֐-׿"),
    ("키릴", "Ѐ-ӿ"),
    ("태국", "฀-๿"),
    ("데바나가리", "ऀ-ॿ"),
    ("벵골", "ঀ-৿"),
    ("타밀", "஀-௿"),
    ("일본어", "぀-ヿ"),
]

# 2026-08-24 확장분: 오타 위험을 없애기 위해 코드포인트로 범위를 생성한다.
# 유니코드 표준 블록 기준(각 스크립트의 공식 할당 범위).
_SCRIPT_LEAK_CODEPOINT_RANGES = [
    ("타아나", 0x0780, 0x07BF),       # Thaana (몰디브)
    ("구자라트", 0x0A80, 0x0AFF),     # Gujarati
    ("구르무키", 0x0A00, 0x0A7F),     # Gurmukhi (펀자브)
    ("오리야", 0x0B00, 0x0B7F),       # Oriya/Odia
    ("텔루구", 0x0C00, 0x0C7F),       # Telugu
    ("칸나다", 0x0C80, 0x0CFF),       # Kannada
    ("말라얄람", 0x0D00, 0x0D7F),     # Malayalam
    ("신할라", 0x0D80, 0x0DFF),       # Sinhala (스리랑카)
    ("라오", 0x0E80, 0x0EFF),         # Lao
    ("티베트", 0x0F00, 0x0FFF),       # Tibetan
    ("미얀마", 0x1000, 0x109F),       # Myanmar
    ("에티오피아", 0x1200, 0x137F),    # Ethiopic (암하라어 등)
    ("몽골", 0x1800, 0x18AF),         # Mongolian (전통 몽골 문자)
    ("크메르", 0x1780, 0x17FF),       # Khmer (캄보디아)
    ("아르메니아", 0x0530, 0x058F),    # Armenian
    ("조지아", 0x10A0, 0x10FF),       # Georgian
]
for _name, _start, _end in _SCRIPT_LEAK_CODEPOINT_RANGES:
    _SCRIPT_LEAK_RANGES.append((_name, f"{chr(_start)}-{chr(_end)}"))

_SCRIPT_LEAK_RE = [
    (name, re.compile("[" + rng + "]"), re.compile(".{0,14}[" + rng + "]+.{0,14}"))
    for name, rng in _SCRIPT_LEAK_RANGES
]

# 괄호 병기 구간. 20자 상한은 긴 괄호가 통째로 지워져 오류를 놓치는 것을 막는다.
_PAREN_RE = re.compile(r"\([^)]{0,20}\)")
# 2글자 이상 연속된 한자만 본다(1글자는 "美/中/日/英" 같은 한국 언론의
# 정상적인 국가명 축약과 구분 불가 — 실제 스크립트 혼입은 항상 2글자 이상).
_CJK_PROBE = re.compile("[一-鿿]{2,}")
_CJK_CTX = re.compile(".{0,14}[一-鿿]{2,}.{0,14}")


def detect_script_leak(title: str, body: str):
    """비허용 문자셋 혼입 검출. [(스크립트명, 문맥), ...] 반환."""
    hits = []
    for field in (title or "", body or ""):
        if not field:
            continue
        for name, probe, ctx_re in _SCRIPT_LEAK_RE:
            if not probe.search(field):
                continue
            for m in ctx_re.finditer(field):
                snippet = m.group(0).replace("\n", " ").strip()
                if snippet and all(snippet != h[1] for h in hits):
                    hits.append((name, snippet))

        # 한자는 괄호 병기를 걷어낸 뒤 남는 것만 오류로 본다
        if _CJK_PROBE.search(field):
            stripped = _PAREN_RE.sub("", field)
            for m in _CJK_CTX.finditer(stripped):
                snippet = m.group(0).replace("\n", " ").strip()
                if snippet and all(snippet != h[1] for h in hits):
                    hits.append(("한자", snippet))
    return hits


def has_script_leak(title: str, body: str) -> bool:
    """저장 시점 하드 블록용. 판정 근거 문맥 없이 True/False만 필요할 때."""
    return bool(detect_script_leak(title, body))
