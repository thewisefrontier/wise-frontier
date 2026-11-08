# -*- coding: utf-8 -*-
"""GA4 기사별 일일 PV → article_pv_daily 적재.

클러스터 선택 로직(중복+중요도)은 실제로 읽히는지와 무관해, 고PV 기사의 패턴을
찾으려고 실측 PV를 쌓는다. 카테고리·국가·키워드는 articles/article_keywords에
이미 있으니 여기선 (article_id, date) 원자료만 저장하고 분석은 조인으로 한다.

GA4 수치는 1~2일 뒤에야 확정되므로 매 실행마다 최근 PV_DAYS일을 통째로 덮어쓴다.
백필: PV_DAYS=365 python scripts/pv_stats.py
"""
import json
import os
import time
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from db import _headers, _url

PROPERTY_ID = os.getenv("GA4_PROPERTY_ID", "540507796")
DAYS = int(os.getenv("PV_DAYS", "3"))
PAGE = 100000
SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


def _token():
    raw = os.getenv("GA4_SERVICE_ACCOUNT_JSON")
    if raw:
        c = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    else:
        c = service_account.Credentials.from_service_account_file("ga4-key.json", scopes=SCOPES)
    c.refresh(Request())
    return c.token


def article_id(path):
    parts = urlsplit(path)
    if parts.path.rstrip("/") not in ("/article", "/article.html"):
        return None
    v = parse_qs(parts.query).get("id", [""])[0]
    return int(v) if v.isdigit() else None


def fetch(token, days):
    rows, offset = [], 0
    while True:
        r = requests.post(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{PROPERTY_ID}:runReport",
            headers={"Authorization": f"Bearer {token}"},
            json={"dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                  "dimensions": [{"name": "date"}, {"name": "pagePathPlusQueryString"}],
                  "metrics": [{"name": "screenPageViews"}, {"name": "totalUsers"}],
                  "limit": PAGE, "offset": offset},
            timeout=60)
        r.raise_for_status()
        d = r.json()
        rows += d.get("rows", [])
        offset += PAGE
        if offset >= d.get("rowCount", 0):
            return rows


def aggregate(rows):
    # ponytail: 같은 기사의 다른 쿼리 변형(?id=1&fbclid=…)끼리 users를 단순 합산 — 소폭 과대계상, 정확한 UV가 필요해지면 customEvent로 article_id 차원 추가
    agg = defaultdict(lambda: [0, 0])
    for row in rows:
        date, path = (v["value"] for v in row["dimensionValues"])
        aid = article_id(path)
        if aid is None:
            continue
        key = (aid, f"{date[:4]}-{date[4:6]}-{date[6:]}")
        agg[key][0] += int(row["metricValues"][0]["value"])
        agg[key][1] += int(row["metricValues"][1]["value"])
    return [{"article_id": a, "date": d, "views": v, "users": u} for (a, d), (v, u) in agg.items()]


def upsert(records):
    h = {**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}
    for i in range(0, len(records), 1000):
        r = requests.post(_url("article_pv_daily"), headers=h, params={"on_conflict": "article_id,date"},
                          json=records[i:i + 1000], timeout=60)
        r.raise_for_status()


def main():
    rows = fetch(_token(), DAYS)
    records = aggregate(rows)
    upsert(records)
    print(f"GA4 {len(rows)}행 → 기사×일 {len(records)}건 적재 (최근 {DAYS}일)")
    sync_view_counts()


def sync_view_counts():
    """articles.view_count(관리자 목록·조회수순)를 GA4 누적으로 맞춘다(2026-11-08 — 사이트 카운터는 봇까지 세서 폐기).
    DB 함수가 값이 다른 행만 한 번에 200행씩 고친다. 기사 행 일괄 UPDATE는 본문 검색 색인 때문에 WAL이 행당
    수백 KB라 1GB 디스크를 채운 적이 있다 — 평소엔 하루 수십 행이라 한두 번이면 끝나고, 상한을 넘으면 다음 날 이어서."""
    done = 0
    for _ in range(5):
        r = requests.post(_url("rpc/sync_view_counts_from_ga4"), headers=_headers(), json={"p_limit": 200}, timeout=60)
        r.raise_for_status()
        n = int(r.json() or 0)
        done += n
        if n < 200:
            break
        time.sleep(30)
    print(f"누적 조회수 GA4 반영 {done}건")


if __name__ == "__main__":
    main()
