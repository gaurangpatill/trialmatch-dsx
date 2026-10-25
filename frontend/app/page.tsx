import Link from "next/link";

import {SiteHeader} from "./site-header";

export default function Home() {
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <SiteHeader active="home" />
      <main className="mx-auto flex min-h-[calc(100vh-68px)] max-w-5xl flex-col justify-center px-6 py-16">
      <p className="mb-3 text-sm font-semibold uppercase tracking-[0.2em] text-accent">
      </p>
      <h1 className="text-5xl font-bold tracking-tight text-ink">TrialMatch</h1>
      <p className="mt-6 max-w-2xl text-lg leading-8 text-slate-700">
        Source-grounded clinical trial discovery and research-access optimization.
      </p>
      <div className="mt-8 flex flex-wrap gap-3">
        <Link className="rounded-lg bg-accent px-5 py-3 font-semibold text-white" href="/patient">
          Open Participant Portal
        </Link>
        <Link className="rounded-lg border border-slate-300 bg-white px-5 py-3 font-semibold text-slate-700 hover:border-accent hover:text-accent" href="/researcher">
          Open Researcher Workspace
        </Link>
      </div>
      <aside className="mt-10 rounded-xl border border-slate-300 bg-white p-6 text-sm leading-6 text-slate-700">
        TrialMatch is a research prototype, not medical advice or a definitive eligibility
        determination. Participant demonstrations use fictional example information only. Optimizer outputs are
        modeled outreach coverage, not observed enrollment.
      </aside>
      </main>
    </div>
  );
}
