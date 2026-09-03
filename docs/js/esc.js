// HTML 이스케이프 공용 함수(2026-09-03 보안 점검 계기 신설).
// functions/article.js(크롤러용 SSR 경로)에는 esc()가 있었지만, 실제 방문자가
// 보는 클라이언트 렌더링 경로(docs/*.html)에는 이스케이프 함수 자체가 전혀
// 없어 DB 필드(제목·국가·바이라인·요약 등)가 이스케이프 없이 그대로
// innerHTML에 들어가고 있었다 — XSS. docs/js/update-log-filter.js·
// image-credit.js와 같은 이유로 공용화해 두 경로가 같은 함수를 쓰게 한다.
// href/src에 넣기 전에 스킴을 검증한다 — HTML 이스케이프만으로는 javascript:
// 스킴 자체를 못 막는다(속성값은 안전해져도 브라우저가 그대로 실행). RSS
// 원문 기사 url처럼 외부에서 온 URL을 href/window.open에 쓰는 곳에 적용.
export function safeUrl(u) {
  const s = String(u || '').trim();
  if (/^(https?:|\/|internal:\/\/)/i.test(s)) return s;
  return '#';
}

export function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
