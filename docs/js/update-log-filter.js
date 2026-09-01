// docs/js/update-log-filter.js
// 기사 "업데이트 기록" 공개 필터 — 단일 소스.
//
// docs/article.html, docs/live.html, functions/article.js 세 곳이 각각 이
// 로직을 복붙해 갖고 있다가, 2026-08-09에 화이트리스트(특정 문구만 통과)를
// 블랙리스트(내부 전용 문구만 제외)로 뒤집는 수정이 2곳에만 반영되고 나머지
// 2곳(docs/article.html, docs/live.html)은 누락돼 2026-09-01에 재발했다
// (id=118417 "업데이트 기록"이 안 붙는 것처럼 보이던 사고). 그래서 ES 모듈로
// 합쳐 세 곳이 전부 이 파일 하나를 import하도록 함 — 이제 규칙을 고치려면
// 여기 한 곳만 고치면 된다.
//
// scripts/export_articles.py의 sanitize_update_log()는 Python이라 이 파일을
// import할 수 없어 별도로 유지된다 — INTERNAL_ONLY_NOTE_RE를 고치면 그쪽도
// 반드시 같이 고칠 것(자세한 사고 이력은 메모리 newsfinal_update_log_display_drift 참조).
//
// 내부 운영 기록(트렌드 감지 경로, 자동 중복정리, 표기·문체 교정, 복수주제
// 파킹, 관리자 수동 정리 사유 등)은 독자에게 노출하지 않는다. 첫 항목은
// '최초 게시', 이후는 이 블랙리스트에 안 걸리는 것만 '내용 업데이트'로
// 일반화해 표시한다. 원문 전체는 admin.html 기사 편집 화면에서 확인한다.

export const INTERNAL_ONLY_NOTE_RE = /실시간 트렌드 감지|자동 중복정리|음역 자동 교정|문자셋 이탈|복수주제 분리 파킹|수동 정리/;

export function publicUpdateLog(log) {
  if (!Array.isArray(log)) return [];
  const out = [];
  for (let i = 0; i < log.length; i++) {
    const l = log[i] || {};
    if (i === 0) out.push({ timestamp: l.timestamp, note: '최초 게시' });
    else if (!INTERNAL_ONLY_NOTE_RE.test(String(l.note || ''))) out.push({ timestamp: l.timestamp, note: '내용 업데이트' });
  }
  return out;
}
