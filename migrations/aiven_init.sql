-- 뉴스파이널(wise-frontier) Aiven Postgres 스탠바이용 스키마
-- Supabase 프로젝트 fotdngseksqaghvtcvqh(뉴스파이널)의 public 스키마를
-- 2026-09-24 기준 information_schema/pg_catalog 조회 결과로 그대로 옮긴 것.
-- CockroachDB용이 아닌 표준 Postgres(Aiven free-1-1gb, pg 18)용.
--
-- Supabase 전용 요소로 이식하지 않은 것:
--   - user_roles.id -> auth.users(id) FK: Supabase Auth 스키마가 없어 제거.
--     (Aiven에는 자체 인증 시스템을 두지 않는 이상 이 FK를 걸 수 없음)
--   - RLS(Row Level Security) 정책: PostgREST/Supabase 클라이언트 접근 모델
--     전용이라 이식 대상 아님. public.travel_guides는 Supabase 쪽에서도
--     RLS가 꺼져 있다는 보안 권고가 있었음(이 마이그레이션과 무관, 별도 확인 필요).
--   - pg_cron, supabase_vault 등 Supabase 관리형 확장: Aiven 무료 플랜에서
--     지원 여부 다르고 이 스키마 자체에서 쓰지 않으므로 제외.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- =========================================================
-- articles (부모 테이블 — 다른 테이블들이 FK로 참조)
-- =========================================================
CREATE TABLE articles (
    id                  BIGSERIAL PRIMARY KEY,
    title_en            text NOT NULL DEFAULT '',
    title_ko            text,
    summary_en          text,
    summary_ko          text,
    url                 text NOT NULL UNIQUE,
    source              text,
    category            text,
    subcategory         text,
    region              text,
    country             text,
    country_flag        text,
    score               integer DEFAULT 0,
    created_at          text,
    sent_telegram       integer DEFAULT 0,
    posted_blog         integer DEFAULT 0,
    full_text           text,
    countries           text[],
    is_published        boolean DEFAULT false,
    image_url           text,
    view_count          integer DEFAULT 0,
    first_published_at  timestamp without time zone,
    update_log          jsonb DEFAULT '[]'::jsonb,
    byline              text DEFAULT '뉴스파이널 편집국',
    company_scanned     boolean DEFAULT false,
    dedup_reviewed      boolean DEFAULT false,
    is_travel           boolean DEFAULT false,
    summary_3lines      text,
    investment_idea     text,
    source_published_at timestamptz,
    source_data         jsonb,
    image_credit        text,
    continuation_of_id  bigint,
    summary_3lines_en   text,
    investment_idea_en  text,
    noindex             boolean NOT NULL DEFAULT false
);

CREATE INDEX articles_created_idx ON articles USING btree (created_at);
CREATE INDEX articles_pub_created_idx ON articles USING btree (created_at DESC)
    WHERE (is_published AND (source = 'NewsFinal'));
CREATE INDEX articles_pub_noindex_created_idx ON articles USING btree (is_published, noindex, created_at DESC);
CREATE INDEX articles_source_created_idx ON articles USING btree (source, created_at DESC);
CREATE INDEX articles_source_subcategory_idx ON articles USING btree (source, subcategory);

