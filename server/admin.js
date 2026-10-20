// server/admin.js — 관리자 페이지용 읽기·쓰기 API + 세션(2026-10-20 Supabase 이탈)
//
// admin.html은 supabase-js 클라이언트를 그대로 쓰고 주소만 /admin-api로 바꿨다. supabase-js가 만드는
// /rest/v1 요청 중 관리자 페이지가 실제로 쓰는 것(조회·eq/neq/gte/ilike/in/or·정렬·range·count·
// single/maybeSingle, 단건/다건 insert, update, delete, RPC 3개 — 2026-10-20 전수 집계)만 처리한다.
// 로그인은 Supabase Auth 대신 Pages 시크릿(ADMIN_EMAIL/ADMIN_PASSWORD)과 비교하고, 성공하면
// ADMIN_SESSION_SECRET으로 서명한 HttpOnly 세션 쿠키를 준다. 세션 없는 요청은 전부 401.
// 식별자는 DB 카탈로그로 실존 확인 후 따옴표 처리, 값은 전부 파라미터 바인딩(server/pgrest.js와 같은 원칙).

import { BadRequest, q, condition, orGroup, RESERVED } from './pgrest.js';

const MAX_ROWS = 1000;
const SESSION_TTL = 12 * 3600;
const COOKIE = 'nf_admin';

const RPCS = {
  find_duplicate_pairs: { sql: "select coalesce(json_agg(t), '[]')::text as j from find_duplicate_pairs($1::int, $2::float8) t",
    args: (b) => [b.p_hours ?? 72, b.p_threshold ?? 0.5] },
  admin_stats: { sql: 'select admin_stats($1::int)::text as j', args: (b) => [b.p_days ?? 30] },
  daily_views: { sql: 'select daily_views($1::int)::text as j', args: (b) => [b.p_days ?? 30] },
};

let catalog = null; // 테이블 → (컬럼 → SQL 타입), isolate 수명 동안 재사용

async function tables(sql) {
  if (catalog) return catalog;
  const rows = await sql`select c.relname as t, a.attname as c, format_type(a.atttypid, a.atttypmod) as ty
                         from pg_attribute a join pg_class c on c.oid = a.attrelid join pg_namespace n on n.oid = c.relnamespace
                         where n.nspname = 'public' and c.relkind in ('r','p') and a.attnum > 0 and not a.attisdropped`;
  const m = {};
  for (const r of rows) (m[r.t] ||= new Map()).set(r.c, r.ty);
  catalog = m;
  return m;
}

// ── 세션 ─────────────────────────────────────────────
const enc = new TextEncoder();
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const fromB64u = (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));

async function hmac(secret, data) {
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return crypto.subtle.sign('HMAC', key, enc.encode(data));
}

async function sameSecret(a, b) {
  // 길이·내용을 직접 비교하면 응답 시간으로 추측할 수 있어, 해시를 고정 길이로 맞춰 비교한다.
  const [x, y] = await Promise.all([crypto.subtle.digest('SHA-256', enc.encode(a)), crypto.subtle.digest('SHA-256', enc.encode(b))]);
  const u = new Uint8Array(x), v = new Uint8Array(y);
  let d = 0;
  for (let i = 0; i < u.length; i++) d |= u[i] ^ v[i];
  return d === 0;
}

export async function makeSessionCookie(env, email) {
  const payload = b64u(enc.encode(JSON.stringify({ email, exp: Math.floor(Date.now() / 1000) + SESSION_TTL })));
  const sig = b64u(await hmac(env.ADMIN_SESSION_SECRET, payload));
  return `${COOKIE}=${payload}.${sig}; Path=/admin-api; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_TTL}`;
}

export const clearCookie = `${COOKIE}=; Path=/admin-api; HttpOnly; Secure; SameSite=Strict; Max-Age=0`;

