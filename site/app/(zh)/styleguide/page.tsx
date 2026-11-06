import type { Metadata } from 'next';
import Styleguide from '@/components/pages/Styleguide';
import { styleguideMetadata } from '@/lib/metadata';

export const metadata: Metadata = styleguideMetadata('zh');

export default function Page() {
  return <Styleguide locale="zh" />;
}
