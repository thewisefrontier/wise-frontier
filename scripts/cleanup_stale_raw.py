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

실행: python scripts/cleanup_stale_raw.py
권장: 하루 1회(자정 근처)면 충분 — 매일 3,000~3,700행씩 유입되는 것에 비해
삭제는 배치로 몰아서 해도 무방.
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


def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal,count=exact",
    }


def main():
    print(f"\n[cleanup_stale_raw] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("  [SKIP] SUPABASE 환경변수 없음")
        return

    cutoff = (now_kst() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M")
    print(f"  → 보존 기준: {RETENTION_DAYS}일 (컷오프 {cutoff} 이전 미발행 원자재 삭제)")

    res = requests.delete(
        f"{SUPABASE_URL}/rest/v1/articles",
        headers=_headers(),
        params={
            "is_published": "eq.false",
            "source": "neq.NewsFinal",
            "created_at": f"lt.{cutoff}",
        },
        timeout=60,
    )

    if res.status_code in (200, 204):
        # Supabase는 count=exact prefer 헤더를 응답 Content-Range로 돌려줌
        deleted = res.headers.get("content-range", "").split("/")[-1]
        print(f"  ✓ 삭제 완료 (약 {deleted}건)")
    else:
        print(f"  [ERROR] 삭제 실패: {res.status_code} — {res.text[:300]}")

    print(f"[cleanup_stale_raw] 완료: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")


if __name__ == "__main__":
    main()
