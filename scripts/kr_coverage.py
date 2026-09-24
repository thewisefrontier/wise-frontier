# -*- coding: utf-8 -*-
"""국내 보도 희소성 — 한국어 보도가 드문 주제를 먼저 쓰게 클러스터 순서를 조정.

근거(2026-09-24): 서치콘솔 7월 상위 노출 검색어가 거의 전부 국내 언론이 안 다루는
틈새 주제(카보베르데·말라위·에티오피아)였고, 수단·미얀마·아이티처럼 국내도 쓰는
주제는 40~70위에 머물렀다. 가설 단계라 발행 기사 source_data.kr_coverage_30d에
측정값을 남겨 2~4주 뒤 서치콘솔 노출수와 대조해 검증한다.

측정: 구글뉴스 한국판 RSS는 관련도순으로 1년치를 섞어 주므로(최신순 아님) 72시간
창은 흔한 주제도 0건이 된다. 실측상 최근 30일 건수가 흔한/틈새 주제를 가른다
(아이티 갱단 29·몽골 광산 33 vs 말라위 0·부르키나파소 0). `when:3d` 연산자는
스팸만 돌려줘 쓰지 않는다.
"""
import calendar
import json
import re
import time
from urllib.parse import quote

import feedparser

WINDOW_DAYS = 30
TOP_N = 15


def count_kr_coverage(query, days=WINDOW_DAYS):
    """최근 days일 한국어 기사 수. RSS 결과 자체가 0건이면 0, 조회 실패면 None."""
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        d = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
    except Exception:
        return None
    if d.get("status") != 200:
        return None
    cutoff = time.time() - days * 86400
    return sum(1 for e in d.entries
               if e.get("published_parsed") and calendar.timegm(e.published_parsed) >= cutoff)


def scarcity_bonus(n):
    if n is None:
        return 0.0
    if n == 0:
        return 15.0
    if n <= 3:
        return 8.0
    if n < 15:
        return 0.0
    return -10.0


def build_queries(clusters, call_llm):
    """후보 클러스터마다 한국어 검색어(국가명 + 핵심어 2~3단어)를 lite 1회 호출로 받는다."""
    lines = []
    for i, c in enumerate(clusters, 1):
        titles = [(a.get("title_ko") or a.get("title_en") or "")[:80] for a in c[:3]]
        lines.append(f"{i}. 국가: {c[0].get('country') or '미상'} / 제목: " + " | ".join(titles))
    prompt = (
        "아래 각 뉴스 묶음을 한국어 뉴스 검색창에 넣을 검색어로 바꿔라. "
        "규칙: 국가명(한국어)을 반드시 포함, 핵심 대상어 1~2개를 더해 총 2~4단어, "
        "기사 제목 흉내 금지, 따옴표·연산자 금지. "
        '출력은 JSON 객체 하나만: {"1": "검색어", "2": "검색어", ...}\n\n' + "\n".join(lines)
    )
    raw = call_llm(prompt) or ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    for i, c in enumerate(clusters, 1):
        q = str(data.get(str(i)) or "").strip()
        country = (c[0].get("country") or "").strip()
        if q and country and country not in q:
            q = f"{country} {q}"
        if q:
            out[i - 1] = q
    return out


def measure(cluster, query, count=count_kr_coverage):
    """(측정값, 가산점). 검색어가 0건이면 너무 좁은 검색어일 수 있어 국가명만으로
    교차 확인 — 국가 자체도 드물면 +15, 국가는 흔하면 불확실로 보고 +8까지만."""
    n = count(query)
    if n != 0:
        return n, scarcity_bonus(n)
    country = (cluster[0].get("country") or "").strip()
    if not country or country == query:
        return 0, scarcity_bonus(0)
    cn = count(country)
    return 0, (scarcity_bonus(0) if cn is not None and cn < 15 else 8.0)


def rerank(clusters, importance, is_severe, cluster_key, call_llm, count=count_kr_coverage, top_n=TOP_N):
    """상위 top_n 후보만 희소성 가산점으로 재정렬. 중대성 높은 사안은 감점하지 않는다
    (국내 언론이 다룰 만큼 크다는 게 배제 사유가 아니라는 기존 방침).
    반환: (재정렬된 clusters, {cluster_key: 30일 국내 기사 수}). 실패하면 원래 순서."""
    try:
        cand_idx = [i for i, c in enumerate(clusters) if not any(a.get("__needs_review__") for a in c)][:top_n]
        if not cand_idx:
            return clusters, {}
        cands = [clusters[i] for i in cand_idx]
        queries = build_queries(cands, call_llm)
        scores, counts = {}, {}
        for j, c in enumerate(cands):
            q = queries.get(j)
            if not q:
                continue
            n, bonus = measure(c, q, count)
            if bonus < 0 and is_severe(c):
                bonus = 0.0
            scores[id(c)] = importance(c) + bonus
            counts[cluster_key(c)] = n
            print(f"  [국내보도] {n if n is not None else '?'}건 {bonus:+.0f} ← '{q}'")
            time.sleep(0.5)
        if not scores:
            return clusters, {}
        head = sorted(cands, key=lambda c: scores.get(id(c), importance(c)), reverse=True)
        taken = set(cand_idx)
        rest = [c for i, c in enumerate(clusters) if i not in taken]
        # 후보가 아니던 클러스터(검토필요 등)의 원래 상대 위치는 유지할 필요가 없다 —
        # 검토필요는 어차피 importance×0.3으로 뒤쪽이라 후보 뒤에 붙인다.
        return head + rest, counts
    except Exception as e:
        print(f"  [국내보도] 측정 실패, 기존 순서 유지: {e}")
        return clusters, {}
