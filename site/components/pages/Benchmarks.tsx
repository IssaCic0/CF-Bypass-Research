import { DICT, type Locale } from '@/content/site';
import { BENCHMARKS, formatValue, sourceUrl } from '@/data/benchmarks';
import { breadcrumb } from '@/lib/jsonld';
import JsonLd from '@/components/JsonLd';
import BarChart from '@/components/BarChart';
import { Notice, Section, TableWrap } from '@/components/ui';

export default function Benchmarks({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  const chart = [
    BENCHMARKS.find((b) => b.id === 'solve.cfsolve.same-site'),
    BENCHMARKS.find((b) => b.id === 'solve.camoufox.same-site'),
    BENCHMARKS.find((b) => b.id === 'route.auto.no-shield'),
    BENCHMARKS.find((b) => b.id === 'turnstile.official'),
  ]
    .filter((b): b is NonNullable<typeof b> => Boolean(b))
    .map((b) => ({ label: b.label[locale], value: b.valueMax ?? b.value, display: formatValue(b, locale) }));

  return (
    <>
      <JsonLd data={breadcrumb(locale, 'benchmarks')} />
      <Section titleAs="h1" title={d.benchmarks.title} lede={d.benchmarks.lede}>
        <div className="grid grid--2">
          <div className="card">
            <h3>{d.benchmarks.methodTitle}</h3>
            <ul style={{ margin: 0, paddingLeft: 20, color: 'var(--muted)', fontSize: 14.5 }}>
              {d.benchmarks.method.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
          <div className="card">
            <h3>{d.benchmarks.tableTitle}</h3>
            <BarChart items={chart} title={locale === 'zh' ? '求解延迟（秒，数值越低越好）' : 'Solve latency (seconds, lower is better)'} />
          </div>
        </div>
      </Section>

      <Section title={d.benchmarks.tableTitle} tight>
        <TableWrap>
          <table>
            <caption>
              {locale === 'zh'
                ? '所有数字来自本机真实运行；日期与来源见列。单位与条件以表中为准。'
                : 'All numbers come from real local runs; see the date and source columns. Units and conditions are in the table.'}
            </caption>
            <thead>
              <tr>
                <th scope="col">{locale === 'zh' ? '指标' : 'Metric'}</th>
                <th scope="col">{locale === 'zh' ? '值' : 'Value'}</th>
                <th scope="col">{locale === 'zh' ? '条件' : 'Conditions'}</th>
                <th scope="col">{locale === 'zh' ? '日期' : 'Date'}</th>
                <th scope="col">{locale === 'zh' ? '来源' : 'Source'}</th>
              </tr>
            </thead>
            <tbody>
              {BENCHMARKS.map((b) => (
                <tr key={b.id}>
                  <td>{b.label[locale]}</td>
                  <td className="num">{formatValue(b, locale)}</td>
                  <td>{b.conditions[locale]}</td>
                  <td className="num">{b.measuredAt}</td>
                  <td>
                    <a href={sourceUrl(b.source)} target="_blank" rel="noopener noreferrer">
                      {b.source}
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      </Section>

      <Section title={d.benchmarks.limitationsTitle}>
        <Notice>
          <ul>
            {d.benchmarks.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Notice>
      </Section>
    </>
  );
}