# DB 스탠바이 준비 (Aiven Postgres)

뉴스파이널(wise-frontier) Supabase가 egress 무료 한도(5GB)를 초과해 IO 스로틀이
걸리는 문제가 있어, **완전 무료** 대체 Postgres를 스탠바이로 준비해 둔 기록.
아직 프로덕션 전환 단계는 아니고, 언제든 넘어갈 수 있도록 준비만 해 둔 상태.

## 왜 CockroachDB가 아니라 Aiven인가

처음엔 CockroachDB Serverless(Basic, 무료)를 시도했으나, 이 조직이
"Continuum"(2026-09-15 이후 신규 조직에 강제 적용되는 유료 전용 체계)으로
묶여 있어 API로 Basic 플랜 클러스터 생성 자체가 거부됨
(`"Continuum organizations create clusters by edition rather than by plan"`).
무조건 무료 유지가 원칙이라 이 경로는 포기.

**Aiven 무료 PostgreSQL**로 전환:
- 1GB storage / 1GB RAM / 1 vCPU, 카드 등록 불필요, 영구 무료
- "all networking costs included" — egress 과금이 없어 지금 문제의 근본 원인을 해결함
- 단점: 비활성 시 자동 전원 차단(수동 재기동 필요), 180일 미사용 시 자동 삭제
  → 스탠바이 용도로는 오히려 적합(상시 비용 없음)

## 현재 상태 (2026-09-24 기준)

- Aiven 프로젝트: `thewisefrontier-1ad5`
- 서비스명: `newsfinal-standby`
- 리전: DigitalOcean Bangalore (`do-blr`)
- 플랜: `free-1-1gb` (가격 $0.00 — API 응답으로 확인)
- 상태: `RUNNING`
- 호스트: `newsfinal-standby-thewisefrontier-1ad5.d.aivencloud.com`
- 포트: `11289`
- DB명: `defaultdb`
- 유저: `avnadmin`
- 비밀번호: Aiven 콘솔 또는 API로 확인 (아래 "연결정보 확인 방법" 참고 — 이 문서에는
  기록하지 않음)

Supabase 원본(`fotdngseksqaghvtcvqh`, 뉴스파이널) 실제 DB 크기 268MB, 테이블
17개 — Aiven 1GB 한도 내 충분.

## 스키마

`migrations/aiven_init.sql`에 표준 Postgres용으로 새로 작성함(적용 전, 미적용
상태). Supabase `information_schema`/`pg_catalog`를 직접 조회해서 만든 것으로
추측 없이 실제 컬럼/제약조건/인덱스를 그대로 옮김. CockroachDB용으로 작성했던
이전 버전(`migrations/cockroachdb_init.sql`)은 커밋된 적이 없어 저장소에 없음
— 이번에 새로 작성.

Supabase 전용이라 이식하지 않은 것:
- `user_roles.id → auth.users(id)` FK: Aiven에는 Supabase Auth 스키마가 없어 제거
- RLS 정책 전반: PostgREST/Supabase 클라이언트 접근 모델 전용이라 이식 대상 아님

## 알려진 제약: 클라우드 세션에서 직접 DB 접속 불가

Claude Code 클라우드 세션(이 문서를 쓴 세션 포함)은 HTTPS 프록시를 통해서만
아웃바운드 접속이 가능하고, **raw TCP DB 접속(psql 등)은 프록시가 구조적으로
지원하지 않음** — 네트워크 정책을 "전체 허용"으로 바꿔도 마찬가지임(실제로
`newsfinal-standby` 포트 11289에 TCP 연결을 시도했으나 타임아웃으로 확인됨,
DNS 해석은 정상).

즉 **마이그레이션 적용 / 연결 테스트 / 스모크 테스트는 로컬 PC에서
`psql`로 직접 진행**해야 함. 클라우드 세션에서 할 수 있는 건 Aiven REST API
(`https://api.aiven.io/v1/...`, `Authorization: Bearer $AIVEN_API_TOKEN`)로
서비스 상태 확인과 마이그레이션 SQL 파일 작성까지.

## 연결정보 확인 방법 (비밀번호 포함)

Aiven API는 기본적으로 `service_uri`/`password`를 `<redacted>`로 가림.
전체 값을 보려면 쿼리 파라미터 `include_secrets=true`를 붙여야 함:

```bash
curl -sS "https://api.aiven.io/v1/project/thewisefrontier-1ad5/service/newsfinal-standby?include_secrets=true" \
  -H "Authorization: Bearer $AIVEN_API_TOKEN" | grep -o '"service_uri":"[^"]*"'
```

또는 Aiven 콘솔 → `newsfinal-standby` 서비스 → Overview → Connection information.

## 적용 완료 (2026-09-24)

Aiven 콘솔의 PG Studio(웹 SQL 에디터, 터미널/psql 설치 불필요)로 적용함.
PG Studio가 한 번에 최대 10개 쿼리로 제한돼 있어 `migrations/aiven_init.sql`을
4개 묶음(각 10/10/10/4개 문장)으로 나눠 순서대로 실행함.

- `information_schema.tables`로 확인 결과 `public` 스키마에 테이블 17개 전부 생성됨
- `settings` 테이블에 INSERT → SELECT → DELETE 스모크 테스트 통과

즉 `newsfinal-standby` 서비스는 스키마까지 준비된 상태로 스탠바이 대기 중.

## 데이터 이전 완료 (2026-09-24)

실제 데이터(17개 테이블 전부)를 로컬 PC에서 이전 완료함. 클라우드 세션은
raw TCP 접속이 안 되므로(위 "알려진 제약" 참고) 아래 전 과정을 사용자
로컬 PC(Windows, PowerShell)에서 진행함.

