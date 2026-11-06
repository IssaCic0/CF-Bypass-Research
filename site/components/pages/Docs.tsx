import { DICT, type Locale } from '@/content/site';
import { breadcrumb } from '@/lib/jsonld';
import JsonLd from '@/components/JsonLd';
import CodeBlock from '@/components/CodeBlock';
import { Section, TableWrap } from '@/components/ui';

export default function Docs({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  return (
    <>
      <JsonLd data={breadcrumb(locale, 'docs')} />
      <Section titleAs="h1" title={d.docs.title} lede={d.docs.lede}>
        <CodeBlock
          code={d.docs.quickstart.join('\n')}
          label="PowerShell / bash"
          copyLabel={locale === 'zh' ? '复制' : 'Copy'}
          copiedLabel={locale === 'zh' ? '已复制' : 'Copied'}
        />
      </Section>

      <Section title={d.docs.endpointsTitle} tight>
        <TableWrap>
          <table>
            <thead>
              <tr>
                <th scope="col">{locale === 'zh' ? '端点' : 'Endpoint'}</th>
                <th scope="col">{locale === 'zh' ? '说明' : 'Description'}</th>
              </tr>
            </thead>
            <tbody>
              {d.docs.endpoints.map((endpoint) => (
                <tr key={endpoint.path}>
                  <td>
                    <code>{endpoint.path}</code>
                  </td>
                  <td>{endpoint.body}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      </Section>

      <Section title={d.docs.linksTitle} tight>
        <ul>
          {d.docs.links.map((link) => (
            <li key={link.href}>
              <a href={link.href} target="_blank" rel="noopener noreferrer">
                {link.label}
              </a>
            </li>
          ))}
        </ul>
      </Section>
    </>
  );
}