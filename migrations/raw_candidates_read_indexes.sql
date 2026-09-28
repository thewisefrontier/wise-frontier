-- 2026-09-29: raw_candidates 읽기 인덱스 (Supabase fotdngseksqaghvtcvqh에 적용 완료,
-- migration명 raw_candidates_read_indexes). Aiven 스탠바이에는 아직 미적용 — 스키마를
-- 맞출 때 이 파일을 그대로 실행한다(docs/DB_STANDBY.md 참고).
--
-- 실측(EXPLAIN ANALYZE, 11만 행):
-- 1) gemini_summarizer.py: created_at >= ? AND summary_ko IS NULL ORDER BY created_at DESC
--    → created_at 인덱스로 최근 36시간 4.4만 행을 읽고 전부 버림, 4.5초.
--    요약 안 된 행만 담는 부분 인덱스로 0.6ms (평소 거의 빈 인덱스라 쓰기 부담도 없음).
-- 2) domestic_kr_writer.py: source LIKE 'DomesticKR:%' / gemini_writer.py: source IN (...)
--    + created_at >= ? ORDER BY created_at DESC → 2.2만 행 필터, 17ms → 1ms.
--    DB 콜레이션이 en_US.UTF-8이라 접두사 LIKE는 text_pattern_ops여야 인덱스를 탄다.
--    기존 (source) 단일 인덱스는 이 인덱스가 대체하므로 삭제(쓰기 인덱스 수 유지).

CREATE INDEX IF NOT EXISTS raw_candidates_unsummarized_idx
    ON raw_candidates (created_at DESC) WHERE summary_ko IS NULL;
CREATE INDEX IF NOT EXISTS raw_candidates_source_pattern_created_idx
    ON raw_candidates (source text_pattern_ops, created_at DESC);
DROP INDEX IF EXISTS raw_candidates_source_idx;
