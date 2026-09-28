# -*- coding: utf-8 -*-
"""스포츠 카테고리(2026-09-28) 회귀 테스트. 실행: python scripts/test_sports_category.py
제목 예시는 9/28 24시간 수집분 실측 샘플에서 가져왔다."""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))

from category_guard import is_sports_title, is_sports_cluster  # noqa: E402
from style_guard import strip_number_commas  # noqa: E402
import fabrication_guard  # noqa: E402

# ── 판별: 실제 스포츠 제목 ──
for t in ["Nigeria Prevail but Senegal Draw On Vieira's Debut in 2027 Afcon Qualifiers",
          "Kohli, Gill's majestic tons cruise India to 8-wicket rout of West Indies in 1st ODI",
          "UEFA Nations League: Greece stun Germany 1-0 as Kourbelis strike leaves Klopp winless",
          "Lando Norris after Baku crash: Some drivers 'shouldn't be in F1'",
          "Springboks coach refuses to blame his 13 changes after shock loss to Wallabies"]:
    assert is_sports_title(t), t
assert is_sports_title("", "U23 베트남, 우즈베키스탄에 0-2 패배…조 2위로 아시안게임 8강 진출")

# ── 판별: 겹치는 말 때문에 오탐 나던/날 제목 ──
for t in ["NBA president begins reconciliation efforts to unite Bar",   # 나이지리아 변호사협회
          "CAF approves $500 million loan to Ecuador",                  # 중남미개발은행
          "Copa Airlines adds new route to Caracas",
          "Marathon talks end without deal on budget",
          "Hunger striker hospitalised after 40 days",
          "Iranian film wins Grand Prix at Cannes",
          "Russia eyes conditional consent to Türkiye's S-400 transfer",
          "Haiti says it is halfway toward goal of training 4,000 police officers"]:
    assert not is_sports_title(t), t
for k in ["경기 침체 우려에 금융감독원 긴급 점검", "이란 핵협상 대표팀 빈 도착",
          "육상 풍력 단지 착공", "경기 사이클 둔화 조짐"]:
    assert not is_sports_title("", k), k

# ── 클러스터: 절반 이상이 스포츠여야 ──
S = {"title_en": "Ireland hit three first-half goals in Nations League win"}
N = {"title_en": "Central bank raises rates"}
assert is_sports_cluster([S, N])
assert not is_sports_cluster([S, N, N])
assert not is_sports_cluster([N, N, {"__needs_review__": True}])
assert is_sports_cluster([S, S, {"__needs_review__": True}])

# ── 숫자 콤마 제거(뉴스파이널 규칙: 1만 미만도 콤마 없이, 1만 이상 억/만) ──
assert strip_number_commas("관중 12,500명") == "관중 1만2500명"
assert strip_number_commas("이적료 3,000,000달러") == "이적료 300만달러"
assert strip_number_commas("2,691달러") == "2691달러"
assert strip_number_commas("1,234,567.5점") == "123만4567.5점"
assert strip_number_commas("12만5,000명") == "12만5000명"
assert strip_number_commas("1,2,3위와 2-1 승리, 2026년") == "1,2,3위와 2-1 승리, 2026년"  # 목록·스코어·연도는 그대로
assert strip_number_commas("") == "" and strip_number_commas(None) is None

# ── 수집: 스포츠 하드 차단·관찰 목록 해제 ──
import rss_fetcher  # noqa: E402
for t in ["Premier League: Arsenal beat Chelsea", "Kaizer Chiefs coach Da Cruz compared to Ruben Amorim",
          "South Korea seeks apology after Zelenskyy reveals North Korean POW transfer"]:
    assert not rss_fetcher.is_noise(t) and not rss_fetcher.is_soft_noise(t), t
assert rss_fetcher.is_noise("Today's top news: Chad, Sudan")  # 다른 노이즈는 그대로

# ── 검증: 스포츠는 위키 조회(추출용 lite 호출 포함)를 건너뛴다 ──
calls = []
fabrication_guard.verify_no_fabricated_names("src", "body", lambda p, **k: calls.append(p) or "없음", wiki=False)
assert len(calls) == 1, calls  # [이름]/[수식어] 판단 1회만
fabrication_guard.call_nvidia, _orig = (lambda p, **k: "OK"), fabrication_guard.call_nvidia
assert fabrication_guard.unsupported_claims("b", "f") == ""
fabrication_guard.call_nvidia = lambda p, **k: "포르투갈이 3-1로 이겼다."
assert fabrication_guard.unsupported_claims("b", "f") == "포르투갈이 3-1로 이겼다."
fabrication_guard.call_nvidia = _orig

# ── 선정: 스포츠끼리만 희소성 순으로 자리 바꿈(비스포츠 위치 불변) ──
import gemini_writer as g  # noqa: E402
common = [{"title_en": "Portugal beat Norway in Nations League", "title_ko": "포르투갈 노르웨이"}]
rare = [{"title_en": "Malawi netball hockey final", "title_ko": "말라위 하키"}]
trend = [{"title_en": "Uruguay football friendly", "title_ko": "우루과이 축구"}]
other = [{"title_en": "Kenya inflation eases", "title_ko": "케냐 물가"}]
info = {g.make_cluster_key(common): {"kr_coverage_30d": 42},
        g.make_cluster_key(rare): {"kr_coverage_30d": 0},
        g.make_cluster_key(trend): {"kr_trend": {"keyword": "우루과이"}}}
assert g.order_sports_by_scarcity([common, other, rare, trend], info) == [trend, other, rare, common]
assert g.order_sports_by_scarcity([other], {}) == [other]

# 사실 대조용 원문 묶음은 작성 규칙 없이 원문만, 상한 안에서
facts = g._cluster_facts([{"title_en": "A", "full_text": "x" * 9000}, {"title_en": "B", "summary_en": "y"}])
assert len(facts) <= 6000 and facts.startswith("A\n") and "B\ny" in facts
print("ok")
