# -*- coding: utf-8 -*-
"""kr_coverage 재정렬 회귀 테스트(네트워크·LLM 모킹). 실행: python scripts/test_kr_coverage.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import kr_coverage as kc  # noqa: E402

kc.time.sleep = lambda s: None

assert [kc.scarcity_bonus(n) for n in (None, 0, 1, 3, 4, 14, 15, 99)] == [0, 15, 8, 8, 0, 0, -10, -10]


def cl(name, country, severe=False, review=False):
    c = [{"title_ko": name, "country": country, "severe": severe}]
    if review:
        c.append({"__needs_review__": True})
    return c


A, B, C, R = cl("아이티 갱단 폭력", "아이티"), cl("말라위 대통령 선거", "말라위"), cl("수단 대학살", "수단", severe=True), cl("혼합", "미상", review=True)
clusters = [A, C, B, R]  # 중요도 순이라고 가정
imp = {id(A): 30, id(C): 28, id(B): 20, id(R): 5}
llm = lambda p: json.dumps({"1": "갱단 폭력", "2": "수단 학살", "3": "말라위 대선"}, ensure_ascii=False)  # 1번은 국가명 누락
counts = {"아이티 갱단 폭력": 40, "수단 학살": 50, "말라위 대선": 0, "말라위": 2}
out, n = kc.rerank(clusters, lambda c: imp[id(c)], lambda c: c[0]["severe"], lambda c: c[0]["title_ko"],
                   llm, count=lambda q: counts[q])
# 말라위 20+15=35, 아이티 30-10=20, 수단은 중대성이라 감점 면제 28 → 말라위, 수단, 아이티, 검토필요
assert [c[0]["title_ko"] for c in out] == ["말라위 대통령 선거", "수단 대학살", "아이티 갱단 폭력", "혼합"], out
assert n == {"아이티 갱단 폭력": 40, "수단 대학살": 50, "말라위 대통령 선거": 0}, n

# 검색어 0건인데 국가는 흔하면 +8까지만
assert kc.measure(cl("x", "태국"), "태국 희귀어", count=lambda q: {"태국 희귀어": 0, "태국": 40}[q]) == (0, 8.0)
# 조회 실패(None)면 가산 없음
assert kc.measure(cl("x", "태국"), "태국 반도체", count=lambda q: None) == (None, 0.0)

# LLM이 깨진 응답을 주거나 예외가 나도 원래 순서 유지
assert kc.rerank(clusters, lambda c: imp[id(c)], lambda c: False, lambda c: "k", lambda p: "모름")[0] == clusters
def boom(p): raise RuntimeError("quota")
assert kc.rerank(clusters, lambda c: imp[id(c)], lambda c: False, lambda c: "k", boom)[0] == clusters
print("ok")