export async function readSession(env, request) {
  const m = (request.headers.get('cookie') || '').match(new RegExp(`(?:^|;\\s*)${COOKIE}=([^;]+)`));
  if (!m || !env.ADMIN_SESSION_SECRET) return null;
  const [payload, sig] = m[1].split('.');
  if (!payload || !sig) return null;
  const expect = b64u(await hmac(env.ADMIN_SESSION_SECRET, payload));
  if (!(await sameSecret(expect, sig))) return null;
  try {
    const s = JSON.parse(new TextDecoder().decode(fromB64u(payload)));
    return s.exp > Date.now() / 1000 ? s : null;
  } catch {
    return null;
  }
}

export async function checkLogin(env, email, password) {
  if (!env.ADMIN_EMAIL || !env.ADMIN_PASSWORD || !env.ADMIN_SESSION_SECRET) return false;
  const okEmail = await sameSecret(String(email || '').trim().toLowerCase(), env.ADMIN_EMAIL.trim().toLowerCase());
  const okPw = await sameSecret(String(password || ''), env.ADMIN_PASSWORD);
  return okEmail && okPw;
}

export async function userFor(sql, email) {
  // admin.html은 user.id로 user_roles(role,name)를 조회한다 — 예전 Supabase Auth id를 그대로 돌려준다.
  const rows = await sql`select id::text as id from user_roles where lower(email) = lower(${email}) limit 1`;
  return { id: rows[0]?.id || '00000000-0000-0000-0000-000000000000', email };
}

// ── REST ─────────────────────────────────────────────
const json = (body, status = 200, extra = {}) =>
  new Response(body, { status, headers: { 'content-type': 'application/json; charset=utf-8', ...extra } });

function whereFrom(cols, sp, params) {
  const where = [];
  for (const [k, v] of sp.entries()) {
    if (k === 'or') where.push(orGroup(cols, v, params));
    else if (!RESERVED.has(k)) where.push(condition(cols, k, v, params));
  }
  return where;
}

function selectList(cols, raw) {
  if (!raw || raw === '*') return '*';
  const list = raw.split(',').map((s) => s.trim()).filter(Boolean);
  for (const c of list) if (!cols.has(c)) throw new BadRequest(`unknown column ${c}`);
  return list.map(q).join(', ');
}

function wantsObject(request) {
  return (request.headers.get('accept') || '').includes('vnd.pgrst.object');
}

function shape(request, row, total) {
  // row: {j: 배열 JSON 텍스트, o: 첫 행 JSON 텍스트, n: 행 수}
  if (wantsObject(request)) {
    if (row.n !== 1) {
      return json(JSON.stringify({ code: 'PGRST116', details: `The result contains ${row.n} rows`, hint: null,
        message: 'JSON object requested, multiple (or no) rows returned' }), 406);
    }
    return json(row.o);
  }
  return json(row.j, 200, total !== undefined ? { 'content-range': total } : {});
}

async function get(sql, request, table, cols, url) {
  const sp = url.searchParams, params = [];
  const where = whereFrom(cols, sp, params);
  const order = (sp.get('order') || '').split(',').filter(Boolean).map((o) => {
    const [col, dir = 'asc', nulls] = o.split('.');
    if (!cols.has(col) || !['asc', 'desc'].includes(dir)) throw new BadRequest('bad order');
    return `${q(col)} ${dir.toUpperCase()}${nulls === 'nullsfirst' ? ' NULLS FIRST' : nulls === 'nullslast' ? ' NULLS LAST' : ''}`;
  });
  const offset = Math.max(0, parseInt(sp.get('offset') || '0', 10) || 0);
  const limit = Math.max(0, Math.min(sp.has('limit') ? parseInt(sp.get('limit'), 10) || 0 : MAX_ROWS, MAX_ROWS));
  const w = where.length ? ` WHERE ${where.join(' AND ')}` : '';
  const body = `SELECT ${selectList(cols, sp.get('select'))} FROM public.${q(table)}${w}`
    + (order.length ? ` ORDER BY ${order.join(', ')}` : '') + ` LIMIT ${limit} OFFSET ${offset}`;
  const [row] = await sql.unsafe(`select coalesce(json_agg(t), '[]')::text as j, (json_agg(t) -> 0)::text as o, count(*)::int as n
                                  from (${body}) t`, params);
  let total;
  if (/count=exact/.test(request.headers.get('prefer') || '')) {
    const [c] = await sql.unsafe(`select count(*)::int as n from public.${q(table)}${w}`, params);
    total = row.n ? `${offset}-${offset + row.n - 1}/${c.n}` : `*/${c.n}`;
  }
  if (request.method === 'HEAD') return new Response(null, { status: 200, headers: total ? { 'content-range': total } : {} });
  return shape(request, row, total);
}

