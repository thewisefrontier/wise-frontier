# 회귀: id=323869 날씨 기사 — 표 블록(▲ 줄)이 본문과 한 문단으로 뭉치던 버그
from style_guard import ensure_paragraphs
p = ["가나다 날씨다. 라마바 날씨다. 사아자 날씨다.", "차카타 날씨다. 파하 날씨다. 거너더 날씨다.", "러머버 날씨다."]
t = ["[지역별 날씨 전망] [오전/오후](최저∼최고기온) | 강수확률"] + [f"▲ 도시{i} : [오전 흐림] (1∼2) <오전 강수확률 30%>, 최대풍속 8km/h" for i in range(5)]
body = "\n\n".join(p + t)
assert ensure_paragraphs(body) == body
print("ok")

# 단정 어조 완화(2026-11-09, id=323869 문장): 예상 어조로, 표·관측 문장은 그대로
import sys, os
os.environ.setdefault("SUPABASE_URL", "http://x")
from weather_report import soften_forecast
t = "제주는 흐리고 비가 내리며 최대풍속 28km/h의 바람이 동반된다. 대구는 최저 6°C, 최고 11°C로 나타난다. 현재 기온은 3°C를 기록한다."
r = soften_forecast(t + "\n\n▲ 서울 : [맑음] (4∼13) 보인다")
assert "동반될 전망이다" in r and "나타날 전망이다" in r
assert "현재 기온은 3°C를 기록한다." in r and r.endswith("(4∼13) 보인다")
print("ok2")

# 2026-11-10 324179: 이미 예상 어조인 문장에 또 붙여 "예상될 전망이다"·"것으로 보일 전망이다"가 됨
r = soften_forecast("뇌우가 동반될 것으로 예상된다. 폭염이 나타날 것으로 보인다. 비가 내린다.")
assert "예상될 전망이다" not in r and "보일 전망이다" not in r, r
assert "예상된다." in r and "것으로 보인다." in r and "내릴 전망이다" in r, r
print("ok3")
