"""
scripts/manual_publish_article.py
-----------------------------------
사용자가 준 소스 기사를 뉴스파이널 스타일로 재구성해 수동으로 발행할 때 쓰는
도구. 원래는 SQL INSERT를 손으로 작성했는데(2026-09-02, id=120822), 그 방식은
두 가지 문제를 냈다:

1. SQL 문자열 리터럴('...')은 "\\n"을 줄바꿈으로 해석하지 않는다(E''나 실제
   개행 문자가 필요) — 후속 프롬프트 수정 때 이걸 몰라서 "\\n"이 문자 그대로
   저장되는 사고가 났다.
2. export_articles.py(정적 스냅샷 docs/data/articles.json 재생성)를 거치지
   않아서, article.html/live.html(Supabase 직접 조회)에는 보이는데 메인
   페이지(index.html, 스냅샷 기반)에는 안 보이는 문제가 있었다.

이 스크립트는 두 문제를 구조적으로 없앤다:
- article 데이터를 Python dict로 작성(실제 개행 문자 자연스럽게 사용) →
  json 직렬화 → Supabase REST API(POST, gemini_writer.py의 save_article()과
  동일한 방식)로 삽입. 이스케이프는 Python/requests가 알아서 처리하므로
  수동 SQL 이스케이프 실수가 원천적으로 발생하지 않는다.
- 삽입 성공 후 export_articles.py를 자동 호출해 정적 스냅샷까지 한 번에 갱신.

사용법:
  1. 아래 ARTICLE 딕셔너리를 수정(또는 이 파일을 복사해 새로 작성)
  2. SUPABASE_URL / SUPABASE_SERVICE_KEY 환경변수 설정 (쓰기 권한 필요 —
     anon key로는 RLS에 막혀 INSERT 안 됨)
  3. python scripts/manual_publish_article.py

SUPABASE_SERVICE_KEY가 없으면(로컬 .env엔 기본적으로 없음) INSERT를
시도하는 대신, MCP execute_sql로 그대로 실행 가능한 안전한 SQL을 출력한다
(jsonb_populate_record 방식 — JSON 파서가 이스케이프를 처리하므로 수동 SQL
이스케이프 문제가 없다).
"""

import os
import sys
import json
import subprocess
import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://fotdngseksqaghvtcvqh.supabase.co").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def _now_kst():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))


def build_article(
    title_ko: str,
    body: str,
    *,
    url: str = "",
    source: str = "NewsFinal",
    category: str,
    subcategory: str,
    region: str,
    country: str,
    countries: list,
    image_url: str = "",
    image_credit: str = "",
    summary_3lines: str,
    investment_idea: str,
    byline: str = "뉴스파이널 편집국",
    update_note: str = "수동 작성 - 사용자 제보 소스 종합 및 외신 교차검증",
) -> dict:
    """gemini_writer.py의 save_article() payload와 같은 스키마로 dict를 만든다."""
    now_str = _now_kst().strftime("%Y-%m-%d %H:%M")
    return {
        "title_ko": title_ko,
        "title_en": title_ko,
        "summary_ko": body,
        "url": url,
        "source": source,
        "category": category,
        "subcategory": subcategory,
        "region": region,
        "country": country,
        "country_flag": "",
        "countries": countries,
        "image_url": image_url,
        "image_credit": image_credit,
        "score": 1,
        "created_at": now_str,
        "first_published_at": now_str,
        "update_log": [{"timestamp": now_str, "note": update_note}],
        "sent_telegram": 0,
        "is_published": True,
        "posted_blog": 0,
        "is_travel": False,
        "byline": byline,
        "summary_3lines": summary_3lines,
        "investment_idea": investment_idea,
    }


def insert_via_rest(article: dict) -> int:
    """article_store.insert_final_article()로 위임 — 다른 writer 스크립트와
    동일한 삽입 경로를 쓴다(2026-09-02, 10여개 스크립트 복붙 로직을
    article_store.py로 공용화하면서 이 파일도 같이 정리)."""
    from article_store import insert_final_article
    art_id = insert_final_article(article)
    if art_id <= 0:
        raise RuntimeError("삽입 실패 — article_store.insert_final_article()가 -1 반환")
    return art_id


