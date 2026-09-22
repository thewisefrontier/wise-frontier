"""
일회성 진단 스크립트 2 — get_today_articles()와 is_opinion_column() 필터를
gemini_writer.py에서 그대로 불러와, 어느 단계에서 500건 → 40건으로 줄어드는지
확인한다. 진단 후 이 파일과 워크플로우는 삭제한다.
"""
import gemini_writer as gw

all_articles = gw.get_today_articles(limit=300)
print(f"get_today_articles() 반환: {len(all_articles)}건")

sources_count = {}
for a in all_articles:
    s = a.get("source") or "?"
    sources_count[s] = sources_count.get(s, 0) + 1
top_sources = sorted(sources_count.items(), key=lambda x: -x[1])[:15]
print("상위 소스:", top_sources)

opinion_skipped = [
    a for a in all_articles
    if gw.is_opinion_column(a.get("title_en") or a.get("title_ko") or "", a.get("full_text") or "")
]
print(f"is_opinion_column 필터로 제외: {len(opinion_skipped)}건")
remaining = [a for a in all_articles if a["id"] not in {x["id"] for x in opinion_skipped}]
print(f"필터 후 남은 건수: {len(remaining)}건")

if opinion_skipped:
    print("제외된 샘플 5건:")
    for a in opinion_skipped[:5]:
        print(" -", (a.get("title_ko") or a.get("title_en") or "")[:60])
