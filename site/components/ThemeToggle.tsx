'use client';

import { useEffect, useState } from 'react';

type Mode = 'dark' | 'light';

export default function ThemeToggle({ label }: { label: string }) {
  const [mode, setMode] = useState<Mode>('dark');

  useEffect(() => {
    const current = document.documentElement.dataset.theme;
    // The server cannot know localStorage; sync the button after hydration.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (current === 'light' || current === 'dark') setMode(current);
  }, []);

  function toggle() {
    const next: Mode = mode === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem('cf-theme', next);
    } catch {
      // Theme persistence is optional; the current page still switches.
    }
    setMode(next);
  }

  return (
    <button
      type="button"
      className="icon-button"
      onClick={toggle}
      aria-label={label}
      aria-pressed={mode === 'dark'}
      title={label}
    >
      <span aria-hidden="true">{mode === 'dark' ? '☾' : '☀'}</span>
    </button>
  );
}