**소스**: Supabase 자동 백업(`db-backup.yml` 워크플로가 매일 만드는
`openssl enc -aes-256-cbc -pbkdf2 -salt` 암호화 덤프, 구글 드라이브에 보관)
중 `newsfinal-20260923.dump.enc`(pg_dump custom format, `--no-owner
--no-privileges`, 스키마+데이터 전체 — `auth`/`storage`/`realtime`/`vault`/
`cron` 등 Supabase 내부 스키마 포함). 복호화 비밀번호는 GitHub 시크릿
`BACKUP_PASSWORD`(로컬 `.env`에도 동일 값).

**절차 요약**:
1. `openssl enc -d -aes-256-cbc -pbkdf2`로 로컬 복호화
2. `pg_restore --list`로 덤프 내용 확인 → `public` 스키마 17개 테이블만
   대상으로 결정(Supabase 내부 스키마는 Aiven에 이식 대상 아님, 기존 방침대로)
3. Aiven 접속 계정(`avnadmin`)이 슈퍼유저가 아니라 `--disable-triggers` 사용
   불가(system trigger 권한 거부) → 옵션 없이 FK 의존순서(부모→자식)를
   pg_restore 자체 계산에 맡김
4. `articles`(22만 행 이상, `full_text` 등 큰 컬럼 포함)를 한 번에
   COPY하면 Aiven 무료 플랜(`free-1-1gb`, 1 vCPU/1GB RAM/1GB 디스크)에서
   연결이 끊김("terminating connection due to administrator command",
   "SSL 연결이 예상치 못하게 끊김") — 순간적으로 디스크 임계치에 닿아
   일시적 읽기전용 전환("cannot execute COPY FROM in a read-only
   transaction")까지 발생함. `articles`만 `pg_restore -t articles -f`로
   평문 SQL 추출 후, Python(`psycopg2`) 배치 스크립트로 2,000행씩 나눠
   커밋 + 실패 시 재시도하는 방식으로 우회함(스크립트는 저장소에 커밋하지
   않음, 1회성 로컬 스크립트).
5. 나머지 16개 테이블은 `pg_restore -t <table>`로 정상 처리(연결 끊김 없음).
6. `pg_get_serial_sequence` 기반으로 시퀀스 값(`articles_id_seq` 등)을
   `MAX(id)` 기준으로 재동기화 — Python 배치 스크립트는 COPY만 하고
   시퀀스는 안 건드리므로 별도 처리 필요했음.

**최종 건수** (2026-09-24 기준, Supabase 원본과 일치 — id 최댓값보다 낮은
건 `auto_dedup.py`/`cleanup_stale_raw.py` 등의 기존 정리 로직 때문으로
정상):

| 테이블 | 건수 |
|---|---|
| articles | 105,170 |
| view_snapshots | 398,189 |
| rss_raw_queue | 28,986 |
| rss_sources | 1,216 |
| entity_review | 2,331 |
| article_keywords | 2,204 |
| companies | 397 |
| gemini_usage_daily | 346 |
| prompts | 75 |
| article_revisions | 107 |
| article_translations | 80 |
| econ_events | 22 |
| opinet_price_history | 3 |
| settings | 2 |
| user_roles | 1 |
| stock_prices / travel_guides | 0 (원본도 비어있음) |

로컬에 남았던 평문 덤프(`newsfinal-20260923.dump`, `articles_data.sql`,
암호화 안 된 상태라 이메일 등 개인정보 포함)는 작업 완료 후 삭제 권고 —
`.enc` 원본만 보관.

**아직 안 한 것**: 앱(`scripts/db.py` 등)을 Aiven으로 전환하는 작업은
이번 범위 밖 — `scripts/db.py`는 원칙대로 건드리지 않았고 Supabase REST
API를 계속 그대로 씀. `newsfinal-standby`는 데이터까지 최신 상태로 갖춘
스탠바이로 대기 중.

### psql로 적용하는 경우 (참고, 터미널 있으면 더 간단함)

```bash
psql "$SERVICE_URI" -f migrations/aiven_init.sql
```

적용 후 스모크 테스트(예시):
```sql
insert into settings (key, value) values ('_smoke_test', 'ok');
select * from settings where key = '_smoke_test';
delete from settings where key = '_smoke_test';
```

## 참고: Supabase 쪽 별개 보안 권고 (이번 작업과 무관, 처리 완료)

Supabase MCP 어드바이저가 `public.travel_guides` 테이블의 RLS(Row Level
Security)가 꺼져 있다고 표시했었음 — anon/authenticated 키로 전체 행
읽기/쓰기가 가능한 상태였음. 이 프로젝트의 `econ_events`/`stock_prices`에
이미 쓰이던 "공개 읽기 + service_role만 쓰기" 패턴을 그대로 적용해 처리함
(2026-09-24):

```sql
ALTER TABLE public.travel_guides ENABLE ROW LEVEL SECURITY;

CREATE POLICY travel_guides_public_read ON public.travel_guides
FOR SELECT
USING (true);

CREATE POLICY travel_guides_service_all ON public.travel_guides
FOR ALL
USING (auth.role() = 'service_role')
WITH CHECK (auth.role() = 'service_role');
```

적용 후 보안 어드바이저 재확인 결과 `travel_guides` 관련 `rls_disabled`
항목은 사라짐. Aiven 쪽 `migrations/aiven_init.sql`에는 이 정책을 옮기지
않음 — Aiven은 PostgREST/anon 키 접근 모델 자체가 없어(순수 Postgres),
RLS 정책이 적용될 대상이 없음.
