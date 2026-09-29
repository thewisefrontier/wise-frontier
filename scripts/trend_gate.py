# -*- coding: utf-8 -*-
"""scripts/trend_gate.py
------------------------
트렌드 기사(gemini_summarizer.py의 run_trend_tracker / run_realtime_trend_tracker /
run_external_trend_articles)는 일반 기사(gemini_writer.py)가 이미 쓰던 발행 전 검증 중 미번역 외국어·
고유명사 날조·숫자 콤마 검사가 빠져 있었다(2026-09-29 점검, id=294288 "kızamık(…문맥상 홍역)" 유출).
이 모듈이 그 검증을 같은 코드(fabrication_guard, style_guard)로 트렌드 경로에 붙인다.

run_trend_gates(...) -> (body, extras, reason)
  reason이 ""이면 통과, 아니면 미발행 사유. 근거 없는 이름이 든 문장만 지워 살릴 수 있으면(일반 기사와 같은 규칙)
  고친 본문을 돌려주고, 3줄요약·투자아이디어에 같은 이름이 남으면 그 항목은 비운다.
"""
from fabrication_guard import (detect_foreign_leftover, verify_no_fabricated_names,
                               drop_flagged_sentences, scrub_extras)
from style_guard import strip_number_commas

TREND_REPAIR_MIN_LEN = 400  # article_store.TREND_MIN_BODY_LEN과 같은 하한


def source_text(articles) -> str:
    """원문 대조용 텍스트: 소스 기사들의 제목+본문(자르지 않음 — 검사 쪽에서 창을 자른다)."""
    parts = []
    for a in articles or []:
        t = a.get("title_ko") or a.get("title_en") or ""
        b = a.get("full_text") or a.get("summary_ko") or a.get("summary_en") or ""
        parts.append(f"{t}\n{b}")
    return "\n\n".join(parts)


def run_trend_gates(title, body, sources, call_llm, extras=(), check_names=True):
    body = strip_number_commas(body)
    fl = detect_foreign_leftover(body, call_llm)
    if fl:
        return body, extras, f"번역 누락 — 외국어 잔존: {fl}"
    src = source_text(sources)
    if check_names and src.strip():
        suspect = verify_no_fabricated_names(src, body, call_llm)
        if suspect:
            fixed = drop_flagged_sentences(body, title, suspect, min_len=TREND_REPAIR_MIN_LEN)
            if not fixed:
                return body, extras, f"고유명사 날조 의심 — {suspect}"
            print(f"  ✂️ 근거 없는 이름 문장 삭제 후 진행: {suspect[:80]}")
            body, extras = fixed, scrub_extras(suspect, *extras)
    return body, extras, ""
