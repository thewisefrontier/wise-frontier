# -*- coding: utf-8 -*-
"""클러스터 우선순위 보정 — 국내 보도 희소성 + 한국 검색 수요.

근거(2026-09-24, 서치콘솔): 7월 상위 노출 검색어는 거의 전부 국내 언론이 안 다루는
틈새 주제(카보베르데·말라위·에티오피아)였고, 수단·미얀마·아이티처럼 국내도 쓰는
주제는 40~70위였다. 카보베르데는 "한국 검색 급증(월드컵) + 한국어 심층기사 드묾"이
겹친 경우라 두 신호를 함께 본다. 가설 단계라 발행 기사 source_data에
kr_coverage_30d / kr_trend를 남겨 2~4주 뒤 노출수와 대조 검증한다.

희소성 측정: 구글뉴스 한국판 RSS는 관련도순으로 1년치를 섞어 주므로(최신순 아님)
72시간 창은 흔한 주제도 0건이 된다. 실측상 최근 30일 건수가 흔한/틈새 주제를 가른다
(아이티 갱단 29·몽골 광산 33 vs 말라위 0·부르키나파소 0). `when:3d`는 스팸만 준다.

검색 수요: 구글은 기사별 PV를 공개하지 않아, 구글 트렌드 한국 급상승 검색어(검색량 +
구글이 붙인 대표 기사 3건 = 그 검색 유입을 받아가는 기사)를 대리 지표로 쓴다.
"""
import calendar
import json
import re
import time
from urllib.parse import quote

import feedparser
import requests

WINDOW_DAYS = 30
TOP_N = 15
DEMAND_BONUS = 30.0
KR_TRENDS_URL = "https://trends.google.com/trending/rss?geo=KR"


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


