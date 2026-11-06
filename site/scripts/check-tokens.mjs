import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const ROOTS = ['components', 'app'];
const SKIP_DIR = ['three'];
const SKIP_FILE = [/opengraph-image\.tsx$/, /layout\.tsx$/];
const HEX = /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/;

function walk(dir, files = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) {
      if (SKIP_DIR.includes(name)) continue;
      walk(p, files);
    } else if (name.endsWith('.tsx') || name.endsWith('.ts')) {
      if (SKIP_FILE.some((re) => re.test(p))) continue;
      files.push(p);
    }
  }
  return files;
}

const files = ROOTS.flatMap((r) => walk(r));
const bad = [];
for (const f of files) {
  const text = readFileSync(f, 'utf8');
  text.split(/\r?\n/).forEach((line, i) => {
    if (HEX.test(line)) bad.push(`${f}:${i + 1}: ${line.trim().slice(0, 120)}`);
  });
}

if (bad.length) {
  console.error('Hardcoded colors found (use var(--token) or the 3D palette whitelist):');
  for (const b of bad.slice(0, 30)) console.error(`  - ${b}`);
  process.exit(1);
}
console.log(`token check OK: ${files.length} files scanned, 0 hardcoded colors`);
