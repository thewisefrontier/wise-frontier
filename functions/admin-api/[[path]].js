// /admin-api/* — 관리자 로그인·세션 + 관리자 전용 /rest/v1(2026-10-20 Supabase 이탈). 처리는 server/admin.js.
import { db } from '../../server/pgrest.js';
import { BadRequest } from '../../server/pgrest.js';
import { checkLogin, makeSessionCookie, clearCookie, readSession, userFor, handleAdminRest } from '../../server/admin.js';

const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...headers } });

export async function onRequest(context) {
  const { request, env, params } = context;
  const path = Array.isArray(params.path) ? params.path : [params.path];
  const sql = db(env);
  try {
    if (path[0] === 'login' && request.method === 'POST') {
      const { email, password } = await request.json().catch(() => ({}));
      if (!(await checkLogin(env, email, password))) {
        await new Promise((r) => setTimeout(r, 1000)); // 무차별 대입 속도 늦추기
        return json({ error: 'invalid_credentials' }, 401);
      }
      return json({ user: await userFor(sql, env.ADMIN_EMAIL) }, 200, { 'set-cookie': await makeSessionCookie(env, env.ADMIN_EMAIL) });
    }
    if (path[0] === 'logout') return json({ ok: true }, 200, { 'set-cookie': clearCookie });

    const session = await readSession(env, request);
    if (!session) return json({ error: 'unauthorized' }, 401);
    if (path[0] === 'session') return json({ user: await userFor(sql, session.email) });
    if (path[0] === 'rest' && path[1] === 'v1' && path.length >= 3) {
      const res = await handleAdminRest(sql, request, path.slice(2));
      res.headers.set('cache-control', 'no-store');
      return res;
    }
    return json({ error: 'not_found' }, 404);
  } catch (e) {
    const status = e instanceof BadRequest ? 400 : 500;
    return json({ message: String((e && e.message) || e).slice(0, 300), code: status === 400 ? 'PGRST100' : 'XX000' }, status);
  } finally {
    context.waitUntil(sql.end());
  }
}
