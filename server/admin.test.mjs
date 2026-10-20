// node server/admin.test.mjs — 실제 DB 없이 관리자 API의 쿼리 모양·안전장치·세션을 검사한다.
import assert from 'node:assert/strict';
import { handleAdminRest, makeSessionCookie, readSession, checkLogin } from './admin.js';

const CAT = [['articles', 'id', 'bigint'], ['articles', 'title_ko', 'text'], ['articles', 'is_published', 'boolean'],
  ['articles', 'source', 'text'], ['articles', 'subcategory', 'text'], ['articles', 'update_log', 'jsonb'],
  ['prompts', 'id', 'integer'], ['prompts', 'name', 'text'], ['prompts', 'version', 'integer']]
  .map(([t, c, ty]) => ({ t, c, ty }));
const calls = [];
const sql = () => Promise.resolve(CAT);
sql.unsafe = (q, p) => { calls.push({ q, p }); return Promise.resolve([{ j: '[{"id":1}]', o: '{"id":1}', n: 1 }]); };
const req = (method, url, { body, headers = {} } = {}) => new Request('https://x/admin-api/rest/v1/' + url,
  { method, body: body === undefined ? undefined : JSON.stringify(body), headers });
const run = async (...a) => { calls.length = 0; const r = await handleAdminRest(sql, a[0], a[1]); return { r, calls: [...calls] }; };

// single(): Accept 객체 → 객체 하나
let { r, calls: c } = await run(req('GET', 'articles?select=*&id=eq.5', { headers: { accept: 'application/vnd.pgrst.object+json' } }), ['articles']);
assert.equal(await r.text(), '{"id":1}');
assert.match(c[0].q, /WHERE "id" = \$1::text::bigint/);

// 미발행 목록 + or(ilike) + count
({ r, calls: c } = await run(req('GET', 'articles?select=id,title_ko&source=eq.NewsFinal&is_published=eq.false&or=(subcategory.ilike.이슈파이널%25,subcategory.ilike.주간정리%25)&order=id.desc&offset=0&limit=30',
  { headers: { prefer: 'count=exact' } }), ['articles']));
assert.equal(c.length, 2);
assert.deepEqual(c[0].p, ['NewsFinal', 'false', '이슈파이널%', '주간정리%']);
assert.equal(r.headers.get('content-range'), '0-0/1');

// insert: 본문은 text로 받아 json으로(이중 인코딩 방지)
({ r, calls: c } = await run(req('POST', 'prompts', { body: { name: 'writer_rules', version: 3 } }), ['prompts']));
assert.equal(r.status, 201);
assert.match(c[0].q, /INSERT INTO public\."prompts" \("name", "version"\) SELECT "name", "version" FROM json_populate_recordset\(null::public\."prompts", \$1::text::json\)/);

// update: 필터 + 본문 파라미터 번호
({ r, calls: c } = await run(req('PATCH', 'articles?id=eq.7', { body: { is_published: true, update_log: [{ note: 'x' }] } }), ['articles']));
assert.equal(r.status, 204);
assert.match(c[0].q, /UPDATE public\."articles" SET "is_published" = \(select "is_published" from json_populate_record\(null::public\."articles", \$2::text::json\)\)/);
assert.match(c[0].q, /WHERE "id" = \$1::text::bigint RETURNING \*/);

// delete
({ r, calls: c } = await run(req('DELETE', 'articles?id=eq.7'), ['articles']));
assert.match(c[0].q, /DELETE FROM public\."articles" WHERE "id" = \$1::text::bigint/);

// 필터 없는 수정·삭제, 없는 컬럼·테이블은 거부
await assert.rejects(run(req('DELETE', 'articles'), ['articles']));
await assert.rejects(run(req('PATCH', 'articles', { body: { is_published: false } }), ['articles']));
await assert.rejects(run(req('POST', 'articles', { body: { nope: 1 } }), ['articles']));
await assert.rejects(run(req('GET', 'pg_shadow'), ['pg_shadow']));

// 세션: 서명 통과·위조·만료
const env = { ADMIN_EMAIL: 'a@b.c', ADMIN_PASSWORD: 'pw-123', ADMIN_SESSION_SECRET: 's'.repeat(40) };
const cookie = (await makeSessionCookie(env, 'a@b.c')).split(';')[0];
assert.equal((await readSession(env, new Request('https://x', { headers: { cookie } }))).email, 'a@b.c');
const forged = cookie.slice(0, -2) + (cookie.endsWith('AA') ? 'BB' : 'AA');
assert.equal(await readSession(env, new Request('https://x', { headers: { cookie: forged } })), null);
assert.equal(await readSession({ ...env, ADMIN_SESSION_SECRET: 'other'.repeat(10) }, new Request('https://x', { headers: { cookie } })), null);
assert.equal(await checkLogin(env, 'A@B.C ', 'pw-123'), true);
assert.equal(await checkLogin(env, 'a@b.c', 'pw-124'), false);
assert.equal(await checkLogin({ ...env, ADMIN_PASSWORD: '' }, 'a@b.c', ''), false);
console.log('admin ok');
