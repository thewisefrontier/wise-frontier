"""
scripts/one_off/migrate_supabase_to_aiven_finish.py
---------------------------------------------------
migrate_supabase_to_aiven.py의 [5]~[7](함수·트리거 → 시퀀스 → 건수 대조)만 다시 돌린다.
2026-10-20 첫 실행이 데이터 복사·제약·인덱스까지 끝낸 뒤 함수 단계 SQL 실수로 멈춰서,
25분짜리 전체 복사를 반복하지 않으려고 분리했다(복사 결과는 그대로 둔다).

실행: one_off_task.yml(script=one_off/migrate_supabase_to_aiven_finish.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from migrate_supabase_to_aiven import connect_src, connect_dst, table_list, source_counts, finish  # noqa: E402


def main():
    src = connect_src()
    src.set_session(readonly=True)
    dst = connect_dst()
    tables = table_list(src.cursor())
    finish(src, dst, tables, source_counts(src.cursor(), tables))


if __name__ == "__main__":
    main()
