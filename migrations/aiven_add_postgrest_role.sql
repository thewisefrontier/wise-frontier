-- 2026-10-04: Aiven을 PostgREST로 노출할 때 쓸 최소권한 역할.
-- 목적: AIVEN_SERVICE_URI의 연결 계정(avnadmin 등, 전체 권한)을 그대로
-- PostgREST에 노출하지 않고, articles/raw_candidates 두 테이블에만 권한을
-- 좁힌 전용 역할을 거치게 한다(이 작업 범위 — docs/scheduled task 참고).
-- NOLOGIN이라 비밀번호가 없다 — PGRST_DB_URI는 여전히 기존 연결 계정을 쓰고,
-- PostgREST가 JWT role claim으로 이 역할로 SET ROLE 전환한다.
-- 멱등: 이미 있으면 조용히 넘어간다.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgrest_svc') THEN
        CREATE ROLE postgrest_svc NOLOGIN;
    END IF;
    -- 토큰 없는(또는 role claim 없는) 요청이 떨어지는 익명 역할 — 권한을
    -- 일절 안 줘서 유효한 JWT 없이는 아무 테이블도 못 보게 한다.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgrest_svc_anon') THEN
        CREATE ROLE postgrest_svc_anon NOLOGIN;
    END IF;
END $$;

-- PGRST_DB_URI 연결 계정이 SET ROLE postgrest_svc(_anon) 할 수 있어야 한다.
-- current_user는 one_off_task.yml 실행 시 AIVEN_SERVICE_URI의 연결 계정.
DO $$
BEGIN
    EXECUTE format('GRANT postgrest_svc TO %I', current_user);
    EXECUTE format('GRANT postgrest_svc_anon TO %I', current_user);
END $$;

GRANT USAGE ON SCHEMA public TO postgrest_svc;

GRANT SELECT, INSERT, UPDATE, DELETE ON articles, raw_candidates TO postgrest_svc;
GRANT USAGE, SELECT ON articles_id_seq, raw_candidates_id_seq TO postgrest_svc;
