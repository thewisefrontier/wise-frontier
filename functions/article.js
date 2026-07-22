// functions/article.js — Cloudflare Pages Function (엣지 SSR)
// /article?id=X 요청을 엣지에서 렌더해 "완성 HTML"을 반환한다.
// 목적: 크롤러가 빈 CSR 셸 대신 본문·SEO 태그가 채워진 HTML을 보게 하여
//       Soft 404 원인을 제거한다. (URL /article?id=X 유지, 마이그레이션 없음)
//
// 방식: article.html 정적 셸을 env.ASSETS로 가져와 HTMLRewriter로
//       <head> SEO 태그와 #article-wrapper 본문만 주입한다.
//       - 크롤러: JS 미실행 → 주입된 SSR 본문/메타를 그대로 색인.
//       - 사용자: 기존 클라 JS(loadArticle/renderArticle)가 재렌더 →
//         조회수 증가·관련기사·티커 등 인터랙션 그대로 작동.
//       클라가 wrapper를 덮어쓰므로 SSR과 클라 결과는 거의 동일하며,
//       별도 이중렌더 가드는 불필요(가드를 넣으면 위 인터랙션이 스킵됨).
//
// 데이터: article.html 상단 상수와 동일한 anon(공개 읽기전용) 값 재사용.
//         원하면 Pages 환경변수로 분리 가능(env.SUPABASE_URL 등).

const SUPABASE_URL = 'https://fotdngseksqaghvtcvqh.supabase.co';
const SUPABASE_ANON_KEY = 'sb_publishable_rT3kSEAAdM0DJlDW5fEWww_M37h6dDW';
const SITE = 'https://newsfinal.co.kr';

const REGION_KO = {
  'africa': '아프리카', 'southeast_asia': '동남아시아',
  'eastern_europe': '동유럽', 'middle_east': '중동',
  'central_asia': '중앙아시아', 'south_asia': '남아시아',
  'latin_america': '라틴아메리카', 'caribbean': '카리브해',
  'oceania': '오세아니아', 'global': '글로벌',
  'east_africa': '동아프리카', 'west_africa': '서아프리카',
  'north_africa': '북아프리카', 'southern_africa': '남아프리카',
};
const regionKo = (r) => REGION_KO[r] || r || '';

// article.html의 cleanBody()와 동일 (마크다운 잔여·프로모 문구 제거)
function cleanBody(text) {
  if (!text) return '';
  text = text
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/\*(.+?)\*/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/\[.{1,20}=.{1,30}\]/g, '')
    .replace(/^\[.{1,30}\]\s*/gm, '');
  const promoPatterns = [
    /[^.]*텔레그램 채널[^.]*\./g,
    /[^.]*구독[^.]*프리미엄[^.]*\./g,
    /[^.]*ET Prime[^.]*\./g,
    /[^.]*뉴스레터[^.]*구독[^.]*\./g,
    /[^.]*앱[^.]*다운로드[^.]*\./g,
    /[^.]*더 많은 정보[^.]*방문[^.]*\./g,
  ];
  promoPatterns.forEach((p) => { text = text.replace(p, ''); });
  return text.trim();
}

