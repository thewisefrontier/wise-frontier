# 뉴스파이널: Supabase 장애/차단 대비 CockroachDB 스탠바이 계획

**상태: 대기(비활성). 실제 전환 코드는 아직 투입되지 않았고, scripts/db.py는 계속 Supabase를 그대로 사용 중이다.**
이 문서는 "Supabase를 10월에라도 못 쓰게 되면 최대한 빨리 갈아탈 수 있게" 준비해둔 실행계획이다.

## 배경
2026-09-24 Cloudflare D1 이전 점검(핫딜월드+머니파이널+뉴스파이널) 과정에서, 뉴스파이널은
Supabase 무료 한도(DB 500MB, egress 월 5GB)를 이미 두 차례 초과해 구조를 바꾼 이력이 확인됨
(원문 텍스트 저장 제거, 백업 주기 일→주 축소). D1은 한도가 더 빡빡하고(일 10만 write, 초과
시 하드 실패) 계정을 핫딜월드·머니파이널과 공유하게 돼 D1 이전은 하지 않기로 결정 —
자세한 배경은 이번 점검 세션 기록 참고. 대신 "Supabase 자체가 막히는" 더 극단적인 상황에
대비해 Postgres 호환 무료 대안(CockroachDB Basic)으로의 전환 경로를 미리 준비해둔다.

## 2026-09-24 실측 업데이트
Supabase 대시보드 확인: Egress 8.06GB 사용 / 5GB 한도 / **3.06GB 초과** 상태.
GitHub Actions `run.yml` 최근 실행 로그에서 `[domestic_kr_writer] 후보 조회 실패: 500 —
{"code":"57014", "message":"canceling statement due to statement timeout"}` 발견 —
IO 스로틀로 추정되는 쿼리 타임아웃. 완전 장애는 아님(워크플로우 자체는 success로 완료,
기사 저장 로그도 정상 발생) — 성능 저하 조짐 단계. 초과분은 다음 결제 주기 시작 시 0으로
리셋되나, 그 전까지는 이런 저하가 간헐적으로 재발할 수 있음.

CockroachDB Basic 클러스터 생성 시도 중 확인된 사항: Cloud Console에서 "+ New cluster"로
들어가면 Basic 없이 바로 유료 Standard/Mission Critical Edition 화면으로 가는 경우가 있음
(원인 미확인 - 이 조직 한정 문제일 수 있음). 공식 절차상 Basic은 "Select a plan" 페이지에서
별도 선택해야 하며, 안 보이면 Cloud API(`POST /v1/clusters`, `"plan":"BASIC"`) 또는 `ccloud
cluster create basic`으로 우회 가능 — 결제 정보 미등록 조직은 Basic 클러스터 1개까지 생성
가능. 아직 클러스터 생성을 완료하지 못한 상태(진행 중).

## 왜 CockroachDB Basic인가
- 무료 티어: 월 5천만 Request Unit + 저장 10GiB (Neon 무료 0.5GB보다 훨씬 여유 있음)
- **Postgres wire protocol 호환** — D1/Turso(SQLite)와 달리 타입 변환이 거의 필요 없음
  (boolean/jsonb/array/uuid 전부 네이티브 지원). 아래 `migrations/cockroachdb_init.sql`이
  Supabase 원본 스키마 17개 테이블을 그대로 이식한 것.
- 참고: 아직 계정을 만들지 않아 실제 클러스터에 스키마를 적용해본 적은 없음 — RU 소비량이
  뉴스파이널의 쓰기 빈도(최대 하루 9만 건대 추정)에서 월 5천만 RU 한도 안에 들어오는지도
  실측 전에는 알 수 없음.

## 이번에 실제로 준비해둔 것
- `migrations/cockroachdb_init.sql` — Supabase 17개 테이블 스키마를 Postgres 표준 문법으로
  이식(BIGSERIAL/SERIAL, JSONB, TEXT[], UUID, UNIQUE 제약 전부 반영). CockroachDB 클러스터가
  생기면 `cockroach sql --url=$COCKROACH_URL < migrations/cockroachdb_init.sql`로 바로 적용
  가능 — 단, 실제 적용은 안 해봤으니 에러 나는 줄이 있으면 그때 스팟 수정 필요.

## 이번에 준비하지 않은 것 (중요 — 솔직하게 남김)
`scripts/db.py`(약 400줄)는 단순 REST 래퍼가 아니라, 실제 장애를 겪으며 쌓인 세부 로직이
빽빽하다: PostgREST 배치 INSERT의 "배열 내 키 집합 불일치 시 전체 거부"(PGRST102) 회피,
NUL 문자 제거, on_conflict 대상 컬럼 명시(안 하면 PK 기준으로만 잡혀 UNIQUE 충돌이 새어나간
사고 이력 있음), 이분 탐색 재시도, `fetched_at` 동시각 정렬 문제 등. 이걸 검증 없이 psycopg2로
통째로 재작성해서 "준비 완료"라고 해두면, 실제 전환 순간(=Supabase가 이미 막혀서 되돌릴 곳도
없는 최악의 타이밍)에야 버그가 튀어나올 위험이 크다고 판단해 **이번 세션에서는 코드 재작성을
하지 않았다.** 대신 아래에 실제 전환 시 밟을 절차와, 변환이 필요한 함수 목록을 남겨둔다.

