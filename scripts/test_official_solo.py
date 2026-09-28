# -*- coding: utf-8 -*-
"""공식 소스 단독 발행 후보 선별(select_official_solo) 회귀 테스트 — 2026-09-28.
사용자 지시: "믿을 수 있는 소스는 그냥 기사로 내는 거 알지? 거래소 공지, 공시 같은 거".
실행: python scripts/test_official_solo.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import gemini_writer as gw  # noqa: E402

T = "공식 발표 본문입니다. " * 40  # 300자 이상


def fake_hydrate(rows):
    for r in rows:
        r.setdefault("full_text", "")


arts = [
    {"id": 1, "source": "Hong Kong - HKEX News", "title_en": "HKEX Welcomes Four New ETFs", "full_text": T},
    {"id": 2, "source": "BBC", "title_en": "General news from a media outlet", "full_text": T},          # 일반 언론 → 제외
    {"id": 3, "source": "Japan - Bank of Japan", "title_en": "Policy statement", "full_text": "짧음"},     # 300자 미만 → 제외
    {"id": 4, "source": "Global - BIS Press Releases", "title_en": "BIS publishes report", "summary_en": T},  # 요약만 있어도 300자 이상이면 통과
]
got = gw.select_official_solo(arts, hydrate=fake_hydrate)
assert [a["id"] for a in got] == [1, 4], got

# 상한
many = [{"id": i, "source": "US - ICE Press Releases", "title_en": f"NYSE notice {i}", "full_text": T} for i in range(20)]
assert len(gw.select_official_solo(many, limit=5, hydrate=fake_hydrate)) == 5

# 공식 목록에 새 중앙은행·거래소가 들어 있어야 한다
for n in ("Japan - Bank of Japan", "India - Reserve Bank of India", "Hong Kong - HKEX News", "Nigeria - NGX Group"):
    assert n in gw.OFFICIAL_SOURCE_NAMES and n in gw.OFFICIAL_SOLO_SOURCE_NAMES, n
# 기업 뉴스룸·UN 브리핑·연준(econ_writer 담당)은 단독 예외 대상이 아니다
for n in ("Samsung Newsroom", "UN News", "UN Security Council", "Federal Reserve", "ECB 유럽중앙은행", "Bank of England"):
    assert n not in gw.OFFICIAL_SOLO_SOURCE_NAMES, n
assert gw.select_official_solo([{"id": 5, "source": "Samsung Newsroom", "title_en": "Samsung launches new TV", "full_text": T}], hydrate=fake_hydrate) == []
assert gw.select_official_solo([], hydrate=fake_hydrate) == []
print("ok")

# 정형 통계·경매 결과 공지는 공식 소스여도 제외(RBI 'Money Market Operations as on …' 실사례)
routine = [{"id": 9, "source": "India - Reserve Bank of India", "title_en": "Money Market Operations as on September 27, 2026", "full_text": T},
           {"id": 10, "source": "India - Reserve Bank of India", "title_en": "RBI imposes monetary penalty on a cooperative bank", "full_text": T}]
assert [a["id"] for a in gw.select_official_solo(routine, hydrate=fake_hydrate)] == [10]
print("routine ok")
