# -*- coding: utf-8 -*-
"""pv_stats.article_id/aggregate 회귀 테스트. 실행: python scripts/test_pv_stats.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))

from pv_stats import aggregate, article_id  # noqa: E402


def row(date, path, views, users):
    return {"dimensionValues": [{"value": date}, {"value": path}],
            "metricValues": [{"value": str(views)}, {"value": str(users)}]}


assert article_id("/article?id=123") == 123
assert article_id("/article.html?id=7&fbclid=x") == 7
assert article_id("/article/?utm_source=a&id=9") == 9
assert article_id("/article?id=abc") is None
assert article_id("/?id=5") is None
assert article_id("/live?id=5") is None

recs = aggregate([
    row("20260924", "/article?id=1", 10, 8),
    row("20260924", "/article?id=1&fbclid=z", 2, 2),
    row("20260923", "/article?id=1", 5, 5),
    row("20260924", "/index.html", 99, 50),
])
assert sorted(recs, key=lambda r: r["date"]) == [
    {"article_id": 1, "date": "2026-09-23", "views": 5, "users": 5},
    {"article_id": 1, "date": "2026-09-24", "views": 12, "users": 10},
], recs
print("ok")
