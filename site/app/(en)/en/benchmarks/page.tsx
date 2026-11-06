import type { Metadata } from 'next';
import Benchmarks from '@/components/pages/Benchmarks';
import { buildMetadata } from '@/lib/metadata';

export const metadata: Metadata = buildMetadata('en', 'benchmarks');

export default function Page() {
  return <Benchmarks locale="en" />;
}
