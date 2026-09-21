"""
scripts/weekly_recap_writer.py
------------------------------
주간 정리 기사 초안 — 지난 한 주 동안 발행한 기사를 주제별로 묶어 "이번 주 무슨 일이 있었나"를 쓴다.
어드민 검토 후 발행(자동 발행 안 함).

2026-09-22 신설(사용자 요청: 콘텐츠 다양화 — "주간 정리 기사", 관련 기사 링크가 많아져 내부 연결에도 도움).

설계:
  1. 자료: 지난 월~일에 발행된 NewsFinal 기사 중 이어지는 사안(trend_*) 기사를 주제별로 묶고,
     기사 수가 많은 주제 최대 TOP_TOPICS개를 고른다(주제당 기사 제목 날짜순 + 최신 기사 본문 발췌).
  2. 작성: Gemini(고품질 모델). 프롬프트는 DB prompts.weekly_recap_rules(노하우 — 저장소에 두지 않음).
  3. 검증: 수치 대조(unsupported_numbers)·합쇼체·근거 없는 서술(unsupported_claims)은 explainer_writer의 것을 재사용.
     제목·기사 링크 목록은 코드가 정한다(모델이 지어낼 수 없게).
  4. 저장: is_published=False, subcategory='주간정리', source_data.article_ids에 본문이 다룬 기사 id →
     기사 페이지 하단 "이번 주 다룬 기사" 링크(DB RPC related_articles가 이 목록을 그대로 돌려줌).

실행: python scripts/weekly_recap_writer.py   (run.yml이 매시 호출 — 일요일 20시 KST 이후 주 1회만 작성)
      python scripts/weekly_recap_writer.py --dry-run   (저장 없이 출력)
"""

import os
import re
import sys
import requests
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date
from dotenv import load_dotenv

load_dotenv()

from gemini_writer import load_prompt, _gemini_client
from article_store import insert_final_article, sb_headers, sb_url
from style_guard import ensure_paragraphs, has_polite_ending
from explainer_writer import unsupported_numbers, unsupported_claims, _body_only

KST = timezone(timedelta(hours=9))
SUBCATEGORY = "주간정리"
TOP_TOPICS = 5
MIN_TOPICS = 2              # 주제가 이보다 적으면 정리할 거리가 부족
MIN_ARTICLES_PER_TOPIC = 2  # 한 주에 기사 2건 이상인 주제만(이어지는 사안)
EXCERPT_CHARS = 600
MAX_RETRY = 2
LINK_LIMIT = 24             # 기사 하단 링크 목록 상한(RPC 상한 30 이내)


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def week_range(now: datetime):
    """방금 끝난 월~일 주. 일요일은 20시 이후에만(그 주 마지막 기사까지 모인 뒤) 이번 주, 아니면 직전 주."""
    today = now.date()
    if today.weekday() == 6 and now.hour >= 20:
        sunday = today
    else:
        sunday = today - timedelta(days=(today.weekday() + 1) % 7 or 7)
    return sunday - timedelta(days=6), sunday


def _get(params):
    res = requests.get(sb_url(), headers=sb_headers(), params=params, timeout=40)
    return res.json() if res.status_code in (200, 206) else []


def fetch_week_articles(mon: date, sun: date) -> list:
    rows = _get({
        "select": "id,title_ko,summary_ko,subcategory,category,created_at,image_url",
        "source": "eq.NewsFinal", "is_published": "eq.true",
        "and": f"(created_at.gte.{mon.isoformat()} 00:00,created_at.lte.{sun.isoformat()} 23:59)",
        "subcategory": "like.trend_*", "order": "created_at.asc", "limit": "400",
    })
    return rows


def group_topics(rows: list) -> list:
    """[(주제명, [기사...])] 기사 수 많은 순."""
    groups = defaultdict(list)
    for a in rows:
        groups[(a.get("subcategory") or "")[len("trend_"):].strip()].append(a)
    topics = [(k, v) for k, v in groups.items() if k and len(v) >= MIN_ARTICLES_PER_TOPIC]
    topics.sort(key=lambda kv: (len(kv[1]), sum(len(a.get("summary_ko") or "") for a in kv[1])), reverse=True)
    return topics[:TOP_TOPICS]


def facts_sheet(topics: list) -> str:
    out = []
    for name, arts in topics:
        lines = [f"■ 주제: {name} (이번 주 기사 {len(arts)}건)"]
        for a in arts:
            d = (a["created_at"] or "")[:10]
            m = re.match(r"\d{4}-(\d{2})-(\d{2})", d)
            lines.append(f"- {int(m.group(1))}월 {int(m.group(2))}일 발행: {a['title_ko']}" if m else f"- {a['title_ko']}")
        last = arts[-1]
        lines.append("  (최신 기사 본문 발췌) " + re.sub(r"\s+", " ", _body_only(last.get("summary_ko")))[:EXCERPT_CHARS])
        out.append("\n".join(lines))
    return "\n\n".join(out)


def make_title(mon: date, sun: date, topics: list) -> str:
    rng = (f"{mon.month}월 {mon.day}~{sun.day}일" if mon.month == sun.month else f"{mon.month}/{mon.day}~{sun.month}/{sun.day}")
    names = ", ".join(n for n, _ in topics[:3])
    return f"[주간 정리] {rng} 이번 주 무슨 일이 있었나 — {names}"


