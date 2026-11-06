import { DICT, PAGES, SITE, type Locale } from '@/content/site';

export default function SiteFooter({ locale }: { locale: Locale }) {
  const d = DICT[locale];
  const year = new Date().getUTCFullYear();
  const updated = new Date().toISOString().slice(0, 10);

  return (
    <footer className="footer">
      <div className="container">
        <div className="footer__grid">
          <div>
            <h2>{SITE.name}</h2>
            <p>{d.home.lede}</p>
          </div>
          <div>
            <h2>{locale === 'zh' ? '站内' : 'Site'}</h2>
            <p>
              <a href={PAGES.docs[locale]}>{d.nav.docs}</a>
              <br />
              <a href={PAGES.security[locale]}>{d.nav.security}</a>
              <br />
              <a href={PAGES.benchmarks[locale]}>{d.nav.benchmarks}</a>
            </p>
          </div>
          <div>
            <h2>{locale === 'zh' ? '仓库' : 'Repository'}</h2>
            <p>
              <a href={SITE.repo} target="_blank" rel="noopener noreferrer">
                GitHub
              </a>
              <br />
              <a href={`${SITE.repo}/blob/master/LICENSE`} target="_blank" rel="noopener noreferrer">
                {d.footer.license}
              </a>
              <br />
              <a href={`${SITE.repo}/blob/master/CHANGELOG.md`} target="_blank" rel="noopener noreferrer">
                CHANGELOG.md
              </a>
            </p>
          </div>
        </div>
        <div className="footer__legal">
          <p>{d.footer.disclaimer}</p>
          <p>{d.footer.trademark}</p>
          <p>
            © {year} {SITE.owner} · {d.footer.updated} {updated}
          </p>
        </div>
      </div>
    </footer>
  );
}