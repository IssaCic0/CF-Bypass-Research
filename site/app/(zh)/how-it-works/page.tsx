import type { Metadata } from 'next';
import How from '@/components/pages/How';
import { buildMetadata } from '@/lib/metadata';

export const metadata: Metadata = buildMetadata('zh', 'how');

export default function Page() {
  return <How locale="zh" />;
}