def _generate(prompt: str):
    return _gemini_client.call(prompt, max_tokens=8000, start_tier=0, temperature=0.4, timeout=(10, 120))


def write_recap(week_label: str, facts: str):
    """본문(str) 또는 None. 재시도 시 지적 사항을 프롬프트에 덧붙인다."""
    rules = load_prompt("weekly_recap_rules")
    if not rules or "{facts}" not in rules:
        # 프롬프트는 DB에만 있다(저장소 비공개 방침) — 못 불러오면 빈 프롬프트로 모델이 엉뚱한 글을 쓰므로 작성하지 않는다
        print("  ⛔ weekly_recap_rules 프롬프트를 불러오지 못함(DB/키 확인) — 작성 안 함")
        return None
    prompt = rules.format(week_label=week_label, facts=facts)
    for attempt in range(MAX_RETRY + 1):
        content = _generate(prompt)
        m = re.search(r"BODY:\s*(.+)$", content or "", re.S)
        body = ensure_paragraphs(m.group(1).strip()) if m else ""
        if not body:
            print("  ❌ 파싱 실패/응답 없음")
            continue
        if has_polite_ending(body):
            prompt += "\n\n[재작성 지시] 모든 문장을 '-다'로 끝내세요. '-습니다/-입니다'는 쓰지 마세요."
            print(f"  ⚠️ 합쇼체 감지 → 재작성({attempt + 1}/{MAX_RETRY})")
            continue
        bad = unsupported_numbers(body, facts)
        if bad:
            prompt += f"\n\n[재작성 지시] 방금 결과에 자료에 없는 수치({', '.join(bad)})가 있었습니다. 자료에 나온 수치만 사용하세요."
            print(f"  ⚠️ 자료에 없는 수치 {bad} → 재작성({attempt + 1}/{MAX_RETRY})")
            continue
        claims = unsupported_claims(body, facts)
        if claims:
            prompt += f"\n\n[재작성 지시] 방금 결과에 자료에 근거 없는 서술이 있었습니다: {claims}\n해당 내용을 빼고 다시 작성하세요."
            print(f"  ⚠️ 근거 없는 서술 → 재작성({attempt + 1}/{MAX_RETRY}): {claims[:100]}")
            continue
        return body
    return None


def main():
    dry = "--dry-run" in sys.argv
    now = now_kst()
    mon, sun = week_range(now)
    url = f"internal://weekly_recap_{sun.isoformat()}"
    if not dry:
        if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
            print("[SKIP] SUPABASE 환경변수 없음")
            return
        if now.weekday() != 6 and now.weekday() != 0 and (now.date() - sun).days > 3:
            return
        if _get({"select": "id", "url": f"eq.{url}"}):
            print(f"[weekly_recap_writer] {sun} 주 정리 이미 존재 — 스킵")
            return
    elif now.weekday() == 6 and now.hour < 20:
        print("(dry-run) 일요일 20시 이전이라 직전 주 기준")

    rows = fetch_week_articles(mon, sun)
    topics = group_topics(rows)
    print(f"[weekly_recap_writer] {mon}~{sun} 트렌드 기사 {len(rows)}건 → 주제 {len(topics)}개: {[(n, len(v)) for n, v in topics]}")
    if len(topics) < MIN_TOPICS:
        print("  → 주제가 부족해 작성 보류")
        return

    facts = facts_sheet(topics)
    week_label = f"{mon.month}월 {mon.day}일(월)~{sun.month}월 {sun.day}일(일)"
    body = write_recap(week_label, facts)
    if not body:
        print("  → 검증을 통과한 본문을 못 만들어 보류")
        return
    title = make_title(mon, sun, topics)
    article_ids = [a["id"] for _, arts in topics for a in arts][::-1][:LINK_LIMIT]  # 최신 기사부터
    if dry:
        print(title + "\n\n" + body + f"\n\n(링크 대상 기사 id {len(article_ids)}개)")
        return
    image = next((a["image_url"] for _, arts in topics[:1] for a in reversed(arts) if a.get("image_url")), "")
    now_str = now.strftime("%Y-%m-%d %H:%M")
    art_id = insert_final_article({
        "title_en": title, "title_ko": title, "summary_en": "", "summary_ko": body, "url": url,
        "source": "NewsFinal", "category": "종합", "subcategory": SUBCATEGORY,
        "region": "global", "country": "", "country_flag": "", "image_url": image, "countries": [],
        "score": 1, "created_at": now_str, "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": f"주간 정리 자동 초안(검토 대기, {mon}~{sun})"}],
        "source_data": {"week_start": mon.isoformat(), "week_end": sun.isoformat(),
                        "topics": [n for n, _ in topics], "article_ids": article_ids, "background": facts},  # background = 어드민 검토 화면에 배경 정보로 표시
        "sent_telegram": 0, "is_published": False,
    })
    print(f"  ✅ 초안 저장 id={art_id}" if art_id and art_id > 0 else "  ❌ 저장 실패")


if __name__ == "__main__":
    main()
