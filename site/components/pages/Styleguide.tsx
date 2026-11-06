import { DICT, type Locale } from '@/content/site';
import CodeBlock from '@/components/CodeBlock';
import { Badge, ButtonLink, Card, Section } from '@/components/ui';

const SWATCHES = [
  { name: 'bg', value: 'var(--bg)' },
  { name: 'surface', value: 'var(--surface)' },
  { name: 'surface-2', value: 'var(--surface-2)' },
  { name: 'line', value: 'var(--line)' },
  { name: 'ink', value: 'var(--ink)' },
  { name: 'muted', value: 'var(--muted)' },
  { name: 'primary', value: 'var(--primary)' },
  { name: 'secondary', value: 'var(--secondary)' },
  { name: 'success', value: 'var(--success)' },
  { name: 'warning', value: 'var(--warning)' },
  { name: 'danger', value: 'var(--danger)' },
  { name: 'info', value: 'var(--info)' },
];

export default function Styleguide({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  return (
    <>
      <Section titleAs="h1" title={d.styleguide.title} lede={d.styleguide.lede}>
        <h3>{d.styleguide.paletteTitle}</h3>
        <div className="styleguide__swatches">
          {SWATCHES.map((swatch) => (
            <div className="swatch" key={swatch.name}>
              <div className="swatch__color" style={{ background: swatch.value }} />
              <div className="swatch__meta">
                {swatch.name}
                <br />
                {swatch.value}
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section title={d.styleguide.typeTitle} tight>
        <div className="type-row" style={{ fontSize: 40, fontWeight: 700, letterSpacing: '-0.02em' }}>
          Display 40 / 700
        </div>
        <div className="type-row" style={{ fontSize: 24, fontWeight: 600 }}>
          Heading 24 / 600
        </div>
        <div className="type-row" style={{ fontSize: 16 }}>
          Body 16 / 400 — 中文正文与 English body share the same scale.
        </div>
        <div className="type-row" style={{ fontSize: 13, color: 'var(--muted)' }}>
          Caption 13 / muted
        </div>
      </Section>

      <Section title={d.styleguide.componentsTitle} tight>
        <div className="grid grid--3">
          <Card>
            <h3>Buttons</h3>
            <div className="hero__actions" style={{ marginTop: 12 }}>
              <ButtonLink href="#">Primary</ButtonLink>
              <ButtonLink href="#" variant="ghost">
                Ghost
              </ButtonLink>
            </div>
          </Card>
          <Card>
            <h3>Badges</h3>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
              <Badge>neutral</Badge>
              <Badge tone="ok">verified</Badge>
              <Badge tone="warn">partial</Badge>
              <Badge tone="danger">limitation</Badge>
            </div>
          </Card>
          <Card>
            <h3>Card</h3>
            <p>Surface, 1px border, 16px radius, 22px padding. Hover lifts 2px.</p>
          </Card>
        </div>
        <div style={{ marginTop: 16 }}>
          <CodeBlock
            code={'const answer = 42; // token colors come from CSS variables'}
            label="TypeScript"
            copyLabel={locale === 'zh' ? '复制' : 'Copy'}
            copiedLabel={locale === 'zh' ? '已复制' : 'Copied'}
          />
        </div>
      </Section>
    </>
  );
}