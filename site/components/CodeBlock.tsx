'use client';

import { useState } from 'react';

export default function CodeBlock({
  code,
  label,
  copyLabel = '复制',
  copiedLabel = '已复制',
}: {
  code: string;
  label?: string;
  copyLabel?: string;
  copiedLabel?: string;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="code">
      <div className="code__bar">
        {label && <span>{label}</span>}
        <button type="button" onClick={copy} aria-live="polite">
          {copied ? copiedLabel : copyLabel}
        </button>
      </div>
      <pre tabIndex={0}>
        <code>{code}</code>
      </pre>
    </div>
  );
}