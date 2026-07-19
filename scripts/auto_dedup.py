"""
auto_dedup.py
------------------
find_duplicate_pairs RPC로 중복 후보 기사 쌍을 조회해 자동으로 처리한다.

- 유사도 70% 이상: 거의 확실한 중복 → merge-articles Edge Function 호출
  (Gemini로 두 기사를 통합 재작성, 먼저 올라온 기사를 유지하고 나중 기사는 미발행)
- 유사도 50~70%: 애매함 → 나중에 작성된 기사만 미발행 처리 (자동 통합 안 함)
  → admin.html "중복기사" 탭에서 사람이 "복구"로 오탐 여부 검토 가능

dedup_reviewed=true인 기사는 RPC에서 이미 제외되므로 재처리되지 않는다.

실행: python scripts/auto_dedup.py
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

MERGE_THRESHOLD = 0.7  # 이 이상이면 자동 통합, 미만이면 미발행만


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


def merge_pair(id_keep: int, id_remove: int) -> bool:
    """merge-articles Edge Function 호출 — Gemini로 통합 재작성"""
    try:
        res = requests.post(
            f"{SUPABASE_URL}/functions/v1/merge-articles",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={"id_keep": id_keep, "id_remove": id_remove},
            timeout=30,
        )
        if res.status_code == 200:
            print(f"  ✅ 통합 완료: 유지 #{id_keep}, 미발행 #{id_remove}")
            return True
        else:
            print(f"  ❌ 통합 실패 (#{id_keep}↔#{id_remove}): HTTP {res.status_code} - {res.text[:300]}")
            return False
    except Exception as e:
        print(f"  ❌ 통합 요청 오류 (#{id_keep}↔#{id_remove}): {e}")
        return False


def run():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[SKIP] SUPABASE 환경변수 없음")
        return

    print(f"[자동 중복정리] {now_kst().strftime('%Y-%m-%d %H:%M')} 시작")

    pairs = fetch_duplicate_pairs(hours=72, threshold=0.5)
    if not pairs:
        print("중복 후보 없음")
        return

    high_pairs = [p for p in pairs if p["score"] >= MERGE_THRESHOLD]
    low_pairs = [p for p in pairs if p["score"] < MERGE_THRESHOLD]

    print(f"중복 후보 {len(pairs)}쌍 — 자동통합 대상 {len(high_pairs)}쌍(≥{int(MERGE_THRESHOLD*100)}%), 미발행만 {len(low_pairs)}쌍")

    # ── 70% 이상: 자동 통합 (Gemini) ──
    merged_remove_ids = set()
    for i, pair in enumerate(high_pairs):
        is_a_earlier = pair["created_at_a"] <= pair["created_at_b"]
        id_keep = pair["id_a"] if is_a_earlier else pair["id_b"]
        id_remove = pair["id_b"] if is_a_earlier else pair["id_a"]

        print(f"→ [{i+1}/{len(high_pairs)}] #{id_keep} ↔ #{id_remove} ({pair['score']*100:.0f}%)")
        if merge_pair(id_keep, id_remove):
            merged_remove_ids.add(id_remove)
        if i < len(high_pairs) - 1:
            time.sleep(8)  # Gemini 호출 간 여유

    # ── 50~70%: 미발행만 ──
    later_ids = set()
    for pair in low_pairs:
        is_a_later = pair["created_at_a"] >= pair["created_at_b"]
        later_id = pair["id_a"] if is_a_later else pair["id_b"]
        later_ids.add(later_id)

    if later_ids:
        if unpublish_articles(sorted(later_ids)):
            print(f"✅ 미발행 처리 완료 {len(later_ids)}건: {sorted(later_ids)}")
        else:
            print("❌ 미발행 처리 중 오류 발생")

    print(f"[자동 중복정리] 완료 — 자동통합 {len(merged_remove_ids)}건, 미발행 {len(later_ids)}건")


if __name__ == "__main__":
    run()
