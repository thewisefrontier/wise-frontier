"""
auto_dedup.py
------------------
find_duplicate_pairs RPC로 중복 후보 기사 쌍을 조회해,
각 쌍에서 나중에 작성된 기사를 자동으로 미발행(is_published=false) 처리한다.

admin.html "중복기사" 탭의 자동 미발행 로직과 동일하되, 사람이 탭을 열지 않아도
주기적으로(워크플로우 스케줄) 서버 쪽에서 실행된다.

- 판단이 애매한 건(오탐)은 그대로 미발행 상태로 두고, admin.html에서 사람이 "복구" 가능.
- dedup_reviewed=true인 기사는 RPC 자체에서 제외되므로, 이미 검토된 쌍은 재처리하지 않는다.

실행: python scripts/auto_dedup.py
"""

import os
import requests
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def fetch_duplicate_pairs(hours=72, threshold=0.5):
    """find_duplicate_pairs RPC 호출"""
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/find_duplicate_pairs",
        headers=_headers(),
        json={"p_hours": hours, "p_threshold": threshold},
        timeout=30,
    )
    if res.status_code != 200:
        print(f"❌ RPC 호출 실패: HTTP {res.status_code} - {res.text[:300]}")
        return []
    return res.json()


def unpublish_articles(ids: list):
    """id 목록을 한 번에 미발행 처리"""
    if not ids:
        return True
    id_list = ",".join(str(i) for i in ids)
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/articles",
        headers=_headers(),
        params={"id": f"in.({id_list})"},
        json={"is_published": False},
        timeout=30,
    )
    if res.status_code not in (200, 204):
        print(f"❌ 미발행 처리 실패: HTTP {res.status_code} - {res.text[:300]}")
        return False
    return True


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    print(f"[자동 중복정리] {now_kst().strftime('%Y-%m-%d %H:%M')} 시작")

    pairs = fetch_duplicate_pairs(hours=72, threshold=0.5)
    if not pairs:
        print("중복 후보 없음")
        return

    later_ids = set()
    for pair in pairs:
        is_a_later = pair["created_at_a"] >= pair["created_at_b"]
        later_id = pair["id_a"] if is_a_later else pair["id_b"]
        later_ids.add(later_id)

    print(f"중복 후보 {len(pairs)}쌍 발견 — 나중 기사 {len(later_ids)}건 미발행 처리 대상")

    if unpublish_articles(sorted(later_ids)):
        print(f"✅ {len(later_ids)}건 미발행 처리 완료: {sorted(later_ids)}")
    else:
        print("❌ 미발행 처리 중 오류 발생")


if __name__ == "__main__":
    run()
