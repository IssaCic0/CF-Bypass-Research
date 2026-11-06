import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';

const config = [
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      // Static export deliberately uses plain anchors: Next Link triggers
      // RSC prefetch 404s and a Canvas removeChild error when leaving the 3D page.
      '@next/next/no-html-link-for-pages': 'off',
    },
  },
  {
    ignores: ['.next/**', 'out/**', 'node_modules/**', 'next-env.d.ts', '.lighthouseci/**', '.lighthouseci-mobile/**'],
  },
];

export default config;
