"""
scripts/cleanup_stale_raw.py
-------------------------------
미발행 원자재(RSS 등에서 긁어왔지만 클러스터링에 못 쓰이고 남은 raw article)
정기 삭제(2026-09-04 신설 — 사용자 지적: "이건 리스크인데" → DB 용량 실측
결과 전체 105,697행 중 91%(96,554건)가 미발행 원자재였고, 그중 70%(74,259건)
가 7일 넘은 완전히 죽은 데이터였음. 삭제 로직이 이 저장소에 아예 없었다).

gemini_writer.py의 get_today_articles()는 최근 96시간(4일)치 원자재만
클러스터링 후보로 본다 — 그보다 오래된 미발행 원자재는 앞으로도 영원히
다시 쓰이지 않는다. 96시간 + 여유(3일)를 더한 7일을 보존 기준으로 삼아,
그보다 오래된 미발행 원자재를 삭제한다.

⚠️ source='NewsFinal'(NewsFinal 자체가 쓴 기사 중 검수 실패로 미발행 처리된
것)은 삭제 대상에서 제외한다 — 이건 사람이 나중에 admin.html에서 검토해
수동 발행할 수도 있는 실제 작성물이라, RSS에서 긁어온 원문 사본과 성격이
다르다(다시 필요하면 원본 소스에서 재수집 가능한 raw article과 달리 이건
유실되면 복구 불가).

⚠️ 2026-09-23: 한 번의 DELETE로 수천 행을 지우다 PostgREST statement timeout에
걸려 실패하는데, 오류를 출력만 하고 exit 0이라 워크플로는 매일 "성공"으로 떴다.
9/2~9/16치 약 5만5천 행이 그대로 쌓여 DB가 무료 한도(500MB) 직전(456MB)까지
갔다. → 작은 배치로 나눠 지우고, 실패하면 exit 1로 드러나게 한다.

또 96시간이 지난 미발행 원자재는 본문(full_text)만 비운다 — 본문을 읽는 가장
긴 창이 gemini_writer의 96시간이라 그 뒤론 쓰이지 않고, 용량 대부분이 본문이다.
기사 작성물(NewsFinal, DomesticKR-Synth)은 제외.

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

RETENTION_DAYS = int(os.getenv("RAW_ARTICLE_RETENTION_DAYS", "7"))
FULLTEXT_RETENTION_HOURS = int(os.getenv("RAW_FULLTEXT_RETENTION_HOURS", "96"))
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
    url = f"{SUPABASE_URL}/rest/v1/articles"
    done = 0
    while True:
        res = requests.get(url, headers=_headers(), timeout=60,
                           params={**filters, "select": "id", "order": "id", "limit": str(BATCH)})
        res.raise_for_status()
        ids = [r["id"] for r in res.json()]
        if not ids:
            return done
        r = apply(url, f"in.({','.join(map(str, ids))})")
        r.raise_for_status()
        done += len(ids)
        if done % 3000 < BATCH:
            print(f"    … {done}건")


def main():
    print(f"\n[cleanup_stale_raw] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    cutoff = (now_kst() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M")
    print(f"  → 삭제: {cutoff} 이전 미발행 원자재")
    deleted = _batched(
        {"is_published": "eq.false", "source": "neq.NewsFinal", "created_at": f"lt.{cutoff}"},
        lambda url, ids: requests.delete(url, headers=_headers(), params={"id": ids}, timeout=60),
    )
    print(f"  ✓ 삭제 {deleted}건")

    ft_cutoff = (now_kst() - timedelta(hours=FULLTEXT_RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M")
    print(f"  → 본문 비우기: {ft_cutoff} 이전 미발행 원자재(작성물 제외)")
    cleared = _batched(
        {"is_published": "eq.false", "source": "not.in.(NewsFinal,DomesticKR-Synth)",
         "created_at": f"lt.{ft_cutoff}", "full_text": "not.is.null"},
        lambda url, ids: requests.patch(url, headers=_headers(), params={"id": ids},
                                        json={"full_text": None}, timeout=60),
    )
    print(f"  ✓ 본문 비움 {cleared}건")

    print(f"[cleanup_stale_raw] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  [ERROR] {e}")
        sys.exit(1)
