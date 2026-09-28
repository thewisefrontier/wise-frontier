# -*- coding: utf-8 -*-
"""JSON 응답 경로도 카테고리를 정규화하는지(2026-09-28 "글ローバル" 사고). 실행: python scripts/test_category_json_path.py"""
import json
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import gemini_writer as g  # noqa: E402

def cat(c):
    return g.parse_title_and_body(json.dumps({"title": "t", "body": "본문입니다. " * 30, "category": c}, ensure_ascii=False))[3]

assert cat("글ローバル") == "글로벌"
assert cat("스포츠") == "스포츠" and cat("경제") == "경제"
print("ok")
