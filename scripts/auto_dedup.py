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

# 정기 발행물은 중복 판정에서 제외한다.
# 다이제스트는 제목이 "[데일리 다이제스트] " 접두어 + 추상 명사구(지정학·기후·공급망)로
# 고정돼 있어 내용이 전혀 달라도 trigram 유사도가 0.5를 넘는다.
# 실측(2026-08-04): 7/31↔7/30 = 0.623, 8/2↔7/30 = 0.580 → 둘 다 오탐 미발행.
# 하루 1건만 생성되고 daily_digest.py가 digest_exists_for_today()로 자체 중복 방지를 하므로
# 애초에 dedup 대상이 아니다.
EXCLUDE_CATEGORIES = {"다이제스트"}

# 국내·국제유가·글로벌마켓동향은 category가 범용 "경제"라 카테고리 제외로는
# 못 걸러낸다. "[국내유가] 휘발유 리터당 N원…" 같은 템플릿 제목이 매일
# 반복돼 trigram 유사도가 항상 50%를 넘는다(실사고: 8/19·8/20 연속 오탐
# 미발행, digest_exists_for_today() 대응하는 자체 중복 방지가 이 계열
# 스크립트엔 없어 dedup에 그대로 노출됐다). frontier_markets_writer.py도
# 신설 시점부터 동일 패턴이 예상돼 미리 등록해둔다. "프론티어마켓동향"은
# 2026-08-21 당일 "글로벌마켓동향"으로 개편되기 전 발행된 기사 1건(id=90417)
# 을 위해 남겨둔 구 subcategory — 지우지 말 것.
EXCLUDE_SUBCATEGORIES = {"국내유가", "국제유가", "글로벌마켓동향", "프론티어마켓동향", "복권당첨정보"}


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


def fetch_categories(ids: list) -> dict:
    """id → (category, subcategory) 매핑. 제외 판정에 쓴다."""
    if not ids:
        return {}
    id_list = ",".join(str(i) for i in ids)
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/articles",
        headers=_headers(),
        params={"id": f"in.({id_list})", "select": "id,category,subcategory"},
        timeout=20,
    )
    if res.status_code != 200:
        print(f"⚠️ 카테고리 조회 실패: HTTP {res.status_code} — 제외 필터 미적용")
        return {}
    return {r["id"]: (r.get("category") or "", r.get("subcategory") or "") for r in res.json()}


def _is_excluded(id_: int, cats: dict) -> bool:
    category, subcategory = cats.get(id_, ("", ""))
    return category in EXCLUDE_CATEGORIES or subcategory in EXCLUDE_SUBCATEGORIES


def unpublish_articles(ids: list, scores: dict = None):
    """id 목록을 미발행 처리하고 사유를 update_log에 남긴다.

    사유를 남기는 이유: 기존에는 is_published만 바꿔 log가 비어 있었고,
    그 탓에 다이제스트 오탐 미발행의 원인을 찾는 데 시간이 걸렸다.
    note 문구는 export의 화이트리스트에 없으므로 공개 JSON에는 실리지 않는다.
    """
    if not ids:
        return True
    scores = scores or {}
    ok = True
    stamp = now_kst().strftime("%Y-%m-%d %H:%M")
    for aid in ids:
        sc = scores.get(aid)
        note = ("자동 중복정리 미발행"
                + (f" — 유사도 {sc*100:.0f}%" if sc is not None else ""))
        log = []
        try:
            g = requests.get(
                f"{SUPABASE_URL}/rest/v1/articles",
                headers=_headers(),
                params={"id": f"eq.{aid}", "select": "update_log"},
                timeout=15,
            )
            if g.status_code == 200 and g.json():
                cur = g.json()[0].get("update_log")
                if isinstance(cur, list):
                    log = cur
        except Exception as e:
            print(f"  ⚠️ #{aid} update_log 조회 실패: {e}")
        log.append({"timestamp": stamp, "note": note})
        res = requests.patch(
            f"{SUPABASE_URL}/rest/v1/articles",
            headers=_headers(),
            params={"id": f"eq.{aid}"},
            json={"is_published": False, "update_log": log},
            timeout=30,
        )
        if res.status_code not in (200, 204):
            print(f"❌ #{aid} 미발행 처리 실패: HTTP {res.status_code} - {res.text[:200]}")
            ok = False
    return ok


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

    # 정기 발행물(다이제스트 등)은 제목 구조상 trigram이 구조적으로 높아 오탐이 난다.
    cats = fetch_categories(sorted({p["id_a"] for p in pairs} | {p["id_b"] for p in pairs}))
    if cats:
        before = len(pairs)
        pairs = [p for p in pairs
                 if not _is_excluded(p["id_a"], cats)
                 and not _is_excluded(p["id_b"], cats)]
        if before != len(pairs):
            print(f"제외 카테고리/서브카테고리로 {before - len(pairs)}쌍 건너뜀")
        if not pairs:
            print("처리할 중복 후보 없음")
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
    later_scores = {}
    for pair in low_pairs:
        is_a_later = pair["created_at_a"] >= pair["created_at_b"]
        later_id = pair["id_a"] if is_a_later else pair["id_b"]
        later_ids.add(later_id)
        # 같은 기사가 여러 쌍에 걸리면 가장 높은 유사도를 기록한다
        if pair["score"] > later_scores.get(later_id, 0):
            later_scores[later_id] = pair["score"]

    if later_ids:
        if unpublish_articles(sorted(later_ids), later_scores):
            print(f"✅ 미발행 처리 완료 {len(later_ids)}건: {sorted(later_ids)}")
        else:
            print("❌ 미발행 처리 중 오류 발생")

    print(f"[자동 중복정리] 완료 — 자동통합 {len(merged_remove_ids)}건, 미발행 {len(later_ids)}건")


if __name__ == "__main__":
    run()