def print_safe_sql(article: dict):
    """SUPABASE_SERVICE_KEY가 없을 때: MCP execute_sql로 바로 실행 가능한 안전한
    SQL을 출력한다. json.dumps가 이스케이프를 전담하므로(개행 -> \\n 이스케이프는
    JSON 파서가 되돌려 실제 개행으로 복원한다) 수동 SQL 이스케이프 실수가 없다."""
    columns = list(article.keys())
    payload_json = json.dumps(article, ensure_ascii=False)
    # $$ 안에 실제 $$ 문자열이 들어있을 위험은 거의 없지만, 방어적으로 확인.
    if "$$" in payload_json:
        raise RuntimeError("payload에 '$$' 문자열이 포함돼 있어 dollar-quoting과 충돌합니다 — 직접 확인 필요")

    # articles 테이블 실제 컬럼 타입과 맞춰야 한다 — text로 통일하면 score/
    # sent_telegram/posted_blog(integer), is_published/is_travel(boolean)에서
    # "column is of type integer but expression is of type text" 오류가 난다.
    # articles 테이블 실제 컬럼 타입(2026-09-02, information_schema.columns로 확인).
    # ⚠️ 시각 컬럼 타입이 일관되지 않다 — created_at은 text인데 first_published_at은
    # timestamp다. 새 컬럼을 다루게 되면 이 매핑도 information_schema.columns로
    # 다시 확인해서 갱신할 것 — 기본값(text)으로 두면 "column is of type X but
    # expression is of type text" 오류가 난다.
    COLUMN_TYPES = {
        "countries": "text[]", "update_log": "jsonb", "source_data": "jsonb",
        "score": "int", "sent_telegram": "int", "posted_blog": "int", "view_count": "int",
        "is_published": "boolean", "is_travel": "boolean",
        "company_scanned": "boolean", "dedup_reviewed": "boolean",
        "first_published_at": "timestamp", "source_published_at": "timestamptz",
    }
    col_defs = ", ".join(f'"{c}" {COLUMN_TYPES.get(c, "text")}' for c in columns)
    print("-- SUPABASE_SERVICE_KEY가 없어 REST 삽입 대신 SQL을 출력합니다.")
    print("-- 아래 SQL을 mcp__*__execute_sql로 그대로 실행하세요 (수동 이스케이프 불필요 — JSON 파서가 처리).")
    print(f"""
insert into articles ({', '.join(f'"{c}"' for c in columns)})
select {', '.join(f'x."{c}"' for c in columns)}
from jsonb_to_record($${payload_json}$$::jsonb) as x({col_defs})
returning id;
""")


def regenerate_static_snapshot():
    """export_articles.py를 호출해 docs/data/articles.json(메인 페이지가 쓰는
    정적 스냅샷)을 갱신한다. 이 단계를 빼먹으면 article.html/live.html에는
    보이는데 메인 페이지엔 안 보이는 문제가 재발한다(2026-09-02, id=120822)."""
    env = os.environ.copy()
    env.setdefault("SUPABASE_URL", SUPABASE_URL)
    env.setdefault("SUPABASE_SERVICE_KEY", SUPABASE_SERVICE_KEY or
                    "sb_publishable_rT3kSEAAdM0DJlDW5fEWww_M37h6dDW")  # 읽기 전용 anon 키 폴백
    env.setdefault("PYTHONIOENCODING", "utf-8")
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("\n[SNAPSHOT] export_articles.py 실행 중...")
    result = subprocess.run(
        [sys.executable, os.path.join(repo_root, "scripts", "export_articles.py")],
        cwd=repo_root, env=env, capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        print("[SNAPSHOT] 실패 — docs/data/articles.json을 수동으로 확인하세요.")
    else:
        print("[SNAPSHOT] 완료 — git status로 변경분 확인 후 커밋·푸시할 것.")


if __name__ == "__main__":
    # 예시 — 실제 사용 시 이 블록을 교체하거나, build_article()을 다른 스크립트에서 import.
    article = build_article(
        title_ko="(제목)",
        body="(본문)",
        category="정치·외교",
        subcategory="realtrend_예시",
        region="middle_east",
        country="미국",
        countries=["미국"],
        summary_3lines="(3줄 요약)",
        investment_idea="(투자 아이디어)",
    )

    if SUPABASE_SERVICE_KEY:
        new_id = insert_via_rest(article)
        print(f"[INSERT] 완료 — id={new_id}")
        regenerate_static_snapshot()
    else:
        print_safe_sql(article)
        print("SQL 실행 후 regenerate_static_snapshot()를 별도로 호출하거나 export_articles.py를 실행하세요.")
