-- wise-frontier(뉴스파이널) CockroachDB 스탠바이 스키마
-- Supabase(Postgres) 원본 스키마(fotdngseksqaghvtcvqh, 2026-09-24 기준 실측)를
-- CockroachDB Basic(Postgres wire-compatible)용으로 이식. D1/Turso(SQLite)와 달리
-- 같은 Postgres 계열이라 타입 변환이 거의 없음 - boolean/jsonb/array/uuid 전부 네이티브 지원.
--
-- 미검증 상태임을 명시: 이 SQL은 실제 CockroachDB 클러스터에 한 번도 적용해보지
-- 않았다(계정 미가입). CockroachDB가 일부 Postgres 확장 문법(특정 시퀀스 옵션 등)을
-- 다르게 처리할 수 있으므로, 실제 전환 시 먼저 이 파일을 그대로 적용해보고 에러가
-- 나는 줄만 스팟 수정할 것 - "테스트 없이 바로 프로덕션 전환 가능"이 아님.

CREATE TABLE articles (
  id BIGSERIAL PRIMARY KEY,
  title_en TEXT NOT NULL DEFAULT '',
  title_ko TEXT,
  summary_en TEXT,
  summary_ko TEXT,
  url TEXT NOT NULL UNIQUE,
  source TEXT,
  category TEXT,
  subcategory TEXT,
  region TEXT,
  country TEXT,
  country_flag TEXT,
  score INTEGER DEFAULT 0,
  created_at TEXT,
  sent_telegram INTEGER DEFAULT 0,
  posted_blog INTEGER DEFAULT 0,
  full_text TEXT,
  countries TEXT[],
  is_published BOOLEAN DEFAULT false,
  image_url TEXT,
  view_count INTEGER DEFAULT 0,
  first_published_at TIMESTAMP,
  update_log JSONB DEFAULT '[]',
  byline TEXT DEFAULT '뉴스파이널 편집국',
  company_scanned BOOLEAN DEFAULT false,
  dedup_reviewed BOOLEAN DEFAULT false,
  is_travel BOOLEAN DEFAULT false,
  summary_3lines TEXT,
  investment_idea TEXT,
  source_published_at TIMESTAMPTZ,
  source_data JSONB,
  image_credit TEXT,
  continuation_of_id BIGINT,
  summary_3lines_en TEXT,
  investment_idea_en TEXT,
  noindex BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE article_keywords (
  article_id BIGINT PRIMARY KEY,
  keyword_ko TEXT,
  keyword_en TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE article_revisions (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT NOT NULL,
  op TEXT NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  changed_columns TEXT[] NOT NULL,
  old_values JSONB NOT NULL,
  note TEXT,
  actor TEXT
);

CREATE TABLE article_translations (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT NOT NULL,
  lang TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (article_id, lang)
);

CREATE TABLE companies (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  name_ko TEXT,
  country TEXT NOT NULL,
  country_flag TEXT,
  exchange TEXT,
  ticker TEXT,
  sector TEXT,
  description TEXT,
  founded_year INTEGER,
  headquarters TEXT,
  website TEXT,
  source_url TEXT,
  is_published BOOLEAN DEFAULT true,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE econ_events (
  id SERIAL PRIMARY KEY,
  event_date DATE NOT NULL,
  event_time TEXT,
  country TEXT,
  country_flag TEXT,
  title TEXT NOT NULL,
  description TEXT,
  importance TEXT DEFAULT 'medium',
  previous_value TEXT,
  forecast_value TEXT,
  actual_value TEXT,
  source_url TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  is_verified BOOLEAN DEFAULT true,
  source TEXT DEFAULT 'manual',
  timezone TEXT,
  announcement_offset_hours INTEGER DEFAULT 0
);

CREATE TABLE entity_review (
  id BIGINT PRIMARY KEY,
  article_id BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'clean',
  suspect_names TEXT,
  checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE gemini_usage_daily (
  date DATE NOT NULL,
  model TEXT NOT NULL,
  key_index INTEGER NOT NULL,
  success_calls INTEGER NOT NULL DEFAULT 0,
  error_429 INTEGER NOT NULL DEFAULT 0,
  error_503 INTEGER NOT NULL DEFAULT 0,
  error_other INTEGER NOT NULL DEFAULT 0,
  prompt_tokens BIGINT NOT NULL DEFAULT 0,
  completion_tokens BIGINT NOT NULL DEFAULT 0,
  total_tokens BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (date, model, key_index)
);

CREATE TABLE opinet_price_history (
  id BIGINT PRIMARY KEY,
  price_date DATE NOT NULL,
  prodcd TEXT NOT NULL,
  price NUMERIC NOT NULL,
  diff NUMERIC,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (price_date, prodcd)
);

CREATE TABLE prompts (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  content TEXT NOT NULL,
  version INTEGER DEFAULT 1,
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE rss_raw_queue (
  id BIGSERIAL PRIMARY KEY,
  link TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  source_name TEXT NOT NULL,
  category TEXT,
  subcategory TEXT,
  summary_en TEXT,
  source_published_at TIMESTAMPTZ,
  raw_tags JSONB,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed BOOLEAN NOT NULL DEFAULT false,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE INDEX idx_rss_raw_queue_processed ON rss_raw_queue(processed, fetched_at, id);

CREATE TABLE rss_sources (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  subcategory TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now(),
  consecutive_fails INTEGER NOT NULL DEFAULT 0,
  total_ok BIGINT NOT NULL DEFAULT 0,
  total_fail BIGINT NOT NULL DEFAULT 0,
  last_ok_at TIMESTAMPTZ,
  last_checked_at TIMESTAMPTZ,
  deactivated_reason TEXT
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE stock_prices (
  id BIGSERIAL PRIMARY KEY,
  stock_code TEXT NOT NULL,
  stock_name TEXT,
  base_date TEXT NOT NULL,
  close_price NUMERIC,
  open_price NUMERIC,
  high_price NUMERIC,
  low_price NUMERIC,
  vs NUMERIC,
  flt_rt NUMERIC,
  volume BIGINT,
  trade_amount BIGINT,
  market_cap BIGINT,
  shares_out BIGINT,
  market_type TEXT NOT NULL,
  fetched_at TIMESTAMPTZ,
  UNIQUE (stock_code, base_date, market_type)
);

CREATE TABLE travel_guides (
  id BIGSERIAL PRIMARY KEY,
  country TEXT NOT NULL,
  guide_type TEXT NOT NULL,
  content_ko TEXT,
  raw_data JSONB,
  source TEXT,
  updated_at TIMESTAMP DEFAULT now(),
  UNIQUE (country, guide_type)
);

CREATE TABLE user_roles (
  id UUID PRIMARY KEY,
  email TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'reporter',
  name TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE view_snapshots (
  article_id BIGINT NOT NULL,
  snap_date DATE NOT NULL,
  view_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (article_id, snap_date)
);
