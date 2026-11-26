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

# 쓴 원문 제목: 소제목이 가리킨 원문의 묶음만
cands = [{"_cluster_titles": ["A", "A2"]}, {"_cluster_titles": ["B"]}]
assert d.used_titles({"[x]": [1]}, cands) == ["A", "A2"]
assert d.used_titles({}, cands) == ["A", "A2", "B"]
print("OK")
