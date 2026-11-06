import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
  reactStrictMode: true,
  productionBrowserSourceMaps: false,
  basePath: process.env.SITE_BASE_PATH || '',
  turbopack: { root: process.cwd() },
  experimental: { globalNotFound: true },
};

export default nextConfig;
