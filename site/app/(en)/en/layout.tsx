import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import '../../globals.css';
import ThemeScript from '@/components/ThemeScript';
import SiteHeader from '@/components/SiteHeader';
import SiteFooter from '@/components/SiteFooter';
import { DICT, SITE } from '@/content/site';
import { metadataBase } from '@/lib/metadata';

export const metadata: Metadata = {
  metadataBase,
  title: SITE.name,
  description: DICT.en.meta.home.description,
  icons: { icon: '/favicon.svg' },
};

export const viewport: Viewport = {
  themeColor: [
    { media: '(prefers-color-scheme: dark)', color: '#0f1419' },
    { media: '(prefers-color-scheme: light)', color: '#f6f8fa' },
  ],
  colorScheme: 'dark light',
};

export default function EnLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta charSet="utf-8" />
      </head>
      <body>
        <a className="skip-link" href="#main">
          Skip to main content
        </a>
        <ThemeScript />
        <SiteHeader locale="en" />
        <main id="main">{children}</main>
        <SiteFooter locale="en" />
      </body>
    </html>
  );
}