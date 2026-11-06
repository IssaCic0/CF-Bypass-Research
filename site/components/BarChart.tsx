export type BarItem = { label: string; value: number; display: string };

export default function BarChart({ items, title }: { items: BarItem[]; title: string }) {
  const max = Math.max(...items.map((i) => i.value));
  const row = 38;
  const height = 28 + items.length * row;
  return (
    <figure style={{ margin: 0 }}>
      <svg viewBox={`0 0 640 ${height}`} role="img" aria-label={title} width="100%">
        <title>{title}</title>
        {items.map((item, index) => {
          const y = 12 + index * row;
          const width = Math.max(4, (item.value / max) * 420);
          return (
            <g key={item.label}>
              <text x="0" y={y + 16} fill="var(--muted)" fontSize="12">
                {item.label}
              </text>
              <rect x="150" y={y + 2} width={width} height="18" rx="6" fill="var(--primary)" opacity={0.85} />
              <text x={150 + width + 8} y={y + 16} fill="var(--ink)" fontSize="12" fontVariant="tabular-nums">
                {item.display}
              </text>
            </g>
          );
        })}
      </svg>
      <figcaption style={{ color: 'var(--muted)', fontSize: 12, marginTop: 8 }}>{title}</figcaption>
    </figure>
  );
}