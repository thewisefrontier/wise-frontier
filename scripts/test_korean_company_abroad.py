# -*- coding: utf-8 -*-
"""
해외 보도 한국 기업 소식 판별 회귀 테스트 (2026-10-04 신설).

raw_candidates 실측(2026-10-04)에서 확인된 오탐 패턴(필리핀 "SK" 청년의회 선거,
나이지리아/델리 "LG" 지방정부·부지사, 미얀마 반군 "KIA")을 경계 사례로 고정하고,
gemini_writer.py 통합(중요도 가산·선진국 게이트 면제·속보 경로)도 함께 검증한다.

실행: python scripts/test_korean_company_abroad.py
"""
import os
import sys
from datetime import timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp949 콘솔에서 이모지/줄표 출력 보호
except Exception:
    pass

os.environ.setdefault("GEMINI_API_KEY", "test")

import korean_company_abroad as kca  # noqa: E402
import gemini_writer as gw  # noqa: E402


def art(source, title_en, summary_en, hours_ago=1, title_ko="", summary_ko=""):
    ts = (gw.now_kst() - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")
    return {"source": source, "title_en": title_en, "title_ko": title_ko,
            "summary_en": summary_en, "summary_ko": summary_ko,
            "full_text": "본문", "created_at": ts, "country": "", "category": "경제"}


# ── 기본 판정 ────────────────────────────────────────────────────────

def test_overseas_lawsuit_is_tier1():
    cluster = [
        art("CBS News", "South Korean workers detained in Georgia Hyundai plant raid",
            "Hundreds of South Korean nationals were detained in an immigration raid "
            "at a Hyundai battery plant in the United States."),
        art("AL.com", "Lawsuit aims to force Hyundai answers on hiring practices",
            "A lawsuit filed in Georgia alleges Hyundai refused to hire non-Koreans at its US plant."),
    ]
    info = kca.classify(cluster)
    assert info is not None and info["tier"] == 1 and info["company"] == "현대차"
    assert "미국" in info["foreign_countries"]


def test_domestic_only_excluded():
    """국내(한국) 발생 사건은 '해외 한국기업 소식'이 아니다."""
    cluster = [
        art("Korea Herald", "Posco strike continues into second day",
            "Posco workers in South Korea extended their walkout over wage talks."),
        art("Yonhap", "Posco union rejects wage offer",
            "The strike at Posco's Pohang plant in South Korea entered its second day."),
    ]
    assert kca.classify(cluster) is None


def test_product_review_without_business_keyword_excluded():
    """사건사고/실적 등 키워드가 전혀 없는 단순 리뷰는 제외."""
    cluster = [
        art("Top Gear", "Kia Tucson review: should you buy one?",
            "We review the new Kia Tucson's handling, interior and fuel economy."),
        art("Auto Blog", "Kia Tucson vs Hyundai Tucson comparison",
            "Comparing the two SUVs' trims and infotainment."),
    ]
    assert kca.classify(cluster) is None


def test_passing_mention_in_list_excluded():
    """표·나열 속에 스치듯 등장(리드 뒤쪽)하는 경쟁사는 주체로 인정하지 않는다."""
    cluster = [
        art("Reuters", "Asian chipmakers rally on AI demand",
            "Shares of Taiwan's TSMC led gains, with Samsung, SK Hynix and Micron also rising."),
        art("AP", "Tech stocks climb across Asia",
            "Chip stocks including TSMC, Samsung and SK Hynix gained in Tuesday trading."),
    ]
    assert kca.classify(cluster) is None


def test_kachin_independence_army_not_matched_as_kia_motors():
    """실측 오탐: 미얀마 반군 KIA(Kachin Independence Army)가 기아차로 잡히면 안 된다."""
    cluster = [
        art("Myanmar Now", "KIA captures hill-top outpost as fighting resumes in Mongmit",
            "The Kachin Independence Army seized a military outpost in northern Shan State."),
        art("Irrawaddy", "KIA advances in Mongmit township",
            "Fighting between the KIA and junta forces continued near Mongmit."),
    ]
    assert kca.classify(cluster) is None


def test_bare_abbreviations_not_matched():
    """실측 오탐: 필리핀 SK(청년의회)·나이지리아/델리 LG(지방정부·부지사)는 약어 단독 매칭이면 걸린다."""
    ph_sk = [art("Philippine Star", "Senate bill eyes to lower age for SK voting, candidacy",
                 "The bill would lower the voting age for Sangguniang Kabataan elections."),
             art("Inquirer", "SC asks gov't to comment on petition vs deferment of SK polls",
                 "The Supreme Court sought comments on a petition to defer SK elections.")]
    ng_lg = [art("Tribune Online", "Anambra: Soludo swears in newly elected 21 LG chairmen",
                 "Governor Soludo inaugurated 21 local government chairmen in Anambra state."),
             art("The Standard", "MAYOR LOWE APPEALS FINDINGS OF LG COMMISSION",
                 "The mayor filed an appeal against the local government commission's findings.")]
    assert kca.classify(ph_sk) is None
    assert kca.classify(ng_lg) is None


def test_overseas_earnings_is_tier2_lower_bonus_than_incident():
    incident = [
        art("CBS News", "Samsung sued over defective washing machines",
            "A class action lawsuit accuses Samsung of selling washing machines with a leak defect "
            "in the United States."),
        art("Top Class Actions", "Samsung washing machine class action settlement reached",
            "Samsung agreed to a settlement over leak defects in washing machines sold in the United States."),
    ]
    earnings = [
        art("Reuters", "Samsung Electronics posts record quarterly profit",
            "Samsung Electronics reported record quarterly profit driven by chip demand in the United States."),
        art("Bloomberg", "Samsung Electronics revenue beats estimates",
            "Samsung's revenue beat Wall Street estimates as chip prices rose in the United States."),
    ]
    assert kca.importance_bonus(incident) == kca.TIER_BONUS[1]
    assert kca.importance_bonus(earnings) == kca.TIER_BONUS[2]
    assert kca.importance_bonus(incident) > kca.importance_bonus(earnings)


# ── gemini_writer.py 통합 ───────────────────────────────────────────

def test_cluster_importance_includes_bonus():
    incident = [
        art("CBS News", "Hyundai sued over defective engines",
            "A class action lawsuit accuses Hyundai of selling vehicles with a defective engine in the US."),
        art("AL.com", "Hyundai faces recall over engine defect",
            "Hyundai is recalling vehicles in the United States due to an engine fire risk."),
    ]
    plain = [art("Reuters", f"의회 예산안 심의 {i}", "본문 요약이 실제로 들어 있는 경우입니다.") for i in range(2)]
    assert gw.cluster_importance(incident) > gw.cluster_importance(plain)


def test_advanced_economy_gate_exempts_tier1_incident(monkeypatch):
    """미국(선진국) 발생 한국기업 사건사고는 4중복 게이트 없이도(2건) 처리돼야 한다."""
    incident = [
        art("CBS News", "Samsung sued over defective washing machines",
            "A class action lawsuit accuses Samsung of selling washing machines with a leak defect in the US."),
        art("Top Class Actions", "Samsung washing machine class action settlement reached",
            "Samsung agreed to a settlement over leak defects in washing machines sold in the United States."),
    ]
    for a in incident:
        a["country"] = "미국"

    monkeypatch.setattr(gw, "get_existing_cluster", lambda key: None)
    monkeypatch.setattr(gw, "get_cluster_article_count", lambda key: 0)
    # 게이트가 막으면 continue로 다음 클러스터로 건너뛰어 _prepare_cluster_material이
    # 아예 호출되지 않는다 — 호출 여부 자체가 "게이트를 통과했는지"의 증거.
    seen = {"reached_prepare": False}
    monkeypatch.setattr(gw, "_prepare_cluster_material", lambda c: (seen.__setitem__("reached_prepare", True) or c))

    monkeypatch.setattr(gw, "get_today_own_articles", lambda: [])
    monkeypatch.setattr(gw, "find_similar_article", lambda *a, **kw: (None, 0))
    monkeypatch.setattr(gw, "find_continuing_story", lambda *a, **kw: None)
    monkeypatch.setattr(gw, "call_gemini_article", lambda *a, **kw: None)  # 실제 발행까지는 안 감(통합 범위 밖)

    gw.run(clusters_override=[incident], max_clusters=1, skip_extras=True)

    assert seen["reached_prepare"] is True, "선진국 게이트가 한국기업 사건사고를 막았다(본문 준비 단계에 못 미침)"


def test_breaking_path_picks_up_tier1_incident_without_mass_casualty(monkeypatch):
    """소송/리콜은 _SEVERITY_HIGH(인명피해류)에 안 걸리지만 속보 경로 대상이어야 한다."""
    incident = [
        art("CBS News", "Samsung sued over defective washing machines",
            "A class action lawsuit accuses Samsung of selling washing machines with a leak defect in the US.",
            hours_ago=1),
        art("Top Class Actions", "Samsung washing machine class action settlement reached",
            "Samsung agreed to a settlement over leak defects in washing machines sold in the United States.",
            hours_ago=1),
    ]
    routine = [art("A", "기업 분기 실적 발표", "본문 요약", hours_ago=1) for _ in range(2)]

    monkeypatch.setattr(gw, "get_today_articles", lambda limit: incident + routine)
    monkeypatch.setattr(gw, "cluster_articles", lambda arts: [incident, routine])
    called = []
    monkeypatch.setattr(gw, "run", lambda **kw: called.append(kw))

    gw.run_breaking()

    assert len(called) == 1
    assert called[0]["clusters_override"] == [incident]


if __name__ == "__main__":
    import unittest.mock as mock  # noqa: F401

    class _MonkeyPatch:
        def __init__(self):
            self._orig = {}

        def setattr(self, obj, name, value):
            self._orig[(obj, name)] = getattr(obj, name)
            setattr(obj, name, value)

        def undo(self):
            for (obj, name), val in self._orig.items():
                setattr(obj, name, val)

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in t.__code__.co_varnames:
                t(mp)
            else:
                t()
            print(f"  ok  {t.__name__}")
        finally:
            mp.undo()
    print(f"\n{len(tests)}건 통과")
