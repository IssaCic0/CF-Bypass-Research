import { DICT, PAGES, type Locale } from '@/content/site';
import { BENCHMARKS, formatValue } from '@/data/benchmarks';
import { faqPage, softwareApplication, website } from '@/lib/jsonld';
import JsonLd from '@/components/JsonLd';
import SceneShell from '@/components/SceneShell';
import Reveal from '@/components/Reveal';
import CodeBlock from '@/components/CodeBlock';
import { ButtonLink, Card, Metric, Notice, Section, TableWrap } from '@/components/ui';

const CURL = `curl -X POST http://127.0.0.1:8878/solve \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"url":"https://example.com","want":"clearance"}'`;

const GO = `client := cfsolve.New("http://127.0.0.1:8878", cfsolve.WithToken(token))
req, _ := http.NewRequestWithContext(ctx, http.MethodGet, "https://example.com", nil)
resp, err := client.Do(ctx, req) // cache hit → no Python, no browser`;

const PY = `import requests
r = requests.post(
    "http://127.0.0.1:8878/solve",
    headers={"Authorization": f"Bearer {token}"},
    json={"url": "https://example.com", "want": "clearance"},
    timeout=90,
)
print(r.json())  # cf_clearance + user_agent + attempts`;

export default function Home({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  const wallRows = ['solve.cfsolve.same-site', 'solve.camoufox.same-site', 'turnstile.official']
    .map((id) => BENCHMARKS.find((b) => b.id === id))
    .filter((b): b is NonNullable<typeof b> => Boolean(b));

  return (
    <>
      <JsonLd data={softwareApplication(locale)} />
      <JsonLd data={website(locale)} />
      <JsonLd data={faqPage(locale)} />

      <section className="hero">
        <div className="container hero__grid">
          <div>
            <span className="hero__eyebrow">{locale === 'zh' ? 'AUTHORIZED TESTING ONLY' : 'AUTHORIZED TESTING ONLY'}</span>
            <h1>{d.home.h1}</h1>
            <p className="hero__lede">{d.home.lede}</p>
            <div className="hero__actions">
              <ButtonLink href={PAGES.docs[locale]}>{d.home.ctaPrimary}</ButtonLink>
              <ButtonLink href={PAGES.how[locale]} variant="ghost">
                {d.home.ctaSecondary}
              </ButtonLink>
            </div>
            <div className="metrics">
              {d.home.metrics.map((metric) => (
                <Metric key={metric.label} value={metric.value} label={metric.label} note={metric.note} />
              ))}
            </div>
          </div>
          <SceneShell variant="wall" locale={locale} />
        </div>
      </section>

      <Section title={d.home.capabilitiesTitle}>
        <div className="grid grid--3">
          {d.home.capabilities.map((item, index) => (
            <Reveal key={item.title} delay={index * 0.04}>
              <Card>
                <h3>{item.title}</h3>
                <p>{item.body}</p>
              </Card>
            </Reveal>
          ))}
        </div>
      </Section>

      <Section title={d.home.pipelineTitle} lede={d.home.pipelineLede} id="pipeline">
        <div className="grid grid--2">
          <SceneShell variant="pipeline" locale={locale} />
          <div>
            <div className="pipeline">
              <div className="pipeline__row" aria-hidden="true">
                {d.home.stages.map((stage, index) => (
                  <span key={stage.name} className="pipeline__row">
                    <span className="pipeline__node">{stage.name}</span>
                    {index < d.home.stages.length - 1 && <span className="pipeline__arrow">→</span>}
                  </span>
                ))}
              </div>
              <div className="pipeline__list">
                {d.home.stages.map((stage) => (
                  <div className="pipeline__item" key={stage.name}>
                    <strong>{stage.name}</strong>
                    <span>{stage.body}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </Section>

      <Section title={d.home.sceneTitle} lede={d.home.sceneLede} tight>
        <TableWrap>
          <table>
            <caption>{locale === 'zh' ? '场景 A 的等价数据（静态降级时同样可见）' : 'Equivalent data for scene A (also visible in static fallback)'}</caption>
            <thead>
              <tr>
                <th scope="col">{locale === 'zh' ? '挑战类型' : 'Challenge'}</th>
                <th scope="col">{locale === 'zh' ? '实测' : 'Measured'}</th>
                <th scope="col">{locale === 'zh' ? '条件' : 'Conditions'}</th>
              </tr>
            </thead>
            <tbody>
              {wallRows.map((row) => (
                <tr key={row.id}>
                  <td>{row.label[locale]}</td>
                  <td className="num">{formatValue(row, locale)}</td>
                  <td>{row.conditions[locale]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      </Section>

      <Section title={d.home.limitationsTitle}>
        <Notice>
          <ul>
            {d.home.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Notice>
      </Section>

      <Section title={d.home.faqTitle}>
        <div className="faq">
          {d.home.faqs.map((item) => (
            <details key={item.q}>
              <summary>{item.q}</summary>
              <div>{item.a}</div>
            </details>
          ))}
        </div>
      </Section>

      <Section title={d.home.codeTitle} lede={d.home.codeLede} id="code">
        <div className="grid grid--2">
          <CodeBlock code={CURL} label="curl" copyLabel={locale === 'zh' ? '复制' : 'Copy'} copiedLabel={locale === 'zh' ? '已复制' : 'Copied'} />
          <CodeBlock code={GO} label="Go" copyLabel={locale === 'zh' ? '复制' : 'Copy'} copiedLabel={locale === 'zh' ? '已复制' : 'Copied'} />
        </div>
        <div style={{ marginTop: 16 }}>
          <CodeBlock code={PY} label="Python" copyLabel={locale === 'zh' ? '复制' : 'Copy'} copiedLabel={locale === 'zh' ? '已复制' : 'Copied'} />
        </div>
      </Section>

      <Section tight>
        <div className="cta">
          <div>
            <h2 style={{ margin: 0, fontSize: 28, letterSpacing: '-0.02em' }}>{d.home.ctaTitle}</h2>
            <p style={{ color: 'var(--muted)', marginTop: 10 }}>{d.home.ctaBody}</p>
          </div>
          <div className="hero__actions" style={{ marginTop: 0 }}>
            <ButtonLink href={PAGES.benchmarks[locale]}>{d.home.ctaSecondary}</ButtonLink>
            <ButtonLink href={PAGES.security[locale]} variant="ghost">
              {DICT[locale].nav.security}
            </ButtonLink>
          </div>
        </div>
      </Section>
    </>
  );
}