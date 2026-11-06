import type { Metadata } from 'next';
import Security from '@/components/pages/Security';
import { buildMetadata } from '@/lib/metadata';

export const metadata: Metadata = buildMetadata('zh', 'security');

export default function Page() {
  return <Security locale="zh" />;
}
