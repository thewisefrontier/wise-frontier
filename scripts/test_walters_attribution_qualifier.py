# -*- coding: utf-8 -*-
"""harvest_art_extra._official_attribution/_QUALIFIER_RE 회귀 테스트.
실사례: 37.1893(Guercino, 2026-10-17 발견) — 월터스 CSV는 "Guercino"뿐이지만
개별 페이지는 "Workshop of Guercino"로 귀속. 네트워크 없이 HTML 스니펫으로 검증."""
from unittest.mock import patch

from harvest_art_extra import _AUTHOR_RE, _QUALIFIER_RE, _official_attribution

SNIPPET_WORKSHOP = '''
<div class="section__author">
    <a href="https://art.thewalters.org/browse/creator/guercino/">Workshop of Guercino (Italian, 1591-1666)</a>
    (Painter)
</div>
'''

SNIPPET_PLAIN = '''
<div class="section__author">
    <a href="https://art.thewalters.org/browse/creator/jules-dupre/">Jules Dupré (French, 1811-1889)</a>
    (Painter)
</div>
'''

# 실사례: 37.572(2026-10-17 발견) — 월터스 페이지가 아포스트로피를 &#039;로 HTML 인코딩함.
SNIPPET_ESCAPED_NAME = '''
<div class="section__author">
    <a href="https://art.thewalters.org/browse/creator/bonifazio/">Workshop of Bonifazio de&#039; Pitati (Italian, 1487-1553)</a>
    (Painter)
</div>
'''


class _FakeResp:
    def __init__(self, text):
        self.text = text


def demo():
    assert _AUTHOR_RE.search(SNIPPET_WORKSHOP).group(1).strip() == "Workshop of Guercino (Italian, 1591-1666)"
    assert _QUALIFIER_RE.match("Workshop of Guercino (Italian, 1591-1666)")
    assert not _QUALIFIER_RE.match("Jules Dupré (French, 1811-1889)")
    # School of Pisa/Pseudo X처럼 풀의 artist_en 자체가 이미 qualifier를 포함한 경우는
    # from_walters()의 official.split(" (")[0] 비교에서 걸러져 캐벗이 안 붙어야 함(오탐 방지) —
    # 그 비교 로직은 여기서 문자열 그대로 재현해 확인.
    official = "School of Pisa (Italian)"
    assert official.split(" (")[0].strip().lower() == "school of pisa"

    with patch("harvest_art_extra.requests.get", return_value=_FakeResp(SNIPPET_WORKSHOP)):
        assert _official_attribution("37.1893") == "Workshop of Guercino (Italian, 1591-1666)"
    with patch("harvest_art_extra.requests.get", return_value=_FakeResp(SNIPPET_PLAIN)):
        assert _official_attribution("37.961") == "Jules Dupré (French, 1811-1889)"
    with patch("harvest_art_extra.requests.get", return_value=_FakeResp(SNIPPET_ESCAPED_NAME)):
        assert _official_attribution("37.572") == "Workshop of Bonifazio de' Pitati (Italian, 1487-1553)"  # &#039; 디코딩 확인
    with patch("harvest_art_extra.requests.get", side_effect=Exception("timeout")):
        assert _official_attribution("x") is None  # 실패 시 None(캐벗 생략, 단정 안 함)

    print("OK")


if __name__ == "__main__":
    demo()
