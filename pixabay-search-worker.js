/**
 * Cloudflare Worker — Pixabay 검색 프록시
 * 배포: wrangler deploy --config wrangler-pixabay.toml
 * 키 설정: wrangler secret put PIXABAY_API_KEY --config wrangler-pixabay.toml
 *
 * GET /?q=검색어
 * → Pixabay hits 배열 반환 (previewURL, largeImageURL, tags만)
 */

const ALLOWED_ORIGINS = [
  'https://newsfinal.co.kr',
  'https://www.newsfinal.co.kr',
];

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    const allowed = ALLOWED_ORIGINS.includes(origin);
    const corsOrigin = allowed ? origin : ALLOWED_ORIGINS[0];

    const corsHeaders = {
      'Access-Control-Allow-Origin': corsOrigin,
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }

    if (request.method !== 'GET') {
      return new Response('Method not allowed', { status: 405, headers: corsHeaders });
    }

    if (!allowed) {
      return new Response(JSON.stringify({ error: 'Forbidden origin' }), {
        status: 403,
        headers: { 'Content-Type': 'application/json', ...corsHeaders }
      });
    }

    const url = new URL(request.url);
    const q = (url.searchParams.get('q') || '').slice(0, 100);
    if (!q) {
      return new Response(JSON.stringify({ error: 'q 파라미터 필요' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json', ...corsHeaders }
      });
    }

    try {
      const res = await fetch(
        `https://pixabay.com/api/?key=${env.PIXABAY_API_KEY}&q=${encodeURIComponent(q)}&image_type=photo&safesearch=true&per_page=12`
      );
      if (!res.ok) {
        return new Response(JSON.stringify({ error: `Pixabay ${res.status}` }), {
          status: 502,
          headers: { 'Content-Type': 'application/json', ...corsHeaders }
        });
      }
      const data = await res.json();
      const hits = (data.hits || []).map(h => ({
        previewURL: h.previewURL,
        largeImageURL: h.largeImageURL,
        tags: h.tags,
      }));
      return new Response(JSON.stringify({ hits }), {
        headers: { 'Content-Type': 'application/json', ...corsHeaders }
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 500,
        headers: { 'Content-Type': 'application/json', ...corsHeaders }
      });
    }
  }
};
