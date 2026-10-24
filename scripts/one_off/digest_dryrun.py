"""
scripts/one_off/digest_dryrun.py
--------------------------------
스타트업·신기술 연재(ai_digest.py TOPICS) 발행 전 점검: 후보 선정 결과와 AI 초안을 보여주기만 하고
DB에는 아무것도 쓰지 않는다(저장·이미지 업로드 없음).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ai_digest as d  # noqa: E402
from daily_digest import parse_title_and_body, format_digest_body, keep_first_local_time, check_date_hallucination, now_kst  # noqa: E402

for name in ("startup", "newtech"):
    topic = d.TOPICS[name]
    print(f"\n==================== {topic['tag']} ====================")
    cands = d.get_candidates(topic)
    print(f"후보 {len(cands)}건 (중복 보도 많은 순)")
    for c in sorted(cands, key=lambda x: -(x.get("_dup_count") or 0))[:25]:
        print(f"  dup={c.get('_dup_count'):>2} | {(c.get('title_en') or c.get('title_ko') or '')[:90]}")
    if len(cands) < d.AI_DIGEST_MIN_CANDIDATES:
        print("→ 후보 부족, 초안 생략")
        continue
    content = d.call_gemini_article(d.build_digest_prompt(topic, cands), max_tokens=8000)
    if not content:
        print("→ Gemini 응답 없음")
        continue
    title, body = parse_title_and_body(content)
    title, body, dropped, n_sections = d.apply_money_guard(topic, title, body, cands)
    for line in dropped:
        print(f"  ✂️ 금액 검증 삭제: {line[:100]}")
    print(f"  금액 검증 후 소식 {n_sections}건")
    bad, why = check_date_hallucination(body, d._digest_sources(cands), base_date=now_kst().date())
    print(f"\n[제목] {title}\n[날짜가드] {'걸림: ' + why if bad else '통과'}  [본문 {len(body)}자]\n")
    print(format_digest_body(keep_first_local_time(body)))
