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

# 2026-11-07 제목-본문 불일치(322909): 제목 금액이 본문에 없으면 첫 소식 소제목으로
assert d.fix_stale_title("[오늘의 스타트업] 지라인, 15억 달러 투자 유치", "[세슘아스트로 7억3000만 달러 유치]\n- 6일 유치") == "[오늘의 스타트업] 세슘아스트로 7억3000만 달러 유치"
assert d.fix_stale_title("[오늘의 스타트업] 세슘, 7억3000만 달러", "[세슘 7억3000만 달러]\n- x") == "[오늘의 스타트업] 세슘, 7억3000만 달러"
assert d.fix_stale_title("[오늘의 AI 소식] 주요 동향", "[a 1억 달러]\n- x") == "[오늘의 AI 소식] 주요 동향"
# 2026-11-08 금액 단위(323178·323179): 1조 이상은 조 단위로
assert d.fix_jo("12500억 달러, 15000억 달러, 20000억 달러, 3000억 달러, 1조2500억 달러") == "1조2500억 달러, 1조5000억 달러, 2조 달러, 3000억 달러, 1조2500억 달러"
print("ok")

# 소식당 불릿 4개 미만은 빼고, 보도 현황 불릿은 지운다(2026-11-08 "최소 4줄")
body = ("[A사 1억 달러 유치]\n- 1\n- 2\n- 3\n- 4\n\n"
        "[B사 출시]\n- 1\n- 해당 소식은 다른 매체 2곳에서도 함께 보도됐다.\n- 2\n- 3\n\n"
        "[C사 인수]\n- 1\n- 2")
b2, short, n = d.drop_short_sections(body)
assert n == 1 and short == ["[B사 출시]", "[C사 인수]"] and "다른 매체" not in b2 and b2.startswith("[A사"), (b2, short)
# 금액 없는 제목인데 그 소식이 빠졌으면 첫 소제목으로 교체, 남아 있으면 유지
assert d.fix_stale_title("[오늘의 신기술] 핵융합 점화 성공", "[전고체 배터리 양산]\n- x") == "[오늘의 신기술] 전고체 배터리 양산"
assert d.fix_stale_title("[오늘의 신기술] 전고체 배터리 양산 외", "[전고체 배터리 양산]\n- x") == "[오늘의 신기술] 전고체 배터리 양산 외"
print("불릿 하한 ok")

# 재게시: 같은 소식인데 본문에 전년 라운드 금액이 추가돼 m<=pm이 깨지던 경우(2026-11-15 id=325671 랭체인, 전날 325332)
_prev = ["[AI 에이전트 플랫폼 랭체인, 3억 달러 투자 유치로 기업 가치 30억 달러 기록]\n- 1년 전 12억5000만 달러 가치."]
_new = ("[AI 에이전트 플랫폼 랭체인, 시리즈 C 투자 유치로 기업 가치 30억 달러 기록]\n- 3억 달러 유치, 1억2500만 달러 유치 당시 12억5000만 달러 가치의 2배.\n\n"
        "[가라지, 4100만 달러 투자 유치]\n- 4100만 달러.")
_b, _dr = d.drop_reposted(_new, _prev)
assert len(_dr) == 1 and "랭체인" in _dr[0] and "가라지" in _b, (_dr, _b)
# 오탐 대조: 금액이 다른 같은 회사 새 라운드는 유지
assert d.drop_reposted("[AI 에이전트 플랫폼 랭체인, 5억 달러 투자 유치]\n- 5억 달러.", _prev)[1] == []
print("재게시 금액 추가 ok")

