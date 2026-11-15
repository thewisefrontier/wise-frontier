import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from article_store import fix_us_midterm as f
# 실사례 id=321954·323284·312600 (2026-11-15)
assert f("비트코인 1.48% 상승…미 대선 불확실성 속 반등") == "비트코인 1.48% 상승…미 중간선거 불확실성 속 반등"
assert f("미 대선 중간선거를 앞두고") == "미 중간선거를 앞두고"
assert f("2027년 한국 대선 후보") == "2027년 한국 대선 후보"  # 오탐 대조: 타국 대선은 보존
print("ok")
