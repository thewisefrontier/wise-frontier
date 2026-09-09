/**
 * Cloudflare Worker — 어드민 기사 편집 시 정적 목록 데이터(articles.json)
 * 즉시 재생성 트리거
 *
 * image-upload-worker.js와 같은 이유·같은 패턴(2026-09-03 보안 점검 반영,
 * origin 화이트리스트 + Bearer 시크릿 인증)으로 별도 워커로 분리.
 *
 * 본문 페이지(article.html)는 Supabase를 라이브로 조회해 항상 최신이지만,
 * 목록 페이지(index.html 등)가 읽는 docs/data/articles.json은 파이프라인
 * 사이클(~1시간 간격)에만 갱신돼 어드민에서 방금 고친 내용이 목록엔 한동안
 * 안 보이는 공백이 있었다(2026-09-10 사용자 지적). 이 워커는 admin.html의
 * 저장·발행·삭제 액션 뒤에 호출돼, GitHub Actions의 export_on_edit.yml
 * (export_articles.py만 도는 가벼운 워크플로우, ~1분)을 workflow_dispatch로
 * 즉시 깨운다 — GITHUB_DISPATCH_TOKEN은 이 리포 하나에만 범위를 좁힌
 * fine-grained PAT(Actions: write, Contents: read/write)를 쓴다.
 *
 * POST /trigger
 * Body: 없음(빈 POST) — 인증만 확인하고 dispatch만 쏜다.
 */

const ALLOWED_ORIGINS = [
  'https://newsfinal.co.kr',
  'https://www.newsfinal.co.kr',
];

const GITHUB_OWNER = 'thewisefrontier';
const GITHUB_REPO = 'wise-frontier';
const WORKFLOW_FILE = 'export_on_edit.yml';

export default {
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    const corsOrigin = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': corsOrigin,
          'Access-Control-Allow-Methods': 'POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
      });
    }

    if (request.method !== 'POST') {
      return new Response('Method not allowed', { status: 405 });
    }

    const auth = request.headers.get('Authorization');
    if (auth !== `Bearer ${env.EXPORT_TRIGGER_SECRET}`) {
      return new Response('Unauthorized', { status: 401 });
    }

    try {
      const ghRes = await fetch(
        `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
        {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
            'Accept': 'application/vnd.github+json',
            'User-Agent': 'newsfinal-export-trigger-worker',
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ ref: 'main' }),
        }
      );

      // GitHub의 workflow_dispatch는 성공 시 본문 없이 204를 반환한다.
      if (ghRes.status !== 204) {
        const detail = await ghRes.text();
        return new Response(JSON.stringify({ error: `GitHub API ${ghRes.status}`, detail }), {
          status: 502,
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': corsOrigin }
        });
      }

      return new Response(JSON.stringify({ triggered: true }), {
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': corsOrigin }
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 500,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': corsOrigin }
      });
    }
  }
};
