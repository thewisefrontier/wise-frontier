# -*- coding: utf-8 -*-
"""lotto_writer.py 회귀 테스트(2026-09-27, 파워볼 상금 문단 뭉침 수정).
실행: python scripts/test_lotto_writer.py"""
import os
import sys

os.environ.setdefault("SUPABASE_URL", "http://fake")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
sys.path.insert(0, os.path.dirname(__file__))
import lotto_writer as w  # noqa: E402

# 2026-09-27 실사고: 잭팟·1등당첨여부·2등·기타등수 문장이 전부 " ".join()으로
# 한 문단에 뭉쳐서 나갔다(사용자 지적: "문단 처리가 제대로 안됐다").
prize = {
    "jackpot_text": "$150 Million",
    "cash_text": "$65.2 Million",
    "groups": [
        "None Powerball JACKPOT WINNERS",
        "",
        "Winners California, Texas",
    ],
    "tiers": [
        ("m5-pb", ["1", "None", "Jackpot"]),
        ("m5", ["2", "12", "$1,000,000"]),
        ("m4-pb", ["3", "34", "$50,000"]),
    ],
}

out = w._pb_prize_sentences(prize)
paras = out.split("\n\n")
assert len(paras) == 4, f"문단 4개(잭팟/1등여부/2등/기타등수) 기대, 실제 {len(paras)}개:\n{out}"
assert "1억" in paras[0] and "현금가치" in paras[0]
assert "1등(잭팟) 당첨자는 나오지 않았다" in paras[1]
assert "2등(5개 번호 일치)" in paras[2]
assert "3등" in paras[3]

# 2026-09-27 실사고: 자체 억/만 그룹핑에 그룹 사이 공백이 들어가
# 공용 규칙(style_guard.to_won_style_amount, "150만8069원")과 어긋나 있었다.
assert w.format_amount(1197258718) == "11억9725만8718원"
assert w._usd_amount_to_kr("$1,000,000") == "100만달러"
assert w._usd_million_to_kr(119.0) == "1억1900만달러"
for s in (w.format_amount(1197258718), w._usd_amount_to_kr("$1,000,000"), w._usd_million_to_kr(119.0)):
    assert " " not in s, f"공백이 남아있음: {s!r}"

# 2026-09-27 사용자 지적("인원수 표기도 콤마 빼") — 1만 미만도 콤마 없이.
assert w.format_count(3081) == "3081"
assert w.format_count(152825) == "15만2825"
assert "," not in w.format_count(3081)

print("ok")
