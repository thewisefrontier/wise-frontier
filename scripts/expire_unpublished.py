"""
expire_unpublished.py
---------------------
되살릴 가치가 없는 사유로 미발행된 자체 기사(source=NewsFinal)를 3일 뒤 삭제한다(2026-10-27 사용자 지시 — "기사가 너무
짧으면 굳이 내지 말고 세이브해뒀다가 … 아니면 그냥 지워버리는 방식", 보류 기간 3일).

삭제 사유(update_log의 note 중 하나라도 해당):
  중복("유사 기사 재발견", "자동 중복정리 미발행", "통합 미발행") · 분량·밀도 부족 · 복수 토픽 혼입
보존(건드리지 않음): 고유명사 날조 의심·번역 누락·날짜 환각(어드민 검토·reverify_held.py 재검증 대상), 사유가 없는 미발행,
  다른 기사가 continuation_of_id로 참조하는 기사.

안전장치: articles의 trg_articles_history 트리거가 삭제 직전 행 전체를 article_revisions(op='DELETE', old_values)에 남긴다.
  이 스크립트가 지운 기록은 actor에 "expire_unpublished"가 붙고, REVISION_KEEP_DAYS(14일) 동안 남아 복구할 수 있다
  — 복구: insert into articles select * from jsonb_populate_record(null::articles, (select old_values from article_revisions
  where article_id=<id> and op='DELETE' order by id desc limit 1));
  14일이 지나면 그 기사의 이력 행도 지워 실제 용량을 비운다(이 스크립트가 지운 것만).

실행: python scripts/expire_unpublished.py           # 대상만 출력(삭제 안 함)
      python scripts/expire_unpublished.py --apply   # 삭제 + 14일 지난 이력 정리
"""

import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

HOLD_DAYS = 3
REVISION_KEEP_DAYS = 14
EXPIRE_REASON_RE = "유사 기사 재발견|자동 중복정리 미발행|통합 미발행|분량 부족|밀도 부족|복수 토픽 혼입"
# 보존 사유가 하나라도 같이 있으면(예: 날조 의심 보류 뒤 중복 표시가 덧붙은 기사) 보존이 우선 — 어드민·재검증 대상이다.
KEEP_REASON_RE = "고유명사 날조|근거 불일치|위키 미확인|번역 누락|날짜 ?환각"
MAX_PER_RUN = 3000   # 이상 동작(조건 오류 등)으로 대량 삭제되는 걸 막는 상한
ACTOR_TAG = "expire_unpublished"

CANDIDATES_SQL = """
select a.id, left(a.title_ko, 50), a.created_at
from articles a
where a.source = 'NewsFinal' and not a.is_published
  and a.update_log::text ~ %(reason)s
  and a.update_log::text !~ %(keep)s
  and a.created_at < to_char(now() at time zone 'Asia/Seoul' - make_interval(days => %(hold)s), 'YYYY-MM-DD HH24:MI')
  and not exists (select 1 from articles x where x.continuation_of_id = a.id)
order by a.id
"""


def main():
    apply = "--apply" in sys.argv
    conn = psycopg2.connect(os.environ["AIVEN_SERVICE_URI"], connect_timeout=20)
    cur = conn.cursor()
    cur.execute(CANDIDATES_SQL, {"reason": EXPIRE_REASON_RE, "keep": KEEP_REASON_RE, "hold": HOLD_DAYS})
    rows = cur.fetchall()
    print(f"[만료 정리] 삭제 대상 {len(rows)}건 (미발행 {HOLD_DAYS}일 경과, 사유: 중복·분량 부족·복수 토픽)")
    for r in rows[:10]:
        print(f"  #{r[0]} {r[2]} {r[1]}")
    if len(rows) > 10:
        print(f"  … 외 {len(rows) - 10}건")
    if not apply:
        print("  (확인만 — 삭제하려면 --apply)")
        return
    if len(rows) > MAX_PER_RUN:
        print(f"  ⛔ 상한 {MAX_PER_RUN}건 초과 — 조건 이상 의심, 삭제하지 않음")
        return

    # 트리거가 남기는 삭제 이력의 actor에 표시를 단다(14일 뒤 이 스크립트가 지운 것만 골라 정리하려고)
    cur.execute("select set_config('request.headers', %s, false)", ('{"user-agent": "%s"}' % ACTOR_TAG,))
    ids = [r[0] for r in rows]
    deleted = 0
    for i in range(0, len(ids), 200):
        cur.execute("delete from articles where id = any(%s) and not is_published", (ids[i:i + 200],))
        deleted += cur.rowcount
        conn.commit()
    print(f"  ✅ 삭제 {deleted}건 (article_revisions에 {REVISION_KEEP_DAYS}일 보관)")

    cur.execute(
        """delete from article_revisions r
           where r.article_id in (select article_id from article_revisions
                                  where op = 'DELETE' and actor like %s and changed_at < now() - make_interval(days => %s))
             and not exists (select 1 from articles a where a.id = r.article_id)""",
        (f"%{ACTOR_TAG}%", REVISION_KEEP_DAYS),
    )
    conn.commit()
    print(f"  🧹 {REVISION_KEEP_DAYS}일 지난 삭제 기사 이력 {cur.rowcount}행 정리")


if __name__ == "__main__":
    main()
