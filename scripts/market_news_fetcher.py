"""
scripts/market_news_fetcher.py
---------------------------------
머니파이널(D:\\thewise\\moneyfinal) 프로젝트가 Finnhub로 수집해 자체
Supabase `market_news` 테이블에 모아둔 미국 시장 뉴스를, 뉴스파이널이
API 키 재발급/자체 Finnhub 연동 없이 그대로 받아와 기존 RSS 인입
파이프라인(is_published=False로 저장 → 이후 gemini_writer.py 등이
합성)에 새 소스로 태우는 어댑터(2026-09-03 신설).

⚠️ rss_fetcher.py를 그대로 import하지 않는다 — 그 파일은 모듈 최상단
(가드 없이)에서 RSS 수집을 즉시 실행하므로 import만 해도 전체 파이프라인이
돌아버린다. 대신 db.py(부작용 없음)만 가져다 쓰고, clean_text/is_duplicate
같은 몇 줄짜리 유틸은 이 파일에 그대로 복제한다(공용화하기엔 너무 작고,
rss_fetcher.py 쪽을 건드리는 게 오히려 리스크가 큼).

접근: GET https://moneyfinal.pages.dev/api/market-news (X-Api-Key 헤더 필요,
env MONEYFINAL_FEED_KEY). 401="키 확인", 500="머니파이널 쪽 설정 문제" —
2026-09-03 실측: 초기엔 401(키 미등록) → 재배포 후에도 500(server not
configured, Supabase 환경변수 미설정) → 재배포 재확인 후 200 정상화됨.

실행: python scripts/market_news_fetcher.py
권장: rss_fetcher.py와 같은 주기로 호출(자체 dedup은 is_url_exists로 처리
되므로 자주 호출해도 안전).
"""

import os
import re
import sys
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
from rapidfuzz import fuzz

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

from db import init_db, is_url_exists, insert_article, mark_sent_telegram, now_kst

init_db()

MONEYFINAL_FEED_URL = "https://moneyfinal.pages.dev/api/market-news"
MONEYFINAL_FEED_KEY = os.getenv("MONEYFINAL_FEED_KEY", "")

FETCH_LIMIT = int(os.getenv("MARKET_NEWS_FETCH_LIMIT", "40"))
SIMILARITY_THRESHOLD = 75

# ── 소스 고정값 (2026-09-03) ────────────────────────────────
# rss_sources 테이블의 기존 "경제/금융·증권" 소스와 동일한 분류를 쓴다
# (Supabase rss_sources 조회로 확인한 실제 값 — category="경제",
# subcategory="금융/증권"). 이 피드는 미국 시장 뉴스 전용이라 국가는
# 항상 고정값을 쓴다(rss_fetcher.py의 detect_country처럼 본문에서 추론할
# 필요가 없음 — 소스 자체가 이미 미국 시장으로 확정돼 있음).
CATEGORY = "경제"
SUBCATEGORY = "금융/증권"
COUNTRY = "미국"
COUNTRY_FLAG = "🇺🇸"
REGION = "global"
SOURCE_TAG = "MoneyFinal-Finnhub"


# ── rss_fetcher.py와 동일한 소규모 유틸(위 주석 참조 — import 대신 복제) ──
_TRANS_ERR_MARKS = (
    "That's an error", "That's all we know", "Server Error",
    "Error 500", "Error 502", "Error 503",
    "unusual traffic from your computer network", "Our systems have detected",
)


def _is_bad_translation(t) -> bool:
    if not t or not isinstance(t, str):
        return True
    return any(m in t for m in _TRANS_ERR_MARKS)


def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    text = re.sub(r'&#x[0-9a-fA-F]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)
    replacements = {
        '\u2019': "'", '\u2018': "'", '\u201c': '"', '\u201d': '"',
        '\u2013': '-', '\u2014': '-', '\xa0': ' ',
    }
    for orig, rep in replacements.items():
        text = text.replace(orig, rep)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def is_duplicate(title, seen_titles):
    for seen in seen_titles:
        if fuzz.token_sort_ratio(title.lower(), seen.lower()) >= SIMILARITY_THRESHOLD:
            return True
    return False


def fetch_market_news(limit: int = FETCH_LIMIT) -> list:
    if not MONEYFINAL_FEED_KEY:
        print("  [SKIP] MONEYFINAL_FEED_KEY 없음")
        return []
    try:
        res = requests.get(
            MONEYFINAL_FEED_URL,
            headers={"X-Api-Key": MONEYFINAL_FEED_KEY},
            params={"limit": str(limit)},
            timeout=15,
        )
        if res.status_code == 401:
            print("  [ERROR] 401 — MONEYFINAL_FEED_KEY 확인 필요")
            return []
        if res.status_code != 200:
            print(f"  [ERROR] 머니파이널 피드 응답 {res.status_code}: {res.text[:200]}")
            return []
        return res.json().get("items", [])
    except Exception as e:
        print(f"  [ERROR] 머니파이널 피드 조회 예외: {e}")
        return []


def translate_ko(text: str, max_chars: int) -> str:
    if not text:
        return ""
    try:
        t = GoogleTranslator(source="auto", target="ko").translate(text[:max_chars])
        t = clean_text(t)
        return "" if _is_bad_translation(t) else t
    except Exception:
        return ""


def main():
    print(f"\n[market_news_fetcher] 시작: {now_kst().strftime('%Y-%m-%d %H:%M')} KST")

    items = fetch_market_news()
    print(f"  → 머니파이널 피드에서 {len(items)}건 수신")
    if not items:
        return

    seen_titles = []
    inserted = 0
    for item in items:
        headline = clean_text(item.get("headline") or "")
        url = (item.get("url") or "").strip()
        if not headline or not url:
            continue

        if is_url_exists(url):
            continue
        if is_duplicate(headline, seen_titles):
            print(f"  [SKIP] 유사 기사 중복 — {headline[:50]}")
            continue
        seen_titles.append(headline)

        summary_en = clean_text(item.get("summary") or "")
        source_name = item.get("source") or "Finnhub"
        published_at = item.get("published_at") or None

        title_ko = translate_ko(headline, 500) or headline
        summary_ko = translate_ko(summary_en, 4500) if summary_en else ""

        article_id = insert_article(
            title_en=headline, title_ko=title_ko,
            summary_en=summary_en, summary_ko=summary_ko,
            url=url, source=f"{source_name} (via {SOURCE_TAG})",
            category=CATEGORY, subcategory=SUBCATEGORY, region=REGION,
            country=COUNTRY, country_flag=COUNTRY_FLAG,
            score=0, full_text="",
            countries=[COUNTRY],
            is_published=False,
            source_published_at=published_at,
        )
        if article_id > 0:
            # gemini_writer.py의 클러스터링 후보 조회(get_today_articles)가
            # sent_telegram=eq.1인 원자재만 뽑아간다(rss_fetcher.py는 텔레그램
            # 발송 성공 시 mark_sent_telegram을 호출해 이 플래그를 세움). 이
            # 호출이 빠져있으면 여기서 넣은 기사는 영원히 후보 풀에 안 잡힌다
            # (2026-09-04 실측 — 42건 전부 sent_telegram=0으로 방치됨).
            mark_sent_telegram(article_id)
            inserted += 1
            print(f"  [OK] {title_ko[:50]}")
        else:
            print(f"  [FAIL] 저장 실패 — {headline[:50]}")

    print(f"[market_news_fetcher] 완료: {inserted}건 신규 저장")


if __name__ == "__main__":
    main()
