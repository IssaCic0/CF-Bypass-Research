import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

const OUT = 'out';
const EXEC_TYPES = new Set(['', 'text/javascript', 'application/javascript', 'module']);
function walk(dir, files = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name); const st = statSync(p);
    if (st.isDirectory()) walk(p, files); else if (name.endsWith('.html')) files.push(p);
  }
  return files;
}
function inlineScripts(html) {
  const out = []; const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi; let m;
  while ((m = re.exec(html))) {
    const attrs = m[1] || ''; const body = m[2] || '';
    if (/\bsrc\s*=/i.test(attrs)) continue;
    const type = (attrs.match(/\btype\s*=\s*["']([^"']+)["']/i) || [])[1] || '';
    if (!EXEC_TYPES.has(type.toLowerCase())) continue;
    out.push(body);
  }
  return out;
}
const hash = (b) => `sha256-${createHash('sha256').update(b, 'utf8').digest('base64')}`;

const fails = [];
if (!existsSync(join(OUT, '_headers'))) fails.push('missing out/_headers');
const headers = existsSync(join(OUT, '_headers')) ? readFileSync(join(OUT, '_headers'), 'utf8') : '';
const headerCsp = (headers.match(/Content-Security-Policy:\s*(.+)/i) || [])[1] || '';
if (!headerCsp) fails.push('missing CSP in _headers');
if (/script-src[^;]*unsafe-inline/i.test(headerCsp)) fails.push('script-src contains unsafe-inline');
if (/script-src[^;]*unsafe-eval/i.test(headerCsp)) fails.push('script-src contains unsafe-eval');
if (!/frame-ancestors\s+'none'/i.test(headerCsp)) fails.push('missing frame-ancestors none');
if (!/object-src\s+'none'/i.test(headerCsp)) fails.push('missing object-src none');
if (!/base-uri\s+'none'/i.test(headerCsp)) fails.push('missing base-uri none');

const files = walk(OUT);
for (const f of files) {
  const html = readFileSync(f, 'utf8');
  const meta = (html.match(/<meta http-equiv="Content-Security-Policy"[^>]*content="([^"]*)"/i) || [])[1] || '';
  if (!meta) fails.push(`${f}: missing meta CSP`);
  if (/script-src[^;]*unsafe-inline/i.test(meta)) fails.push(`${f}: meta script-src unsafe-inline`);
  for (const body of inlineScripts(html)) {
    const h = hash(body);
    if (headerCsp && !headerCsp.includes(`'${h}'`)) fails.push(`${f}: inline hash missing (or unquoted) in _headers: ${h}`);
    if (meta && !meta.includes(`'${h}'`)) fails.push(`${f}: inline hash missing (or unquoted) in meta: ${h}`);
  }
  for (const m of html.matchAll(/<(script|img|iframe|source)\b[^>]*\bsrc="(https?:\/\/[^"]+)"/gi)) {
    fails.push(`${f}: external resource ${m[1]} ${m[2]}`);
  }
  for (const m of html.matchAll(/<link\b[^>]*>/gi)) {
    const tag = m[0];
    const rel = (tag.match(/\brel="([^"]+)"/i) || [])[1] || '';
    const href = (tag.match(/\bhref="([^"]+)"/i) || [])[1] || '';
    if (['stylesheet', 'preload', 'modulepreload', 'icon'].includes(rel.toLowerCase()) && /^https?:\/\//i.test(href)) {
      fails.push(`${f}: external ${rel} ${href}`);
    }
  }
}

if (fails.length) {
  console.error(`CSP check failed (${fails.length}):`);
  for (const f of fails.slice(0, 30)) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`CSP OK: ${files.length} html files, ${(headerCsp.match(/sha256-/g) || []).length} hashes`);
