// server/pgrest.js — 사이트용 PostgREST 호환 읽기 계층(Cloudflare Pages Functions + Hyperdrive → Aiven)
//
// 2026-10-20 Supabase 이탈(무료 플랜 이그레스 초과로 공개 REST 402). 브라우저 코드는 그대로
// `/rest/v1/{table}?...` 형식으로 부르고, 이 모듈이 그 쿼리 문법의 "프런트가 실제로 쓰는 부분"만
// SQL로 바꿔 Aiven에 묻는다(2026-10-20 docs/*.html 전수 조사: eq/neq/gt/gte/lt/lte/like/ilike/in/is/cs,
// not. 접두, or=(...), order, limit/offset, Range, Prefer: count=exact).
//
// 보안: 테이블·컬럼은 화이트리스트(+DB 카탈로그로 실존 확인) 후 따옴표 식별자로만 넣고, 값은 전부
// $n 파라미터로 바인딩한다. 공개 읽기 전용 — 쓰기는 지정된 RPC(조회수)만.
// 응답 JSON은 Postgres가 json_agg로 만든 문자열을 그대로 흘려 Worker CPU(무료 10ms)를 거의 안 쓴다.

import postgres from 'postgres';

const MAX_ROWS = 1000; // Supabase 기본 max-rows와 동일 — 프런트가 이 상한을 전제로 짜여 있다

// forced: 익명 공개에 항상 덧붙는 조건(Supabase RLS가 하던 역할). hidden: 공개 응답에서 뺄 컬럼.
const TABLES = {
  articles: { forced: 'is_published = true', hidden: new Set(['full_text']) },
  econ_events: { forced: null, hidden: new Set() },
  companies: { forced: 'is_published = true', hidden: new Set() },
};

const RPCS = {
  related_articles: {
    sql: 'select coalesce(json_agg(t), \'[]\')::text as j from related_articles($1::bigint, $2::int) t',
    args: (b) => [b.p_id, b.p_limit ?? 6],
  },
  increment_view_count: {
    sql: 'select increment_view_count($1::int)::text as j',
    args: (b) => [b.article_id],
    noContent: true,
  },
};

const OPS = { eq: '=', neq: '<>', gt: '>', gte: '>=', lt: '<', lte: '<=' };
const RESERVED = new Set(['select', 'order', 'limit', 'offset', 'or', 'and']);

let columnCache = null; // isolate 수명 동안 재사용(테이블별 컬럼 → data_type)

export function db(env) {
  // Hyperdrive가 커넥션 풀을 들고 있으니 요청마다 클라이언트를 새로 만드는 게 공식 권장 패턴.
  return postgres(env.HYPERDRIVE.connectionString, { max: 5, fetch_types: false, prepare: true });
}

async function columns(sql) {
  if (columnCache) return columnCache;
  const rows = await sql`select table_name, column_name, data_type from information_schema.columns
                         where table_schema = 'public' and table_name in ${sql(Object.keys(TABLES))}`;
  const m = {};
  for (const r of rows) (m[r.table_name] ||= new Map()).set(r.column_name, r.data_type);
  columnCache = m;
  return m;
}

class BadRequest extends Error {}

