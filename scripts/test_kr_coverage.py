# -*- coding: utf-8 -*-
"""kr_coverage 재정렬 회귀 테스트(네트워크·LLM 모킹). 실행: python scripts/test_kr_coverage.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import kr_coverage as kc  # noqa: E402

kc.time.sleep = lambda s: None
NO_TRENDS = lambda: []

assert [kc.scarcity_bonus(n) for n in (None, 0, 1, 3, 4, 14, 15, 99)] == [0, 15, 8, 8, 0, 0, -10, -10]


def cl(name, country, severe=False, review=False, en=""):
    c = [{"title_ko": name, "title_en": en, "country": country, "severe": severe}]
    if review:
        c.append({"__needs_review__": True})
    return c


# ── 희소성 ──
A, B, C, R = cl("아이티 갱단 폭력", "아이티"), cl("말라위 대통령 선거", "말라위"), cl("수단 대학살", "수단", severe=True), cl("혼합", "미상", review=True)
clusters = [A, C, B, R]
imp = {id(A): 30, id(C): 28, id(B): 20, id(R): 5}
llm = lambda p: json.dumps({"1": "갱단 폭력", "2": "수단 학살", "3": "말라위 대선"}, ensure_ascii=False)
counts = {"아이티 갱단 폭력": 40, "수단 학살": 50, "말라위 대선": 0, "말라위": 2}
key = lambda c: c[0]["title_ko"]
out, info = kc.rerank(clusters, lambda c: imp[id(c)], lambda c: c[0]["severe"], key, llm,
                      count=lambda q: counts[q], fetch_trends=NO_TRENDS)
# 말라위 20+15=35, 수단 28(중대성이라 감점 면제), 아이티 30-10=20
assert [key(c) for c in out] == ["말라위 대통령 선거", "수단 대학살", "아이티 갱단 폭력", "혼합"], out
assert info == {"아이티 갱단 폭력": {"kr_coverage_30d": 40}, "수단 대학살": {"kr_coverage_30d": 50},
                "말라위 대통령 선거": {"kr_coverage_30d": 0}}, info

assert kc.measure(cl("x", "태국"), "태국 희귀어", count=lambda q: {"태국 희귀어": 0, "태국": 40}[q]) == (0, 8.0)
assert kc.measure(cl("x", "태국"), "태국 반도체", count=lambda q: None) == (None, 0.0)
assert kc.rerank(clusters, lambda c: imp[id(c)], lambda c: False, key, lambda p: "모름", fetch_trends=NO_TRENDS)[0] == clusters
def boom(p): raise RuntimeError("quota")
assert kc.rerank(clusters, lambda c: imp[id(c)], lambda c: False, key, boom, fetch_trends=NO_TRENDS)[0] == clusters

# ── 검색 수요 ──
xml = """<rss><channel>
<item><title>일본 대 우루과이</title><ht:approx_traffic>2,000+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>우루과이, 일본에 1-3 패배</ht:news_item_title></ht:news_item>
<ht:news_item><ht:news_item_title>&apos;역전&apos; 일본</ht:news_item_title></ht:news_item></item>
<item><title>심형래</title><ht:approx_traffic>200+</ht:approx_traffic></item>
</channel></rss>"""
kc.requests.get = lambda *a, **k: type("R", (), {"text": xml})()
tr = kc.fetch_kr_trending()
assert tr == [{"keyword": "일본 대 우루과이", "traffic": 2000, "news": ["우루과이, 일본에 1-3 패배", "'역전' 일본"]},
              {"keyword": "심형래", "traffic": 200, "news": []}], tr

cls_llm = lambda p: '결과: [{"i": 1, "groups": [["Japan","일본"],["Uruguay","우루과이"]]}, {"i": 2, "groups": [["x"]]}]'
ct = kc.classify_trends(tr, cls_llm)
assert [t["keyword"] for t in ct] == ["일본 대 우루과이"] and ct[0]["groups"] == [["japan", "일본"], ["uruguay", "우루과이"]]

J = cl("", "일본", en="Japan beat Uruguay 3-1 in friendly")
Jonly = cl("", "일본", en="Japan raises interest rates")
assert kc.match_trend(J, ct)["keyword"] == "일본 대 우루과이"
assert kc.match_trend(Jonly, ct) is None  # 국가명만 겹치면 매칭 안 됨

# 수요 매칭 클러스터는 top_n 밖이어도 끌어올려 +30, 희소성 조회 안 함
X = cl("말라위 옥수수", "말라위")
imp2 = {id(X): 25, id(J): 3, id(Jonly): 10}
llm2 = lambda p: cls_llm(p) if "급상승" in p else json.dumps({"1": "말라위 옥수수"}, ensure_ascii=False)
out, info = kc.rerank([X, Jonly, J], lambda c: imp2[id(c)], lambda c: False, lambda c: c[0]["title_en"] or c[0]["title_ko"],
                      llm2, count=lambda q: {"말라위 옥수수": 20}[q], fetch_trends=lambda: tr, top_n=1)
assert out[0] is J and out[1] is X and out[2] is Jonly, [c[0] for c in out]  # 후보 J 3+30=33 > X 25-10=15, 후보 밖 Jonly는 뒤에
assert info["Japan beat Uruguay 3-1 in friendly"]["kr_trend"]["traffic"] == 2000
assert "kr_trend" not in info.get("말라위 옥수수", {})

s = kc.demand_prompt_suffix(ct[0])
assert "일본 대 우루과이" in s and "2,000" in s and "우루과이, 일본에 1-3 패배" in s and "원문에 없는" in s
print("ok")

# 가산점은 프론티어 국가만(선진국 연예 기사가 +15 받던 실운영 사고), 감점은 그대로
US, ML = cl("미국 배우", "미국"), cl("말라위 옥수수", "말라위")
imp3 = {id(US): 20, id(ML): 10}
q3 = lambda p: json.dumps({"1": "미국 배우", "2": "말라위 옥수수"}, ensure_ascii=False)
out, info = kc.rerank([US, ML], lambda c: imp3[id(c)], lambda c: False, key, q3,
                      count=lambda q: 0, fetch_trends=NO_TRENDS, bonus_eligible=lambda c: c[0]["country"] != "미국")
assert out[0] is ML, [c[0] for c in out]  # 말라위 10+15=25 > 미국 20+0
print("ok2")
