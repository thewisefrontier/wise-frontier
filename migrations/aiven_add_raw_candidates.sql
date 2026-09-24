-- 2026-09-24: Aiven 스탠바이에 raw_candidates 스키마 추가(증분 마이그레이션).
-- migrations/aiven_init.sql이 최초 적용 완료된 뒤에 신설된 테이블이라 별도 적용.
-- 데이터는 미러링하지 않는다 — 스키마만 맞춘다(docs/DB_STANDBY.md 참고).

CREATE TABLE IF NOT EXISTS raw_candidates (
    id                  BIGSERIAL PRIMARY KEY,
    url                 text NOT NULL UNIQUE,
    title_en            text,
    title_ko            text,
    summary_en          text,
    summary_ko          text,
    full_text           text,
    source              text NOT NULL,
    category            text,
    subcategory         text,
    region              text,
    country             text,
    score               integer DEFAULT 0,
    created_at          text NOT NULL,
    sent_telegram       integer DEFAULT 0,
    source_published_at text,
    source_data         jsonb,
    image_url           text,
    image_credit        text
);
CREATE INDEX IF NOT EXISTS raw_candidates_created_idx ON raw_candidates (created_at);
CREATE INDEX IF NOT EXISTS raw_candidates_source_idx ON raw_candidates (source);
CREATE INDEX IF NOT EXISTS raw_candidates_sent_telegram_idx ON raw_candidates (sent_telegram, created_at);
