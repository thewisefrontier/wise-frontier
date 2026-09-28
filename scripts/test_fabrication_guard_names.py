# -*- coding: utf-8 -*-
"""위키 미확인 이름을 원문 근거로 재확인하는 names_without_source_support 회귀 테스트(2026-09-28).
실행: python scripts/test_fabrication_guard_names.py   (실제 NVIDIA 시험은 NVIDIA_API_KEY가 있을 때만)"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import fabrication_guard as g  # noqa: E402

names = ["구글(Google)", "유엔 안전보장이사회", "제미나이 프라임"]
src = "Google and Amazon announced ... The UN Security Council met on Monday ... Flipkart said ..."

# 1) 가짜 NVIDIA: 지어낸 이름만 지목 / 없음 / 엉뚱한 응답 / 예외
g.call_nvidia = lambda p, max_tokens=0: "제미나이 프라임"
assert g.names_without_source_support(names, src) == ["제미나이 프라임"]
g.call_nvidia = lambda p, max_tokens=0: "없음"
assert g.names_without_source_support(names, src) == []
g.call_nvidia = lambda p, max_tokens=0: "없음 (모든 이름이 원문에 근거가 있음)"
assert g.names_without_source_support(names, src) == []
g.call_nvidia = lambda p, max_tokens=0: "잘 모르겠습니다"
assert g.names_without_source_support(names, src) == names          # 엉뚱한 응답 → 보수적으로 그대로
def boom(*a, **k): raise RuntimeError("x")
g.call_nvidia = boom
assert g.names_without_source_support(names, src) == names          # 실패 → 그대로
g.call_nvidia = None
assert g.names_without_source_support(names, src) == names          # 미설정 → 그대로
assert g.names_without_source_support([], src) == []
# 두 번 다 걸린 이름만 남긴다(2026-09-29) / 두 번째 실패면 첫 결과 유지
seq = iter(["제미나이 프라임, 구글(Google)", "제미나이 프라임"])
g.call_nvidia = lambda p, max_tokens=0: next(seq)
assert g.names_without_source_support(names, src) == ["제미나이 프라임"]
seq = iter(["제미나이 프라임", "없음"])
g.call_nvidia = lambda p, max_tokens=0: next(seq)
assert g.names_without_source_support(names, src) == []
seq = iter(["제미나이 프라임"])
g.call_nvidia = lambda p, max_tokens=0: next(seq)                    # 두 번째 호출 예외(StopIteration)
assert g.names_without_source_support(names, src) == ["제미나이 프라임"]
g.call_nvidia = None
print("단위 테스트 ok")

# 2) 실제 NVIDIA(키 있을 때): 진짜 이름은 통과, 지어낸 이름만 남는지
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    import importlib
    import nvidia_client
    importlib.reload(nvidia_client)
    g.call_nvidia = nvidia_client.call_nvidia
    if os.environ.get("NVIDIA_API_KEY"):
        out = g.names_without_source_support(names, src)
        print("실제 NVIDIA 결과(기대: ['제미나이 프라임']):", out)
        assert "구글(Google)" not in out and "유엔 안전보장이사회" not in out, out
        assert "제미나이 프라임" in out, out
        print("실제 NVIDIA ok")
except AssertionError:
    raise
except Exception as e:
    print("실제 NVIDIA 시험 건너뜀:", e)

# 2차 검수(second_review): PASS/FAIL/불분명/실패/미설정 — 통과가 아니면 발행하지 않는다(fail-closed). 2026-09-28
g.call_nvidia = lambda p, max_tokens=0: "PASS"
assert g.second_review("t", "본문", "자료") == (True, "2차 검수 통과")
g.call_nvidia = lambda p, max_tokens=0: "FAIL: 자료에 없는 수치"
ok, why = g.second_review("t", "본문", "자료"); assert not ok and "자료에 없는 수치" in why
g.call_nvidia = lambda p, max_tokens=0: "글쎄요"
assert g.second_review("t", "본문", "자료")[0] is False
g.call_nvidia = boom
assert g.second_review("t", "본문", "자료")[0] is False
g.call_nvidia = None
assert g.second_review("t", "본문", "자료")[0] is False
g.call_nvidia = lambda p, max_tokens=0: "PASS"
assert g.second_review("t", "본문", "")[0] is False          # 근거 자료가 없으면 검수 불가 → 보류
print("second_review 단위 테스트 ok")


def test_drop_flagged_sentences():
    import fabrication_guard as g
    body = ("네팔 북부에서 폭우로 25명이 숨졌다. 부상자는 트리부반 대학교 부속 병원으로 옮겨졌다. 당국은 수색을 이어가고 있다.\n"
            + "당국은 도로 복구에 나섰다. " * 30)
    assert g.flagged_names("[위키 미확인] 트리부반 대학교 부속 병원, 히말라야 홀리데이스") == ["트리부반 대학교 부속 병원", "히말라야 홀리데이스"]
    assert g.flagged_names("[이름] 고란 베시치(Goran Vesić) → 없음 (원본에 없음)") == ["고란 베시치(Goran Vesić)"]
    out = g.drop_flagged_sentences(body, "네팔 폭우 참사", "[위키 미확인] 트리부반 대학교 부속 병원", min_len=100)
    assert out and "트리부반" not in out and "25명이 숨졌다." in out and "수색을 이어가고 있다." in out
    assert g.drop_flagged_sentences(body, "트리부반 대학교 부속 병원 참사", "[위키 미확인] 트리부반 대학교 부속 병원", min_len=100) is None
    assert g.drop_flagged_sentences(body, "t", "[위키 미확인] 트리부반 대학교 부속 병원", min_len=5000) is None
    assert g.drop_flagged_sentences(body, "t", "없는이름 그냥 텍스트", min_len=100) is None

test_drop_flagged_sentences()
print("drop_flagged_sentences 단위 테스트 ok")