def _json_from(raw, opener, closer):
    m = re.search(re.escape(opener) + r".*" + re.escape(closer), raw or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


# ── 검색 수요(한국 급상승 검색어) ─────────────────────────────────────

def fetch_kr_trending():
    """[{keyword, traffic, news:[국내 기사 제목]}]. 실패하면 빈 목록."""
    try:
        xml = requests.get(KR_TRENDS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15).text
    except Exception:
        return []
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        kw = re.search(r"<title>(.*?)</title>", item, re.S)
        tr = re.search(r"<ht:approx_traffic>([\d,]+)", item)
        news = re.findall(r"<ht:news_item_title>(.*?)</ht:news_item_title>", item, re.S)
        if kw:
            unesc = lambda s: (s.replace("&apos;", "'").replace("&quot;", '"').replace("&amp;", "&")
                               .replace("&lt;", "<").replace("&gt;", ">").strip())
            out.append({"keyword": unesc(kw.group(1)),
                        "traffic": int(tr.group(1).replace(",", "")) if tr else 0,
                        "news": [unesc(n) for n in news][:3]})
    return out


def classify_trends(trends, call_llm):
    """해외 사안 검색어만 골라 매칭용 단어 묶음을 붙인다(lite 1회).
    groups: [[동의어...], ...] — 묶음마다 하나 이상, 모든 묶음이 클러스터에 나와야 매칭."""
    if not trends:
        return []
    lines = [f"{i}. {t['keyword']} — 국내 기사: " + " / ".join(t["news"]) for i, t in enumerate(trends, 1)]
    prompt = (
        "다음은 한국 구글 급상승 검색어와 구글이 붙인 국내 기사 제목이다. "
        "해외 국가가 주인공인 사안(외국끼리의 스포츠 경기, 해외 재난·정치·경제·사건)만 골라라. "
        "한국 국내 이슈, 한국 연예인, 한국 대표팀 경기는 제외.\n"
        "고른 것마다 해외 영문 기사 묶음과 대조할 단어 묶음을 2~3개 만들어라. 묶음 하나에는 같은 대상을 "
        "가리키는 영어·한국어 표기를 넣는다(예: [\"Uruguay\",\"우루과이\"]). 국가명 하나만으로는 부족하니 "
        "사안을 특정하는 대상(상대국·인물·사건명)을 반드시 포함.\n"
        '출력은 JSON 배열 하나만: [{"i": 번호, "groups": [["...","..."],["...","..."]]}]. 해당 없으면 []\n\n'
        + "\n".join(lines)
    )
    data = _json_from(call_llm(prompt), "[", "]")
    out = []
    for x in data if isinstance(data, list) else []:
        try:
            t = trends[int(x["i"]) - 1]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        groups = [[str(w).strip().lower() for w in g if str(w).strip()]
                  for g in x.get("groups") or [] if isinstance(g, list)]
        groups = [g for g in groups if g]
        # ponytail: 묶음 2개 이상 요구로 "Japan" 하나에 일본 기사 전부가 걸리는 오탐을 막음 — 단일 고유명사 사안은 놓칠 수 있음
        if len(groups) >= 2:
            out.append({**t, "groups": groups})
    return out


def match_trend(cluster, trends):
    blob = " ".join((a.get(k) or "") for a in cluster
                    for k in ("title_en", "title_ko", "summary_en", "summary_ko")).lower()
    for t in trends:
        if all(any(w in blob for w in g) for g in t["groups"]):
            return t
    return None


def demand_prompt_suffix(trend):
    news = "\n".join(f"- {n}" for n in trend.get("news") or [])
    return (
        f"\n\n[한국 검색 수요] 지금 한국에서 '{trend['keyword']}' 검색이 급증했다"
        f"(약 {trend.get('traffic', 0):,}회 이상). 제목 앞부분에 이 검색어의 핵심 단어를 자연스럽게 넣어라.\n"
        + (f"국내 언론은 이미 아래 제목들로 보도했다. 이걸 되풀이하지 말고, 위에 제공된 해외 원문에만 있는 "
           f"사실·현지 반응·배경을 중심으로 써라. 이 제목들에만 있고 원문에 없는 내용은 쓰지 마라.\n{news}\n"
           if news else "")
    )


# ── 희소성(국내 보도량) ───────────────────────────────────────────────

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
    data = _json_from(call_llm(prompt), "{", "}")
    if not isinstance(data, dict):
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


def rerank(clusters, importance, is_severe, cluster_key, call_llm,
           count=count_kr_coverage, fetch_trends=fetch_kr_trending, top_n=TOP_N,
           bonus_eligible=lambda c: True):
    """상위 top_n 후보 + 한국 검색 수요와 맞는 클러스터를 보정 점수로 재정렬.
    - 검색 수요 매칭: +30, 희소성 감점 없음(국내 보도가 많아도 수요가 더 크다).
    - 나머지: 희소성 가감점. 중대성 높은 사안은 감점하지 않는다(국내 언론이 다룰 만큼
      크다는 게 배제 사유가 아니라는 기존 방침). 가산점은 bonus_eligible(프론티어 국가)만 —
      첫 실운영(9/24)에서 국가 제한 없이 주니 "미국 배우 샘 워싱턴" 같은 선진국 연예
      기사가 +15를 받았다. 근거였던 7월 상위 노출은 프론티어 국가의 틈새 주제였다.
    반환: (재정렬된 clusters, {cluster_key: {"kr_coverage_30d": n} 또는 {"kr_trend": {...}}}).
    실패하면 원래 순서."""
    try:
        normal = [c for c in clusters if not any(a.get("__needs_review__") for a in c)]
        if not normal:
            return clusters, {}
        try:
            trends = classify_trends(fetch_trends(), call_llm)
        except Exception as e:
            print(f"  [검색수요] 조회 실패, 건너뜀: {e}")
            trends = []
        if trends:
            print(f"  [검색수요] 해외 관련 급상승 {len(trends)}건: " + ", ".join(t["keyword"] for t in trends))
        demand = {id(c): t for c in normal for t in [match_trend(c, trends)] if t}

        cands = normal[:top_n] + [c for c in normal[top_n:] if id(c) in demand]
        scores, info = {}, {}
        for c in cands:
            t = demand.get(id(c))
            if t:
                scores[id(c)] = importance(c) + DEMAND_BONUS
                info[cluster_key(c)] = {"kr_trend": {k: t[k] for k in ("keyword", "traffic", "news")}}
                print(f"  [검색수요] +{DEMAND_BONUS:.0f} ← '{t['keyword']}' {(c[0].get('title_ko') or c[0].get('title_en') or '')[:40]}")

        rest_cands = [c for c in cands if id(c) not in demand]
        queries = build_queries(rest_cands, call_llm) if rest_cands else {}
        for j, c in enumerate(rest_cands):
            q = queries.get(j)
            if not q:
                continue
            n, bonus = measure(c, q, count)
            if bonus < 0 and is_severe(c):
                bonus = 0.0
            if bonus > 0 and not bonus_eligible(c):
                bonus = 0.0
            scores[id(c)] = importance(c) + bonus
            info[cluster_key(c)] = {"kr_coverage_30d": n}
            print(f"  [국내보도] {n if n is not None else '?'}건 {bonus:+.0f} ← '{q}'")
            time.sleep(0.5)
        if not scores:
            return clusters, {}
        head = sorted(cands, key=lambda c: scores.get(id(c), importance(c)), reverse=True)
        taken = {id(c) for c in cands}
        # 검토필요 클러스터는 어차피 importance×0.3으로 뒤쪽이라 후보 뒤에 붙인다.
        return head + [c for c in clusters if id(c) not in taken], info
    except Exception as e:
        print(f"  [국내보도] 측정 실패, 기존 순서 유지: {e}")
        return clusters, {}
