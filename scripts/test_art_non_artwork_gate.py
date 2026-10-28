# -*- coding: utf-8 -*-
"""오늘의 그림 후보 중 그림 아닌 스캔본 차단(_NON_ARTWORK) 회귀 테스트. 네트워크 없음.
2026-10-28 실사고(id=319743): BnF 문서철 591쪽 스캔본이 원제(프랑스어 파일명) 그대로 오늘의 그림으로 발행됨.
실행: python scripts/test_art_non_artwork_gate.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
from art_weekly_writer import _NON_ARTWORK  # noqa: E402

blocked = [
    "Collection d'autographes, de dessins et de portraits de personnages célèbres, français et étrangers, "
    "du XVIe au XIXe siècle, formée par Alexandre Bixio. XVIe-XIXe siècle. VIII. S - Z - btv1b10028242b (591 of 618)",
    "Boswell - Life of Johnson - Volume 5 - Frontispiece",
    "A Descriptive Catalog of Pictures, Poetical and Historical Inventions, Painted by William Blake in - B1978.43.1418",
]
allowed = [  # 오탐 대조: 실제 그림 원제
    "H.W. Mesdag - Strand met boten en schelpvissers - B3 - Cultural Heritage Agency of the Netherlands Art Collection",
    "Circumcision ceremony for Akbar’s sons, painting 126 from an Akbar-nama (Book of Akbar) of Abu’l Fazl",
    "Portrait of Mrs. Francis Stanton Blake (Eugenia White Blake)",
    "The Assumption of the Virgin",
]
for t in blocked:
    assert _NON_ARTWORK.search(t), t
for t in allowed:
    assert not _NON_ARTWORK.search(t), t
print("OK")
