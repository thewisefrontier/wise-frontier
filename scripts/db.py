"""
db.py — Supabase REST API 버전
기존 SQLite 인터페이스를 그대로 유지하면서 내부를 Supabase REST API로 교체
IPv6 문제 없이 HTTP로 동작
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """GitHub Actions 러너(UTC)와 무관하게 정확한 KST 현재시각 반환"""
    return datetime.now(timezone.utc).astimezone(KST)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

def _headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def _url(table="articles"):
    return f"{SUPABASE_URL}/rest/v1/{table}"


def init_db():
    """하위 호환용 — Supabase 전환 완료로 실제 동작 없음"""
    pass



def is_url_exists(url: str) -> bool:
    res = requests.get(
        _url(),
        headers=_headers(),
        params={"select": "id", "url": f"eq.{url}", "limit": "1"},
        timeout=10
    )
    if res.status_code in (200, 206):
        return len(res.json()) > 0
    return False


def existing_urls(urls: list, chunk: int = 50) -> set:
    """urls 중 articles에 이미 있는 것만 집합으로 반환 — 항목마다 is_url_exists()를
    따로 부르면(왕복 1회씩) 소스당 최대 5개, 1200+소스에서 실측 초당 1건 수준으로
    병목이 됐다(2026-09-22, rss_collector.py 최초 실전 시험: 13분에 689건). 대신 배치
    전체를 IN 쿼리로 묶되, GET 쿼리스트링 길이 상한(게이트웨이 통상 8~16KB)을 피하려
    chunk개씩 나눠 보낸다."""
    found = set()
    for i in range(0, len(urls), chunk):
        part = urls[i:i + chunk]
        quoted = ",".join(f'"{u}"' for u in part)
        res = requests.get(
            _url(), headers=_headers(),
            params={"select": "url", "url": f"in.({quoted})"}, timeout=10,
        )
        if res.status_code in (200, 206):
            found.update(r["url"] for r in res.json())
    return found


def insert_article(
    title_en, title_ko, summary_en, summary_ko,
    url, source, category, subcategory, region, country, country_flag, score,
    full_text="", countries=None, is_published=False, source_published_at=None,
    source_data=None, image_url=None, image_credit=None,
) -> int:
    payload = {
        "title_en": title_en or "",
        "title_ko": title_ko,
        "summary_en": summary_en,
        "summary_ko": summary_ko,
        "url": url,
        "source": source,
        "category": category,
        "subcategory": subcategory,
        "region": region,
        "country": country,
        "country_flag": country_flag,
        "score": score,
        "full_text": full_text or None,
        "countries": ([country] + [c for c in (countries or []) if c and c != country]) if country else (countries or None),
        "is_published": is_published,
        "created_at": now_kst().strftime("%Y-%m-%d %H:%M"),
        "sent_telegram": 0,
        "posted_blog": 0,
    }
    # 원문(RSS) 발행일 — 값이 있을 때만 실어 기존 동작에 영향 없게 한다
    if source_published_at:
        payload["source_published_at"] = source_published_at
    # RSS 원문 태그(<category> 등) — 트렌드 중복판정에서 country 대신/보조로
    # 쓸 수 있게 원문 그대로 보존한다(2026-09-08, gemini_summarizer.py 참고).
    if source_data:
        payload["source_data"] = source_data
    # 대표 이미지 — 수집 단계에서 이미 이미지를 구한 수집기(domestic_kr_fetcher.py
    # 등)만 채운다. 값이 없을 때만 실어 기존 동작(발행 단계에서 gemini_writer.py가
    # 채우는 흐름)에 영향 없게 한다.
    if image_url:
        payload["image_url"] = image_url
    if image_credit:
        payload["image_credit"] = image_credit
    headers = {**_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_url(), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        if data:
            return data[0].get("id", -1)
    elif res.status_code == 409:
        return -1  # 중복
    return -1


def mark_sent_telegram(article_id: int):
    res = requests.patch(
        f"{_url()}?id=eq.{article_id}",
        headers=_headers(),
        json={"sent_telegram": 1},
        timeout=10
    )
    return res.status_code in (200, 204)


def mark_posted_blog(article_id: int):
    res = requests.patch(
        f"{_url()}?id=eq.{article_id}",
        headers=_headers(),
        json={"posted_blog": 1},
        timeout=10
    )
    return res.status_code in (200, 204)


def get_top_articles(date: str = None, limit: int = 10, region: str = None) -> list:
    if date is None:
        date = now_kst().strftime("%Y-%m-%d")
    params = {
        "select": "*",
        "sent_telegram": "eq.1",
        "created_at": f"like.{date}%",
        "order": "score.desc",
        "limit": str(limit),
    }
    if region:
        params["region"] = f"eq.{region}"
    res = requests.get(_url(), headers=_headers(), params=params, timeout=15)
    if res.status_code in (200, 206):
        return res.json()
    return []


def get_articles_by_country(country: str, limit: int = 5) -> list:
    res = requests.get(
        _url(),
        headers=_headers(),
        params={
            "select": "*",
            "country": f"eq.{country}",
            "sent_telegram": "eq.1",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


def get_articles_by_region(region: str, limit: int = 10) -> list:
    res = requests.get(
        _url(),
        headers=_headers(),
        params={
            "select": "*",
            "region": f"eq.{region}",
            "sent_telegram": "eq.1",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


def get_unposted_articles(limit: int = 10) -> list:
    res = requests.get(
        _url(),
        headers=_headers(),
        params={
            "select": "*",
            "sent_telegram": "eq.1",
            "posted_blog": "eq.0",
            "order": "score.desc",
            "limit": str(limit),
        },
        timeout=15
    )
    if res.status_code in (200, 206):
        return res.json()
    return []


# ── RSS 소스 자동 관리 (2026-09-22) ──────────────────────────────────
# 소스 건강 상태(연속 실패·마지막 정상 응답 시각)를 rss_sources 테이블 자체에 남겨
# 어드민에서 보고 SQL로 조회할 수 있게 한다(이전엔 로컬 data/state.json에만 있어서
# 안 보였다). rss_collector.py가 실행 하나당 한 번의 배치 UPSERT로만 갱신한다
# (개별 소스마다 UPDATE를 부르면 queue_insert 초기 버전과 같은 병목이 재발한다).

def load_rss_with_health() -> list:
    """활성 소스 + 건강 컬럼(연속실패 등)을 페이지네이션으로 전량 로드."""
    sources, offset, page = [], 0, 1000
    while True:
        res = requests.get(
            _url("rss_sources"), headers=_headers(),
            params={"select": "id,name,category,subcategory,url,consecutive_fails",
                    "is_active": "eq.true", "order": "id.asc",
                    "limit": str(page), "offset": str(offset)},
            timeout=15,
        )
        res.raise_for_status()
        batch = res.json()
        sources.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return sources


def _bulk_upsert_sources(rows: list, headers: dict) -> int:
    """rows는 이미 전부 같은 키 집합이어야 한다(PGRST102, queue_insert_bulk와 동일 함정).
    호출부가 outcome 종류별로 나눠 불러 이 조건을 보장한다."""
    updated = 0
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        if not chunk:
            continue
        res = requests.post(f'{_url("rss_sources")}?on_conflict=id', headers=headers, json=chunk, timeout=30)
        if res.status_code in (200, 201, 204):
            updated += len(chunk)
        else:
            print(f"  [WARN] update_source_health 실패 status={res.status_code} 건수={len(chunk)} body={res.text[:200]}")
    return updated


def update_source_health(outcomes: dict, fail_threshold: int) -> dict:
    """outcomes: {source_id: 'ok'|'fail'|'too_old'}. 배치 UPSERT로 반영하고
    연속 실패가 fail_threshold를 넘긴 소스는 is_active=false로 자동 비활성화한다.
    반환값: {"updated": n, "deactivated": [id, ...]}.

    ⚠️ outcome 종류별로 UPSERT를 따로 보낸다(같은 요청 안 행끼리 키 집합이 달라지면
    PostgREST가 통째로 거부한다 — PGRST102, queue_insert_bulk와 같은 함정). "값이
    없으면 None으로 채워 키를 맞춘다" 대신 이 방식을 쓴 이유: last_ok_at처럼
    outcome이 아닐 때 "건드리지 않아야"(None으로 덮어쓰면 안 됨) 하는 컬럼이 섞여
    있어, 같은 요청에 넣으려면 어차피 현재값을 다시 읽어와 채워야 해 더 복잡해진다."""
    if not outcomes:
        return {"updated": 0, "deactivated": []}
    current = load_rss_with_health()
    by_id = {s["id"]: s for s in current}
    now_iso = datetime.now(timezone.utc).isoformat()
    headers = {**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}

    def base(sid):
        # ⚠️ PostgREST의 POST+on_conflict UPSERT는 내부적으로 "INSERT 시도 후 충돌
        # 시 UPDATE"라, id만 보내면 name/category/subcategory/url(NOT NULL)이 비어
        # INSERT 단계에서 거절된다(23502, 2026-09-22 실전 시험 — 1213건 전부 실패).
        # 실제로는 항상 기존 행이라 값이 바뀔 일 없지만, 방금 읽어온 현재값을 그대로
        # 함께 실어 INSERT 쪽 제약을 만족시킨다(진짜 UPDATE는 결과적으로 동일).
        s = by_id.get(sid, {})
        return {"name": s.get("name", ""), "category": s.get("category", ""),
                "subcategory": s.get("subcategory", ""), "url": s.get("url", "")}

    # total_ok/total_fail(누적치)은 "현재값+1"이 필요해 REST 배치 UPSERT로 한 번에
    # 못 한다(PostgREST는 증분식 UPSERT 미지원) — 소스당 개별 PATCH를 부르면
    # queue_insert 초기 버전과 같은 병목이 재발하므로 아예 갱신 안 함. 지금
    # 판단에 필요한 건 누적치가 아니라 "연속 실패 중인지"뿐이라 없어도 무해하다.
    ok_rows = [{"id": sid, **base(sid), "last_checked_at": now_iso, "consecutive_fails": 0, "last_ok_at": now_iso}
               for sid, o in outcomes.items() if o == "ok"]
    too_old_rows = [{"id": sid, **base(sid), "last_checked_at": now_iso, "consecutive_fails": 0}
                     for sid, o in outcomes.items() if o == "too_old"]

    deactivated = []
    fail_rows, deact_rows = [], []
    for sid, o in outcomes.items():
        if o != "fail":
            continue
        new_fails = (by_id.get(sid, {}).get("consecutive_fails") or 0) + 1
        if new_fails >= fail_threshold:
            deact_rows.append({"id": sid, **base(sid), "last_checked_at": now_iso, "consecutive_fails": new_fails,
                                "is_active": False, "deactivated_reason": f"{new_fails}회 연속 실패(자동)"})
            deactivated.append(sid)
        else:
            fail_rows.append({"id": sid, **base(sid), "last_checked_at": now_iso, "consecutive_fails": new_fails})

    updated = sum(_bulk_upsert_sources(rows, headers) for rows in (ok_rows, too_old_rows, fail_rows, deact_rows))
    return {"updated": updated, "deactivated": deactivated}


# ── RSS 수집/처리 분리 큐 (2026-09-22) ──────────────────────────────
# rss_collector.py가 채우고 rss_processor.py가 비운다. articles 테이블과 완전히
# 별개라 다른 writer의 소비 로직에 영향이 없다(newsfinal_db_backup_architecture와
# 같은 원칙: 새 테이블은 GRANT도 RLS와 별도로 확인 — 이미 반영됨, apply_migration
# rss_raw_queue_grants 참고).

def image_used_recently(fragment: str, hours: int = 24) -> bool:
    """image_url에 fragment(보통 Pixabay 사진 고유 id)가 포함된 기사가 최근
    hours 시간 내에 있으면 True.

    실사고(2026-09-22): 러시아 총선 기사와 베를린 지방선거 기사가 서로 다른
    검색어("election ballot voting booth" vs "ballot political protest city
    hall")로 Pixabay를 검색했는데도 둘 다 같은 스톡사진(같은 photo id)이
    1등으로 나와, 무관한 두 기사에 같은 사진이 2시간 간격으로 실렸다
    (사용자 지적). article_image.py의 일반 Pixabay 검색 경로(entity 없이
    제목/본문 키워드로 찾는 경우)에서만 쓴다 — 위키미디어 entity 경로(백악관
    기사에 백악관 사진처럼 "같은 대상은 같은 사진이 맞는" 경우)는 의도된
    재사용이라 이 체크 대상이 아니다."""
    since = (now_kst() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    res = requests.get(
        _url("articles"), headers=_headers(),
        params={"select": "id", "image_url": f"ilike.*{fragment}*",
                "created_at": f"gte.{since}", "limit": "1"},
        timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def queue_link_exists(link: str) -> bool:
    res = requests.get(
        _url("rss_raw_queue"), headers=_headers(),
        params={"select": "id", "link": f"eq.{link}", "limit": "1"}, timeout=10,
    )
    return res.status_code in (200, 206) and len(res.json()) > 0


def queue_insert(link, title, source_name, category, subcategory,
                  summary_en="", source_published_at=None, raw_tags=None) -> int:
    payload = {
        "link": link, "title": title, "source_name": source_name,
        "category": category, "subcategory": subcategory, "summary_en": summary_en or "",
    }
    if source_published_at:
        payload["source_published_at"] = source_published_at
    if raw_tags:
        payload["raw_tags"] = raw_tags
    headers = {**_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
    res = requests.post(_url("rss_raw_queue"), headers=headers, json=payload, timeout=15)
    if res.status_code in (200, 201):
        data = res.json()
        return data[0]["id"] if data else -1
    return -1


def _pg_text(s):
    """Postgres text 컬럼이 거부하는 임베디드 NUL(\\x00)·기타 C0 제어문자를 제거.
    2026-09-22 실전 시험에서 발견: 배치(80건) 안에 한 건이라도 이게 섞이면 그 배치
    전체가 INSERT 자체에서 실패한다(단일 문장이라 부분성공이 안 됨) — buffered
    4631건 중 실제 삽입 160건에 그쳤던 사고의 원인. 일부 RSS(특히 인코딩이 불안정한
    소규모 현지 매체)가 제목·요약에 NUL을 흘려보낸다."""
    if not s:
        return s
    return "".join(c for c in s if c == "\n" or c == "\t" or ord(c) >= 0x20)


def queue_insert_bulk(rows: list) -> int:
    """rss_raw_queue에 여러 행을 한 번의 POST로 적재. 2026-09-22 실전 시험에서 건마다
    개별 POST(queue_insert)를 부르면 소스 1200+개 × 최대 5건에서 순차 왕복이 쌓여
    15분+에도 안 끝났다(존재-확인 병목을 없앤 뒤에도 남아있던 두 번째 병목). rows는
    각각 queue_insert()와 같은 키(link/title/source_name/category/subcategory/
    summary_en/source_published_at/raw_tags)의 dict. 반환값은 실제 삽입된 건수
    (link 충돌로 조용히 스킵된 건 제외).

    ⚠️ 배치 전체가 한 SQL 문이라 이 함수 자체의 정제만으로는 못 막는 오류(그 외
    제약 위반 등)가 또 나올 수 있다 — 실패 시 배치를 반으로 쪼개 재시도하고,
    그래도 안 되면(단일 행까지 쪼개도 실패) 그 행만 버리고 계속 진행해
    한 건이 전체를 막지 않게 한다."""
    if not rows:
        return 0

    def build(rs):
        # ⚠️ PostgREST 배치 INSERT는 배열 안 모든 객체가 "완전히 같은 키 집합"이어야
        # 한다(PGRST102 "All object keys must match") — 값이 없다고 키 자체를 생략하면
        # 배치마다 키 구성이 달라져 요청 전체가 거부된다(2026-09-22 실전 시험: 이분
        # 재시도가 거의 매번 1건까지 쪼개져 15분+ 걸린 진짜 원인. NUL 문자는 원인이
        # 아니었음). 값이 없으면 None으로 채워 키는 항상 동일하게 유지한다.
        return [{
            "link": r["link"], "title": _pg_text(r["title"]), "source_name": r["source_name"],
            "category": r.get("category"), "subcategory": r.get("subcategory"),
            "summary_en": _pg_text(r.get("summary_en")) or "",
            "source_published_at": r.get("source_published_at"),
            "raw_tags": r.get("raw_tags"),
        } for r in rs]

    def post(rs):
        headers = {**_headers(), "Prefer": "resolution=ignore-duplicates,return=representation"}
        try:
            res = requests.post(_url("rss_raw_queue"), headers=headers, json=build(rs), timeout=30)
        except requests.RequestException as e:
            print(f"  [WARN] queue_insert_bulk 네트워크 오류({len(rs)}건): {e}")
            return None
        if res.status_code in (200, 201):
            return len(res.json())
        print(f"  [WARN] queue_insert_bulk 실패 status={res.status_code} 건수={len(rs)} body={res.text[:300]}")
        return None

    n = post(rows)
    if n is not None:
        return n
    if len(rows) == 1:
        print(f"  [DROP] 단건도 실패해 포기: {rows[0].get('link')}")
        return 0
    # 절반으로 쪼개 재시도 — 배치 안의 문제 행 하나가 전체를 막지 않게 이분 탐색
    mid = len(rows) // 2
    return queue_insert_bulk(rows[:mid]) + queue_insert_bulk(rows[mid:])


def queue_claim_batch(limit: int = 150) -> list:
    """처리 대기 중(processed=false)인 항목을 오래된 순으로 가져온다.

    ⚠️ 여러 처리기 실행이 동시에 돌면 같은 행을 중복으로 집어갈 수 있다 — 하지만
    run.yml이 concurrency 그룹(cancel-in-progress: false)으로 겹침 자체를 막고 있어
    (newsfinal-auto-run) 지금 구조에서는 발생하지 않는다. 나중에 별도 워크플로에서
    이 큐를 처리하게 되면 SELECT ... FOR UPDATE SKIP LOCKED 같은 잠금이 필요해진다."""
    res = requests.get(
        _url("rss_raw_queue"), headers=_headers(),
        params={"select": "*", "processed": "eq.false", "order": "fetched_at.asc", "limit": str(limit)},
        timeout=20,
    )
    return res.json() if res.status_code in (200, 206) else []


def queue_delete(row_id: int):
    requests.delete(f'{_url("rss_raw_queue")}?id=eq.{row_id}', headers=_headers(), timeout=10)


def queue_mark_failed(row_id: int, error: str, max_attempts: int = 3):
    """실패 시 attempts를 늘리고, 상한을 넘으면 영구 실패로 보고 큐에서 지운다
    (깨진 링크 하나가 매 실행 재시도되며 배치를 계속 잡아먹는 걸 방지)."""
    res = requests.get(_url("rss_raw_queue"), headers=_headers(),
                        params={"select": "attempts", "id": f"eq.{row_id}"}, timeout=10)
    attempts = (res.json()[0]["attempts"] + 1) if res.status_code in (200, 206) and res.json() else 1
    if attempts >= max_attempts:
        queue_delete(row_id)
        return
    requests.patch(f'{_url("rss_raw_queue")}?id=eq.{row_id}', headers=_headers(),
                    json={"attempts": attempts, "last_error": str(error)[:500]}, timeout=10)


if __name__ == "__main__":
    init_db()
