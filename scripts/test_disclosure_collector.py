# -*- coding: utf-8 -*-
"""거래소 공시 수집기의 중요도 규칙 회귀 테스트(2026-09-28). 공시 전량이 아니라 중요 공시만 통과해야 DB가 안전하다.
실행: python scripts/test_disclosure_collector.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import disclosure_collector as dc  # noqa: E402


def tier(title):
    if dc.CN_NOISE.search(title):
        return None
    return next((t for t, rx in dc.CN_TIERS if rx.search(title)), None)


# 통과해야 하는 중요 공시
assert tier("*ST岭南 关于公司股票存在可能因股价低于面值被终止上市的风险提示公告") == 1
assert tier("XX公司关于收到中国证监会立案告知书的公告") == 1
assert tier("XX公司关于控股股东、实际控制人变更的公告") == 1
assert tier("XX公司重大资产重组预案") == 2
assert tier("XX公司2026年前三季度业绩预告") == 3
assert tier("XX公司关于签订股权收购相关协议的公告") == 3
# 흔한 정형 공시는 통과하면 안 된다(하루 600건 중 10건 안팎만 남아야 함)
for noise in ("XX公司关于以集中竞价交易方式回购股份的进展公告", "XX公司关于股东减持股份计划的公告",
              "XX公司2026年半年度权益分派实施公告", "XX公司董事会会议决议公告", "XX公司法律意见书",
              "XX公司关于实际控制人部分股份质押的公告", "XX公司关于最近五年被证券监管部门处罚或采取监管措施情况的公告",
              "XX公司向特定对象发行A股股票募集说明书", "XX公司关于召开2026年第三次临时股东大会的通知"):
    assert tier(noise) is None, noise
# 대만: 사소한 공시 제외
for n in ("公告本公司股票面額由「新台幣10元」變更為「新台幣0.5元」", "公告本公司名稱由「甲」更名為「乙」", "公告本公司董事會決議配發股利"):
    assert dc.TW_NOISE.search(n), n
assert not dc.TW_NOISE.search("公告本公司工地火災事件說明")
# 상한과 소스명이 공식 소스 목록과 일치
import gemini_writer as gw  # noqa: E402
for s in (dc.CN_SOURCE, dc.TW_SOURCE):
    assert s in gw.OFFICIAL_SOURCE_NAMES and s in gw.OFFICIAL_SOLO_SOURCE_NAMES, s
assert dc.DAILY_CAP[dc.CN_SOURCE] <= 15 and dc.DAILY_CAP[dc.TW_SOURCE] <= 10
print("ok")
