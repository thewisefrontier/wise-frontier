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
    arxiv_id_from_url, fetch_arxiv_meta, _iso_age_days, ARXIV_MAX_AGE_DAYS,
)
from geo_detect import detect_region, detect_country, detect_countries, GLOBAL_COUNTRIES
from db import (
    insert_article, queue_claim_batch, queue_delete, queue_mark_failed, existing_urls,
)

MAX_PROCESS_PER_RUN = int(os.getenv("MAX_PROCESS_PER_RUN", "150"))

# 2026-09-22 실사고: 항목 하나당 크롤링→번역(제목)→번역(요약)→텔레그램→DB저장을
# 순차로 처리해(전부 네트워크 I/O 대기) 실측 처리량이 15분에 200건(시간당
# 800건)뿐이었다 — MAX_PROCESS_PER_RUN=1500은 애초에 도달 불가능한 상한이었고,
# 큐가 8,400건→10,698건으로 오히려 늘어나는 걸 보고 발견했다. collector.py와
# 같은 이유(다른 서버로 나가는 I/O 대기 작업)로 병렬화한다. 다만 번역 대상이
# translate.google.com·api.telegram.org로 소수 엔드포인트에 몰려 collector의
# 40 워커처럼 공격적으로 가면 그 서비스만 자체 유발 과호출로 막힐 수 있어
# 보수적으로 6개만 썼다. 기존 sleep(0.1) 단일 스로틀은 동시성 자체가 자연스러운
# 페이싱이 되므로 제거.
#
# ⚠️ 2026-09-23: 6워커+큐 조회 1000행 상한(queue_claim_batch, db.py)이
# 겹쳐서도 여전히 큐가 순증가(시간당 유입 1,000~1,900건 vs 처리 상한
# 1,000건/30분)했다. db.py 쪽 1000행 상한은 페이지네이션으로 풀었고,
# 여기서는 워커를 10으로 올린다 — 소스 확충(430→1,213) 이후 유입이
# 구조적으로 커진 만큼 번역 엔드포인트 쪽 여유도 다시 실측해서 맞춘다.
# 과호출 신호(429/과도한 번역실패)가 보이면 낮출 것.
PROCESS_WORKERS = int(os.getenv("PROCESS_WORKERS", "10"))

# 2026-09-23: 원자재마다 텔레그램에 보내던 걸 사이클당 이만큼만 골라 보낸다.
# 텔레그램 봇은 같은 채널에 분당 20건이 공식 한도라(core.telegram.org/bots/faq)
# 전량 발송 구조에선 처리량이 사이클당 약 400건에 묶였고, 채널에도 3초에 한 건씩
# 쏟아졌다. 저장(기사 재료)은 전량, 발송만 선별(사용자 결정).
TELEGRAM_MAX_PER_RUN = int(os.getenv("TELEGRAM_MAX_PER_RUN", "30"))


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

    # 2026-09-23: 여기서 본문을 크롤링·저장하지 않는다(arXiv 초록만 예외). 하루
    # 2만 건 넘는 원자재 본문이 무료 DB 한도(500MB)를 넘겼고, 건당 최대 8초인
    # 크롤링이 이 처리기의 병목이었다. 기사화 직전에 db.hydrate_full_text()가
    # 실제 재료가 되는 소수 행만 채운다.
    full_text = arxiv_abstract

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

    # sent_telegram=1은 이 코드베이스에서 "기사 재료로 쓸 수 있는 원자재" 표시다
    # (gemini_writer.get_today_articles 등이 eq.1로 거름 — market_news_fetcher도
    # 발송 없이 세운다). 텔레그램 실제 발송은 main()이 소수만 골라 따로 한다.
    article_id = insert_article(
        title_en=title, title_ko=title_ko, summary_en=summary_en, summary_ko=summary_ko,
        url=link, source=name, category=category, subcategory=subcategory,
        region=region, country=country_name, country_flag=country_flag,
        score=0, full_text=full_text, countries=country_names, is_published=False,
        source_published_at=src_published, source_data=tag_source_data, sent_telegram=1,
    )
    if article_id <= 0:
        return True  # 이미 있던 링크 — 다시 발송하지 않는다
    print(f"[SAVE] [{category}>{subcategory}] [{country_name}] {title_ko[:60]}")
    return {"args": (title_ko, summary_ko, link, name, category, subcategory, region, country_name),
            "source": name, "published": src_published or ""}


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
    candidates = []
    with ThreadPoolExecutor(max_workers=PROCESS_WORKERS) as ex:
        futures = {ex.submit(_safe_process_row, row): row for row in batch}
        for fut in as_completed(futures):
            row = futures[fut]
            ok = fut.result()
            if ok:
                queue_delete(row["id"])
                done += 1
                if isinstance(ok, dict):
                    candidates.append(ok)
            else:
                queue_mark_failed(row["id"], "processing failed")
                failed += 1
    print(f"[처리 완료] 성공 {done} | 재시도대상 {failed}")

    picked = pick_for_telegram(candidates, TELEGRAM_MAX_PER_RUN)
    sent = 0
    for c in picked:
        try:
            if send_telegram(*c["args"]).get("ok"):
                sent += 1
        except Exception as e:
            print(f"[FAIL] 텔레그램 발송 실패 — {e}")
    print(f"[텔레그램] 후보 {len(candidates)}건 중 {len(picked)}건 선별 → {sent}건 발송")


def pick_for_telegram(candidates: list, limit: int, per_source: int = 2) -> list:
    """최신 발행순으로, 같은 매체는 per_source건까지만 골라 limit건을 채운다."""
    picked, per = [], {}
    for c in sorted(candidates, key=lambda c: c["published"], reverse=True):
        if len(picked) >= limit:
            break
        if per.get(c["source"], 0) >= per_source:
            continue
        per[c["source"]] = per.get(c["source"], 0) + 1
        picked.append(c)
    return picked


if __name__ == "__main__":
    main()
