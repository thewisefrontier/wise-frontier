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
from db import is_url_exists, queue_insert

MAX_WORKERS = 40  # rss_fetcher.py의 이전 결정과 동일 — 순수 I/O 대기라 GIL 경합 없음


def _raw_tags(entry) -> list:
    tags = [t.get("term", "").strip() for t in (entry.get("tags") or []) if t.get("term")]
    return normalize_tags(tags, limit=10)


def collect():
    sources = load_rss()
    print(f"[수집] {len(sources)}개 소스 병렬 수집 시작...")

    seen_titles = []
    queued = skipped_noise = skipped_dup = skipped_existing = 0

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
                continue
            if status != "ok" or not items:
                rss_health[name]["fail"] += 1
                continue
            rss_health[name]["ok"] += 1

            for data in items:
                title, link, entry, s = data["title"], data["link"], data["entry"], data["source"]

                if is_noise(title):
                    skipped_noise += 1
                    continue
                if is_duplicate(title, seen_titles):
                    skipped_dup += 1
                    continue
                seen_titles.append(title)
                if is_url_exists(link):  # 이미 승격된(articles) 링크 — 재수집 불필요
                    skipped_existing += 1
                    continue

                row_id = queue_insert(
                    link=link, title=title, source_name=name,
                    category=s["category"], subcategory=s["subcategory"],
                    summary_en=extract_summary(entry),
                    source_published_at=entry_published_iso(entry),
                    raw_tags=_raw_tags(entry) or None,
                )
                if row_id > 0:
                    queued += 1

    save_state()
    print(f"[수집 완료] 큐 적재 {queued}건 | 노이즈제외 {skipped_noise} | 유사중복 {skipped_dup} | 기존기사 {skipped_existing}")


if __name__ == "__main__":
    try:
        collect()
    except Exception as e:
        # 부분 적재는 소스 완료마다 이미 커밋됐으므로 여기서 죽어도 유실은 rss_health
        # 갱신분 정도다. continue-on-error 워크플로에서 다음 실행이 이어받는다.
        print(f"[ERROR] 수집 중단: {e}")
        sys.exit(1)
