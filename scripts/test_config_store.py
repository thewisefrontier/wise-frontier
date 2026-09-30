# -*- coding: utf-8 -*-
"""config_store.py 회귀 테스트(2026-09-30, Supabase 이그레스 대응 — prompts·rss_sources를
D1 우선/Supabase 폴백으로 전환). D1 시크릿 없이도 Supabase 폴백 경로가 항상 죽지 않아야 한다.
실행: python scripts/test_config_store.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import config_store as cs  # noqa: E402


def test_d1_not_ready_when_secrets_missing(monkeypatch):
    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "NEWSFINAL_CONFIG_D1_ID"):
        monkeypatch.delenv(k, raising=False)
    assert cs._d1_ready() is False


def test_load_photo_gate_always_sets_blocked_domains_as_set(monkeypatch):
    monkeypatch.setattr(cs, "load_prompt", lambda name, fallback="": "")  # 조회 실패 시뮬레이션
    cfg = cs.load_photo_gate("x", {"blocked_domains": ["a.com", "b.com"], "other": 1})
    assert cfg["blocked_domains"] == {"a.com", "b.com"}
    assert isinstance(cfg["blocked_domains"], set)


def test_load_photo_gate_parses_json_from_prompt(monkeypatch):
    monkeypatch.setattr(cs, "load_prompt", lambda name, fallback="": '{"blocked_domains": ["c.com"]}')
    cfg = cs.load_photo_gate("x", {"blocked_domains": []})
    assert cfg["blocked_domains"] == {"c.com"}


if __name__ == "__main__":
    class _MP:
        def __init__(self):
            self._saved = {}

        def delenv(self, k, raising=True):
            self._saved[k] = os.environ.pop(k, None)

        def setattr(self, obj, name, val):
            self._saved[(id(obj), name)] = (obj, name, getattr(obj, name))
            setattr(obj, name, val)

        def undo(self):
            for k, v in self._saved.items():
                if isinstance(k, tuple):
                    obj, name, old = v
                    setattr(obj, name, old)
                elif v is not None:
                    os.environ[k] = v

    for fn in (test_d1_not_ready_when_secrets_missing,
               test_load_photo_gate_always_sets_blocked_domains_as_set,
               test_load_photo_gate_parses_json_from_prompt):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
        print(f"  ok  {fn.__name__}")
    print("config_store 테스트 통과")
