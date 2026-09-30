CREATE TABLE IF NOT EXISTS prompts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  content TEXT NOT NULL,
  version INTEGER DEFAULT 1,
  is_active INTEGER DEFAULT 1,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompts_name_active ON prompts(name, is_active);

CREATE TABLE IF NOT EXISTS rss_sources (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  subcategory TEXT NOT NULL,
  url TEXT NOT NULL,
  is_active INTEGER DEFAULT 1,
  created_at TEXT,
  consecutive_fails INTEGER DEFAULT 0,
  total_ok INTEGER DEFAULT 0,
  total_fail INTEGER DEFAULT 0,
  last_ok_at TEXT,
  last_checked_at TEXT,
  deactivated_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_rss_sources_active ON rss_sources(is_active);
