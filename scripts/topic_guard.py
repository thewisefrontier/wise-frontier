"""
topic_guard.py — 복수 주제(그랩백/다이제스트) 원문 감지 공용 모듈

gemini_writer.py(단독 기사화 파킹)와 gemini_summarizer.py(트렌드 추적기)가
공용으로 쓴다. 원래 gemini_writer.py에만 있던 걸 분리 — 트렌드 추적기의
get_trend_articles()가 이 필터 없이 원문을 가져오다, "인도네시아 무역
적자·국제 범죄 조직 검거 등 주요 사건 일지"류 다국가 다이제스트 원문이
그대로 섞여 들어와 아이티/미얀마 트렌드 기사에 무관한 문단이 끼는 사고가
반복됐다(2026-09-07, 사용자 지적으로 트렌드 그룹 전수 확인 중 발견).
"""

import re


def split_multi_topic_title(title: str) -> list:
    """
    복수 주제 제목을 개별 토픽으로 분리.
    예: "우간다 군 수뇌부 갈등 및 나이지리아 채용 사기 주의보"
    → ["우간다 군 수뇌부 갈등", "나이지리아 채용 사기 주의보"]
    단일 주제면 빈 리스트 반환.
    """
    if not title:
        return []
    separators = [' 및 ', ' and ', ' & ', ' et ', '…및', ', and ', '; ']
    for sep in separators:
        if sep.lower() in title.lower():
            parts = [p.strip() for p in title.split(sep) if p.strip() and len(p.strip()) > 5]
            if len(parts) >= 2:
                return parts
    return []


def is_multi_topic_title(title: str) -> bool:
    """복수 주제 제목 여부 — 분리 가능한 패턴 + 글로벌 종합 제목"""
    if not title:
        return False
    if len(split_multi_topic_title(title)) >= 2:
        return True
    if re.match(r'^글로벌\s+\S+.+(?:변화|동향|행보|흐름|속에서|격화|가속화)', title):
        return True
    if re.search(r'각국의?\s+(경제|사회|정치|행보|대응|현안)', title):
        return True
    if re.match(r'^전\s+세계\s+주요국', title):
        return True
    if '등 글로벌' in title or '등 주요 단신' in title or '등 주요 현안' in title:
        return True
    country_names = ['나이지리아','케냐','가나','에티오피아','필리핀','베트남',
                     '인도네시아','태국','이집트','우간다','탄자니아','수단',
                     '키르기스스탄','미얀마','캄보디아','인도','중국','미국',
                     '방글라데시','파키스탄','카자흐스탄','라오스','캄보디아']
    hits = [c for c in country_names if c in title]
    if len(hits) >= 3:
        return True
    return False


def is_multi_topic_body(text: str) -> bool:
    """
    본문 앞 3문단이 서로 다른 국가/주제를 다루는지 감지.
    각 문단에서 국가명을 추출해서 3개 이상 다른 국가가 나오면 복수 주제로 판단.
    """
    if not text:
        return False
    # 앞 600자만 분석
    lead = text[:600]
    paragraphs = [p.strip() for p in re.split(r'[.!?。]\s+', lead) if len(p.strip()) > 20][:6]

    country_names = ['나이지리아','케냐','가나','에티오피아','필리핀','베트남',
                     '인도네시아','태국','이집트','우간다','탄자니아','수단',
                     '키르기스스탄','미얀마','캄보디아','방글라데시','파키스탄',
                     '카자흐스탄','라오스','카메룬','코트디부아르','세네갈',
                     '잠비아','짐바브웨','앙골라','모잠비크','르완다']

    found_countries = set()
    for para in paragraphs:
        for c in country_names:
            if c in para:
                found_countries.add(c)

    # 3개 이상 다른 국가가 앞부분에 나오면 복수 주제
    return len(found_countries) >= 3
