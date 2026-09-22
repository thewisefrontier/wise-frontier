"""
scripts/rss_processor.py
-------------------------
rss_raw_queue를 배치로 비우며 번역·본문크롤링·텔레그램 발송·최종 저장까지 한다
("만드는" 단계 — rss_collector.py가 채워둔 것을 소비). 한 번 실행에 MAX_PROCESS_PER_RUN
건까지만 처리해 실행 시간을 예측 가능하게 묶는다. 실패한 항목은 재시도 횟수를 세다가
상한을 넘으면 큐에서 지운다(깨진 링크 하나가 매 실행 배치를 계속 잡아먹는 것 방지).

2026-09-22 신설 — 설계 배경은 rss_collector.py 상단 docstring 참고.

실행: python scripts/rss_processor.py
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from deep_translator import GoogleTranslator

from rss_fetcher import (
    clean_text, _is_bad_translation, is_soft_noise, send_telegram, detect_lang,
    crawl_full_text, arxiv_id_from_url, fetch_arxiv_meta, _iso_age_days, ARXIV_MAX_AGE_DAYS,
)
from geo_detect import detect_region, detect_country, detect_countries, GLOBAL_COUNTRIES
from db import (
    insert_article, mark_sent_telegram, queue_claim_batch, queue_delete, queue_mark_failed, existing_urls,
)

MAX_PROCESS_PER_RUN = int(os.getenv("MAX_PROCESS_PER_RUN", "150"))

# 2026-09-22 실사고: 항목 하나당 크롤링→번역(제목)→번역(요약)→텔레그램→DB저장을
# 순차로 처리해(전부 네트워크 I/O 대기) 실측 처리량이 15분에 200건(시간당
# 800건)뿐이었다 — MAX_PROCESS_PER_RUN=1500은 애초에 도달 불가능한 상한이었고,
# 큐가 8,400건→10,698건으로 오히려 늘어나는 걸 보고 발견했다. collector.py와
# 같은 이유(다른 서버로 나가는 I/O 대기 작업)로 병렬화한다. 다만 번역 대상이
# translate.google.com·api.telegram.org로 소수 엔드포인트에 몰려 collector의
# 40 워커처럼 공격적으로 가면 그 서비스만 자체 유발 과호출로 막힐 수 있어
# 보수적으로 6개만 쓴다. 기존 sleep(0.1) 단일 스로틀은 동시성 자체가 자연스러운
# 페이싱이 되므로 제거.
PROCESS_WORKERS = int(os.getenv("PROCESS_WORKERS", "6"))


def process_row(row: dict) -> bool:
    """성공적으로 처리(발행 준비 완료 또는 정당하게 스킵)했으면 True — 큐에서 지운다.
    일시적 오류(네트워크 등)면 False — attempts를 늘리고 다음 배치에서 재시도."""
    title = row["title"]
    link = row["link"]
    name = row["source_name"]
    category = row["category"]
    subcategory = row["subcategory"]
    summary_en = row.get("summary_en") or ""
    src_published = row.get("source_published_at")
    raw_tags = row.get("raw_tags") or []

    # arXiv는 페이지 크롤링 대신 공식 API로 초록·제출일을 받는다(rss_fetcher.py와 동일 로직)
    arxiv_abstract = ""
    axid = arxiv_id_from_url(link)
    if axid:
        abstract, pub = fetch_arxiv_meta(axid)
        if pub:
            src_published = pub
            age = _iso_age_days(pub)
            if age is not None and age > ARXIV_MAX_AGE_DAYS:
                print(f"[SKIP] arXiv 제출 {age:.0f}일 경과 — {title[:50]}")
                return True
        if not abstract:
            print(f"[SKIP] arXiv 초록 확보 실패 — {title[:50]}")
            return True
        arxiv_abstract = abstract

    soft_noise = is_soft_noise(title)
    src_lang = detect_lang(title + " " + summary_en)

    full_text = arxiv_abstract or crawl_full_text(link, timeout=8)
    if full_text:
        print(f"  [크롤링] {len(full_text)}자 추출")

    region = detect_region(name)
    content_flag, content_country = detect_country(title + " " + summary_en, source=name)
    all_countries = detect_countries(title + " " + summary_en, source=name)
    country_names = [n for _, n in all_countries]

    if country_names:
        frontier_countries = [n for n in country_names if n not in GLOBAL_COUNTRIES]
        if frontier_countries:
            country_name = frontier_countries[0]
            country_flag = next((f for f, n in all_countries if n == country_name), "")
        else:
            country_name = country_names[0]
            country_flag = next((f for f, n in all_countries if n == country_name), "")
            category = "글로벌"
            region = "global"
    else:
        country_name, country_flag = "", ""
        category = "글로벌"
        region = "global"
        country_names = []

    try:
        title_ko = clean_text(GoogleTranslator(source="auto", target="ko").translate(title[:500]))
        if _is_bad_translation(title_ko):
            print(f"[번역실패] 원문 유지 — {title[:50]}")
            title_ko = title
    except Exception:
        title_ko = title

    summary_ko = ""
    if summary_en:
        try:
            summary_ko = clean_text(GoogleTranslator(source="auto", target="ko").translate(summary_en[:4500]))
            if _is_bad_translation(summary_ko):
                summary_ko = ""
        except Exception:
            summary_ko = ""

    if src_lang != "en" and full_text:
        lang_labels = {"fr": "[원문: 프랑스어]", "ar": "[원문: 아랍어]",
                       "pt": "[원문: 포르투갈어]", "id": "[원문: 인도네시아어]"}
        full_text = lang_labels.get(src_lang, "[원문: " + src_lang + "]") + "\n" + full_text

    tag_source_data = {"tags": raw_tags} if raw_tags else None

    if soft_noise:
        insert_article(
            title_en=title, title_ko=title_ko, summary_en=summary_en, summary_ko=summary_ko,
            url=link, source=name, category=category, subcategory=subcategory,
            region=region, country=country_name, country_flag=country_flag,
            score=0, full_text=full_text, countries=country_names, is_published=False,
            source_published_at=src_published, source_data=tag_source_data,
        )
        print(f"[SOFT] [{category}] [{country_name}] {title_ko[:50]}")
        return True

    res = send_telegram(title_ko, summary_ko, link, name, category, subcategory, region, country_name)
    if not res.get("ok"):
        print(f"[FAIL] 텔레그램 발송 실패 — {res}")
        return False  # 재시도 대상(일시적 오류일 수 있음)

    article_id = insert_article(
        title_en=title, title_ko=title_ko, summary_en=summary_en, summary_ko=summary_ko,
        url=link, source=name, category=category, subcategory=subcategory,
        region=region, country=country_name, country_flag=country_flag,
        score=0, full_text=full_text, countries=country_names, is_published=False,
        source_published_at=src_published, source_data=tag_source_data,
    )
    if article_id > 0:
        mark_sent_telegram(article_id)
    print(f"[SENT] [{category}>{subcategory}] [{country_name}] {title_ko}")
    return True


def main():
    batch = queue_claim_batch(MAX_PROCESS_PER_RUN)
    print(f"[처리] 대기 {len(batch)}건 (최대 {MAX_PROCESS_PER_RUN})")

    # "이미 articles에 있는 링크인지"는 배치 전체를 한 번의 IN 쿼리로 확인한다(수집기가
    # 이 확인을 안 하고 그냥 쌓기만 하므로 — rss_collector.py 참고). 이미 있으면 번역·
    # 크롤링·텔레그램 없이 큐에서만 지운다(그 링크는 이전 주기에 이미 정상 발행됐다는 뜻).
    already = existing_urls([r["link"] for r in batch])
    dup = [r for r in batch if r["link"] in already]
    batch = [r for r in batch if r["link"] not in already]
    for row in dup:
        queue_delete(row["id"])
    if dup:
        print(f"[스킵] 이미 발행된 링크 {len(dup)}건 — 큐에서만 제거")

    def _safe_process_row(row):
        try:
            return process_row(row)
        except Exception as e:
            print(f"[ERROR] id={row['id']} {row['title'][:40]}: {e}")
            return False

    done = failed = 0
    with ThreadPoolExecutor(max_workers=PROCESS_WORKERS) as ex:
        futures = {ex.submit(_safe_process_row, row): row for row in batch}
        for fut in as_completed(futures):
            row = futures[fut]
            ok = fut.result()
            if ok:
                queue_delete(row["id"])
                done += 1
            else:
                queue_mark_failed(row["id"], "processing failed")
                failed += 1
    print(f"[처리 완료] 성공 {done} | 재시도대상 {failed}")


if __name__ == "__main__":
    main()
