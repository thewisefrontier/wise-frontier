# -*- coding: utf-8 -*-
"""메트로폴리탄 미술관 Open Access API(CC0, isPublicDomain 플래그 공식 제공)에서
"고전명화이야기"에 쓸 대량 후보를 수집. 사용자 요청("적어도 1년은 해야하는데
미술 관련 DB나 깃허브 없어?")에 대한 답 — Met의 Collection API가 정확히
이 용도의 공식 공개 데이터임을 확인(github.com/metmuseum/openaccess,
collectionapi.metmuseum.org).

필터: isHighlight(미술관이 직접 선정한 대표작) + isPublicDomain=true +
artistEndDate 사후 70년 경과(또는 작가 불명 시 objectEndDate로 대체 판단).

실행: python scripts/harvest_met_artworks.py
결과: scripts/data/art_weekly_met_artworks.json에 덮어씀 — art_weekly_writer.py가
이 파일을 ARTWORKS에 자동으로 이어붙인다. 목록이 짧아지면(=1년 넘게 반복)
DEPARTMENTS에 부서를 더 추가하거나 재실행할 것(Met 쪽 데이터가 갱신되면
결과가 달라질 수 있음 — 재실행 후 반드시 scripts/test_art_weekly_writer.py로
회귀 확인).
"""
import json
import os
import re
import time
import requests
import sys

BASE = "https://collectionapi.metmuseum.org/public/collection/v1"
OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "art_weekly_met_artworks.json")
CURRENT_YEAR = 2026
PD_CUTOFF_DEATH_YEAR = CURRENT_YEAR - 70  # 1956
ANONYMOUS_SAFE_OBJECT_YEAR = 1900  # 작가 불명일 때 안전판 — 이보다 오래된 작품만

# (departmentId, region, fallback_country, fallback_flag)
DEPARTMENTS = [
    (11, "europe", "", ""),       # European Paintings
    (6, "asia", "", ""),          # Asian Art
    (1, "global", "미국", "🇺🇸"),  # The American Wing
    (9, "europe", "", ""),        # Drawings and Prints
    (14, "asia", "", ""),         # Islamic Art
    (17, "europe", "", ""),       # Medieval Art
]

NATIONALITY_TO_KO = {
    "French": ("프랑스", "🇫🇷"), "Dutch": ("네덜란드", "🇳🇱"), "Italian": ("이탈리아", "🇮🇹"),
    "Spanish": ("스페인", "🇪🇸"), "British": ("영국", "🇬🇧"), "German": ("독일", "🇩🇪"),
    "American": ("미국", "🇺🇸"), "Flemish": ("벨기에", "🇧🇪"), "Belgian": ("벨기에", "🇧🇪"),
    "Austrian": ("오스트리아", "🇦🇹"), "Russian": ("러시아", "🇷🇺"), "Japanese": ("일본", "🇯🇵"),
    "Chinese": ("중국", "🇨🇳"), "Korean": ("한국", "🇰🇷"), "Swiss": ("스위스", "🇨🇭"),
    "Norwegian": ("노르웨이", "🇳🇴"), "Danish": ("덴마크", "🇩🇰"), "Swedish": ("스웨덴", "🇸🇪"),
    "Indian": ("인도", "🇮🇳"), "Persian": ("이란", "🇮🇷"), "Turkish": ("튀르키예", "🇹🇷"),
    "Mexican": ("멕시코", "🇲🇽"), "Canadian": ("캐나다", "🇨🇦"),
}


def parse_death_year(bio_or_enddate: str) -> int | None:
    m = re.search(r"(\d{4})\s*$", (bio_or_enddate or "").strip())
    return int(m.group(1)) if m else None


def parse_nationality(bio: str) -> str | None:
    for key in NATIONALITY_TO_KO:
        if key in (bio or ""):
            return key
    return None


def fetch_json(url, retries=3):
    for i in range(retries):
        try:
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                return res.json()
        except Exception:
            pass
        time.sleep(1)
    return None