function bodyColumns(cols, rows) {
  const keys = [...new Set(rows.flatMap((r) => Object.keys(r || {})))];
  if (!keys.length) throw new BadRequest('empty body');
  for (const k of keys) if (!cols.has(k)) throw new BadRequest(`unknown column ${k}`);
  return keys;
}

async function write(sql, request, table, cols, url) {
  const prefer = request.headers.get('prefer') || '';
  const returning = /return=representation/.test(prefer) || wantsObject(request);
  const sp = url.searchParams, params = [];
  const t = `public.${q(table)}`;
  let stmt;
  if (request.method === 'POST') {
    const body = await request.json();
    const rows = Array.isArray(body) ? body : [body];
    const keys = bodyColumns(cols, rows);
    params.push(JSON.stringify(rows));
    const kl = keys.map(q).join(', ');
    const conflict = /resolution=ignore-duplicates/.test(prefer) ? ' ON CONFLICT DO NOTHING' : '';
    stmt = `INSERT INTO ${t} (${kl}) SELECT ${kl} FROM json_populate_recordset(null::${t}, $1::text::json)${conflict} RETURNING *`;
  } else {
    const where = whereFrom(cols, sp, params);
    if (!where.length) throw new BadRequest('filter required'); // 전체 행 수정·삭제 사고 방지
    if (request.method === 'PATCH') {
      const body = await request.json();
      const keys = bodyColumns(cols, [body]);
      params.push(JSON.stringify(body));
      const n = params.length;
      const set = keys.map((k) => `${q(k)} = (select ${q(k)} from json_populate_record(null::${t}, $${n}::text::json))`).join(', ');
      stmt = `UPDATE ${t} SET ${set} WHERE ${where.join(' AND ')} RETURNING *`;
    } else if (request.method === 'DELETE') {
      stmt = `DELETE FROM ${t} WHERE ${where.join(' AND ')} RETURNING *`;
    } else {
      return json(JSON.stringify({ message: 'method not allowed' }), 405);
    }
  }
  const [row] = await sql.unsafe(`with w as (${stmt})
                                  select coalesce(json_agg(w), '[]')::text as j, (json_agg(w) -> 0)::text as o, count(*)::int as n from w`, params);
  const status = request.method === 'POST' ? 201 : 200;
  if (!returning) return new Response(null, { status: request.method === 'POST' ? 201 : 204 });
  if (wantsObject(request)) return shape(request, row);
  return json(row.j, status);
}

export async function handleAdminRest(sql, request, path) {
  if (path[0] === 'rpc') {
    const rpc = RPCS[path[1]];
    if (!rpc || request.method !== 'POST') throw new BadRequest(`unknown rpc ${path[1]}`);
    const body = await request.json().catch(() => ({}));
    const [row] = await sql.unsafe(rpc.sql, rpc.args(body));
    return json(row.j);
  }
  const table = path[0];
  const cols = (await tables(sql))[table];
  if (!cols || path.length !== 1) throw new BadRequest(`unknown table ${table}`);
  const url = new URL(request.url);
  if (request.method === 'GET' || request.method === 'HEAD') return get(sql, request, table, cols, url);
  return write(sql, request, table, cols, url);
}
