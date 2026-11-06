import { DICT, type Locale } from '@/content/site';
import { breadcrumb } from '@/lib/jsonld';
import JsonLd from '@/components/JsonLd';
import { Card, Section } from '@/components/ui';

export default function Changelog({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  return (
    <>
      <JsonLd data={breadcrumb(locale, 'changelog')} />
      <Section titleAs="h1" title={d.changelog.title} lede={d.changelog.lede}>
        <div className="grid grid--3">
          {d.changelog.items.map((item) => (
            <Card key={item.version}>
              <div style={{ color: 'var(--muted)', fontSize: 12, fontVariantNumeric: 'tabular-nums' }}>{item.date}</div>
              <h3>{item.version}</h3>
              <p>{item.body}</p>
            </Card>
          ))}
        </div>
      </Section>
    </>
  );
}