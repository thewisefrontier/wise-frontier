// /rest/v1/* — 브라우저가 예전 Supabase REST와 같은 모양으로 부르는 공개 읽기 API(2026-10-20 Supabase 이탈).
// 실제 처리는 server/pgrest.js(화이트리스트·파라미터 바인딩)가 한다.
import { db, handleTable, handleRpc, errorResponse } from '../../../server/pgrest.js';

export async function onRequest(context) {
  const { request, env, params } = context;
  const path = Array.isArray(params.path) ? params.path : [params.path];
  const sql = db(env);
  try {
    if (path[0] === 'rpc' && path.length === 2) {
      if (request.method !== 'POST') return new Response(null, { status: 405 });
      const body = await request.json().catch(() => ({}));
      return await handleRpc(sql, path[1], body);
    }
    if (path.length !== 1 || !['GET', 'HEAD'].includes(request.method)) {
      return new Response(JSON.stringify({ message: 'read-only' }), { status: 405 });
    }
    return await handleTable(sql, path[0], new URL(request.url), request.headers);
  } catch (e) {
    return errorResponse(e);
  } finally {
    context.waitUntil(sql.end());
  }
}
