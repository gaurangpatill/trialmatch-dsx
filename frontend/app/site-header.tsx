import Link from "next/link";

type ProductArea = "home" | "patient" | "researcher";

export function SiteHeader({active}: {active: ProductArea}) {
  return (
    <header className="site-header">
      <div className="site-header__inner">
        <Link className="site-brand" href="/" aria-label="TrialMatch home">
          <span className="site-brand__mark" aria-hidden="true">T+</span>
          <span><strong>TrialMatch</strong><small>Making clinical research more accessible</small></span>
        </Link>
        <nav className="site-nav" aria-label="TrialMatch portals">
          <Link aria-current={active === "patient" ? "page" : undefined} href="/patient">Participant Portal</Link>
          <Link aria-current={active === "researcher" ? "page" : undefined} href="/researcher">Researcher Workspace</Link>
        </nav>
      </div>
    </header>
  );
}
