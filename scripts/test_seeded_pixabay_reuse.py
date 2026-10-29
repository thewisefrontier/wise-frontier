"""fetch_seeded_pixabay_image 재사용 회피 회귀 테스트.

실사례(2026-10-28~29): 장중특징주 319974(구글)·319975(아마존)와 종목동향 320049가 모두
pid10482055 — 지명 필터 뒤 후보 2장이 다 최근 사용이라 고정 인덱스로 후퇴했다.
실행: python scripts/test_seeded_pixabay_reuse.py (네트워크·DB 없이 가짜 함수로 검사)
"""
import article_image as ai

POOL = {
    "stock market chart": [{"id": 1}, {"id": 2}],
    "trading floor": [{"id": 3}, {"id": 4}],
}
stored = []
ai.PIXABAY_API_KEY = "x"
ai._pixabay_hits = lambda q, allowed: POOL.get(q, [])
ai._store_pixabay_hit = lambda q, hit, key: stored.append((q, hit["id"])) or f"pid{hit['id']}"


def run(used, keywords=("stock market chart", "trading floor")):
    ai.db.pixabay_ids_used_recently = lambda ids: {str(i) for i in ids} & used
    stored.clear()
    return ai.fetch_seeded_pixabay_image(list(keywords), 0, "t"), stored[-1]


# 첫 키워드 후보가 전부 최근 사용 → 다음 키워드의 미사용 사진(이전: id 1 재사용)
assert run({"1", "2"}) == ("pid3", ("trading floor", 3))
# 미사용이 있으면 첫 키워드 안에서 해결(기존 동작 유지)
assert run({"1"}) == ("pid2", ("stock market chart", 2))
# 모든 키워드 소진 → 첫 키워드 고정 인덱스로 후퇴(기존 동작)
assert run({"1", "2", "3", "4"}) == ("pid1", ("stock market chart", 1))
# 첫 키워드 결과 없음 → 다음 키워드
assert run(set(), ("없는 키워드", "trading floor")) == ("pid3", ("trading floor", 3))
print("OK")
