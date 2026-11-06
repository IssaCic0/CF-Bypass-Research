import { PAGES, type Locale, type PageKey } from '@/content/site';

export function href(locale: Locale, key: PageKey): string {
  return PAGES[key][locale];
}

export function otherLocale(locale: Locale): Locale {
  return locale === 'zh' ? 'en' : 'zh';
}

export function altHref(locale: Locale, key: PageKey): string {
  return PAGES[key][otherLocale(locale)];
}

export const INDEXABLE: PageKey[] = (Object.keys(PAGES) as PageKey[]).filter((key) => PAGES[key].index);

export const NAV_PAGES: PageKey[] = (Object.keys(PAGES) as PageKey[]).filter((key) => PAGES[key].nav);

export type Route = { locale: Locale; key: PageKey; path: string };

export function allRoutes(): Route[] {
  return (['zh', 'en'] as Locale[]).flatMap((locale) =>
    (Object.keys(PAGES) as PageKey[]).map((key) => ({ locale, key, path: PAGES[key][locale] }))
  );
}

export function isIndexable(key: PageKey): boolean {
  return PAGES[key].index;
}