"""
일회성 진단 스크립트 3 — 지금 이 순간 후보 풀(340건대)로 실제 cluster_articles()가
클러스터를 몇 개나 만드는지 확인한다. 마지막 NewsFinal 기사가 13:50 이후
2시간 넘게 없는 이유가 (a) 후보 풀 자체가 비정상적으로 작았던 일시적 문제였는지
(b) 후보는 충분한데 클러스터링/생성 단계에서 막히는지 구분한다.
"""
import gemini_writer as gw

all_articles = gw.get_today_articles(limit=300)
print(f"get_today_articles() 반환: {len(all_articles)}건")

opinion_skipped = [
    a for a in all_articles
    if gw.is_opinion_column(a.get("title_en") or a.get("title_ko") or "", a.get("full_text") or "")
]
all_articles = [a for a in all_articles if a["id"] not in {x["id"] for x in opinion_skipped}]
print(f"칼럼필터 후: {len(all_articles)}건")

clusters = gw.cluster_articles(all_articles)
print(f"클러스터 발견: {len(clusters)}개")
sizes = sorted([len(c) for c in clusters], reverse=True)
print("클러스터 크기 분포(상위 20):", sizes[:20])

today_own = gw.get_today_own_articles()
print(f"오늘 이미 생성된 NewsFinal 자체기사(today_own_articles): {len(today_own)}건")

# 상위 5개 클러스터의 국가/카테고리/제목 미리보기
for i, c in enumerate(clusters[:5]):
    country = c[0].get("country") or ""
    category = c[0].get("category") or ""
    print(f"[클러스터 {i+1}] {country}/{category} — {len(c)}건")
    for a in c[:3]:
        print("   -", (a.get("title_ko") or a.get("title_en") or "")[:50])
