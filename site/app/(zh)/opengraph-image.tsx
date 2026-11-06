import { ImageResponse } from 'next/og';

export const dynamic = 'force-static';
export const size = { width: 1200, height: 630 };
export const contentType = 'image/png';

export default function OG() {
  return new ImageResponse(
    (
      <div
        style={{
          width: '100%',
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          padding: 80,
          background: '#06080D',
          color: '#EAF1F8',
          fontSize: 64,
        }}
      >
        <div style={{ color: '#4DE3C1', fontSize: 28, letterSpacing: 4 }}>CF SOLVER</div>
        <div style={{ marginTop: 24 }}>Cloudflare 过盾</div>
        <div style={{ marginTop: 16, color: '#9FB0C3', fontSize: 26 }}>一次求解 · 30 分钟回放 · 缓存热路径 &lt;3ms</div>
      </div>
    ),
    { ...size }
  );
}