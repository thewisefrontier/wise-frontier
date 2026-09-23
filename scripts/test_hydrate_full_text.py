# -*- coding: utf-8 -*-
"""
db.hydrate_full_text() 회귀 테스트 (2026-09-23 신설).

RSS 처리기가 본문을 저장하지 않게 바꾸면서, 기사화 직전에 소수 행만 본문을
채우는 헬퍼. DB 저장분 우선 → 없으면 크롤링 → 결과 재저장(실패는 "") 순서와
건너뛰기 조건을 검증한다.

실행: python scripts/test_hydrate_full_text.py
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import db  # noqa: E402
import text_crawl  # noqa: E402


class Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def json(self):
        return self._d


def _setup(stored=None, crawl=None):
    calls = {"get": 0, "crawl": [], "patch": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["get"] += 1
        ids = {int(x) for x in params["id"][4:-1].split(",")}
        return Resp([{"id": i, "full_text": t} for i, t in (stored or {}).items() if i in ids])

    def fake_patch(url, headers=None, json=None, timeout=None):
        calls["patch"].append((url.split("id=eq.")[1], json["full_text"]))
        return Resp(None, 204)

    def fake_crawl(url, timeout=10):
        calls["crawl"].append(url)
        return (crawl or {}).get(url, "")

    db.requests.get = fake_get
    db.requests.patch = fake_patch
    text_crawl.crawl_full_text = fake_crawl
    return calls


def test_in_memory_text_untouched():
    c = _setup()
    rows = [{"id": 1, "source": "BBC", "url": "https://a", "full_text": "있음"}]
    assert db.hydrate_full_text(rows) == 0
    assert c["get"] == 0 and not c["crawl"] and rows[0]["full_text"] == "있음"


def test_db_stored_text_used_without_crawl():
    c = _setup(stored={1: "DB 본문"})
    rows = [{"id": 1, "source": "BBC", "url": "https://a"}]
    db.hydrate_full_text(rows)
    assert rows[0]["full_text"] == "DB 본문"
    assert not c["crawl"] and not c["patch"]


def test_previous_failure_marker_not_recrawled():
    c = _setup(stored={1: ""})
    rows = [{"id": 1, "source": "BBC", "url": "https://a"}]
    db.hydrate_full_text(rows)
    assert rows[0]["full_text"] == "" and not c["crawl"]


def test_missing_is_crawled_and_saved_back():
    c = _setup(crawl={"https://a": "크롤링 본문"})
    rows = [{"id": 7, "source": "BBC", "url": "https://a"}]
    assert db.hydrate_full_text(rows) == 1
    assert rows[0]["full_text"] == "크롤링 본문"
    assert c["patch"] == [("7", "크롤링 본문")]


def test_crawl_failure_saved_as_empty_marker():
    c = _setup(crawl={})
    rows = [{"id": 8, "source": "BBC", "url": "https://b"}]
    assert db.hydrate_full_text(rows) == 0
    assert rows[0]["full_text"] == "" and c["patch"] == [("8", "")]


def test_skips_own_articles_and_non_http():
    c = _setup()
    rows = [{"id": 1, "source": "NewsFinal", "url": "internal://x"},
            {"id": 2, "source": "BBC", "url": "internal://y"},
            {"id": 3, "source": "BBC"}]
    db.hydrate_full_text(rows)
    assert not c["crawl"] and not c["patch"]


def test_check_db_false_skips_lookup():
    c = _setup(crawl={"https://a": "본문"})
    rows = [{"id": 1, "source": "BBC", "url": "https://a"}]
    db.hydrate_full_text(rows, check_db=False)
    assert c["get"] == 0 and rows[0]["full_text"] == "본문"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}건 통과")
