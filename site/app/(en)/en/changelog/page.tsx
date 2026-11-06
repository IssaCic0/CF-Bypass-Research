import type { Metadata } from 'next';
import Changelog from '@/components/pages/Changelog';
import { buildMetadata } from '@/lib/metadata';

export const metadata: Metadata = buildMetadata('en', 'changelog');

export default function Page() {
  return <Changelog locale="en" />;
}
