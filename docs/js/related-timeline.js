// 기사 하단 "이 사안의 흐름" 타임라인(2026-09-22 신설).
// 같은 사안(예: 사헬 쿠데타)의 이전·이후 발행 기사를 날짜순으로 보여줘 독자가 맥락을 따라가고,
// 크롤러에는 기사 사이 내부 링크가 SSR HTML로 노출된다.
// 관련 기사 판정은 DB RPC related_articles()가 한다(제목 핵심어 겹침, 최근 60일).
// docs/article.html(클라)과 functions/article.js(엣지 SSR) 양쪽이 같은 모듈을 쓴다 —
// 두 곳이 각자 갖고 있다가 어긋난 전례(update-log-filter.js, image-credit.js)를 반복하지 않기 위함.

export async function fetchRelatedTimeline(supabaseUrl, anonKey, id, limit = 6) {
  try {
    const res = await fetch(`${supabaseUrl}/rest/v1/rpc/related_articles`, {
      method: 'POST',
      headers: { apikey: anonKey, 'Content-Type': 'application/json' },
      body: JSON.stringify({ p_id: Number(id), p_limit: limit }),
    });
    if (!res.ok) return [];
    const rows = await res.json();
    return Array.isArray(rows) ? rows : [];
  } catch (e) {
    return []; // 흐름 표시는 부가 기능 — 실패해도 기사 렌더를 막지 않는다
  }
}

function dayLabel(createdAt) {
  const m = String(createdAt || '').match(/(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${Number(m[2])}월 ${Number(m[3])}일` : '';
}

// rows: related_articles() 결과, current: {id, title_ko, created_at}(지금 보는 기사 — 흐름 속 위치 표시용)
export function relatedTimelineHtml(rows, esc, current) {
  if (!Array.isArray(rows) || rows.length === 0) return '';
  const isRecap = !!current && current.subcategory === '주간정리';
  const items = rows.map((r) => ({ id: r.id, title: r.title_ko, at: r.created_at, here: false }));
  if (current && !isRecap) items.push({ id: current.id, title: current.title_ko || current.title_en, at: current.created_at, here: true });
  // 사안 흐름은 오래된 것 → 최신, 주간 정리의 "이번 주 다룬 기사"는 최신순
  items.sort((a, b) => isRecap ? String(b.at).localeCompare(String(a.at)) : String(a.at).localeCompare(String(b.at)));
  const li = items.map((it) => `
      <li style="padding:7px 0 7px 14px;position:relative;border-left:2px solid ${it.here ? 'var(--accent2)' : 'var(--border)'};">
        <span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted);display:block;">${esc(dayLabel(it.at))}${it.here ? ' · 지금 보는 기사' : ''}</span>
        ${it.here
          ? `<span style="font-size:14px;font-weight:700;color:var(--text);">${esc(it.title)}</span>`
          : `<a href="/article?id=${it.id}" style="font-size:14px;color:var(--text);text-decoration:none;">${esc(it.title)}</a>`}
      </li>`).join('');
  return `
      <section class="issue-timeline" style="margin-top:40px;padding:16px 18px;border:1px solid var(--border);border-radius:8px;">
        <div class="related-title" style="margin-bottom:10px;">${isRecap ? '이번 주 다룬 기사' : '이 사안의 흐름'}</div>
        <ol style="list-style:none;margin:0;padding:0;">${li}
        </ol>
      </section>`;
}
