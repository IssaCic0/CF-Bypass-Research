import { chromium } from '@playwright/test';
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

const BASE = process.env.SITE_BASE_URL || 'http://127.0.0.1:3100';
const OUT = resolve('../plans/证据/ui-v1/site-verify.json');
const ROUTES = [
  '/',
  '/how-it-works/',
  '/benchmarks/',
  '/docs/',
  '/security/',
  '/changelog/',
  '/styleguide/',
  '/en/',
  '/en/how-it-works/',
  '/en/benchmarks/',
  '/en/docs/',
  '/en/security/',
  '/en/changelog/',
  '/en/styleguide/',
];
const axeSource = readFileSync('node_modules/axe-core/axe.min.js', 'utf8');

const report = {
  base: BASE,
  generatedAt: new Date().toISOString(),
  routes: [],
  reducedMotion: null,
  webglDisabled: null,
  notFound: null,
  failures: [],
};

function fail(message) {
  report.failures.push(message);
  console.error(`FAIL ${message}`);
}

function expectedLang(route) {
  return route === '/en' || route.startsWith('/en/') ? 'en' : 'zh-CN';
}

async function checkRoute(page, route) {
  const errors = [];
  const onConsole = (msg) => {
    if (msg.type() === 'error') errors.push(`console: ${msg.text()}`);
  };
  const onPageError = (err) => errors.push(`pageerror: ${err.message}`);
  page.on('console', onConsole);
  page.on('pageerror', onPageError);

  const response = await page.goto(BASE + route, { waitUntil: 'networkidle' });
  if (!response || response.status() !== 200) fail(`${route}: status ${response?.status()}`);
  const data = await page.evaluate(() => {
    const jsonld = Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map((s) => s.textContent || '');
    return {
      lang: document.documentElement.lang,
      title: document.title,
      h1: document.querySelectorAll('h1').length,
      description: document.querySelector('meta[name="description"]')?.getAttribute('content') || '',
      canonical: document.querySelector('link[rel="canonical"]')?.getAttribute('href') || '',
      alternates: document.querySelectorAll('link[rel="alternate"][hreflang]').length,
      robots: document.querySelector('meta[name="robots"]')?.getAttribute('content') || '',
      scene: document.querySelector('.scene')?.dataset.scene || null,
      jsonld,
    };
  });
  if (data.lang !== expectedLang(route)) fail(`${route}: lang ${data.lang} != ${expectedLang(route)}`);
  if (data.h1 !== 1) fail(`${route}: h1 count ${data.h1}`);
  if (!data.title) fail(`${route}: empty title`);
  if (!data.description) fail(`${route}: empty description`);
  const noindex = route.includes('styleguide');
  if (!noindex) {
    if (!data.canonical) fail(`${route}: empty canonical`);
    if (data.alternates < 3) fail(`${route}: hreflang ${data.alternates} < 3`);
  } else if (!data.robots.includes('noindex')) {
    fail(`${route}: styleguide must be noindex`);
  }
  for (const block of data.jsonld) {
    try {
      JSON.parse(block);
    } catch {
      fail(`${route}: invalid JSON-LD`);
    }
  }
  if (errors.length) fail(`${route}: ${errors.join(' | ').slice(0, 300)}`);
  report.routes.push({ route, ...data, jsonld: undefined, errors });
  page.off('console', onConsole);
  page.off('pageerror', onPageError);
}

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || undefined,
  args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  for (const route of ROUTES) await checkRoute(page, route);

  // Axe on every route, served same-origin so the CSP `script-src 'self'` allows it.
  let axeTotal = 0;
  for (const route of ROUTES) {
    await page.route('**/axe.min.js', (r) => r.fulfill({ status: 200, contentType: 'application/javascript', body: axeSource }));
    await page.goto(BASE + route, { waitUntil: 'networkidle' });
    await page.addScriptTag({ url: `${BASE}/axe.min.js` });
    const violations = await page.evaluate(async () => {
      const result = await window.axe.run(document, {
        runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] },
      });
      return result.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.length }));
    });
    axeTotal += violations.length;
    if (violations.length) fail(`${route}: axe ${JSON.stringify(violations)}`);
    await page.unroute('**/axe.min.js');
  }
  report.axeTotal = axeTotal;

  // reduced-motion must not mount WebGL.
  const reduced = await browser.newContext({ reducedMotion: 'reduce' });
  const reducedPage = await reduced.newPage();
  await reducedPage.goto(BASE + '/', { waitUntil: 'networkidle' });
  await reducedPage.waitForTimeout(2000);
  report.reducedMotion = await reducedPage.evaluate(() => ({
    tier: document.querySelector('.scene')?.dataset.scene,
    canvas: document.querySelectorAll('.scene canvas').length,
  }));
  if (report.reducedMotion.tier !== 'static') fail('reduced-motion: expected static');
  if (report.reducedMotion.canvas !== 0) fail('reduced-motion: canvas mounted');
  await reduced.close();
  await context.close();
} finally {
  await browser.close();
}

// WebGL disabled must also fall back to static.
const noGl = await chromium.launch({
  executablePath: process.env.CHROME_PATH || undefined,
  args: ['--disable-webgl'],
});
try {
  const page = await noGl.newPage();
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  report.webglDisabled = await page.evaluate(() => ({
    tier: document.querySelector('.scene')?.dataset.scene,
    canvas: document.querySelectorAll('.scene canvas').length,
  }));
  if (report.webglDisabled.tier !== 'static') fail('webgl-disabled: expected static');
  if (report.webglDisabled.canvas !== 0) fail('webgl-disabled: canvas mounted');
} finally {
  await noGl.close();
}

// 404 page is a real static artifact with a title and noindex.
{
  const browser404 = await chromium.launch({ executablePath: process.env.CHROME_PATH || undefined });
  try {
    const page = await browser404.newPage();
    await page.goto(`${BASE}/404.html`, { waitUntil: 'networkidle' });
    report.notFound = await page.evaluate(() => ({
      title: document.title,
      h1: document.querySelectorAll('h1').length,
      robots: document.querySelector('meta[name="robots"]')?.getAttribute('content') || '',
    }));
    if (!report.notFound.title) fail('404: empty title');
    if (report.notFound.h1 !== 1) fail('404: h1 count');
    if (!report.notFound.robots.includes('noindex')) fail('404: missing noindex');
  } finally {
    await browser404.close();
  }
}

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, JSON.stringify(report, null, 2), 'utf8');
console.log(`routes checked: ${report.routes.length}`);
console.log(`axe violations: ${report.axeTotal}`);
console.log(`reduced-motion: ${JSON.stringify(report.reducedMotion)}`);
console.log(`webgl-disabled: ${JSON.stringify(report.webglDisabled)}`);
console.log(`evidence: ${OUT}`);
if (report.failures.length) {
  console.error(`FAILURES: ${report.failures.length}`);
  process.exit(1);
}
console.log('site verification OK');