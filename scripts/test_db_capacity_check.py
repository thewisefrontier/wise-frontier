"""db_capacity_check.evaluate 임계값 회귀 테스트(2026-10-20, Supabase 402 차단 뒤 Aiven 한도 감시)."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from db_capacity_check import evaluate

assert evaluate(377, 6, 17, 0) == ("ok", [])            # 2026-10-20 실측치
assert evaluate(620, 6, 17, 0)[0] == "warn"             # 60% 초과
assert evaluate(800, 6, 17, 0)[0] == "crit"             # 75% 초과
assert evaluate(377, 14, 17, 0)[0] == "warn"            # 연결 80% 이상
assert evaluate(377, 6, 17, 1)[0] == "warn"             # 멈춘 트랜잭션
assert evaluate(800, 14, 17, 1)[0] == "crit"            # 위험이 경고에 덮이지 않음
assert len(evaluate(800, 14, 17, 1)[1]) == 3
print("ok")