-- =========================================================
-- articles 자식 테이블
-- =========================================================
CREATE TABLE article_keywords (
    article_id  bigint PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    keyword_ko  text,
    keyword_en  text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 원본 Supabase에도 articles에 대한 FK가 걸려 있지 않음(삭제된 글의 이력도 보존하려는 의도로 추정)
CREATE TABLE article_revisions (
    id               BIGSERIAL PRIMARY KEY,
    article_id       bigint NOT NULL,
    op               text NOT NULL CHECK (op = ANY (ARRAY['UPDATE'::text, 'DELETE'::text])),
    changed_at       timestamptz NOT NULL DEFAULT now(),
    changed_columns  text[] NOT NULL,
    old_values       jsonb NOT NULL,
    note             text,
    actor            text
);
CREATE INDEX article_revisions_article_idx ON article_revisions USING btree (article_id, changed_at DESC);

CREATE TABLE article_translations (
    id          BIGSERIAL PRIMARY KEY,
    article_id  bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    lang        text NOT NULL,
    title       text NOT NULL,
    summary     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (article_id, lang)
);
CREATE INDEX idx_article_translations_lang ON article_translations USING btree (lang, article_id);

CREATE TABLE entity_review (
    id             bigint NOT NULL PRIMARY KEY,
    article_id     bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    status         text NOT NULL DEFAULT 'clean',
    suspect_names  text,
    checked_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entity_review_article_id_idx ON entity_review USING btree (article_id);
CREATE INDEX entity_review_status_idx ON entity_review USING btree (status);

CREATE TABLE view_snapshots (
    article_id  bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    snap_date   date NOT NULL,
    view_count  integer NOT NULL DEFAULT 0,
    PRIMARY KEY (article_id, snap_date)
);
CREATE INDEX idx_view_snapshots_date ON view_snapshots USING btree (snap_date);

-- =========================================================
-- 독립 테이블
-- =========================================================
CREATE TABLE companies (
    id           text PRIMARY KEY,
    name         text NOT NULL,
    name_ko      text,
    country      text NOT NULL,
    country_flag text,
    exchange     text,
    ticker       text,
    sector       text,
    description  text,
    founded_year integer,
    headquarters text,
    website      text,
    source_url   text,
    is_published boolean DEFAULT true,
    created_at   text,
    updated_at   text
);

CREATE TABLE econ_events (
    id                          SERIAL PRIMARY KEY,
    event_date                  date NOT NULL,
    event_time                  text,
    country                     text,
    country_flag                text,
    title                       text NOT NULL,
    description                 text,
    importance                  text DEFAULT 'medium',
    previous_value              text,
    forecast_value              text,
    actual_value                text,
    source_url                  text,
    created_at                  timestamptz DEFAULT now(),
    is_verified                 boolean DEFAULT true,
    source                      text DEFAULT 'manual',
    timezone                    text,
    announcement_offset_hours   integer DEFAULT 0
);
CREATE INDEX idx_econ_events_date ON econ_events USING btree (event_date);

CREATE TABLE gemini_usage_daily (
    date               date NOT NULL,
    model              text NOT NULL,
    key_index          integer NOT NULL,
    success_calls      integer NOT NULL DEFAULT 0,
    error_429          integer NOT NULL DEFAULT 0,
    error_503          integer NOT NULL DEFAULT 0,
    error_other        integer NOT NULL DEFAULT 0,
    prompt_tokens      bigint NOT NULL DEFAULT 0,
    completion_tokens  bigint NOT NULL DEFAULT 0,
    total_tokens       bigint NOT NULL DEFAULT 0,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (date, model, key_index)
);

-- 원본에도 id 컬럼에 시퀀스 기본값이 없음(수집 스크립트가 값을 직접 채워 넣음)
CREATE TABLE opinet_price_history (
    id          bigint NOT NULL PRIMARY KEY,
    price_date  date NOT NULL,
    prodcd      text NOT NULL,
    price       numeric NOT NULL,
    diff        numeric,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (price_date, prodcd)
);
CREATE INDEX opinet_price_history_date_idx ON opinet_price_history USING btree (price_date);

CREATE TABLE prompts (
    id          SERIAL PRIMARY KEY,
    name        text NOT NULL,
    content     text NOT NULL,
    version     integer DEFAULT 1,
    is_active   boolean DEFAULT true,
    created_at  timestamptz DEFAULT now()
);

CREATE TABLE rss_raw_queue (
    id                    BIGSERIAL PRIMARY KEY,
    link                  text NOT NULL UNIQUE,
    title                 text NOT NULL,
    source_name           text NOT NULL,
    category              text,
    subcategory           text,
    summary_en            text,
    source_published_at   timestamptz,
    raw_tags              jsonb,
    fetched_at            timestamptz NOT NULL DEFAULT now(),
    processed             boolean NOT NULL DEFAULT false,
    attempts              integer NOT NULL DEFAULT 0,
    last_error            text
);
CREATE INDEX rss_raw_queue_pending_idx ON rss_raw_queue USING btree (fetched_at) WHERE (NOT processed);

CREATE TABLE rss_sources (
    id                   SERIAL PRIMARY KEY,
    name                 text NOT NULL,
    category             text NOT NULL,
    subcategory          text NOT NULL,
    url                  text NOT NULL UNIQUE,
    is_active            boolean DEFAULT true,
    created_at           timestamptz DEFAULT now(),
    consecutive_fails    integer NOT NULL DEFAULT 0,
    total_ok             bigint NOT NULL DEFAULT 0,
    total_fail           bigint NOT NULL DEFAULT 0,
    last_ok_at           timestamptz,
    last_checked_at      timestamptz,
    deactivated_reason   text
);
CREATE INDEX rss_sources_active_idx ON rss_sources USING btree (is_active);

CREATE TABLE settings (
    key         text PRIMARY KEY,
    value       text NOT NULL,
    created_at  timestamptz DEFAULT now()
);

CREATE TABLE stock_prices (
    id            BIGSERIAL PRIMARY KEY,
    stock_code    text NOT NULL,
    stock_name    text,
    base_date     text NOT NULL,
    close_price   numeric,
    open_price    numeric,
    high_price    numeric,
    low_price     numeric,
    vs            numeric,
    flt_rt        numeric,
    volume        bigint,
    trade_amount  bigint,
    market_cap    bigint,
    shares_out    bigint,
    market_type   text NOT NULL,
    fetched_at    timestamptz,
    UNIQUE (stock_code, base_date, market_type)
);

-- 원본 Supabase에서 RLS가 꺼져 있던 테이블(보안 권고 대상, 이 마이그레이션과는 무관)
CREATE TABLE travel_guides (
    id          BIGSERIAL PRIMARY KEY,
    country     text NOT NULL,
    guide_type  text NOT NULL,
    content_ko  text,
    raw_data    jsonb,
    source      text,
    updated_at  timestamp without time zone DEFAULT now(),
    UNIQUE (country, guide_type)
);

-- 원본에서는 id가 auth.users(id)를 참조하는 FK였으나, Aiven에는 Supabase Auth
-- 스키마가 없어 FK 없이 UUID만 유지. 값 채우는 주체(직접 지정)가 바뀌어야 함.
CREATE TABLE user_roles (
    id          uuid PRIMARY KEY,
    email       text NOT NULL,
    role        text NOT NULL DEFAULT 'reporter',
    name        text,
    created_at  timestamptz DEFAULT now()
);
