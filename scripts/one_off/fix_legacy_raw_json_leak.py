# -*- coding: utf-8 -*-
"""2026-06~10월, gemini_writer.py 파싱 실패 시 raw JSON/라벨 텍스트가 그대로
summary_ko에 저장된 과거 기사 일괄 복구(2026-10-07 조사로 발견, 25건).
parse_title_and_body()는 이미 당일 수정돼 있어 이 복구에도 그대로 재사용한다.

실행: python scripts/one_off/fix_legacy_raw_json_leak.py          (미리보기만)
      python scripts/one_off/fix_legacy_raw_json_leak.py --apply  (실제 반영)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

import requests
import gemini_writer as gw

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

IDS = [295443, 294160, 292431, 194692, 189591, 175480, 165274, 157449, 145058,
       107789, 107306, 99924, 94138, 89451, 81208, 80112, 77168, 76448, 72665,
       48017, 44206, 43755, 37988, 2714, 2676]


def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }


def fetch(ids):
    id_list = ",".join(str(i) for i in ids)
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/articles",
        headers=_headers(),
        params={"id": f"in.({id_list})", "select": "id,summary_ko,update_log"},
        timeout=30,
    )
    res.raise_for_status()
    return {r["id"]: r for r in res.json()}


def main():
    apply = "--apply" in sys.argv
    rows = fetch(IDS)
    ok, skip = 0, 0
    for aid in IDS:
        row = rows.get(aid)
        if not row:
            print(f"#{aid}: DB에 없음 — 스킵")
            skip += 1
            continue
        raw = row.get("summary_ko") or ""
        _, body, *_ = gw.parse_title_and_body(raw)
        if not body:
            print(f"#{aid}: 복구 실패(파싱 안 됨, len(raw)={len(raw)}) — 수동 확인 필요")
            skip += 1
            continue
        if len(body) >= len(raw):
            print(f"#{aid}: 변화 없음(이미 정상?) — 스킵")
            skip += 1
            continue
        print(f"#{aid}: {len(raw)}자 raw → {len(body)}자 복구")
        print("   " + body[:100].replace("\n", " "))
        if apply:
            log = row.get("update_log") or []
            if not isinstance(log, list):
                log = []
            import datetime
            kst_now = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
            log.append({
                "timestamp": kst_now,
                "note": "파싱 버그 복구(일괄, 2026-10-07) — summary_ko에 저장돼 있던 파싱 실패 raw 응답을 "
                        "parse_title_and_body()로 재추출한 본문으로 교체",
            })
            r = requests.patch(
                f"{SUPABASE_URL}/rest/v1/articles",
                headers=_headers(),
                params={"id": f"eq.{aid}"},
                json={"summary_ko": body, "update_log": log},
                timeout=30,
            )
            if r.status_code not in (200, 204):
                print(f"   ❌ 업데이트 실패: HTTP {r.status_code} {r.text[:200]}")
            else:
                ok += 1
    print(f"\n{'적용' if apply else '미리보기'} 완료 — 처리 {ok}건, 스킵 {skip}건")


if __name__ == "__main__":
    main()
