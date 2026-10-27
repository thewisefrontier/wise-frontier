# -*- coding: utf-8 -*-
"""db.pixabay_ids_used_recently()의 PostgREST or/ilike 조회가 실제로 통하는지 확인(2026-10-27, 1회용).
4416515는 10/27 기사 #319604(article_pixabay_4416515.jpg)에 쓰인 사진, 1은 안 쓰인 id."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db  # noqa: E402

got = db.pixabay_ids_used_recently([4416515, 1])
print("결과:", got)
assert got == {"4416515"}, got
print("ok")