// 표시용 텍스트 이스케이프(속성/텍스트 노드 공용)
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// created_at은 KST text("YYYY-MM-DD HH:MM"). 타임존 오차 없이 문자열 파싱.
function formatDateKST(str) {
  const m = String(str || '').match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return esc(str || '');
  return `${m[1]}년 ${Number(m[2])}월 ${Number(m[3])}일 ${m[4]}:${m[5]}`;
}
function formatUpdateTimeKST(str) {
  const m = String(str || '').match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return esc(String(str || '').slice(-11));
  return `${m[2]}/${m[3]} ${m[4]}:${m[5]}`;
}
function toIsoKST(str) {
  const m = String(str || '').match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  return m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:00+09:00` : '';
}

// article.html renderArticle()의 본문 처리(관련기사 제외)를 서버에서 재현.
// 관련기사 내부링크는 sitemap 전체등록이 발견을 담당하고, 사용자 화면엔
// 클라 renderArticle이 채우므로 SSR에서는 생략(함수 단순화·DB 쿼리 1건 유지).
function buildWrapperHtml(a) {
  const title = a.title_ko || a.title_en || '';
  const body = cleanBody(a.summary_ko || a.summary_en || '');

  // "- " 시작 줄을 <ul><li>로 묶기 (renderArticle과 동일)
  // \n\n(단락 구분)을 먼저 플레이스홀더로 치환해 split('\n') 시 소실 방지
  const bodyWithPlaceholder = body.replace(/\n\n/g, '\x00');
  const bulletified = bodyWithPlaceholder.split('\n').reduce((acc, line) => {
    const trimmed = line.trim();
    if (trimmed.startsWith('- ')) {
      if (!acc.inList) { acc.html += '<ul>'; acc.inList = true; }
      acc.html += `<li>${trimmed.slice(2)}</li>`;
    } else {
      if (acc.inList) { acc.html += '</ul>'; acc.inList = false; }
      acc.html += line + '\n';
    }
    return acc;
  }, { html: '', inList: false });
  let processedBody = bulletified.html + (bulletified.inList ? '</ul>' : '');

  const bodyHtml = processedBody
    .replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g, '<img src="$2" alt="$1" loading="lazy" style="max-width:100%;border-radius:6px;margin:8px 0;display:block;">')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\x00/g, '</p><p>')
    .replace(/\n/g, '<br>')
    .replace(/<br>(<ul>)/g, '$1')
    .replace(/(<\/ul>)<br>/g, '$1');

  const isTrend = a.source === 'NewsFinal'
    && ['trend_', 'realtrend_', 'extrend_'].some((p) => (a.subcategory || '').startsWith(p));

  const showSub = a.subcategory && a.subcategory !== a.category
    && !a.subcategory.startsWith('cluster_')
    && !a.subcategory.startsWith('solo_')
    && !a.subcategory.startsWith('digest_')
    && !a.subcategory.startsWith('trend_')
    && !a.subcategory.startsWith('realtrend_')
    && !a.subcategory.startsWith('extrend_')
    && !a.subcategory.endsWith('briefing');

  const countries = Array.isArray(a.countries) ? a.countries : [];

  const tagsHtml = `
      <div class="article-tags">
        ${a.category ? `<a class="tag" href="/?category=${encodeURIComponent(a.category)}">${esc(a.category)}</a>` : ''}
        ${showSub ? `<span class="tag">${esc(a.subcategory)}</span>` : ''}
        ${a.region ? `<span class="tag">${esc(regionKo(a.region))}</span>` : ''}
        ${countries.length > 0
          ? countries.map((c) => `<a class="tag" href="/?country=${encodeURIComponent(c)}">${esc(c)}</a>`).join('')
          : (a.country ? `<a class="tag" href="/?country=${encodeURIComponent(a.country)}">${esc(a.country)}</a>` : '')}
      </div>`;

  const metaHtml = `
      <div class="article-meta">
        ${a.first_published_at && a.first_published_at !== a.created_at
          ? `<span>🕐 최초 게시: ${formatDateKST(a.first_published_at)}</span><span>🔄 최종 업데이트: ${formatDateKST(a.created_at)}</span>`
          : `<span>🕐 ${formatDateKST(a.created_at)}</span>`}
        <span>${esc(a.byline || '뉴스파이널 편집국')}</span>
        ${a.region ? `<span>🌍 ${esc(regionKo(a.region))}</span>` : ''}
        ${a.country ? `<span>📍 ${esc(a.country)}</span>` : ''}
      </div>`;

  const updateLogHtml = (Array.isArray(a.update_log) && a.update_log.length > 0) ? `
      <div style="margin:12px 0;border-left:3px solid var(--accent2);font-family:'IBM Plex Mono',monospace;font-size:11px;">
        <div style="padding:6px 10px;font-weight:700;color:var(--accent2);border-bottom:1px solid var(--border);">업데이트 기록</div>
        ${[...a.update_log].reverse().map((l, i) => `
          <div style="display:flex;gap:10px;padding:4px 10px;${i === 0 ? 'background:rgba(26,95,168,0.06);color:var(--text);font-weight:600;' : 'color:var(--muted);'}">
            <span style="min-width:78px;flex-shrink:0;">${formatUpdateTimeKST(l.timestamp)}</span>
            <span>${esc(l.note || '업데이트')}</span>
          </div>`).join('')}
      </div>` : '';

  const heroHtml = a.image_url
    ? `<img class="article-hero" src="${esc(a.image_url)}" alt="${esc(title)}" loading="lazy" style="width:100%;max-height:420px;object-fit:cover;border-radius:8px;margin:16px 0 8px;display:block;">`
    : '';

  return `
      <a class="back-btn" href="/">← 홈으로</a>

      ${isTrend ? `<div class="trend-badge"><span class="dot"></span>TREND</div>` : ''}
${tagsHtml}

      <h1 class="article-title">${esc(title)}</h1>
${metaHtml}
${updateLogHtml}
      ${heroHtml}

      <div class="article-body">
        <p>${bodyHtml}</p>
      </div>

      <div class="source-link">ⓒ NewsFinal <button onclick="shareArticle()" style="float:right;background:none;border:1px solid var(--border);border-radius:6px;padding:4px 12px;cursor:pointer;color:var(--text);font-size:12px;">🔗 공유</button><br>뉴스파이널 편집국</div>`;
}

export async function onRequestGet(context) {
  const { env } = context;
  const url = new URL(context.request.url);
  const id = url.searchParams.get('id');

  // 정적 셸 (env.ASSETS = Pages 정적 자산 바인딩). .html 요청은 CF가
  // 확장자 없는 경로로 정규화(리디렉션)하지만 최종적으로 원본 셸을 반환한다.
  const shell = await env.ASSETS.fetch(new URL('/article.html', url.origin));

  // id 없으면 셸 그대로 반환(클라가 "기사를 찾을 수 없습니다" 처리)
  if (!id) return shell;

  try {
    // Supabase 서버 조회 (is_published=true 1건)
    let a = null;
    const apiUrl = `${SUPABASE_URL}/rest/v1/articles?id=eq.${encodeURIComponent(id)}&is_published=eq.true&select=*`;
    const dbRes = await fetch(apiUrl, { headers: { apikey: SUPABASE_ANON_KEY } });
    if (dbRes.ok) {
      const rows = await dbRes.json();
      a = rows[0] || null;
    }

    // 기사 없음/미게시 → 404 상태로 셸 반환(크롤러에 not found 신호)
    if (!a) {
      return new Response(shell.body, {
        status: 404,
        headers: { 'content-type': 'text/html; charset=utf-8' },
      });
    }

    const title = a.title_ko || a.title_en || '';
    const fullTitle = `${title} — NewsFinal`;
    const desc = cleanBody(a.summary_ko || a.summary_en || '').replace(/\s+/g, ' ').trim().slice(0, 150);
    const canonical = `${SITE}/article?id=${a.id}`;
    const ogImage = a.image_url || `${SITE}/favicon-512.png`;
    const publishedIso = toIsoKST(a.created_at);
    const kwParts = [a.category, a.country, regionKo(a.region)].filter(Boolean);
    const newsKeywords = kwParts.join(', ');
    const wrapperHtml = buildWrapperHtml(a);

    // og:type 요소 뒤에 붙일 추가 메타(원본 head에 없는 것들)
    const extraHead =
      `\n  <meta property="og:url" content="${esc(canonical)}">` +
      `\n  <meta property="article:section" content="${esc(a.category || '')}">` +
      (publishedIso ? `\n  <meta property="article:published_time" content="${esc(publishedIso)}">` : '') +
      (newsKeywords ? `\n  <meta name="news_keywords" content="${esc(newsKeywords)}">` : '') +
      `\n  <meta name="twitter:card" content="summary_large_image">` +
      `\n  <meta name="twitter:title" content="${esc(fullTitle)}">` +
      `\n  <meta name="twitter:description" content="${esc(desc)}">` +
      `\n  <meta name="twitter:image" content="${esc(ogImage)}">`;

    const rewriter = new HTMLRewriter()
      .on('title', { element(el) { el.setInnerContent(fullTitle); } })
      .on('meta[name="description"]', { element(el) { el.setAttribute('content', desc); } })
      .on('meta[property="og:title"]', { element(el) { el.setAttribute('content', fullTitle); } })
      .on('meta[property="og:description"]', { element(el) { el.setAttribute('content', desc); } })
      .on('meta[property="og:image"]', { element(el) { el.setAttribute('content', ogImage); } })
      .on('meta[property="og:type"]', { element(el) { el.after(extraHead, { html: true }); } })
      .on('link[rel="canonical"]', { element(el) { el.setAttribute('href', canonical); } })
      .on('#article-wrapper', { element(el) { el.setInnerContent(wrapperHtml, { html: true }); } });

    // 표준 패턴: transform 결과의 body+init을 그대로 상속해 반환.
    // (res.body만 빼서 new Response로 감싸면 지연 스트림이 끊겨 빈 응답이 됨)
    const transformed = rewriter.transform(shell);
    const resp = new Response(transformed.body, transformed);
    resp.headers.set('content-type', 'text/html; charset=utf-8');
    resp.headers.set('cache-control', 'no-store');
    return resp;
  } catch (e) {
    // 어떤 실패에도 백지 방지: 원본 셸(CSR)로 폴백 + 원인 헤더 노출
    const fb = await env.ASSETS.fetch(new URL('/article.html', url.origin));
    return new Response(fb.body, {
      status: 200,
      headers: {
        'content-type': 'text/html; charset=utf-8',
        'x-ssr-error': String(e && e.message || e).slice(0, 150),
      },
    });
  }
}
