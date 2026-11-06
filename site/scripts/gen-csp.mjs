import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const OUT = 'out';
const EXEC_TYPES = new Set(['', 'text/javascript', 'application/javascript', 'module']);

function walk(dir, files = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, files);
    else if (name.endsWith('.html')) files.push(p);
  }
  return files;
}

function inlineScripts(html) {
  const out = [];
  const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
  let m;
  while ((m = re.exec(html))) {
    const attrs = m[1] || '';
    const body = m[2] || '';
    if (/\bsrc\s*=/i.test(attrs)) continue;
    const type = (attrs.match(/\btype\s*=\s*["']([^"']+)["']/i) || [])[1] || '';
    if (!EXEC_TYPES.has(type.toLowerCase())) continue;
    out.push(body);
  }
  return out;
}

function hash(body) {
  return `sha256-${createHash('sha256').update(body, 'utf8').digest('base64')}`;
}

const htmlFiles = walk(OUT);
const hashes = new Set();
for (const f of htmlFiles) for (const body of inlineScripts(readFileSync(f, 'utf8'))) hashes.add(hash(body));

const list = [...hashes].sort();
const scriptHashes = list.map((h) => `'${h}'`).join(' ');
const headerCsp = [
  "default-src 'self'",
  `script-src 'self' ${scriptHashes}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "media-src 'none'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
  'upgrade-insecure-requests',
].join('; ');

const metaCsp = headerCsp.replace("; frame-ancestors 'none'", '');

writeFileSync(join(OUT, '_headers'), `/*
  Content-Security-Policy: ${headerCsp}
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Resource-Policy: same-origin
`);

const meta = `<meta http-equiv="Content-Security-Policy" content="${metaCsp.replace(/"/g, '&quot;')}">`;
for (const f of htmlFiles) {
  let html = readFileSync(f, 'utf8');
  html = html.replace(/<meta http-equiv="Content-Security-Policy"[^>]*>/i, '');
  const charset = /<meta\s+charset=["']utf-8["'][^>]*>/i;
  if (charset.test(html)) html = html.replace(charset, (m) => `${m}${meta}`);
  else html = html.replace(/<head(\s[^>]*)?>/i, (m) => `${m}${meta}`);
  writeFileSync(f, html);
}

console.log(`inline script hashes: ${list.length}`);
console.log(`html files: ${htmlFiles.length}`);
console.log(`_headers written: ${existsSync(join(OUT, '_headers'))}`);
