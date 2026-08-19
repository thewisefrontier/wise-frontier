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

한자(CJK)는 별도 처리한다. "고(故)", "대(對)중국", "시진핑(習近平)"처럼
괄호 안 병기는 한국 언론의 정상 관행이므로, 괄호 구간을 제거한 뒤에도
남는 한자만 오류로 본다. 실측(2026-08-05, 7/20 이후) 이 방식으로 오탐 0건.
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
]

_SCRIPT_LEAK_RE = [
    (name, re.compile("[" + rng + "]"), re.compile(".{0,14}[" + rng + "]+.{0,14}"))
    for name, rng in _SCRIPT_LEAK_RANGES
]

# 괄호 병기 구간. 20자 상한은 긴 괄호가 통째로 지워져 오류를 놓치는 것을 막는다.
_PAREN_RE = re.compile(r"\([^)]{0,20}\)")
_CJK_PROBE = re.compile("[一-鿿]")
_CJK_CTX = re.compile(".{0,14}[一-鿿]+.{0,14}")


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
