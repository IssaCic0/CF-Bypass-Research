import type { ReactNode } from 'react';

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ');
}

export function Container({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('container', className)}>{children}</div>;
}

export function Section({
  id,
  title,
  lede,
  children,
  tight = false,
  className,
  titleAs = 'h2',
}: {
  id?: string;
  title?: string;
  lede?: string;
  children: ReactNode;
  tight?: boolean;
  className?: string;
  titleAs?: 'h1' | 'h2';
}) {
  const Heading = titleAs;
  return (
    <section id={id} className={cx('section', tight && 'section--tight', className)}>
      <Container>
        {(title || lede) && (
          <div className="section__head">
            {title && <Heading>{title}</Heading>}
            {lede && <p>{lede}</p>}
          </div>
        )}
        {children}
      </Container>
    </section>
  );
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('card', className)}>{children}</div>;
}

export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'neutral' | 'ok' | 'warn' | 'danger';
}) {
  return <span className={cx('badge', tone !== 'neutral' && `badge--${tone}`)}>{children}</span>;
}

export function ButtonLink({
  href,
  children,
  variant = 'primary',
  external = false,
}: {
  href: string;
  children: ReactNode;
  variant?: 'primary' | 'ghost';
  external?: boolean;
}) {
  return (
    <a
      className={cx('btn', variant === 'primary' ? 'btn--primary' : 'btn--ghost')}
      href={href}
      {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
    >
      {children}
    </a>
  );
}

export function Notice({ children }: { children: ReactNode }) {
  return <div className="notice">{children}</div>;
}

export function Metric({ value, label, note }: { value: string; label: string; note: string }) {
  return (
    <div className="metric">
      <div className="metric__value">{value}</div>
      <div className="metric__label">{label}</div>
      <div className="metric__note">{note}</div>
    </div>
  );
}

export function TableWrap({ children }: { children: ReactNode }) {
  return (
    <div className="table-wrap" tabIndex={0}>
      {children}
    </div>
  );
}