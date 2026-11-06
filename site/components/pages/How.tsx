import { DICT, type Locale } from '@/content/site';
import { breadcrumb } from '@/lib/jsonld';
import JsonLd from '@/components/JsonLd';
import CodeBlock from '@/components/CodeBlock';
import Reveal from '@/components/Reveal';
import { Card, Section } from '@/components/ui';

const AUTO = `curl -X POST http://127.0.0.1:8878/solve \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"url":"https://example.com","route":"auto"}'`;

const TURNSTILE = `curl -X POST http://127.0.0.1:8878/solve \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"url":"https://example.com","want":"turnstile","sitekey":"0x4AAAAAAA..."}'`;

export default function How({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  return (
    <>
      <JsonLd data={breadcrumb(locale, 'how')} />
      <Section titleAs="h1" title={d.how.title} lede={d.how.lede}>
        <div className="grid grid--2">
          {d.how.sections.map((section, index) => (
            <Reveal key={section.title} delay={index * 0.03}>
              <Card>
                <h3>{section.title}</h3>
                <p>{section.body}</p>
              </Card>
            </Reveal>
          ))}
        </div>
      </Section>

      <Section title={d.how.codeTitle} lede={d.how.codeLede} tight>
        <div className="grid grid--2">
          <CodeBlock code={AUTO} label="route=auto" copyLabel={locale === 'zh' ? '复制' : 'Copy'} copiedLabel={locale === 'zh' ? '已复制' : 'Copied'} />
          <CodeBlock code={TURNSTILE} label="turnstile" copyLabel={locale === 'zh' ? '复制' : 'Copy'} copiedLabel={locale === 'zh' ? '已复制' : 'Copied'} />
        </div>
      </Section>
    </>
  );
}