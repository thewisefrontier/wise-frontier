"""
scripts/rss_collector.py
-------------------------
RSS 수집 전용(번역·본문크롤링·텔레그램 발송 없음) — "일단 계속 수집해두는" 단계.

2026-09-22 신설. 기존 rss_fetcher.py는 피드 조회 → 번역·본문크롤링·텔레그램발송·DB저장을
한 실행 안에서 순차로 다 했다. 소스가 430→1200+개로 늘며 실제 병목(번역·크롤링·발송의
순차 루프 — 병렬 피드 조회 자체가 아니라)이 워크플로 타임아웃에 가까워졌고, 그 처리
루프는 수집이 전부 끝난 "뒤"에만 시작되는 구조라 중간에 죽으면 그 실행분 기사가
통째로 0건이 됐다(사용자 지시: "RSS를 가져와서 곧바로 만드는게 아니라 일단 RSS를
계속 수집해두는 시스템").

이 스크립트는 그중 가볍고 빠른 부분(피드 조회 + 제목만으로 되는 필터)만 하고,
살아남은 항목을 `rss_raw_queue`에 소스가 완료되는 즉시(전체 완료를 기다리지 않고)
저장한다 — 타임아웃에 걸려도 이미 들어온 만큼은 남는다. 번역·본문크롤링·텔레그램
발송·최종 저장은 rss_processor.py가 별도 실행에서 배치로 비운다.

실행: python scripts/rss_collector.py
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from rss_fetcher import (
    load_rss, fetch_source, is_noise, is_duplicate, extract_summary,
    entry_published_iso, state, rss_health, save_state,
)
from dedup_guard import normalize_tags
from db import queue_insert_bulk

MAX_WORKERS = 40   # rss_fetcher.py의 이전 결정과 동일 — 순수 I/O 대기라 GIL 경합 없음
FLUSH_EVERY = 80    # 이만큼 모이면 한 번의 POST로 큐에 적재(개별 POST는 3000여 건에서
                    # 15분+ 걸림 — 2026-09-22 두 번째 병목 실측). 값은 타임아웃에 걸려도
                    # 잃는 양(최대 이 개수)과 요청 수(총건수/이 값)의 절충.


def _raw_tags(entry) -> list:
    tags = [t.get("term", "").strip() for t in (entry.get("tags") or []) if t.get("term")]
    return normalize_tags(tags, limit=10)


def collect():
    sources = load_rss()
    print(f"[수집] {len(sources)}개 소스 병렬 수집 시작...")

    seen_titles = []
    queued = skipped_noise = skipped_dup = buffered = 0
    src_ok = src_fail = src_too_old = 0
    fail_samples = []  # 진단용 — 실패 사유 앞부분만 몇 개 수집(2026-09-22, 라이브 실행 결과 이상 조사)
    buf = []  # 아직 큐에 반영 안 한 대기분 — FLUSH_EVERY마다 한 번에 내보낸다

    def flush():
        nonlocal queued, buf
        if buf:
            queued += queue_insert_bulk(buf)
            buf = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_source, s): s for s in sources}
        # 소스 하나가 끝날 때마다 바로 큐에 적재한다 — 전체 완료를 기다리는 순간
        # 이 워크플로 타임아웃에 걸려 죽으면 그때까지 모은 게 통째로 날아간다.
        for future in as_completed(futures):
            items, name, status = future.result()
            if name not in rss_health:
                rss_health[name] = {"ok": 0, "fail": 0, "status": "active"}

            if status == "too_old":
                rss_health[name]["too_old"] = rss_health[name].get("too_old", 0) + 1
                src_too_old += 1
                continue
            if status != "ok" or not items:
                rss_health[name]["fail"] += 1
                src_fail += 1
                if len(fail_samples) < 15:
                    fail_samples.append(f"{name}: {status}")
                continue
            rss_health[name]["ok"] += 1
            src_ok += 1

            # 노이즈·유사중복은 제목만으로 판단(네트워크 없음). "이미 articles에 있는
            # 링크인지"는 여기서 항목마다 확인하지 않는다 — 1200+소스에서 항목마다
            # DB 왕복(is_url_exists)을 걸면 초당 1건 수준으로 병목(2026-09-22 첫 실전
            # 시험: 13분에 689건). 그 확인은 처리기가 자기 배치(최대 150건) 전체를
            # 한 번의 IN 쿼리로 묶어서 한다(rss_processor.py) — 큐 자체는 link UNIQUE라
            # 같은 미처리 링크를 반복 수집해도 안전하게 무시된다.
            for data in items:
                title, link, entry, s = data["title"], data["link"], data["entry"], data["source"]

                if is_noise(title):
                    skipped_noise += 1
                    continue
                if is_duplicate(title, seen_titles):
                    skipped_dup += 1
                    continue
                seen_titles.append(title)
                buffered += 1

                buf.append({
                    "link": link, "title": title, "source_name": name,
                    "category": s["category"], "subcategory": s["subcategory"],
                    "summary_en": extract_summary(entry),
                    "source_published_at": entry_published_iso(entry),
                    "raw_tags": _raw_tags(entry) or None,
                })
                if len(buf) >= FLUSH_EVERY:
                    flush()

        flush()  # 남은 대기분(FLUSH_EVERY 미만) 마무리

    save_state()
    print(f"[수집 완료] 큐 적재 {queued}건(버퍼 {buffered}건 중) | 노이즈제외 {skipped_noise} | 유사중복 {skipped_dup}")
    print(f"[소스 결과] 성공 {src_ok} | 발행일초과 {src_too_old} | 실패 {src_fail} (총 {len(sources)})")
    for s in fail_samples:
        print(f"  [실패예시] {s}")


if __name__ == "__main__":
    try:
        collect()
    except Exception as e:
        # 부분 적재는 소스 완료마다 이미 커밋됐으므로 여기서 죽어도 유실은 rss_health
        # 갱신분 정도다. continue-on-error 워크플로에서 다음 실행이 이어받는다.
        print(f"[ERROR] 수집 중단: {e}")
        sys.exit(1)
