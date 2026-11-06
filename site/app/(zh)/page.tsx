import type { Metadata } from 'next';
import Home from '@/components/pages/Home';
import { buildMetadata } from '@/lib/metadata';

export const metadata: Metadata = buildMetadata('zh', 'home');

export default function Page() {
  return <Home locale="zh" />;
}
