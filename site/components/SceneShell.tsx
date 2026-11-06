'use client';

import dynamic from 'next/dynamic';
import { useEffect, useRef, useState } from 'react';
import type { Locale } from '@/content/site';
import SceneBoundary from './three/SceneBoundary';

const ChallengeWall = dynamic(() => import('./three/ChallengeWall'), { ssr: false, loading: () => null });

const WALL_LABELS = ['jsd', 'managed', 'turnstile'];
const PIPE_LABELS = ['Domain', 'Detect', 'Backend', 'Token', 'Replay'];
const Pipeline = dynamic(() => import('./three/Pipeline'), { ssr: false, loading: () => null });

type Tier = 'full' | 'lite' | 'static';
type Variant = 'wall' | 'pipeline';

function detectTier(): Tier {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return 'static';
  const nav = navigator as Navigator & {
    deviceMemory?: number;
    connection?: { saveData?: boolean };
  };
  if (nav.connection?.saveData) return 'static';
  const mem = nav.deviceMemory ?? 8;
  const cores = nav.hardwareConcurrency ?? 8;
  if (mem <= 4 || cores <= 4) return 'static';
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    if (!gl) return 'static';
  } catch {
    return 'static';
  }
  // Touch devices get the static diagram: the WebGL chunk is too expensive for
  // mobile TBT, and the page already has an equivalent table/list.
  if (window.matchMedia('(pointer: coarse)').matches) return 'static';
  if (mem <= 6) return 'lite';
  return 'full';
}

function WallFallback({ locale }: { locale: Locale }) {
  const label = locale === 'zh' ? '挑战墙静态示意' : 'Challenge wall static diagram';
  return (
    <div className="scene__fallback">
      <svg viewBox="0 0 600 300" role="img" aria-label={label}>
        <title>{label}</title>
        <defs>
          <linearGradient id="wall-gradient" x1="0" x2="1">
            <stop offset="0%" stopColor="var(--primary)" stopOpacity="0.55" />
            <stop offset="100%" stopColor="var(--secondary)" stopOpacity="0.55" />
          </linearGradient>
        </defs>
        <line x1="40" y1="170" x2="560" y2="170" stroke="url(#wall-gradient)" strokeWidth="2" />
        {[
          { x: 90, label: 'jsd' },
          { x: 250, label: 'managed' },
          { x: 410, label: 'turnstile' },
        ].map((panel) => (
          <g key={panel.label}>
            <rect x={panel.x} y="86" width="120" height="80" rx="12" fill="var(--surface)" stroke="var(--line)" />
            <text x={panel.x + 60} y="132" textAnchor="middle" fill="var(--ink)" fontSize="13" fontWeight="600">
              {panel.label}
            </text>
          </g>
        ))}
        <circle cx="300" cy="226" r="14" fill="var(--primary)" opacity="0.9" />
        <text x="300" y="262" textAnchor="middle" fill="var(--muted)" fontSize="11">
          cf_clearance
        </text>
      </svg>
    </div>
  );
}

function PipelineFallback({ locale }: { locale: Locale }) {
  const label = locale === 'zh' ? '流水线静态示意' : 'Pipeline static diagram';
  const nodes = ['Domain', 'Detect', 'Backend', 'Token', 'Replay'];
  return (
    <div className="scene__fallback">
      <svg viewBox="0 0 600 220" role="img" aria-label={label}>
        <title>{label}</title>
        <line x1="40" y1="110" x2="560" y2="110" stroke="var(--line)" strokeWidth="2" />
        {nodes.map((node, index) => {
          const x = 60 + index * 120;
          return (
            <g key={node}>
              <circle cx={x} cy="110" r="18" fill="var(--surface)" stroke="var(--primary)" strokeWidth="2" />
              <text x={x} y="150" textAnchor="middle" fill="var(--ink)" fontSize="11" fontWeight="600">
                {node}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export default function SceneShell({ variant, locale }: { variant: Variant; locale: Locale }) {
  const [tier, setTier] = useState<Tier>('static');
  const [armed, setArmed] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Capability detection must run after hydration; SSR renders the static tier.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setTier(detectTier());
  }, []);

  useEffect(() => {
    if (tier === 'static') return;
    if (variant === 'pipeline') {
      const el = ref.current;
      if (!el) return;
      const io = new IntersectionObserver(
        (entries) => {
          if (entries.some((entry) => entry.isIntersecting)) setArmed(true);
        },
        { rootMargin: '200px' }
      );
      io.observe(el);
      return () => io.disconnect();
    }
    const ric = (window as Window & { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number }).requestIdleCallback;
    if (ric) {
      const id = ric(() => setArmed(true), { timeout: 2000 });
      return () => {
        const cic = (window as Window & { cancelIdleCallback?: (id: number) => void }).cancelIdleCallback;
        if (cic) cic(id);
      };
    }
    const timer = window.setTimeout(() => setArmed(true), 600);
    return () => window.clearTimeout(timer);
  }, [tier, variant]);

  const Fallback = variant === 'wall' ? <WallFallback locale={locale} /> : <PipelineFallback locale={locale} />;
  const caption =
    locale === 'zh'
      ? tier === 'static'
        ? '静态降级：WebGL 不可用、低端设备或 reduced-motion'
        : '装饰性 3D 场景；等价数据在下方列表/表格'
      : tier === 'static'
        ? 'Static fallback: no WebGL, low-end device, or reduced-motion'
        : 'Decorative 3D scene; equivalent data is in the list/table below';

  return (
    <div
      ref={ref}
      className={`scene${tier === 'static' ? ' scene--static' : ''}`}
      data-scene={tier}
      data-variant={variant}
    >
      {tier === 'static' || !armed ? (
        Fallback
      ) : (
        <div aria-hidden="true" style={{ position: 'absolute', inset: 0 }}>
          <SceneBoundary fallback={Fallback}>
            {variant === 'wall' ? <ChallengeWall tier={tier} /> : <Pipeline tier={tier} />}
          </SceneBoundary>
        </div>
      )}
      <div className="scene__labels" aria-hidden="true">
        {(variant === 'wall' ? WALL_LABELS : PIPE_LABELS).map((label) => (
          <span className="scene__label" key={label}>
            {label}
          </span>
        ))}
      </div>
      <span className="scene__caption">{caption}</span>
    </div>
  );
}