const q = (ident) => '"' + ident.replace(/"/g, '""') + '"';

function splitTop(s) {
  // or=(a.eq.1,b.ilike.*x*) 의 최상위 쉼표만 자른다(괄호·따옴표 안 쉼표는 보존).
  const out = []; let depth = 0, cur = '', quote = false;
  for (const ch of s) {
    if (ch === '"') quote = !quote;
    if (!quote && (ch === '(' || ch === '{')) depth++;
    if (!quote && (ch === ')' || ch === '}')) depth--;
    if (!quote && depth === 0 && ch === ',') { out.push(cur); cur = ''; continue; }
    cur += ch;
  }
  if (cur) out.push(cur);
  return out;
}

function condition(cols, col, expr, params) {
  if (!cols.has(col)) throw new BadRequest(`unknown column ${col}`);
  let negate = false;
  if (expr.startsWith('not.')) { negate = true; expr = expr.slice(4); }
  const dot = expr.indexOf('.');
  if (dot < 0) throw new BadRequest(`bad filter ${col}`);
  const op = expr.slice(0, dot), val = expr.slice(dot + 1);
  const c = q(col);
  let sqlPart;
  if (OPS[op]) {
    params.push(val);
    sqlPart = `${c} ${OPS[op]} $${params.length}`;
  } else if (op === 'like' || op === 'ilike') {
    params.push(val.replace(/\*/g, '%'));
    sqlPart = `${c}::text ${op === 'like' ? 'LIKE' : 'ILIKE'} $${params.length}`;
  } else if (op === 'in') {
    const items = splitTop(val.replace(/^\(|\)$/g, '')).map((v) => v.replace(/^"|"$/g, ''));
    params.push(items);
    sqlPart = `${c}::text = any($${params.length}::text[])`;
  } else if (op === 'is') {
    const v = { null: 'NULL', true: 'TRUE', false: 'FALSE' }[val.toLowerCase()];
    if (!v) throw new BadRequest(`bad is value`);
    sqlPart = `${c} IS ${v}`;
  } else if (op === 'cs') {
    params.push(val);
    sqlPart = cols.get(col) === 'jsonb' ? `${c} @> $${params.length}::jsonb` : `${c} @> $${params.length}::text[]`;
  } else {
    throw new BadRequest(`unsupported operator ${op}`);
  }
  return negate ? `NOT (${sqlPart})` : sqlPart;
}

function orGroup(cols, raw, params) {
  const inner = raw.replace(/^\(|\)$/g, '');
  const parts = splitTop(inner).map((p) => {
    const d = p.indexOf('.');
    if (d < 0) throw new BadRequest('bad or');
    return condition(cols, p.slice(0, d), p.slice(d + 1), params);
  });
  return parts.length ? `(${parts.join(' OR ')})` : 'TRUE';
}

export async function handleTable(sql, table, url, headers) {
  const meta = TABLES[table];
  if (!meta) throw new BadRequest(`unknown table ${table}`);
  const cols = (await columns(sql))[table];
  if (!cols) throw new BadRequest(`unknown table ${table}`);
  const sp = url.searchParams;
  const params = [];

  const selRaw = sp.get('select') || '*';
  const selected = selRaw === '*'
    ? [...cols.keys()].filter((c) => !meta.hidden.has(c))
    : selRaw.split(',').map((s) => s.trim()).filter(Boolean);
  for (const c of selected) {
    if (!cols.has(c) || meta.hidden.has(c)) throw new BadRequest(`unknown column ${c}`);
  }

  const where = meta.forced ? [meta.forced] : [];
  for (const [k, v] of sp.entries()) {
    if (k === 'or') where.push(orGroup(cols, v, params));
    else if (!RESERVED.has(k)) where.push(condition(cols, k, v, params));
  }

  const order = (sp.get('order') || '').split(',').filter(Boolean).map((o) => {
    const [col, dir = 'asc', nulls] = o.split('.');
    if (!cols.has(col) || !['asc', 'desc'].includes(dir)) throw new BadRequest('bad order');
    const n = nulls === 'nullsfirst' ? ' NULLS FIRST' : nulls === 'nullslast' ? ' NULLS LAST' : '';
    return `${q(col)} ${dir.toUpperCase()}${n}`;
  });

  let offset = parseInt(sp.get('offset') || '0', 10) || 0;
  let limit = sp.has('limit') ? parseInt(sp.get('limit'), 10) : MAX_ROWS;
  const range = (headers.get('range') || '').match(/^(\d+)-(\d+)?$/);
  if (range) {
    offset = parseInt(range[1], 10);
    if (range[2] !== undefined) limit = Math.min(limit, parseInt(range[2], 10) - offset + 1);
  }
  limit = Math.max(0, Math.min(Number.isFinite(limit) ? limit : MAX_ROWS, MAX_ROWS));

  const whereSql = where.length ? ` WHERE ${where.join(' AND ')}` : '';
  const body = `SELECT ${selected.map(q).join(', ')} FROM public.${q(table)}${whereSql}`
    + (order.length ? ` ORDER BY ${order.join(', ')}` : '') + ` LIMIT ${limit} OFFSET ${offset}`;
  const rows = await sql.unsafe(`select coalesce(json_agg(t), '[]')::text as j, count(*)::int as n from (${body}) t`, params);
  const n = rows[0].n;

  let total = '*';
  if (/count=exact/.test(headers.get('prefer') || '')) {
    const c = await sql.unsafe(`select count(*)::int as n from public.${q(table)}${whereSql}`, params);
    total = String(c[0].n);
  }
  const contentRange = n ? `${offset}-${offset + n - 1}/${total}` : `*/${total}`;
  return new Response(rows[0].j, {
    status: 200,
    headers: { 'content-type': 'application/json; charset=utf-8', 'content-range': contentRange },
  });
}

export async function handleRpc(sql, name, body) {
  const rpc = RPCS[name];
  if (!rpc) throw new BadRequest(`unknown rpc ${name}`);
  const rows = await sql.unsafe(rpc.sql, rpc.args(body || {}));
  if (rpc.noContent) return new Response(null, { status: 204 });
  return new Response(rows[0].j, { status: 200, headers: { 'content-type': 'application/json; charset=utf-8' } });
}

export function errorResponse(e) {
  const status = e instanceof BadRequest ? 400 : 503;
  const headers = { 'content-type': 'application/json; charset=utf-8' };
  if (status === 503) headers['retry-after'] = '60';
  return new Response(JSON.stringify({ message: String((e && e.message) || e).slice(0, 200) }), { status, headers });
}

// functions/article.js 용: 기사 1건(공개분) + 관련 기사 타임라인을 한 번에.
export async function fetchArticle(sql, id) {
  const cols = (await columns(sql)).articles;
  const pick = [...cols.keys()].filter((c) => !TABLES.articles.hidden.has(c)).map(q).join(', ');
  const rows = await sql.unsafe(`select row_to_json(t)::text as j from (select ${pick} from public.articles
                                 where id = $1::bigint and is_published = true) t`, [id]);
  return rows.length ? JSON.parse(rows[0].j) : null;
}

export async function fetchRelated(sql, id, limit = 6) {
  try {
    const rows = await sql.unsafe('select coalesce(json_agg(t), \'[]\')::text as j from related_articles($1::bigint, $2::int) t', [id, limit]);
    return JSON.parse(rows[0].j);
  } catch {
    return []; // 타임라인은 부가기능 — 실패해도 본문 렌더는 계속
  }
}
