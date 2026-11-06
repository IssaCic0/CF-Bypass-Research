'use client';

import { usePathname } from 'next/navigation';
import { DICT, NAV_KEYS, PAGES, SITE, type Locale } from '@/content/site';
import ThemeToggle from '@/components/ThemeToggle';

function languageTarget(locale: Locale, pathname: string): string {
  if (locale === 'zh') {
    if (pathname === '/' || pathname === '/styleguide/') return '/en/';
    return `/en${pathname}`;
  }
  if (pathname === '/en/') return '/';
  return pathname.replace(/^\/en/, '') || '/';
}

export default function SiteHeader({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  const pathname = usePathname();
  const other: Locale = locale === 'zh' ? 'en' : 'zh';

  return (
    <header className="site-header">
      <div className="container site-header__inner">
        <a className="brand" href={PAGES.home[locale]}>
          <span className="brand__mark" aria-hidden="true" />
          <span className="brand__text">{SITE.name}</span>
        </a>

        <nav className="site-nav" aria-label={locale === 'zh' ? '主导航' : 'Main navigation'}>
          {NAV_KEYS.map((key) => {
            const path = PAGES[key][locale];
            const active = pathname === path;
            return (
              <a key={key} href={path} aria-current={active ? 'page' : undefined}>
                {d.nav[key]}
              </a>
            );
          })}
        </nav>

        <div className="header-actions">
          <a
            className="icon-button"
            href={languageTarget(locale, pathname)}
            hrefLang={other === 'zh' ? 'zh-CN' : 'en'}
            lang={other === 'zh' ? 'zh-CN' : 'en'}
            aria-label={d.nav.language}
            title={d.nav.language}
          >
            {other === 'zh' ? '中' : 'EN'}
          </a>
          <ThemeToggle label={d.nav.theme} />
          <a
            className="icon-button"
            href={SITE.repo}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={d.nav.github}
            title={d.nav.github}
          >
            GH
          </a>
          <details className="mobile-menu">
            <summary aria-label={d.nav.menu}>
              <span aria-hidden="true">☰</span>
            </summary>
            <nav aria-label={locale === 'zh' ? '移动导航' : 'Mobile navigation'}>
              {NAV_KEYS.map((key) => (
                <a key={key} href={PAGES[key][locale]}>
                  {d.nav[key]}
                </a>
              ))}
            </nav>
          </details>
        </div>
      </div>
    </header>
  );
}