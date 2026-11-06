import { DICT, PAGES, SITE, type Locale, type PageKey } from '@/content/site';
import { SITE_URL } from '@/lib/metadata';

const LICENSE_URL = `${SITE.repo}/blob/master/LICENSE`;

export function softwareApplication(locale: Locale) {
  const d = DICT[locale];
  return {
    '@context': 'https://schema.org',
    '@type': 'SoftwareApplication',
    name: SITE.name,
    applicationCategory: 'DeveloperApplication',
    operatingSystem: 'Linux, Windows, macOS, Docker',
    description: d.meta.home.description,
    url: `${SITE_URL}${PAGES.home[locale]}`,
    codeRepository: SITE.repo,
    license: LICENSE_URL,
    inLanguage: locale === 'zh' ? 'zh-CN' : 'en',
    offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
  };
}

export function website(locale: Locale) {
  const d = DICT[locale];
  return {
    '@context': 'https://schema.org',
    '@type': 'WebSite',
    name: SITE.name,
    url: `${SITE_URL}${PAGES.home[locale]}`,
    inLanguage: locale === 'zh' ? 'zh-CN' : 'en',
    description: d.meta.home.description,
  };
}

export function faqPage(locale: Locale) {
  const d = DICT[locale];
  return {
    '@context': 'https://schema.org',
    '@type': 'FAQPage',
    mainEntity: d.home.faqs.map((item) => ({
      '@type': 'Question',
      name: item.q,
      acceptedAnswer: { '@type': 'Answer', text: item.a },
    })),
  };
}

export function breadcrumb(locale: Locale, key: PageKey) {
  const d = DICT[locale];
  const home = PAGES.home[locale];
  const current = PAGES[key][locale];
  return {
    '@context': 'https://schema.org',
    '@type': 'BreadcrumbList',
    itemListElement: [
      { '@type': 'ListItem', position: 1, name: d.nav.home, item: `${SITE_URL}${home}` },
      { '@type': 'ListItem', position: 2, name: d.nav[key as keyof typeof d.nav] ?? key, item: `${SITE_URL}${current}` },
    ],
  };
}