'use client';

import { Canvas, useFrame } from '@react-three/fiber';
import { useRef } from 'react';
import type { Group } from 'three';

const PANELS = [
  { label: 'jsd', sub: 'JS challenge', x: -2.1, color: '#4DE3C1' },
  { label: 'managed', sub: 'managed challenge', x: 0, color: '#A78BFA' },
  { label: 'turnstile', sub: 'Turnstile token', x: 2.1, color: '#60A5FA' },
];

function Panel({ x, color }: { x: number; color: string }) {
  return (
    <group position={[x, 0.45, 0]}>
      <mesh>
        <boxGeometry args={[1.65, 1.05, 0.07]} />
        <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.16} roughness={0.42} metalness={0.18} />
      </mesh>
    </group>
  );
}

function Token() {
  const ref = useRef<Group>(null);
  useFrame((state, delta) => {
    if (!ref.current) return;
    ref.current.rotation.y += delta * 0.35;
    ref.current.rotation.x = Math.sin(state.clock.elapsedTime * 0.5) * 0.12;
  });
  return (
    <group ref={ref} position={[0, -1.15, 0.1]}>
      <mesh>
        <icosahedronGeometry args={[0.3, 0]} />
        <meshStandardMaterial color="#4DE3C1" emissive="#4DE3C1" emissiveIntensity={0.85} roughness={0.2} metalness={0.1} />
      </mesh>
    </group>
  );
}

function CameraRig() {
  useFrame((state, delta) => {
    if (typeof window !== 'undefined') {
      const w = window as Window & { __CF_SCENE_METRICS?: boolean; __sceneFrames?: number };
      if (w.__CF_SCENE_METRICS) w.__sceneFrames = (w.__sceneFrames ?? 0) + 1;
    }
    const tx = state.pointer.x * 0.35;
    const ty = state.pointer.y * 0.18;
    const k = Math.min(1, delta * 3);
    state.camera.position.x += (tx - state.camera.position.x) * k;
    state.camera.position.y += (ty - state.camera.position.y) * k;
    state.camera.lookAt(0, 0, 0);
  });
  return null;
}

export default function ChallengeWall({ tier = 'full' }: { tier?: 'full' | 'lite' }) {
  return (
    <Canvas
      dpr={tier === 'lite' ? 1 : [1, 1.5]}
      camera={{ position: [0, 0, 6], fov: 45 }}
      gl={{ alpha: true, antialias: true, powerPreference: 'high-performance' }}
      style={{ width: '100%', height: '100%' }}
    >
      <ambientLight intensity={0.85} />
      <directionalLight position={[3, 4, 5]} intensity={1.15} />
      <pointLight position={[-3, 2, 2]} color="#A78BFA" intensity={2.2} />
      {PANELS.map((panel) => (
        <Panel key={panel.label} x={panel.x} color={panel.color} />
      ))}
      <mesh rotation={[Math.PI / 2, 0, 0]} position={[0, -0.1, 0]}>
        <cylinderGeometry args={[0.012, 0.012, 5.4, 8]} />
        <meshStandardMaterial color="#4DE3C1" emissive="#4DE3C1" emissiveIntensity={0.55} />
      </mesh>
      <Token />
      <CameraRig />
    </Canvas>
  );
}