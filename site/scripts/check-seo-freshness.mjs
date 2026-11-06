import { readFileSync } from 'node:fs';

const DAY = 86_400_000;
const data = JSON.parse(readFileSync('seo/keywords.json', 'utf8'));
const reviewed = Date.parse(data.lastReviewed);
if (!Number.isFinite(reviewed)) {
  console.error('seo/keywords.json: invalid lastReviewed');
  process.exit(1);
}
const days = Math.floor((Date.now() - reviewed) / DAY);
const limit = data.reviewIntervalDays ?? 120;
if (!Array.isArray(data.rows) || data.rows.length < 10) {
  console.error('seo/keywords.json: need at least 10 rows');
  process.exit(1);
}
for (const row of data.rows) {
  if (!row.zh || !row.en || !row.page || !row.tier) {
    console.error('seo/keywords.json: row missing zh/en/page/tier', row);
    process.exit(1);
  }
}
if (days > limit) {
  console.error(`seo/keywords.json: lastReviewed ${days} days ago (> ${limit}); refresh keywords`);
  process.exit(1);
}
console.log(`SEO freshness OK: ${data.rows.length} rows, reviewed ${days} days ago (limit ${limit})`);
