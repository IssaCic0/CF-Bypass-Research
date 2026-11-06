import type { Metadata } from 'next';
import Docs from '@/components/pages/Docs';
import { buildMetadata } from '@/lib/metadata';

export const metadata: Metadata = buildMetadata('en', 'docs');

export default function Page() {
  return <Docs locale="en" />;
}
