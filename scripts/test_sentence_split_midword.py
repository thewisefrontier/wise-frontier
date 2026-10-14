# -*- coding: utf-8 -*-
"""kss 단어 중간 분리 회귀 테스트(2026-10-14, id=308600 "자마코이\\n\\n스", id=308586 "지 난 6일").
실행: python scripts/test_sentence_split_midword.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import style_guard as sg  # noqa: E402

art = ("작가 스튜어트는 1855년 미국 펜실베이니아주 필라델피아에서 태어나 파리에서 활동한 화가로, 동시대에 활동한 "
       "존 싱어 사전트(John Singer Sargent)와 함께 언급되며 '필라델피아 출신의 파리인'이라는 별명으로 불렸다. "
       "그의 아버지는 설탕 재벌이자 저명한 미술 수집가인 윌리엄 후드 스튜어트(William Hood Stewart)였으며, "
       "아버지를 따라 1865년 필라델피아에서 파리로 이주하여 예술적 환경 속에서 성장했다. "
       "스튜어트는 에두아르도 자마코이스(Eduardo Zamacoïs)에게 사사하고 에콜 데 보자르에서 "
       "장레옹 제롬(Jean-Léon Gérôme)에게 배웠으며, 이후 라이문도 데 마드라조(Raimundo de Madrazo)의 문하에서도 수학했다.")
out = sg.ensure_paragraphs(art)
assert "자마코이스(" in out, out
assert out.replace("\n\n", " ") == art, out  # 문장 경계에서만 나뉘고 글자는 그대로
sents = sg._split_sentences(art)
assert len(sents) == 3 and all(s.endswith("다.") for s in sents), sents
print("midword split ok")

# 정상 문장 분리는 유지
assert len(sg._split_sentences("첫 문장이다. 둘째 문장이다. \"셋째다.\" 넷째다.")) >= 3
print("normal split ok")
