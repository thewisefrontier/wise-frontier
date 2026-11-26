# -*- coding: utf-8 -*-
"""오늘의 해킹: 다른 사건이 공통 낱말로 묶이지 않고, 어제 다룬 사건은 다시 걸리는지. 실행: python scripts/test_hacking_digest.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_digest as d  # noqa: E402

t = d.TOPICS["hacking"]
kw = lambda s: d._topic_kw(t, {"title_en": s})  # noqa: E731

# 서로 다른 사건: 공통 낱말(hackers·data·breach)만 겹치면 묶이면 안 된다
assert len(kw("Hackers breach 1Password, steal customer data") & kw("Hackers steal Danish CPR registry data in breach")) < 2
# 같은 사건(다음 날 다른 매체): 고유어 2개 이상 겹쳐 재게시로 걸려야 한다
assert len(kw("CJ올리브영 해킹, 고객 개인정보 유출") & kw("CJ올리브영 업무시스템 해킹으로 790명 유출")) >= 1
assert len(kw("1Password confirms breach of internal vault systems") & kw("1Password says hackers accessed internal vault")) >= 2

# 키워드: 'breach of restraining order'·'Lifehacker'는 안 걸린다
assert not d._matches(t, {"title_en": "Man jailed for breach of restraining order"})
assert not d._matches(t, {"title_en": "Lifehacker picks the best laptops"})
assert d._matches(t, {"title_en": "Ransomware gang hits hospital chain"})

# 11/26 시험에서 한 덩어리가 됐던 서로 다른 사건들
assert len(kw("랜섬웨어 조직 에베레스트, 코스닥 상장사 \"고영테크놀러지 해킹\" 주장…28일 데이터 공개 예고")
           & kw("코스닥 상장사 팸텍, 랜섬웨어 공격 당해…주주·임직원 개인정보 유출 KISA 신고")) < 2
assert len(kw("[단독] 가상자산 전문매체 토큰포스트 해킹…회원정보·지갑 주소·토큰 거래 내역까지 유출 정황")
           & kw("[단독] 다우키움그룹 계열사 이머니 ‘엑스원’ 해킹 정황…고객 개인정보 유출")) < 2

# 이전 연재 소제목과 같은 사건은 뺀다, 다른 사건은 둔다
d._prev_cache[t["key"]] = [{"summary_ko": "[비밀번호 관리 업체 1Password 백엔드 시스템 해킹으로 고객 정보 유출]\n- a"}]
body = "[1Password 백엔드 해킹 추가 조사 결과 공개]\n- x\n\n[전북은행 AI 에이전트 해킹]\n- y"
out, dropped = d.drop_previous_events(t, body)
assert dropped == ["[1Password 백엔드 해킹 추가 조사 결과 공개]"] and "전북은행" in out, (out, dropped)
d._prev_cache.clear()

# 외국 문자·잘못된 만 단위 불릿 삭제
out, dropped = d.drop_bad_lines("[a]\n- 한전ON 고객 65048만명\n- 10월에 880万명\n- BGP 하이ジャッキング\n- 정상 문장이다.")
assert len(dropped) == 3 and "정상" in out, dropped

# 쓴 원문 제목: 소제목이 가리킨 원문의 묶음만
cands = [{"_cluster_titles": ["A", "A2"]}, {"_cluster_titles": ["B"]}]
assert d.used_titles({"[x]": [1]}, cands) == ["A", "A2"]
assert d.used_titles({}, cands) == ["A", "A2", "B"]
print("OK")
