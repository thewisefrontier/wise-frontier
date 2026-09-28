# -*- coding: utf-8 -*-
"""구글뉴스 피드 보호(2026-09-28): ① 중요도 점수는 피드 이름이 아니라 실제 언론사 수로 센다 ② 연속 실패해도 자동 비활성화하지 않는다.
실행: python scripts/test_google_source_protection.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import db  # noqa: E402
import gemini_writer as gw  # noqa: E402

# ① 발행처 판정
g = {"source": "구글뉴스 주제-증시동향", "title_en": "Stocks fall on rate fears - Reuters"}
assert gw._publisher(g) == "gn:reuters"
assert gw._publisher({"source": "China - 구글뉴스 거래소·지수 공고", "title_en": "上交所将发布新指数 - 新浪财经"}) == "gn:新浪财经"
assert gw._publisher({"source": "구글뉴스 x", "title_en": "제목만 있음", "url": "https://www.bbc.com/news/1"}) == "gn:bbc.com"
assert gw._publisher({"source": "구글뉴스 x", "title_en": "제목만", "url": "https://news.google.com/rss/articles/abc"}) == "구글뉴스 x"
assert gw._publisher({"source": "BBC", "title_en": "A - B"}) == "BBC"          # 일반 피드는 그대로

# 같은 구글 피드의 서로 다른 언론사 3곳 = 독립 매체 3곳(종전엔 1곳)
cl = [{"source": "구글뉴스 주제-AI", "title_en": f"Story - {p}", "summary_en": "본문 요약입니다. " * 5, "full_text": "x"} for p in ("Reuters", "AP", "BBC")]
same = [{"source": "구글뉴스 주제-AI", "title_en": "Story - Reuters", "summary_en": "본문 요약입니다. " * 5, "full_text": "x"} for _ in range(3)]
assert gw.cluster_importance(cl) > gw.cluster_importance(same)

# ② 보호 대상 판정
assert db.is_protected_source({"name": "구글뉴스 주제-AI", "url": "https://news.google.com/rss/search?q=a"})
assert db.is_protected_source({"name": "Japan - 구글뉴스 금융·증시", "url": "x"})
assert db.is_protected_source({"name": "Something", "url": "https://news.google.com/rss/topics/x"})
assert not db.is_protected_source({"name": "BBC Business", "url": "https://feeds.bbci.co.uk/x"})
print("ok")
