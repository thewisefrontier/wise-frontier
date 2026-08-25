"""
scripts/news_context.py
-------------------------
구글 뉴스 RSS에서 실제 최신 헤드라인 몇 개를 가져와, Gemini 프롬프트에
"추정이 아니라 실제 보도 근거"로 끼워 넣는 용도. 추가 LLM 호출이나
쿼터 비용 없이(단순 RSS 파싱) 실제 헤드라인을 그대로 인용 가능한
소재로 제공한다.

2026-08-26 도입 배경: oil_price_writer.py 등 자체 데이터 API만 쓰는
스크립트들은 "가격 변동 배경"을 Gemini 자체 지식으로만 추정 서술해야
했다(실제 근거가 없어 "~로 풀이된다" 같은 헤지 표현을 강제해야 했음).
실제 헤드라인을 근거로 주면 더 구체적이고 검증 가능한 서술이 가능해진다.

⚠️ 날씨처럼 "제공된 데이터에만 근거" 원칙이 핵심 안전장치인 스크립트에
쓸 때는, 헤드라인도 데이터와 동급의 "제공된 사실"로 취급하고 그 헤드라인
문구를 벗어난 해석·추측을 못 하게 프롬프트에서 명시해야 한다.
"""

from urllib.parse import quote

import feedparser


def fetch_headlines(query: str, limit: int = 5, hl: str = "en-US", gl: str = "US") -> list:
    """구글 뉴스 검색 RSS에서 최신 헤드라인 목록 반환.
    실패해도 예외를 삼키고 빈 리스트를 반환한다(호출부가 이 실패로
    죽지 않도록 — 헤드라인은 보강 재료일 뿐 핵심 데이터가 아니다)."""
    lang = hl.split("-")[0]
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={gl}:{lang}"
    try:
        d = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        return [e.title for e in d.entries[:limit] if getattr(e, "title", None)]
    except Exception:
        return []