`scripts/article_store.py`, `exporters/export_articles.py` 등 DB에 접근하는 다른 파일도
있는 것으로 파악되나(이전 조사에서 이름만 확인, 전체 내용은 미확인) 이번에 들여다보지 않았음.

## 전환 경로 두 가지 (택1, 활성화 시점에 결정)

### 경로 A: CockroachDB 앞에 PostgREST를 직접 띄워서 기존 코드 재사용
Supabase가 내부적으로 쓰는 PostgREST(오픈소스)를 작은 컨테이너로 직접 띄워 CockroachDB에
연결하면, `scripts/db.py`의 REST 호출 패턴(`?on_conflict=`, `Prefer:` 헤더 등)을 거의
그대로 재사용할 수 있어 코드 변경이 적다.
- 장점: `scripts/db.py` 재작성 최소화, 위에서 말한 미검증 위험을 피함
- 단점: **PostgREST가 CockroachDB를 완전히 지원하는지 미검증**(PostgREST는 Postgres의
  시스템 카탈로그를 읽어 스키마를 추론하는데, CockroachDB가 이를 100% 동일하게 노출하는지
  확인 안 됨) + 컨테이너를 어딘가(Oracle Cloud 무료 VM 등)에 직접 띄우고 관리해야 함(운영 부담 추가)

### 경로 B: `scripts/db.py`를 psycopg2 기반으로 재작성
- 장점: 확실히 동작하는 표준 SQL 경로, 별도 프록시 서버 불필요
- 단점: 위 20여 개 함수(is_url_exists, insert_article, queue_insert_bulk,
  queue_claim_batch, update_source_health 등)의 PostgREST 특유 로직(on_conflict,
  이분 재시도, PGRST102 회피)을 SQL의 `ON CONFLICT ... DO UPDATE`, 명시적 트랜잭션 등으로
  하나하나 다시 옮겨야 함 — 실질적으로 하루 이상 걸리는 코드 작업

**권장**: 실제로 막히는 상황이 오면 경로 A를 먼저 15분 정도 스모크 테스트해보고(PostgREST가
CockroachDB에서 기본 SELECT/INSERT/UPSERT를 문제없이 처리하는지), 되면 A로, 안 되면 B로
간다 — 이 판단 자체를 지금 미리 해둘 수는 없다(실측 필요).

## 활성화 체크리스트 (Supabase가 실제로 막혔을 때)
1. https://cockroachlabs.cloud 에서 Basic(무료) 클러스터 생성 — 웹 UI에서 Basic이 안 보이면
   Cloud API(`POST /v1/clusters`, `"plan":"BASIC"`) 또는 `ccloud cluster create basic`
   CLI로 생성. 리소스 한도(RU/저장용량)를 반드시 무료 제공량(5천만 RU/10GiB)에 맞춰 명시
   설정 — "Unlimited"로 두면 과금될 수 있음.
2. `migrations/cockroachdb_init.sql` 적용, 에러 나는 줄 스팟 수정
3. 경로 A/B 중 하나 선택 후 스모크 테스트(테이블 1개로 insert/select/upsert 확인)
4. Supabase에서 데이터 이전: `pg_dump`로 데이터만 덤프(`--data-only`) 후 CockroachDB로 복원
   (스키마는 이미 2단계에서 만들어져 있으므로 데이터만 옮기면 됨) — Supabase가 완전히
   응답 불가 상태라면 가장 최근 `newsfinal-backups` R2 백업(주 1회 암호화 pg_dump)에서 복원
5. GitHub Actions 시크릿에 `COCKROACH_DATABASE_URL` 추가, `scripts/db.py`(또는 그 대체
   모듈)가 이를 읽도록 배포
6. `docs/article.html`의 브라우저 직접 Supabase REST 호출(기사 조회, 조회수 증가 RPC)을
   대체할 방법 필요 — PostgREST를 쓴다면(경로 A) 그 엔드포인트를 그대로 가리키면 되고,
   psycopg2로 갔다면(경로 B) 기존 Worker 3개와 같은 패턴으로 작은 프록시 Worker를 새로 만들어야 함
7. 워크플로우 1개(`run.yml`)로 먼저 실전 검증 후 나머지 워크플로우 전환

## 롤백
Supabase가 다시 정상화되면 GitHub Actions 시크릿을 원래 `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`
경로로 되돌리면 됨(코드에서 두 경로를 환경변수로 스위칭하게 만들어두면 더 매끄러움 — 실제
전환 작업 시 고려).

## 모니터링 (사전 경보 — 완전히 막히기 전에 조짐을 알아채기 위함)
- Supabase DB 크기 400MB(80%) 또는 월 egress 4GB(80%) 도달 시 이 문서를 다시 열어 준비 상태 점검
- Supabase 측 공지(플랜 변경, 계정 정지 경고 메일 등)를 받으면 즉시 위 체크리스트 착수
- **2026-09-24 기준 이미 egress 초과(3.06GB) 상태 — 다음 결제 주기 리셋 전까지 IO 스로틀
  재발 가능성 있음, 수시 모니터링 필요**
