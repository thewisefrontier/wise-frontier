# -*- coding: utf-8 -*-
"""
트렌드 기사 발행 게이트 회귀 테스트 (2026-09-22 신설).

이 게이트가 잘못 동작하면 (a) 빈약한 기사가 그대로 발행되거나
(b) 멀쩡한 기사가 조용히 미발행 처리된다. 둘 다 눈에 잘 안 띄므로 테스트로 고정한다.
LLM 판정은 가짜 함수로 갈아끼워 네트워크 없이 검증한다.

실행: python scripts/test_trend_gate.py
"""
import sys

# 게이트가 ⚠️ 이모지를 출력하는데 Windows 기본 콘솔(cp949)에서는 인코딩 에러가
# 난다(프로덕션인 GitHub Actions는 UTF-8이라 무관). 로컬에서도 테스트가 돌게 맞춘다.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import article_store as store  # noqa: E402

LONG_OK = "가" * 800
MID = "나" * 500          # 400~700 → LLM 판정 대상
SHORT = "다" * 300        # 400 미만 → 길이만으로 차단


def payload(sub="realtrend_test", body=MID, published=True, source="NewsFinal"):
    return {"source": source, "subcategory": sub, "summary_ko": body,
            "is_published": published,
            "update_log": [{"timestamp": "2026-09-22 10:00", "note": "최초 게시"}]}


def gate(p, llm=lambda body: (False, "")):
    """LLM 판정을 주입해 게이트를 돌린다."""
    orig = store.judge_thin_by_llm
    store.judge_thin_by_llm = llm
    try:
        store._gate_thin_trend(p)
    finally:
        store.judge_thin_by_llm = orig
    return p


def test_short_trend_blocked():
    p = gate(payload(body=SHORT))
    assert p["is_published"] is False
    assert "분량 부족" in p["update_log"][0]["note"]


def test_normal_length_passes():
    assert gate(payload(body=MID))["is_published"] is True


def test_long_skips_llm_entirely():
    """700자 이상은 판정을 아예 돌리지 않는다(호출 낭비 방지)."""
    called = []

    def spy(body):
        called.append(body)
        return True, "THIN"

    p = gate(payload(body=LONG_OK), llm=spy)
    assert p["is_published"] is True
    assert called == [], "700자 이상인데 LLM을 호출했다"


def test_llm_thin_blocks():
    p = gate(payload(body=MID), llm=lambda b: (True, "VERDICT: THIN | 사실수: 2"))
    assert p["is_published"] is False
    assert "밀도 부족" in p["update_log"][0]["note"]


def test_llm_failure_fails_open():
    """외부 API가 죽어도 발행을 막으면 안 된다."""
    assert gate(payload(body=MID), llm=lambda b: (False, ""))["is_published"] is True


def test_non_trend_untouched():
    """주간시세·복권 같은 템플릿 기사는 짧아도 건드리지 않는다."""
    assert gate(payload(sub="주간시세", body=SHORT))["is_published"] is True


def test_already_unpublished_reason_kept():
    """다른 사유로 이미 미발행이면 그 사유를 덮어쓰지 않는다."""
    p = payload(body=SHORT, published=False)
    p["update_log"][0]["note"] = "다주제 혼입 미발행"
    assert gate(p)["update_log"][0]["note"] == "다주제 혼입 미발행"


def test_other_source_untouched():
    assert gate(payload(source="Reuters", body=SHORT))["is_published"] is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
