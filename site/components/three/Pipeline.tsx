'use client';

import { Canvas } from '@react-three/fiber';

const NODES = [
  { label: 'Domain', x: -2.4, color: '#4DE3C1' },
  { label: 'Detect', x: -1.2, color: '#60A5FA' },
  { label: 'Backend', x: 0, color: '#A78BFA' },
  { label: 'Token', x: 1.2, color: '#FBBF24' },
  { label: 'Replay', x: 2.4, color: '#4ADE80' },
];

export default function Pipeline({ tier = 'full' }: { tier?: 'full' | 'lite' }) {
  return (
    <Canvas
      dpr={tier === 'lite' ? 1 : [1, 1.5]}
      camera={{ position: [0, 0.35, 6], fov: 45 }}
      gl={{ alpha: true, antialias: true, powerPreference: 'high-performance' }}
      frameloop="demand"
      style={{ width: '100%', height: '100%' }}
    >
      <ambientLight intensity={0.9} />
      <directionalLight position={[2, 4, 5]} intensity={1.0} />
      {NODES.slice(0, -1).map((node, index) => {
        const next = NODES[index + 1];
        return (
          <mesh key={`${node.label}-line`} position={[(node.x + next.x) / 2, 0, 0]}>
            <boxGeometry args={[next.x - node.x - 0.48, 0.025, 0.025]} />
            <meshStandardMaterial color="#223041" emissive="#223041" emissiveIntensity={0.2} />
          </mesh>
        );
      })}
      {NODES.map((node) => (
        <group key={node.label} position={[node.x, 0, 0]}>
          <mesh>
            <sphereGeometry args={[0.22, 24, 24]} />
            <meshStandardMaterial color={node.color} emissive={node.color} emissiveIntensity={0.35} roughness={0.35} />
          </mesh>
        </group>
      ))}
    </Canvas>
  );
}