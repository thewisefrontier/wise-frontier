// node server/pgrest.test.mjs — 실제 DB 없이 쿼리 변환 결과와 주입 차단을 검사한다.
import assert from 'node:assert/strict';
import { handleTable } from './pgrest.js';

const COLS = [['id', 'bigint'], ['title_ko', 'text'], ['summary_ko', 'text'], ['full_text', 'text'],
  ['created_at', 'text'], ['source', 'text'], ['is_published', 'boolean'], ['countries', 'text[]'],
  ['country', 'text'], ['category', 'text'], ['subcategory', 'text']]
  .map(([c, d]) => ({ table_name: 'articles', column_name: c, sqltype: d }));

const calls = [];
const sql = () => Promise.resolve(COLS);
sql.unsafe = (q, params) => { calls.push({ q, params }); return Promise.resolve([{ j: '[]', n: 0 }]); };
const hdr = (o) => ({ get: (k) => o[k.toLowerCase()] ?? null });
const run = async (qs, h = {}) => {
  calls.length = 0;
  const r = await handleTable(sql, 'articles', new URL('https://x/rest/v1/articles?' + qs), hdr(h));
  return { status: r.status, range: r.headers.get('content-range'), calls: [...calls] };
};

// 검색(index.html): OR + ilike, 강제 공개 조건, 행 상한
let r = await run('select=id,title_ko&source=eq.NewsFinal&is_published=eq.true&or=(title_ko.ilike.*케냐*,summary_ko.ilike.*케냐*)&order=created_at.desc&limit=50');
assert.match(r.calls[0].q, /WHERE is_published = true AND "source" = \$1::text::text AND "is_published" = \$2::text::boolean AND \("title_ko"::text ILIKE \$3::text OR "summary_ko"::text ILIKE \$4::text\)/);
assert.deepEqual(r.calls[0].params, ['NewsFinal', 'true', '%케냐%', '%케냐%']);
assert.match(r.calls[0].q, /ORDER BY "created_at" DESC LIMIT 50 OFFSET 0/);

// 아카이브: 같은 키 반복(gte+lte), count=exact, Range
r = await run('select=*&created_at=gte.2026-10-01&created_at=lte.2026-10-19 23:59&category=eq.사회&order=created_at.desc&limit=20&offset=40',
  { prefer: 'count=exact', range: '40-59' });
assert.equal(r.calls.length, 2);
assert.ok(!r.calls[0].q.includes('"full_text"'), 'full_text는 공개 응답에서 빠져야 함');
assert.match(r.calls[0].q, /LIMIT 20 OFFSET 40/);
assert.deepEqual(r.calls[0].params, ['2026-10-01', '2026-10-19 23:59', '사회']);

// 국가 페이지: 배열 포함 + like 와일드카드, 상한 1000
r = await run('select=id&countries=cs.{케냐}&subcategory=like.digest_*&order=created_at.desc&limit=5000');
assert.match(r.calls[0].q, /"countries" @> \$1::text::text\[\]/);
assert.deepEqual(r.calls[0].params, ['{케냐}', 'digest_%']);
assert.match(r.calls[0].q, /LIMIT 1000 /);

// 주입·비공개 컬럼·없는 컬럼은 거부
for (const bad of ['select=full_text', 'select=id;drop table x', 'order=id;drop', 'nope=eq.1', 'id=xx.1', 'order=id.sideways']) {
  await assert.rejects(run(bad), undefined, bad);
}
console.log('pgrest ok');
