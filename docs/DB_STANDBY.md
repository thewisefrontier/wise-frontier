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
아직 실제 데이터(268MB) 이전은 하지 않음 — 필요 시점에 별도로 진행.

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
