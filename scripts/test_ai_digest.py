# -*- coding: utf-8 -*-
"""ai_digest.py 회귀 테스트(2026-10-25): 키워드 단어 경계·외화 금액 병기/검증. 실행: python scripts/test_ai_digest.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import ai_digest as d  # noqa: E402

T = d.TOPICS
# 부분문자열 오탐(시험 실측): lidar⊂solidarity, anthropic⊂philanthropic, ai⊂Dubai
assert not d._matches(T["newtech"], {"title_en": "Solidarity march in Paris"})
assert d._matches(T["newtech"], {"title_en": "New Lidar sensor unveiled"})
assert not d._matches(T["ai"], {"title_en": "Philanthropic gift to museum"})
assert not d._matches(T["ai"], {"title_en": "Dubai-bound flight"}) and d._matches(T["ai"], {"title_en": "Why AI is hot"})
assert d._matches(T["startup"], {"title_en": "Three startups raise funds"})

# 금액: 코드가 환산해 병기하고, 병기에 없는 금액은 불릿째 지운다.
a = d.annotate_money("raises $200M; valuation of $1.2 billion, $277,000, 50 million dollars, €15M")
for s in ("[2억 달러]", "[12억 달러]", "[27만7000 달러]", "[5000만 달러]", "[1500만 유로]"):
    assert s in a, a
allowed = d._allowed_money([{"title_en": "Acme raises $200M", "full_text": "valuation of $1.2 billion"}])
body = "[투자]\n- 2억 달러를 유치했다.\n- 기업가치는 12억 달러다.\n\n[오류]\n- 200만 달러를 유치했다.\n- 다른 사실이다.\n\n[남김]\n- 사실만."
out, dropped, n = d.strip_unverified_money(body, allowed)
assert "200만" not in out and dropped == ["- 200만 달러를 유치했다."] and n == 3, (out, dropped, n)
out, dropped, n = d.strip_unverified_money("[잘못 200만 달러]\n- 사실.\n\n[좋음]\n- 사실.", allowed)
assert "[잘못" not in out and n == 1

# 천 단위 쉼표는 사이트 표기("6000만")에 맞춰 제거, 제목의 검증 안 된 금액은 기본 제목으로.
t, b, _, _ = d.apply_money_guard(T["startup"], "[오늘의 스타트업] 에이크미 6,000만 달러 유치", "[A]\n- 6,000만 달러 유치.",
                                 [{"title_en": "Acme raises $60M", "full_text": ""}])
assert t.endswith("6000만 달러 유치") and "6000만 달러" in b, (t, b)
t, _, _, _ = d.apply_money_guard(T["startup"], "[오늘의 스타트업] 에이크미 1억 달러 유치", "[A]\n- 사실.", [{"title_en": "Acme raises $60M", "full_text": ""}])
assert t == "[오늘의 스타트업] 주요 동향", t

# 2026-10-28 id=319745: "R$ 2,9 bilhões"가 "[29 달러]"로 병기돼 "네오스페이스 29달러에 인수"로 발행.
# id=319744: "S$150 million"이 미국 달러로 병기. 접두 통화는 따로, 소수점 콤마는 숫자로 이어 붙이지 않는다.
a = d.annotate_money("Itaú compra NeoSpace por R$ 2,9 bilhões")
assert "달러" not in a, a
a = d.annotate_money("Singapore announces S$150 million; chip exports could fall by up to US$100 billion; Reuters $589 million")
assert "[1억5000만 싱가포르달러]" in a and "[1000억 달러]" in a and "[5억8900만 달러]" in a, a
assert "[1억5000만 달러]" not in a, a
allowed = d._allowed_money([{"title_en": "Singapore announces S$150 million investment", "full_text": ""}])
_, dropped, _ = d.strip_unverified_money("[싱가포르]\n- 1억5000만 달러를 투자했다.", allowed)
assert dropped, "S$를 미국 달러로 쓴 불릿이 통과됨"

# 2026-10-30 id=320245 [기업 분석] 원스톱 시스템즈: 주당순손실 "$0.02"가 "[0 달러]"로 병기돼 "주당 0 달러"로 발행.
a = d.annotate_money("net loss of $600,000 or $0.02 per share; EPS $1.25; $12.6 million; $3")
assert "[0.02 달러]" in a and "[1.25 달러]" in a and "[1260만 달러]" in a and "[3 달러]" in a, a
assert "0.02달러" in d._money_tokens(a), d._money_tokens(a)

# 2026-11-01 재게시 게이트: id=320496 리빌드 21억 달러(10/29 320018 재게시)·320495 알파벳 5000억 달러(10/30 320241) 제외,
# 새 금액이 섞인 소식(칼시 10억 + 하이브 1억2900만)·금액 없는 소식은 유지.
NL = chr(10)
prev = [NL.join(["[슬레이트 오토를 분사시킨 리빌드, 21억 달러 조달]", "- 리빌드가 21억 달러를 조달했다.", "",
                 "[알파벳 주가 10% 이상 하락]", "- 시가총액 5000억 달러가 증발했다.", "",
                 "[칼시, 10억 달러 유치]", "- 칼시가 10억 달러를 모았다."])]
new = NL.join(["[미국 제조기업 리빌드 매뉴팩처링, 21억 달러 투자 유치]", "- 리빌드가 21억 달러를 유치했다.", "",
               "[알파벳 주가 12.6% 이상 급락 및 5000억 달러 시총 증발]", "- 5000억 달러가 사라졌다.", "",
               "[크립토 VC 펀딩, 칼시 10억 달러 및 하이브 1억 2900만 달러 확보]", "- 하이브가 1억 2900만 달러 대출을 받았다.", "",
               "[앤스로픽, IPO 신청]", "- 앤스로픽이 신청서를 냈다.", "",
               "[파워래티스, 8600만 달러 유치]", "- 8600만 달러를 유치했다."])
b, dropped = d.drop_reposted(new, prev)
assert [x[:8] for x in dropped] == ["[미국 제조기업", "[알파벳 주가 "], dropped
assert "하이브" in b and "앤스로픽" in b and "파워래티스" in b and "리빌드" not in b and NL * 3 not in b, b
assert d.drop_reposted(new, [])[0] == new

a = d.annotate_money("Oracle plans $20-billion bond and share sale; a $5-million seed; $3 fee")  # 321735
assert "[200억 달러]" in a and "[500만 달러]" in a and "[3 달러]" in a and "[20 달러]" not in a, a

# 2026-11-04 날짜 밀림: 엄격 묶음(핵심어 3개 겹침)의 가장 이른 보도일을 쓰고, 2개만 겹치는 무관 기사는 섞지 않는다
pool = [{"source_published_at": "2026-11-03T15:00:00+00:00"}, {"source_published_at": "2026-11-02T20:00:00+00:00"},
        {"source_published_at": "2026-11-01T01:00:00+00:00"}]
kws = [{"openai", "quietly", "renaming", "data", "centers"}, {"openai", "rebrand", "data", "centers"}, {"openai", "data", "other"}]
assert d._first_pub(pool, kws, 0).startswith("2026-11-02"), d._first_pub(pool, kws, 0)
assert d._first_pub(pool, kws, 2).startswith("2026-11-01")

print("ok")
