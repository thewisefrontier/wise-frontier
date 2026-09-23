"""
scripts/title_cache.py
----------------------
트렌드 감지용 최근 원자재(제목·요약) 증분 캐시 (2026-09-24 신설).

gemini_summarizer의 실시간 트렌드 감지·트렌드 추적은 최근 며칠치 원자재 전량을
훑는다. 매 실행 DB에서 전량을 받으면 실행당 ~90MB로 Supabase 무료 이그레스
(5GB/월)를 혼자 넘겼다(원자재 전량 저장 전환 후 모집단이 하루 2.8만 건).
DB 안 집계는 43초로 PostgREST 8초 제한에 걸려 불가 → GitHub Actions 캐시에
받아둔 행을 두고, 매 실행 "마지막으로 받은 시각 이후" 새 행만 받는다.

캐시 파일 경로는 RT_CACHE_PATH(run.yml이 러너 임시 폴더로 지정 — heavy job이
`git add -A`로 커밋하므로 저장소 안에 두면 안 된다). 캐시가 없으면(첫 실행·
만료) 창 전체를 한 번 받는다.
"""

import gzip
import json
import os
from datetime import datetime, timedelta

from db import now_kst, requests, _url, _headers

CACHE_PATH = os.getenv("RT_CACHE_PATH", "")
KEEP_DAYS = 9          # 실시간 트렌드 창(2일+7일)이 가장 길다
OVERLAP_MIN = 15       # 삽입 시각과 커밋 시각 차이로 빠지는 행 방지용 겹침
FIELDS = "id,created_at,title_en,title_ko,summary_en,summary_ko,source,country,category,region,source_data"

_mem = None


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def _fetch_since(since: str) -> list:
    rows, offset = [], 0
    while True:
        res = requests.get(_url(), headers={**_headers(), "Range": f"{offset}-{offset + 999}"}, timeout=30,
                           params={"select": FIELDS, "source": "neq.NewsFinal", "created_at": f"gte.{since}",
                                   "order": "created_at.asc,id.asc"})
        if res.status_code not in (200, 206):
            raise RuntimeError(f"원자재 조회 실패 {res.status_code}: {res.text[:200]}")
        batch = res.json()
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        offset += 1000


def _load_file() -> list:
    if not CACHE_PATH or not os.path.exists(CACHE_PATH):
        return []
    try:
        with gzip.open(CACHE_PATH, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠️ 트렌드 캐시 읽기 실패(전체 재조회): {e}")
        return []


def _save_file(rows: list):
    if not CACHE_PATH:
        return
    tmp = CACHE_PATH + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, CACHE_PATH)  # 도중에 타임아웃으로 죽어도 기존 캐시가 깨지지 않게


def load_recent(days: int) -> list:
    """최근 days일(≤KEEP_DAYS) 원자재를 최신순으로 반환. 같은 프로세스에선 한 번만 갱신한다."""
    global _mem
    if _mem is None:
        cutoff = _fmt(now_kst() - timedelta(days=KEEP_DAYS))
        cached = [r for r in _load_file() if (r.get("created_at") or "") >= cutoff]
        if cached:
            last = max(r["created_at"] for r in cached)
            since = _fmt(datetime.strptime(last, "%Y-%m-%d %H:%M") - timedelta(minutes=OVERLAP_MIN))
        else:
            since = cutoff
        new = _fetch_since(since)
        by_id = {r["id"]: r for r in cached}
        by_id.update({r["id"]: r for r in new})
        _mem = sorted(by_id.values(), key=lambda r: (r.get("created_at") or "", r["id"]), reverse=True)
        print(f"  [트렌드 캐시] 캐시 {len(cached)}건 + 신규 {len(new)}건 조회(since {since}) → {len(_mem)}건")
        try:
            _save_file(_mem)
        except Exception as e:
            print(f"  ⚠️ 트렌드 캐시 저장 실패(다음 실행에 전체 재조회): {e}")
    cut = _fmt(now_kst() - timedelta(days=days))
    return [r for r in _mem if (r.get("created_at") or "") >= cut]
