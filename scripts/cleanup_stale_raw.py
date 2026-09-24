"""
scripts/cleanup_stale_raw.py
-------------------------------
원자재(클러스터링 후보, raw_candidates 테이블) 정기 삭제(2026-09-04 신설 —
사용자 지적: "이건 리스크인데" → DB 용량 실측 결과 전체 105,697행 중 91%
(96,554건)가 미발행 원자재였고, 그중 70%(74,259건)가 7일 넘은 완전히 죽은
데이터였음. 삭제 로직이 이 저장소에 아예 없었다).

gemini_writer.py의 get_today_articles()는 최근 96시간(4일)치 원자재만
클러스터링 후보로 본다 — 그보다 오래된 원자재는 앞으로도 영원히 다시
쓰이지 않는다. 보존 기준을 정확히 그 96시간으로 맞춘다(여유를 더 두지
않음).

⚠️ 2026-09-24: 원자재를 발행 기사와 같은 무거운 articles 스키마(30여 컬럼)에
넣던 걸 raw_candidates(클러스터링에 실제 쓰는 컬럼만 남긴 좁은 테이블)로
분리했다(하루 6만 건 넘는 원자재의 발행 전환율이 약 0.06%였는데도 무거운
스키마의 행당 오버헤드를 그대로 지고 있었음). raw_candidates는 원자재
전용이라 is_published/source 필터가 필요 없다 — 테이블 전체가 대상이다.
7일이던 보존 기준도 실제 클러스터링 창(96시간)에 정확히 맞춰 줄였다
(예전 "+3일 여유"는 유입량이 지금보다 훨씬 적을 때 정한 값).

⚠️ 2026-09-23: 한 번의 DELETE로 수천 행을 지우다 PostgREST statement timeout에
걸려 실패하는데, 오류를 출력만 하고 exit 0이라 워크플로는 매일 "성공"으로 떴다.
→ 작은 배치로 나눠 지우고, 실패하면 exit 1로 드러나게 한다.

실행: python scripts/cleanup_stale_raw.py
"""

import os
import sys
import requests
from datetime import timedelta
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

from db import now_kst

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

RETENTION_HOURS = int(os.getenv("RAW_CANDIDATE_RETENTION_HOURS", "96"))
BATCH = 300


def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def _batched(filters: dict, apply) -> int:
    """filters에 맞는 행 id를 BATCH개씩 조회해 apply(id목록)를 반복. 처리 건수 반환."""
    url = f"{SUPABASE_URL}/rest/v1/raw_candidates"
    done, last = 0, 0
    while True:
        # id>last로 이어서 조회한다 — 매번 앞에서부터 다시 찾으면 방금 지운(아직
        # vacuum 전) 죽은 행을 반복해서 훑어 점점 느려지다 타임아웃(실측 1.2만 건째).
        res = requests.get(url, headers=_headers(), timeout=60,
                           params={**filters, "id": f"gt.{last}", "select": "id", "order": "id",
                                   "limit": str(BATCH)})
        res.raise_for_status()
        ids = [r["id"] for r in res.json()]
        if not ids:
            return done
        r = apply(url, f"in.({','.join(map(str, ids))})")
        r.raise_for_status()
        last = ids[-1]
        done += len(ids)
        if done % 3000 < BATCH:
            print(f"    … {done}건")


def main():
    print(f"\n[cleanup_stale_raw] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    cutoff = (now_kst() - timedelta(hours=RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M")
    print(f"  → 삭제: {cutoff} 이전 원자재(raw_candidates)")
    deleted = _batched(
        {"created_at": f"lt.{cutoff}"},
        lambda url, ids: requests.delete(url, headers=_headers(), params={"id": ids}, timeout=60),
    )
    print(f"  ✓ 삭제 {deleted}건")

    print(f"[cleanup_stale_raw] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  [ERROR] {e}")
        sys.exit(1)
