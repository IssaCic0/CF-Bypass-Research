import './globals.css';

export default function GlobalNotFound() {
  return (
    <html lang="zh-CN">
      <head>
        <meta charSet="utf-8" />
        <title>404 — CF Solver</title>
        <meta name="robots" content="noindex" />
      </head>
      <body>
        <main className="container section">
          <p className="hero__eyebrow">404</p>
          <h1 style={{ margin: 0, fontSize: 40, letterSpacing: '-0.02em' }}>页面不存在</h1>
          <p style={{ color: 'var(--muted)', marginTop: 12 }}>This page does not exist.</p>
          <div className="hero__actions">
            <a className="btn btn--primary" href="/">
              回首页 / Home
            </a>
            <a className="btn" href="/en/">
              English
            </a>
          </div>
        </main>
      </body>
    </html>
  );
}