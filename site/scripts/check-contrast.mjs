import { readFileSync } from 'node:fs';

const css = readFileSync('app/globals.css', 'utf8');
const darkBlock = css.match(/:root\s*\{([\s\S]*?)\}/)[1];
const lightBlock = css.match(/html\[data-theme="light"\]\s*\{([\s\S]*?)\}/)[1];
const parse = (block) =>
  Object.fromEntries([...block.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})/g)].map((m) => [m[1], m[2]]));

const lum = (hex) => {
  const h = hex.replace('#', '');
  const v = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4));
  return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
};
const ratio = (a, b) => {
  const l1 = lum(a), l2 = lum(b);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
};

const failures = [];
for (const [name, tokens] of [['dark', parse(darkBlock)], ['light', parse(lightBlock)]]) {
  for (const key of ['ink', 'muted', 'primary', 'secondary', 'success', 'warning', 'danger', 'info']) {
    for (const bg of ['bg', 'surface']) {
      const value = ratio(tokens[key], tokens[bg]);
      if (value < 4.5) failures.push(`${name}: ${key} on ${bg} = ${value.toFixed(2)} < 4.5`);
    }
  }
  const button = ratio(tokens['on-primary'], tokens.primary);
  if (button < 4.5) failures.push(`${name}: on-primary on primary = ${button.toFixed(2)} < 4.5`);
  const focus = ratio(tokens.primary, tokens.bg);
  if (focus < 3) failures.push(`${name}: focus primary on bg = ${focus.toFixed(2)} < 3`);
}

if (failures.length) {
  console.error('contrast failures:');
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log('contrast OK: dark/light text tokens >= 4.5, focus ring >= 3');