# 날짜 근거 게이트(2026-11-17 326625 애플 16→15일, 326626 소풍커넥트 15→13일): 소제목 "(원문 N)"으로 원문을 잇고
# 그 원문의 날짜 근거(요일·명시 날짜)에 없는 날짜 구절만 지운다.
cands = [
    {"title_en": "Apple needs your help to make health AI better", "source_published_at": "2026-11-16T08:52:29+00:00",
     "full_text": "Apple said in a newsroom post published Sunday ... Adults aged 21 and older"},
    {"title_en": "소풍커넥트, 데모데이 개최", "source_published_at": "2026-11-16T06:21:41+00:00",
     "full_text": "소풍커넥트는 지난 13일 서울 광진구 NEST 광진에서 데모데이를 개최했다. 지난 10월 21일과 28일 열린 MINI IR DAY"},
    {"title_en": "Plural raises €1bn", "source_published_at": "2026-11-16T13:43:51+00:00",
     "full_text": "Plural announced the money on Monday on its website."},
    {"title_en": "Acme raises $5M seed", "source_published_at": "2026-11-16T05:00:00+00:00", "full_text": "Acme has raised a seed round."},
]
raw = ("[애플 연구] (원문 1)\n- 애플은 새로운 연구 조사를 11월 16일 발표했다.\n- 21세 이상이 참여한다.\n\n"
       "[소풍 데모데이] (원문 2)\n- 11월 15일 소풍커넥트가 데모데이를 개최했다.\n- 10월 21일과 28일 사전 행사를 열었다.\n\n"
       "[플루럴] (원문 3)\n- 11월 16일(현지시간) 플루럴이 펀드를 조성했다.\n- 30일 이내에 집행한다.\n\n"
       "[애크미] （원문 4）\n- 11월 16일 애크미가 시드 투자를 유치했다.\n- 11월 3일부터 접수했다.\n\n"
       "[표시 없음]\n- 11월 1일 무언가 했다.")
b, tags = d.split_source_tags(raw)
assert "원문" not in b and tags == {"[애플 연구]": [1], "[소풍 데모데이]": [2], "[플루럴]": [3], "[애크미]": [4]}, (b, tags)
out, rm = d.strip_unsupported_dates(b, tags, cands)
assert "조사를 발표했다." in out and "- 소풍커넥트가 데모데이를" in out, out          # 근거(15일·13일)와 다른 날짜 삭제
assert "11월 16일(현지시간) 플루럴" in out and "11월 16일 애크미" in out, out      # 요일 근거 일치 / 근거 없으면 발행일 허용
assert "10월 21일과 28일" in out and "30일 이내" in out and "11월 3일부터" in out and "11월 1일 무언가" in out, out  # 범위·기간·표시 없는 소식은 그대로
assert len(rm) == 2, rm
ok = raw.replace("11월 16일 발표", "11월 15일 발표").replace("11월 15일 소풍", "11월 13일 소풍")
assert d.strip_unsupported_dates(*d.split_source_tags(ok), cands)[1] == []          # 올바른 날짜는 건드리지 않음
print("date gate ok")
# "layer"·"fewer"를 ayer(어제)로 읽어 발행 전날만 근거로 삼던 오탐(11/17 시험, 드림팀·액슬)
c = {"title_en": "Dreamteam Raises $53M", "source_published_at": "2026-11-16T14:00:24+00:00", "full_text": "That intelligence layer captures fewer errors."}
assert d.strip_unsupported_dates("[드림팀]\n- 11월 16일 드림팀이 투자를 유치했다.", {"[드림팀]": [1]}, [c])[1] == []
assert "(월) 발행]" not in d.build_digest_prompt(d.TOPICS["startup"], [c]) and "[원문 1] Dreamteam" in d.build_digest_prompt(d.TOPICS["startup"], [c])
print("date gate 오탐 ok")

# 2026-11-26 id=330337: "Rs 28.80 crore"가 "28억8000만루피"(실제 2억8800만, 10배)로 발행 — 루피도 병기·검증한다.
a = d.annotate_money("Prosus will invest Rs 28.80 crore; round of Rs 38.4 crore at Rs 21,221.92 per share")
assert "[2억8800만 루피]" in a and "[3억8400만 루피]" in a, a
allowed = d._allowed_money([{"title_en": "Vegapay", "full_text": "Prosus will invest Rs 28.80 crore"}])
out, dropped, _ = d.strip_unverified_money("[베가페이]\n- 프로수스가 2억8800만루피를 투자한다.\n- 28억8000만루피를 투자한다.", allowed)
assert dropped == ["- 28억8000만루피를 투자한다."] and "2억8800만루피" in out, (out, dropped)
print("rupee ok")
