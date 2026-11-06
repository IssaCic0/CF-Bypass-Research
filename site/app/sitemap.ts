import type { MetadataRoute } from 'next';
import { PAGES } from '@/content/site';
import { allRoutes } from '@/lib/routes';
import { SITE_URL } from '@/lib/metadata';

export const dynamic = 'force-static';

export default function sitemap(): MetadataRoute.Sitemap {
  return allRoutes()
    .filter((route) => PAGES[route.key].index)
    .map((route) => ({
      url: `${SITE_URL}${route.path}`,
      lastModified: new Date('2026-11-06'),
      changeFrequency: 'weekly' as const,
      priority: route.key === 'home' ? 1 : 0.7,
      alternates: {
        languages: {
          'zh-CN': `${SITE_URL}${PAGES[route.key].zh}`,
          en: `${SITE_URL}${PAGES[route.key].en}`,
        },
      },
    }));
}