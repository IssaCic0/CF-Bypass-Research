import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { DICT, NAV_KEYS, PAGES } from '../../content/site.ts';
import { BENCHMARKS } from '../../data/benchmarks.ts';

const LOCALES = ['zh', 'en'];

function sameShape(a, b, at = '') {
  assert.equal(typeof a, typeof b, `${at}: type`);
  if (Array.isArray(a)) {
    assert.equal(a.length, b.length, `${at}: array length`);
    a.forEach((item, index) => sameShape(item, b[index], `${at}[${index}]`));
    return;
  }
  if (a && typeof a === 'object') {
    assert.deepEqual(Object.keys(a).sort(), Object.keys(b).sort(), `${at}: keys`);
    for (const key of Object.keys(a)) sameShape(a[key], b[key], `${at}.${key}`);
  }
}

test('zh/en dictionaries have identical shape', () => {
  sameShape(DICT.zh, DICT.en, 'DICT');
});

test('every nav page has metadata in both locales', () => {
  for (const locale of LOCALES) {
    for (const key of NAV_KEYS) {
      const meta = DICT[locale].meta[key];
      assert.ok(meta, `${locale}.${key}: missing meta`);
      assert.ok(meta.title.length > 0, `${locale}.${key}: empty title`);
      assert.ok(meta.description.length > 0, `${locale}.${key}: empty description`);
    }
  }
});

test('titles are unique and within length limits', () => {
  const titles = [];
  for (const locale of LOCALES) {
    const max = locale === 'zh' ? 45 : 60;
    for (const key of NAV_KEYS) {
      const title = DICT[locale].meta[key].title;
      assert.ok(title.length <= max, `${locale}.${key}: title ${title.length} > ${max}`);
      titles.push(title);
    }
  }
  assert.equal(new Set(titles).size, titles.length, 'duplicate titles');
});

test('descriptions are unique and 80–160 chars', () => {
  const descriptions = [];
  for (const locale of LOCALES) {
    for (const key of NAV_KEYS) {
      const description = DICT[locale].meta[key].description;
      assert.ok(description.length >= 80, `${locale}.${key}: description ${description.length} < 80`);
      assert.ok(description.length <= 160, `${locale}.${key}: description ${description.length} > 160`);
      descriptions.push(description);
    }
  }
  assert.equal(new Set(descriptions).size, descriptions.length, 'duplicate descriptions');
});

test('indexable surface is six pages per locale (12 total)', () => {
  const indexable = Object.values(PAGES).filter((page) => page.index);
  assert.equal(indexable.length, 6);
  assert.equal(indexable.length * 2, 12);
});

test('benchmark schema, ids, dates and source paths', () => {
  const ids = new Set();
  for (const b of BENCHMARKS) {
    assert.ok(b.id, 'missing id');
    assert.ok(!ids.has(b.id), `duplicate benchmark id ${b.id}`);
    ids.add(b.id);
    assert.ok(b.label.zh && b.label.en, `${b.id}: missing label`);
    assert.ok(b.conditions.zh && b.conditions.en, `${b.id}: missing conditions`);
    assert.ok(Number.isFinite(b.value), `${b.id}: value not finite`);
    if (b.valueMax !== undefined) assert.ok(b.valueMax >= b.value, `${b.id}: valueMax < value`);
    assert.match(b.measuredAt, /^\d{4}-\d{2}(-\d{2})?$/, `${b.id}: bad date ${b.measuredAt}`);
    assert.ok(['verified', 'partial', 'limitation'].includes(b.status), `${b.id}: bad status`);
    const source = b.source.split('#')[0];
    assert.ok(fs.existsSync(path.resolve('..', source)), `${b.id}: source missing ${source}`);
  }
});

test('SEO keyword rows point to real pages and are fresh', () => {
  const data = JSON.parse(fs.readFileSync('seo/keywords.json', 'utf8'));
  assert.ok(data.rows.length >= 10);
  const paths = new Set(
    Object.values(PAGES).flatMap((page) => [page.zh, page.en])
  );
  for (const row of data.rows) {
    assert.ok(row.zh && row.en, 'keyword row missing zh/en');
    assert.ok(paths.has(row.page), `keyword page not in routes: ${row.page}`);
  }
  const reviewed = Date.parse(data.lastReviewed);
  assert.ok(Number.isFinite(reviewed), 'invalid lastReviewed');
  const days = Math.floor((Date.now() - reviewed) / 86_400_000);
  assert.ok(days <= (data.reviewIntervalDays ?? 120), `keywords are ${days} days old`);
});