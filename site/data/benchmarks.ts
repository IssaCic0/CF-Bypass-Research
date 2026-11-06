import type { Locale } from '@/content/site';

export type BenchmarkUnit = 'ms' | 's' | 'qps' | 'bytes' | 'mb' | 'count' | 'percent';
export type BenchmarkStatus = 'verified' | 'partial' | 'limitation';

export type Benchmark = {
  id: string;
  label: Record<Locale, string>;
  value: number;
  valueMax?: number;
  unit: BenchmarkUnit;
  measuredAt: string;
  conditions: Record<Locale, string>;
  source: string;
  status: BenchmarkStatus;
};

export const BENCHMARKS: Benchmark[] = [
  {
    id: 'solve.cfsolve.same-site',
    label: { zh: '同站 A/B：cfsolve 纯协议', en: 'Same-site A/B: cfsolve pure protocol' },
    value: 18.43,
    unit: 's',
    measuredAt: '2026-11-01',
    conditions: { zh: 'glassdoor.co.uk；同站 A/B；cfsolve', en: 'glassdoor.co.uk; same-site A/B; cfsolve' },
    source: 'docs/对接-new-api.md',
    status: 'verified',
  },
  {
    id: 'solve.camoufox.same-site',
    label: { zh: '同站 A/B：Camoufox', en: 'Same-site A/B: Camoufox' },
    value: 19.56,
    unit: 's',
    measuredAt: '2026-11-01',
    conditions: { zh: '同一目标与时间窗；浏览器对照组', en: 'Same target and window; browser control' },
    source: 'docs/对接-new-api.md',
    status: 'verified',
  },
  {
    id: 'replay.cached.p99',
    label: { zh: '缓存热路径 p99', en: 'Cached hot-path p99' },
    value: 2.93,
    unit: 'ms',
    measuredAt: '2026-11-03',
    conditions: { zh: '离线压测；5589 QPS；0 错', en: 'Offline load test; 5589 QPS; 0 errors' },
    source: 'CHANGELOG.md',
    status: 'verified',
  },
  {
    id: 'replay.cached.qps',
    label: { zh: '缓存热路径 QPS', en: 'Cached hot-path QPS' },
    value: 5589,
    unit: 'qps',
    measuredAt: '2026-11-03',
    conditions: { zh: '离线压测；Go 客户端直接回放', en: 'Offline load test; Go client replays directly' },
    source: 'CHANGELOG.md',
    status: 'verified',
  },
  {
    id: 'route.auto.no-shield',
    label: { zh: 'route=auto 无盾站放行', en: 'route=auto no-shield pass' },
    value: 0.55,
    unit: 's',
    measuredAt: '2026-11-01',
    conditions: { zh: '真机 example.com；0 次求解', en: 'Real example.com; 0 solves' },
    source: 'docs/00-索引.md',
    status: 'verified',
  },
  {
    id: 'turnstile.official',
    label: { zh: 'Turnstile 官方测试 sitekey', en: 'Turnstile official test sitekey' },
    value: 6,
    valueMax: 8,
    unit: 's',
    measuredAt: '2026-11',
    conditions: { zh: '官方测试 sitekey；纯协议', en: 'Official test sitekey; pure protocol' },
    source: 'README.md',
    status: 'verified',
  },
  {
    id: 'clearance.bytes',
    label: { zh: 'cf_clearance 大小', en: 'cf_clearance size' },
    value: 463,
    unit: 'bytes',
    measuredAt: '2026-11',
    conditions: { zh: '端到端实测样本', en: 'End-to-end measured sample' },
    source: 'README.md',
    status: 'verified',
  },
  {
    id: 'pure-only.memory',
    label: { zh: '--pure-only 内存', en: '--pure-only memory' },
    value: 72,
    unit: 'mb',
    measuredAt: '2026-11',
    conditions: { zh: '无浏览器进程', en: 'No browser process' },
    source: 'README.md',
    status: 'verified',
  },
  {
    id: 'tests.python',
    label: { zh: 'Python 测试', en: 'Python tests' },
    value: 971,
    unit: 'count',
    measuredAt: '2026-11-05',
    conditions: { zh: 'pytest；离线', en: 'pytest; offline' },
    source: 'docs/facts.json',
    status: 'verified',
  },
  {
    id: 'coverage.python',
    label: { zh: 'Python 覆盖率', en: 'Python coverage' },
    value: 90,
    unit: 'percent',
    measuredAt: '2026-11-05',
    conditions: { zh: '项目口径', en: 'Project baseline' },
    source: 'docs/facts.json',
    status: 'verified',
  },
  {
    id: 'tests.go',
    label: { zh: 'Go 测试', en: 'Go tests' },
    value: 46,
    unit: 'count',
    measuredAt: '2026-11-05',
    conditions: { zh: 'go test', en: 'go test' },
    source: 'docs/facts.json',
    status: 'verified',
  },
  {
    id: 'findings.total',
    label: { zh: '研究结论条数', en: 'Findings' },
    value: 54,
    unit: 'count',
    measuredAt: '2026-11-05',
    conditions: { zh: 'findings.json 校验通过', en: 'findings.json validated' },
    source: 'docs/facts.json',
    status: 'verified',
  },
];

const UNIT: Record<BenchmarkUnit, { zh: string; en: string }> = {
  ms: { zh: 'ms', en: 'ms' },
  s: { zh: 's', en: 's' },
  qps: { zh: 'QPS', en: 'QPS' },
  bytes: { zh: 'B', en: 'B' },
  mb: { zh: 'MB', en: 'MB' },
  count: { zh: '', en: '' },
  percent: { zh: '%', en: '%' },
};

export function formatValue(b: Benchmark, locale: Locale): string {
  const n = Number.isInteger(b.value) ? b.value.toString() : b.value.toFixed(2).replace(/0$/, '');
  const max = b.valueMax !== undefined ? `–${b.valueMax}` : '';
  return `${n}${max} ${UNIT[b.unit][locale]}`.trim();
}

export function sourceUrl(source: string): string {
  const file = source.split('#')[0];
  return `https://github.com/lza6/CF-Bypass-Research/blob/master/${file}`;
}