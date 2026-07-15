// functions/article.js — 임시 진단 버전
// 목적: /article?id=X 요청 시 각 단계 상태를 화면에 텍스트로 출력해
//       실패 지점(env.ASSETS 유무 / 셸 fetch / DB 조회)을 특정한다.
// 원인 확인 후 본버전으로 즉시 교체 예정.

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  const id = url.searchParams.get('id');
  const out = [];

  out.push('[1] article function ALIVE');
  out.push('    id = ' + id);
  out.push('    origin = ' + url.origin);
  out.push('    env keys = ' + (env ? Object.keys(env).join(', ') : '(env null)'));
  out.push('    has env.ASSETS = ' + (env && env.ASSETS ? 'YES' : 'NO'));

  // [2] 정적 셸 가져오기 시도
  if (env && env.ASSETS) {
    try {
      const shell = await env.ASSETS.fetch(new URL('/article.html', url.origin));
      out.push('[2] ASSETS /article.html -> status ' + shell.status
        + ' | ctype ' + shell.headers.get('content-type')
        + ' | redirected ' + shell.redirected);
      const txt = await shell.text();
      out.push('    shell length = ' + txt.length + ' chars');
      out.push('    has #article-wrapper = ' + txt.includes('id="article-wrapper"'));
    } catch (e) {
      out.push('[2] ASSETS ERROR: ' + e.message);
    }
  } else {
    out.push('[2] SKIP (no env.ASSETS)');
  }

  // [3] Supabase 조회 시도
  try {
    const apiUrl = 'https://fotdngseksqaghvtcvqh.supabase.co/rest/v1/articles?id=eq.'
      + encodeURIComponent(id || '') + '&is_published=eq.true&select=id,title_ko';
    const r = await fetch(apiUrl, { headers: { apikey: 'sb_publishable_rT3kSEAAdM0DJlDW5fEWww_M37h6dDW' } });
    out.push('[3] Supabase -> status ' + r.status);
    const rows = await r.json();
    out.push('    rows = ' + (Array.isArray(rows) ? rows.length : JSON.stringify(rows).slice(0, 200)));
    if (Array.isArray(rows) && rows[0]) out.push('    title_ko = ' + rows[0].title_ko);
  } catch (e) {
    out.push('[3] Supabase ERROR: ' + e.message);
  }

  // [4] HTMLRewriter 존재 확인
  out.push('[4] typeof HTMLRewriter = ' + (typeof HTMLRewriter));

  return new Response(out.join('\n'), {
    headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' },
  });
}
