import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';
import '../globals.css';
import ThemeScript from '@/components/ThemeScript';
import SiteHeader from '@/components/SiteHeader';
import SiteFooter from '@/components/SiteFooter';
import { DICT, SITE } from '@/content/site';
import { metadataBase } from '@/lib/metadata';

export const metadata: Metadata = {
  metadataBase,
  title: SITE.name,
  description: DICT.zh.meta.home.description,
  icons: { icon: '/favicon.svg' },
};

export const viewport: Viewport = {
  themeColor: [
    { media: '(prefers-color-scheme: dark)', color: '#0f1419' },
    { media: '(prefers-color-scheme: light)', color: '#f6f8fa' },
  ],
  colorScheme: 'dark light',
};

export default function ZhLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <meta charSet="utf-8" />
      </head>
      <body>
        <a className="skip-link" href="#main">
          跳到主要内容
        </a>
        <ThemeScript />
        <SiteHeader locale="zh" />
        <main id="main">{children}</main>
        <SiteFooter locale="zh" />
      </body>
    </html>
  );
}