def main():
    seen_ids = set()
    object_ids = []
    for dept_id, region, _, _ in DEPARTMENTS:
        data = fetch_json(f"{BASE}/search?departmentId={dept_id}&isHighlight=true&isPublicDomain=true&q=painting")
        if not data or not data.get("objectIDs"):
            continue
        for oid in data["objectIDs"]:
            if oid not in seen_ids:
                seen_ids.add(oid)
                object_ids.append((oid, region))
    print(f"후보 objectID {len(object_ids)}건 수집, 상세 조회 시작...", file=sys.stderr)

    results = []
    rejected = {"no_image": 0, "not_pd": 0, "too_recent": 0, "no_title": 0}
    for i, (oid, region) in enumerate(object_ids):
        obj = fetch_json(f"{BASE}/objects/{oid}")
        if not obj:
            continue
        if not obj.get("isPublicDomain"):
            rejected["not_pd"] += 1
            continue
        image = obj.get("primaryImage") or ""
        if not image:
            rejected["no_image"] += 1
            continue
        title = (obj.get("title") or "").strip()
        if not title or len(title) < 2:
            rejected["no_title"] += 1
            continue

        artist_en = (obj.get("artistDisplayName") or "").strip()
        bio = obj.get("artistDisplayBio") or ""
        death_year = parse_death_year(obj.get("artistEndDate") or "")
        obj_end = obj.get("objectEndDate")

        if artist_en and death_year:
            if death_year > PD_CUTOFF_DEATH_YEAR:
                rejected["too_recent"] += 1
                continue
        elif isinstance(obj_end, int) and obj_end > ANONYMOUS_SAFE_OBJECT_YEAR:
            # 작가 불명인데 작품 자체도 최근이면 위험 — 스킵
            rejected["too_recent"] += 1
            continue

        nat_key = parse_nationality(bio)
        country, flag = NATIONALITY_TO_KO.get(nat_key, ("", ""))

        # 2026-09-27 추가 — 초기 350건 dry-run 중 신윤복만큼도 안 알려진 화가
        # (예: 이정 "풍죽도")는 위키백과 근거자료가 아예 안 잡히는 경우가 흔해
        # (0자) 매일 발행이 자꾸 스킵될 위험을 확인. 위키백과에만 기대지 않고,
        # 미술관이 직접 확정한 소장품 기록(작가 생몰년·문화권·시대·재질·기증
        # 출처)을 항상 존재하는 1차 근거자료로 별도 저장 — 위키백과는 있으면
        # 보충용으로만 쓴다(build_article_prompt에서 합침).
        museum_facts = []
        if bio:
            museum_facts.append(f"작가: {artist_en}({bio})")
        if obj.get("culture"):
            museum_facts.append(f"문화권: {obj['culture']}")
        if obj.get("period"):
            museum_facts.append(f"시대: {obj['period']}")
        if obj.get("objectDate"):
            museum_facts.append(f"제작 시기: {obj['objectDate']}")
        if obj.get("medium"):
            museum_facts.append(f"재질/기법: {obj['medium']}")
        if obj.get("classification"):
            museum_facts.append(f"분류: {obj['classification']}")
        if obj.get("creditLine"):
            museum_facts.append(f"소장 경위: {obj['creditLine']}")
        museum_facts.append(f"현재 뉴욕 메트로폴리탄 미술관({obj.get('department','')})에 소장.")

        results.append({
            "title_ko": title,
            "title_en": title,
            "artist_ko": artist_en or "작자 미상",
            "artist_en": artist_en or "Unknown artist",
            "year_label": obj.get("objectDate") or "",
            "country": country,
            "country_flag": flag,
            "region": region,
            "wiki_query": f"{title} {artist_en}".strip(),
            "direct_image_url": image,
            "met_object_id": oid,
            "met_department": obj.get("department"),
            "museum_grounding": " ".join(museum_facts),
        })
        if (i + 1) % 50 == 0:
            print(f"  진행 {i+1}/{len(object_ids)}, 통과 {len(results)}건", file=sys.stderr)

    print(f"\n최종 통과 {len(results)}건 / 거부 {rejected}", file=sys.stderr)

    # 제목 중복 제거(같은 작품이 여러 부서 검색에 겹칠 수 있음)
    dedup = {}
    for r in results:
        dedup[r["met_object_id"]] = r
    final = list(dedup.values())
    print(f"중복 제거 후 {len(final)}건", file=sys.stderr)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    print(f"저장 완료: {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
