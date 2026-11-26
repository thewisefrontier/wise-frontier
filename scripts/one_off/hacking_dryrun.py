# -*- coding: utf-8 -*-
"""오늘의 해킹 시험 생성(2026-11-26, 저장·이미지 업로드 없음). 실행: one_off_task.yml(script=one_off/hacking_dryrun.py)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ai_digest as d  # noqa: E402

_orig = d.get_candidates


def _show(topic, *a, **k):
    cands = _orig(topic, *a, **k)
    print(f"\n후보(사건 대표) {len(cands)}건")
    for c in cands:
        print(f"  dup={c.get('_dup_count'):>2} body={len(c.get('full_text') or ''):>5} | {(c.get('title_en') or '')[:90]}")
        for t in (c.get("_cluster_titles") or [])[1:6]:
            print(f"        └ {t[:90]}")
    return cands


_last = {}


def _save(topic, title, body, n, **k):
    _last.update(body=body, used=k.get("used_titles") or [])
    print(f"\n[제목] {title}\n[본문 {len(body)}자]\n{body}\n\n[used_titles {len(k.get('used_titles') or [])}건]")
    return 1


d.get_candidates = _show
d.save_digest = _save
d.fetch_article_image = lambda *a, **k: ""
d.digest_exists_for_today = lambda t: False
d.run("hacking")

# 재게시 시험: 방금 결과를 "어제 연재"로 놓고 후보를 다시 뽑으면 같은 사건이 빠져야 한다(Gemini 호출 없음).
if _last:
    d._prev_cache[d.TOPICS["hacking"]["key"]] = [{"summary_ko": _last["body"], "source_data": {"used_titles": _last["used"]}}]
    again = _orig(d.TOPICS["hacking"])
    print(f"\n[재게시 시험] 어제 연재로 가정 후 남은 후보 {len(again)}건:")
    for c in again:
        print(f"  | {(c.get('title_en') or '')[:90]}")
