import { DICT, type Locale } from '@/content/site';
import { breadcrumb } from '@/lib/jsonld';
import JsonLd from '@/components/JsonLd';
import { Card, Notice, Section } from '@/components/ui';

export default function Security({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  return (
    <>
      <JsonLd data={breadcrumb(locale, 'security')} />
      <Section titleAs="h1" title={d.security.title} lede={d.security.lede}>
        <div className="grid grid--2">
          {d.security.sections.map((section) => (
            <Card key={section.title}>
              <h3>{section.title}</h3>
              <p>{section.body}</p>
            </Card>
          ))}
        </div>
      </Section>
      <Section tight>
        <Notice>
          <p>{d.footer.disclaimer}</p>
          <p>{d.footer.trademark}</p>
        </Notice>
      </Section>
    </>
  );
}