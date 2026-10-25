import Link from "next/link";

import {Constellation} from "./constellation";

export default function Home() {
  return (
    <div className="home-hero">
      <Constellation />
      <main className="hero-content home-content">
        <section className="home-intro" aria-labelledby="home-title">
          <h1 id="home-title">TrialMatch<span aria-hidden="true">.</span></h1>
          <p>Source-grounded clinical trial discovery and research-access optimization.</p>
          <div className="home-actions">
            <Link href="/patient">
              <span>Open Participant Portal</span><span aria-hidden="true">↗</span>
            </Link>
            <Link href="/researcher">
              <span>Open Researcher Workspace</span><span aria-hidden="true">↗</span>
            </Link>
          </div>
        </section>
        <div className="home-footer">
          <div className="home-disclaimer">
            <span className="home-info" aria-hidden="true">i</span>
            <aside>
              TrialMatch is a research prototype, not medical advice or a definitive eligibility
              determination. Participant demonstrations use fictional example information only. Optimizer outputs are
              modeled outreach coverage, not observed enrollment.
            </aside>
          </div>
          <span className="home-prototype">Research prototype</span>
        </div>
      </main>
    </div>
  );
}
