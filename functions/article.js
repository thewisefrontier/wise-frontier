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

import { publicUpdateLog } from '../docs/js/update-log-filter.js';
import { imageCreditLabel } from '../docs/js/image-credit.js';
import { esc } from '../docs/js/esc.js';

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
// 상단 [업데이트] 블록을 걷어내고 원 본문만 반환한다. 요약·메타 설명 전용.
// 본문 렌더에는 쓰지 않는다 — 독자에게는 업데이트가 맨 위에 보여야 한다.
// 카드나 검색 스니펫에 "■ 8월 4일 12:31 — …"이 먼저 나오면 무슨 사건인지
// 알 수 없으므로, 사건을 설명하는 원 본문 리드를 쓴다.
function stripUpdates(text) {
  const s = String(text || '');
  if (!s.replace(/^\s+/, '').startsWith('[업데이트]')) return s;
  const i = s.indexOf('\n────────');
  if (i === -1) return s;
  const rest = s.slice(i + 1);
  const nl = rest.indexOf('\n');
  return nl === -1 ? '' : rest.slice(nl + 1).replace(/^\n+/, '');
}

function cleanBody(text) {
  if (!text) return '';
  text = text
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/\*(.+?)\*/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/\[.{1,20}=.{1,30}\]/g, '')
    // 같은 줄에 내용이 이어지는 프리픽스 태그(예: "[속보] 내용")만 제거.
    // 국가 헤더처럼 대괄호만 있고 줄바꿈으로 끝나는 경우([베트남]\n...)는 보존.
    .replace(/^\[.{1,30}\][ \t]*(?=\S)/gm, '');
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

  // "- " 시작 줄을 <ul><li>로, 나머지는 <p>로 묶는다 (renderArticle과 동일).
  // 문단(빈 줄) 단위로 먼저 블록을 나눈 뒤 블록별로 처리한다.
  // ⚠️ 실사고(2026-08-17, id=80581 다이제스트): 예전엔 \n\n을 플레이스홀더로
  // 먼저 치환하고 나서 split('\n')을 했는데, 그러면 문단 경계가 진짜 개행이
  // 아니게 되어 split이 그 지점을 못 끊었다. 그 결과 불릿("- ") 줄 바로 뒤에
  // 빈 줄이 오면 다음 섹션 전체가 그 불릿 <li> 안으로 통째로 흡수됐다.
  // 블록을 먼저 나누면 이런 교차 오염이 구조적으로 불가능하다.
  // ⚠️ 또한 이전엔 전체를 바깥 <p>로 한 번 더 감쌌는데, <p> 안에 <ul>이 들어가는
  // 건 HTML 파서가 허용하지 않아 브라우저가 <p>를 강제로 끊고 빈 <p></p>를
  // 끼워넣었다(id=80581에서 재확인). 블록마다 <p>·<ul>을 스스로 완결된
  // 형태로 내보내고, 바깥에서는 더 이상 <p>로 감싸지 않는다.
  // 다이제스트 등의 "[섹션 소제목]" 줄은 <h2>로 승격(SEO 헤더 구조).
  const SECTION_RE = /^\[(.+)\]$/;
  const htmlBlocks = body.split(/\n\n+/).map((block) => {
    const acc = block.split('\n').reduce((acc, line) => {
      const trimmed = line.trim();
      const sectionMatch = trimmed.match(SECTION_RE);
      if (sectionMatch) {
        acc.inList = false;
        acc.parts.push({ type: 'h2', text: sectionMatch[1] });
      } else if (trimmed.startsWith('- ')) {
        if (!acc.inList) { acc.parts.push({ type: 'ul', items: [] }); acc.inList = true; }
        acc.parts[acc.parts.length - 1].items.push(trimmed.slice(2));
      } else {
        acc.inList = false;
        const last = acc.parts[acc.parts.length - 1];
        if (last && last.type === 'p') { last.lines.push(line); }
        else { acc.parts.push({ type: 'p', lines: [line] }); }
      }
      return acc;
    }, { parts: [], inList: false });
    return acc.parts.map((p) => {
      if (p.type === 'ul') return `<ul>${p.items.map((i) => `<li>${i}</li>`).join('')}</ul>`;
      if (p.type === 'h2') return `<h2>${p.text}</h2>`;
      return `<p>${p.lines.join('<br>')}</p>`;
    }).join('');
  });
  const processedBody = htmlBlocks.join('');

  const bodyHtml = processedBody
    .replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g, '<img src="$2" alt="$1" loading="lazy" style="max-width:100%;border-radius:6px;margin:8px 0;display:block;">')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

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
        ${a.region && regionKo(a.region) !== a.category ? `<span class="tag">${esc(regionKo(a.region))}</span>` : ''}
        ${countries.length > 0
          ? countries.map((c) => `<a class="tag" href="/country.html?name=${encodeURIComponent(c)}">${esc(c)}</a>`).join('')
          : (a.country ? `<a class="tag" href="/country.html?name=${encodeURIComponent(a.country)}">${esc(a.country)}</a>` : '')}
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


  // 업데이트 기록 공개 필터 — docs/js/update-log-filter.js 공용 모듈 (2026-09-01,
  // docs/article.html·docs/live.html·이 파일 3곳 복붙 드리프트 재발 계기로 통합).
  const _pubLog = publicUpdateLog(a.update_log);
  const updateLogHtml = _pubLog.length > 0 ? `
      <div style="margin:12px 0;border-left:3px solid var(--accent2);font-family:'IBM Plex Mono',monospace;font-size:11px;">
        <div style="padding:6px 10px;font-weight:700;color:var(--accent2);border-bottom:1px solid var(--border);">업데이트 기록</div>
        ${[..._pubLog].reverse().map((l, i) => `
          <div style="display:flex;gap:10px;padding:4px 10px;${i === 0 ? 'background:rgba(26,95,168,0.06);color:var(--text);font-weight:600;' : 'color:var(--muted);'}">
            <span style="min-width:78px;flex-shrink:0;">${formatUpdateTimeKST(l.timestamp)}</span>
            <span>${esc(l.note || '업데이트')}</span>
          </div>`).join('')}
      </div>` : '';

  const s3Html = a.summary_3lines ? `
      <div style="margin:16px 0;padding:14px 16px;background:rgba(26,95,168,0.06);border-radius:8px;border-left:3px solid var(--accent2);">
        <div style="font-weight:700;color:var(--accent2);font-size:13px;margin-bottom:8px;">📌 3줄 요약</div>
        <ul style="margin:0;padding-left:18px;">
          ${String(a.summary_3lines).split(/\\n|\n/).filter((l) => l.trim()).map((l) => `<li style="margin-bottom:4px;font-size:14px;line-height:1.6;">${esc(l.trim().replace(/^[*\-•]\s*/, ''))}</li>`).join('')}
        </ul>
      </div>` : '';

  const investHtml = a.investment_idea ? `
      <div style="margin:24px 0;padding:16px 18px;background:rgba(200,140,20,0.07);border-radius:8px;border-left:3px solid #c88c14;">
        <div style="font-weight:700;color:#c88c14;font-size:13px;margin-bottom:8px;">💡 투자 아이디어</div>
        <div style="font-size:14px;line-height:1.8;color:var(--text);">${esc(String(a.investment_idea)).replace(/\\n|\n/g, '<br>')}</div>
      </div>` : '';

  const _imgCredit = imageCreditLabel(a.image_url, a.image_credit);
  const heroHtml = a.image_url
    ? `<img class="article-hero" src="${esc(a.image_url)}" alt="${esc(title)}" loading="lazy" style="width:100%;max-height:420px;object-fit:cover;border-radius:8px;margin:16px 0 8px;display:block;">` + (_imgCredit ? `<div style="font-size:12px;color:#888;margin:0 0 12px;text-align:right;">${esc(_imgCredit)}</div>` : '')
    : '';

  return `
      <a class="back-btn" href="/">← 홈으로</a>

      ${isTrend ? `<div class="trend-badge"><span class="dot"></span>TREND</div>` : ''}
${tagsHtml}

      <h1 class="article-title">${esc(title)}</h1>
${metaHtml}
${s3Html}
${updateLogHtml}
      ${heroHtml}

      <div class="article-body">
        ${bodyHtml}
      </div>
${investHtml}

      <div class="source-link">ⓒ NewsFinal <button onclick="shareArticle()" style="float:right;background:none;border:1px solid var(--border);border-radius:6px;padding:4px 12px;cursor:pointer;color:var(--text);font-size:12px;">🔗 공유</button></div>`;
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
    // Worker의 fetch()는 대상 서버 응답의 Cache-Control에 따라 Cloudflare 엣지에
    // 캐시될 수 있다 — 이 응답에 실어 보내는 no-store 헤더는 우리가 브라우저에
    // 주는 응답에만 적용되고, 이 서브리퀘스트 자체와는 무관하다. 그 결과 기사가
    // 갱신돼도 낡은 캐시가 한동안 노출되는 문제가 있었다(실사고: id=63237).
    const dbRes = await fetch(apiUrl, { headers: { apikey: SUPABASE_ANON_KEY }, cache: 'no-store' });

    // ⚠️ 실사고(2026-08-16): dbRes.ok가 false인 걸 "기사 없음"과 똑같이 취급해
    // 404를 내리고 있었다 — Supabase 일시 오류(레이트리밋·타임아웃 등)로 멀쩡한
    // 기사가 순간적으로 "없는 페이지"로 신고되는 버그. "확실히 없음"과 "일시적으로
    // 조회 실패"는 구분해야 한다 — 후자는 503(+Retry-After)로 응답해 크롤러가
    // 나중에 재시도하게 해야지, 확정적 404/소프트 200으로 잘못 신고하면 안 된다.
    if (!dbRes.ok) {
      return new Response(shell.body, {
        status: 503,
        headers: { 'content-type': 'text/html; charset=utf-8', 'retry-after': '120' },
      });
    }

    const rows = await dbRes.json();
    a = rows[0] || null;

    // 기사 없음/미게시 → 404 상태로 셸 반환(크롤러에 not found 신호)
    if (!a) {
      return new Response(shell.body, {
        status: 404,
        headers: { 'content-type': 'text/html; charset=utf-8' },
      });
    }

    const title = a.title_ko || a.title_en || '';
    const fullTitle = `${title} — NewsFinal`;
    const desc = cleanBody(stripUpdates(a.summary_ko) || a.summary_en || '').replace(/\s+/g, ' ').trim().slice(0, 150);
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
    // ⚠️ 실사고(2026-08-16, GSC "Soft 404" 253건): 렌더링 단계 예외를 전부
    // status 200 + 빈 CSR 셸로 응답하고 있었다. 크롤러 입장에선 "200인데
    // 실제로는 빈 페이지"로 보여 소프트 404로 분류된다. 이건 확정 실패가
    // 아니라 일시적 문제이므로 503(+Retry-After)으로 응답해 재시도를 유도한다.
    const fb = await env.ASSETS.fetch(new URL('/article.html', url.origin));
    return new Response(fb.body, {
      status: 503,
      headers: {
        'content-type': 'text/html; charset=utf-8',
        'retry-after': '120',
        'x-ssr-error': String(e && e.message || e).slice(0, 150),
      },
    });
  }
}
