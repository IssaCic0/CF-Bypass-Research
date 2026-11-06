import type { Metadata } from 'next';
import { DICT, PAGES, SITE, type Locale, type NavKey } from '@/content/site';

export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || 'http://localhost:3000';
export const metadataBase = new URL(SITE_URL);

export function buildMetadata(locale: Locale, key: NavKey): Metadata {
  const d = DICT[locale];
  const meta = d.meta[key];
  const path = PAGES[key][locale];
  return {
    metadataBase,
    title: meta.title,
    description: meta.description,
    alternates: {
      canonical: path,
      languages: {
        'zh-CN': PAGES[key].zh,
        en: PAGES[key].en,
        'x-default': PAGES[key].zh,
      },
    },
    openGraph: {
      title: meta.title,
      description: meta.description,
      url: path,
      siteName: SITE.name,
      locale: locale === 'zh' ? 'zh_CN' : 'en_US',
      type: 'website',
    },
    twitter: {
      card: 'summary_large_image',
      title: meta.title,
      description: meta.description,
    },
  };
}

export function styleguideMetadata(locale: Locale): Metadata {
  const d = DICT[locale];
  return {
    metadataBase,
    title: `${d.styleguide.title} — ${SITE.name}`,
    description: d.styleguide.lede,
    robots: { index: false, follow: false },
  };
}