"use client";

import {FormEvent, useCallback, useEffect, useMemo, useRef, useState} from "react";

import {SiteHeader} from "../site-header";

import type {
  ApiError,
  TrialDetail,
  TrialLocation,
  TrialSearchResponse,
  TrialSummary,
  TrialSummaryGenerationResponse,
} from "@/lib/clinical-contracts";

const exampleSearches = ["Breast Cancer", "Heart Failure", "Prostate Cancer", "Colorectal Cancer"];
const phaseOptions = ["PHASE1", "PHASE2", "PHASE3", "PHASE4", "EARLY_PHASE1", "NOT_APPLICABLE", "NA"];
type SortOption = "source" | "title" | "status" | "ma_sites";

export function PatientPortal() {
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [phase, setPhase] = useState("");
  const [studyAge, setStudyAge] = useState("");
  const [location, setLocation] = useState("");
  const [sort, setSort] = useState<SortOption>("source");
  const [page, setPage] = useState(1);
  const [trials, setTrials] = useState<TrialSummary[]>([]);
  const [knownTrials, setKnownTrials] = useState<Record<string, TrialSummary>>({});
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(0);
  const [freshness, setFreshness] = useState<string | null>(null);
  const [searching, setSearching] = useState(true);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [activeTrial, setActiveTrial] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, TrialDetail>>({});
  const [detailLoading, setDetailLoading] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [generatedSummaries, setGeneratedSummaries] = useState<Record<string, TrialSummaryGenerationResponse>>({});
  const [summaryLoading, setSummaryLoading] = useState<Record<string, boolean>>({});
  const [summaryErrors, setSummaryErrors] = useState<Record<string, string | null>>({});

  const discoveryHeadingRef = useRef<HTMLHeadingElement>(null);
  const lastFocusedElement = useRef<HTMLElement | null>(null);
  const summaryCacheRef = useRef(new Map<string, TrialSummaryGenerationResponse>());
  const summaryRequestsRef = useRef(new Map<string, Promise<void>>());
  const displayedTrials = useMemo(() => sortTrials(trials, sort), [trials, sort]);

  async function searchTrials(searchQuery: string, requestedPage = 1, focusResults = true) {
    const ageValue = studyAge.trim() ? Number(studyAge) : null;
    if (ageValue !== null && (!Number.isFinite(ageValue) || ageValue < 0 || ageValue > 125)) {
      setSearchError("Study age must be between 0 and 125 years.");
      return;
    }
    setSearching(true);
    setSearchError(null);
    try {
      const normalizedQuery = searchQuery.trim();
      const parameters = new URLSearchParams({page: String(requestedPage), page_size: "12"});
      if (normalizedQuery) parameters.set("query", normalizedQuery);
      if (phase) parameters.set("phase", phase);
      if (ageValue !== null) parameters.set("age_years", String(ageValue));
      if (location.trim()) parameters.set("location", location.trim());
      const response = await fetch(`/api/trials?${parameters}`, {cache: "no-store"});
      const payload = (await response.json()) as TrialSearchResponse | ApiError;
      if (!response.ok || !("items" in payload)) {
        throw new Error(errorMessage(payload as ApiError, "Trial search failed."));
      }
      setTrials(payload.items);
      setKnownTrials((current) => {
        const next = {...current};
        payload.items.forEach((trial) => { next[trial.nct_id] = trial; });
        return next;
      });
      setAppliedQuery(normalizedQuery);
      setTotal(payload.pagination.total_items);
      setTotalPages(payload.pagination.total_pages);
      setPage(payload.pagination.page);
      setFreshness(payload.data_freshness.retrieved_at);
      if (focusResults) requestAnimationFrame(() => {
        discoveryHeadingRef.current?.focus({preventScroll: true});
        discoveryHeadingRef.current?.scrollIntoView({behavior: "smooth", block: "start"});
      });
    } catch (reason) {
      setTrials([]);
      setTotal(0);
      setTotalPages(0);
      setSearchError(reason instanceof Error ? reason.message : "Trial search failed.");
    } finally {
      setSearching(false);
    }
  }

  useEffect(() => {
    void searchTrials("", 1, false);
    // Initial curated discovery uses the local index; detail is fetched only after a study is opened.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void searchTrials(query, 1);
  }

  function useExample(value: string) {
    setQuery(value);
    void searchTrials(value, 1);
  }

  function clearFilters() {
    setPhase("");
    setStudyAge("");
    setLocation("");
  }

  function openDetails(trial: TrialSummary) {
    lastFocusedElement.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setActiveTrial(trial.nct_id);
    setSummaryErrors((current) => ({...current, [trial.nct_id]: null}));
    void loadDetails(trial);
    void loadSummary(trial.nct_id);
  }

  function loadSummary(nctId: string): Promise<void> {
    if (summaryCacheRef.current.has(nctId)) {
      setSummaryErrors((current) => ({...current, [nctId]: null}));
      return Promise.resolve();
    }
    const existingRequest = summaryRequestsRef.current.get(nctId);
    if (existingRequest) return existingRequest;

    setSummaryLoading((current) => ({...current, [nctId]: true}));
    setSummaryErrors((current) => ({...current, [nctId]: null}));
    let request!: Promise<void>;
    request = (async () => {
      try {
        const response = await fetch(`/api/trials/${encodeURIComponent(nctId)}/summary`, {
          method: "POST",
          cache: "no-store",
        });
        const payload = (await response.json()) as TrialSummaryGenerationResponse | ApiError;
        if (!response.ok || !("plain_language_summary" in payload)) {
          throw new Error(errorMessage(payload as ApiError, "AI summary generation failed."));
        }
        if (payload.nct_id !== nctId || !payload.plain_language_summary.trim()) {
          throw new Error("The AI summary response did not match the selected study.");
        }
        summaryCacheRef.current.set(nctId, payload);
        setSummaryErrors((current) => ({...current, [nctId]: null}));
        setGeneratedSummaries((current) => ({...current, [nctId]: payload}));
        setDetails((current) => current[nctId]
          ? {...current, [nctId]: {...current[nctId], plain_language_summary: payload.plain_language_summary}}
          : current);
        setTrials((current) => current.map((item) => item.nct_id === nctId
          ? {...item, plain_language_summary: payload.plain_language_summary}
          : item));
        setKnownTrials((current) => current[nctId]
          ? {...current, [nctId]: {...current[nctId], plain_language_summary: payload.plain_language_summary}}
          : current);
      } catch (reason) {
        if (!summaryCacheRef.current.has(nctId)) {
          setSummaryErrors((current) => ({
            ...current,
            [nctId]: reason instanceof Error
              ? reason.message
              : "AI explanation is temporarily unavailable. You can still read the official study description.",
          }));
        }
      } finally {
        setSummaryLoading((current) => ({...current, [nctId]: false}));
        if (summaryRequestsRef.current.get(nctId) === request) {
          summaryRequestsRef.current.delete(nctId);
        }
      }
    })();
    summaryRequestsRef.current.set(nctId, request);
    return request;
  }

  async function loadDetails(trial: TrialSummary, refresh = false) {
    setDetailError(null);
    const existing = details[trial.nct_id];
    if (!refresh && existing?.detail_status === "retrieved") return;
    setDetailLoading(trial.nct_id);
    try {
      const response = await fetch(
        `/api/trials/${encodeURIComponent(trial.nct_id)}${refresh ? "?refresh=true" : ""}`,
        {cache: "no-store"},
      );
      const payload = (await response.json()) as TrialDetail | ApiError;
      if (!response.ok || !("detail_status" in payload)) {
        throw new Error(errorMessage(payload as ApiError, "Study details could not be loaded."));
      }
      const generated = summaryCacheRef.current.get(trial.nct_id);
      const resolvedPayload: TrialDetail = generated
        ? {...payload, plain_language_summary: generated.plain_language_summary}
        : payload;
      setDetails((current) => ({...current, [trial.nct_id]: resolvedPayload}));
      const updated: TrialSummary = {
        ...trial,
        overall_status: resolvedPayload.overall_status,
        conditions: resolvedPayload.conditions,
        phases: resolvedPayload.phases,
        minimum_age: resolvedPayload.minimum_age,
        maximum_age: resolvedPayload.maximum_age,
        sex: resolvedPayload.sex,
        locations: resolvedPayload.locations,
        retrieved_at: resolvedPayload.retrieved_at,
        detail_status: resolvedPayload.detail_status,
        screening_coverage_status: resolvedPayload.screening_coverage.status,
        source_description: resolvedPayload.source_description,
        brief_summary: resolvedPayload.brief_summary,
        detailed_description: resolvedPayload.detailed_description,
        plain_language_summary: resolvedPayload.plain_language_summary,
      };
      setTrials((current) => current.map((item) => item.nct_id === trial.nct_id ? updated : item));
      setKnownTrials((current) => ({...current, [trial.nct_id]: updated}));
    } catch (reason) {
      setDetailError(reason instanceof Error ? reason.message : "Study details could not be loaded.");
    } finally {
      setDetailLoading(null);
    }
  }

  const closeDetails = useCallback(() => {
    setActiveTrial(null);
    setDetailError(null);
    requestAnimationFrame(() => lastFocusedElement.current?.focus());
  }, []);

  const activeSummary = activeTrial ? knownTrials[activeTrial] : undefined;
  const activeDetail = activeTrial ? details[activeTrial] : undefined;

  return (
    <main className="min-h-screen bg-canvas text-ink">
      <SiteHeader active="patient" />

      <section className="border-b border-emerald-950/10 bg-[radial-gradient(circle_at_top_right,_#d9f4e8,_transparent_44%),linear-gradient(135deg,#f7fbf8_0%,#edf5ef_100%)]">
        <div className="mx-auto max-w-7xl px-5 py-8 sm:py-12 lg:px-8">
          <div className="grid min-w-0 gap-8 lg:grid-cols-[minmax(0,1.85fr)_minmax(0,1fr)] lg:items-start">
            <div className="min-w-0">
              <div className="max-w-4xl">
                <p className="text-xs font-black uppercase tracking-[0.18em] text-accent">Start with official study discovery</p>
                <h1 className="mt-3 max-w-3xl text-4xl font-black tracking-[-0.045em] sm:text-6xl">Find recruiting clinical studies.</h1>
                <p className="mt-4 max-w-3xl text-base leading-7 text-slate-700 sm:text-lg">Search source-grounded records, explore what each study is about, and review published requirements and official study information.</p>
              </div>
              <form className="mt-7 max-w-5xl rounded-3xl border border-emerald-950/10 bg-white p-3 shadow-[0_24px_80px_-32px_rgba(17,33,31,0.35)] sm:p-4" onSubmit={submitSearch} role="search">
                <div className="flex flex-col gap-3 sm:flex-row">
                  <label className="sr-only" htmlFor="trial-query">Condition or study keyword</label>
                  <div className="flex min-w-0 flex-1 items-center gap-3 rounded-2xl bg-slate-50 px-4 ring-1 ring-inset ring-slate-200 focus-within:ring-2 focus-within:ring-accent">
                    <span aria-hidden="true" className="text-xl text-slate-400">⌕</span>
                    <input autoFocus className="w-full bg-transparent py-4 text-base outline-none placeholder:text-slate-400 sm:text-lg" id="trial-query" maxLength={200} onChange={(event) => setQuery(event.target.value)} placeholder="Search a condition, such as breast cancer" type="search" value={query} />
                  </div>
                  <button className="rounded-2xl bg-accent px-8 py-4 font-bold text-white shadow-lg shadow-teal-900/15 transition hover:bg-teal-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-wait disabled:opacity-60" disabled={searching} type="submit">{searching ? "Searching…" : "Find studies"}</button>
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-2 px-1 text-xs text-slate-500"><span className="font-semibold">Try:</span>{exampleSearches.map((example) => <button className="rounded-full bg-emerald-50 px-3 py-1.5 font-semibold text-emerald-800 hover:bg-emerald-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent" key={example} onClick={() => useExample(example)} type="button">{example}</button>)}</div>
              </form>
            </div>

            <div className="min-w-0 space-y-5 lg:col-start-2 lg:row-span-2 lg:row-start-1">
              <ol aria-label="How TrialMatch works" className="grid min-w-0 gap-3 rounded-3xl border border-slate-200 bg-white p-4 sm:grid-cols-2 sm:p-5 lg:grid-cols-1">
                {[["1", "Search", "Find clinical trials by health condition."], ["2", "Explore", "Browse recruiting studies and review their details."], ["3", "Understand", "Review published requirements and official study information."]].map(([step, title, text]) => <li className="flex min-w-0 gap-3" key={step}><span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-100 text-sm font-black text-emerald-900">{step}</span><div className="min-w-0"><p className="font-black">{title}</p><p className="mt-1 text-sm leading-5 text-slate-600">{text}</p></div></li>)}
              </ol>
              <div className="rounded-2xl border border-amber-200 bg-amber-50/80 p-4 text-sm leading-6 text-amber-950 sm:flex sm:items-start sm:gap-3"><span aria-hidden="true" className="text-lg">ⓘ</span><p><strong>Research information only.</strong> TrialMatch helps you discover and review published study information. It is not medical advice or a determination of eligibility; study staff must confirm current details and participation requirements.</p></div>
            </div>

            <section aria-labelledby="discovery-heading" className="min-w-0 lg:col-start-1 lg:row-start-2">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <p className="text-xs font-bold uppercase tracking-[0.2em] text-accent">Official discovery</p>
                <h2 className="mt-2 scroll-mt-4 text-3xl font-black tracking-tight outline-none" id="discovery-heading" ref={discoveryHeadingRef} tabIndex={-1}>{appliedQuery ? `Studies matching “${appliedQuery}”` : "Explore recruiting studies"}</h2>
                <p className="mt-2 text-sm text-slate-600">{total.toLocaleString()} indexed result{total === 1 ? "" : "s"}. Opening a study retrieves expanded official fields.</p>
              </div>
              <p className="text-xs text-slate-500">Index updated {formatTimestamp(freshness)}</p>
            </div>

            <details className="mt-5 rounded-2xl border border-slate-200 bg-white shadow-sm" open>
              <summary className="cursor-pointer list-none px-5 py-4 text-sm font-bold">Filter and sort results <span aria-hidden="true" className="float-right text-slate-400">＋</span></summary>
              <form className="grid gap-4 border-t border-slate-100 p-5 sm:grid-cols-2 lg:grid-cols-4" onSubmit={submitSearch}>
                <label className="text-sm font-semibold">Study phase<select className="mt-2 w-full rounded-xl border border-slate-300 bg-white px-3 py-3 font-normal" onChange={(event) => setPhase(event.target.value)} value={phase}><option value="">Any published phase</option>{phaseOptions.map((value) => <option key={value} value={value}>{formatPhase(value)}{value === "NA" ? " (legacy source value)" : ""}</option>)}</select></label>
                <label className="text-sm font-semibold">Age to check<input className="mt-2 w-full rounded-xl border border-slate-300 px-3 py-3 font-normal" max="125" min="0" onChange={(event) => setStudyAge(event.target.value)} placeholder="e.g. 42" step="0.1" type="number" value={studyAge} /></label>
                <label className="text-sm font-semibold">Facility, city, or state<input className="mt-2 w-full rounded-xl border border-slate-300 px-3 py-3 font-normal" maxLength={200} onChange={(event) => setLocation(event.target.value)} placeholder="e.g. Boston" value={location} /></label>
                <label className="text-sm font-semibold">Sort this page<select className="mt-2 w-full rounded-xl border border-slate-300 bg-white px-3 py-3 font-normal" onChange={(event) => setSort(event.target.value as SortOption)} value={sort}><option value="source">Official index order</option><option value="title">Study title A–Z</option><option value="status">Recruiting status</option><option value="ma_sites">Most Massachusetts sites</option></select></label>
                <div className="flex flex-wrap justify-end gap-3 sm:col-span-2 lg:col-span-4"><button className="rounded-xl px-4 py-2 text-sm font-bold text-slate-600 hover:bg-slate-100" onClick={clearFilters} type="button">Clear filter fields</button><button className="rounded-xl bg-ink px-5 py-2.5 text-sm font-bold text-white hover:bg-slate-800" type="submit">Apply filters</button></div>
              </form>
            </details>

            {searchError ? <Alert message={searchError} onRetry={() => void searchTrials(appliedQuery, page)} /> : null}
            <div aria-busy={searching} aria-live="polite" className="mt-5 grid gap-5">
              {searching ? <SearchSkeleton /> : null}
              {!searching && !searchError && trials.length === 0 ? <div className="rounded-3xl border border-dashed border-slate-300 bg-white p-10 text-center"><div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-slate-100 text-xl">⌕</div><h3 className="mt-4 text-lg font-bold">No studies found</h3><p className="mx-auto mt-2 max-w-md text-sm leading-6 text-slate-600">Try a broader condition, remove a filter, or check another term. No study result is fabricated when the official-source index has no match.</p></div> : null}
              {!searching && displayedTrials.map((trial) => <TrialCard key={trial.nct_id} onOpen={() => void openDetails(trial)} trial={trial} />)}
            </div>
            {totalPages > 1 && !searching ? <nav aria-label="Study result pages" className="mt-6 flex items-center justify-between rounded-2xl border border-slate-200 bg-white p-3 text-sm"><button className="rounded-xl px-4 py-2 font-bold hover:bg-slate-100 disabled:opacity-40" disabled={page <= 1} onClick={() => void searchTrials(appliedQuery, page - 1)} type="button">← Previous</button><span className="text-slate-500">Page {page} of {totalPages}</span><button className="rounded-xl px-4 py-2 font-bold hover:bg-slate-100 disabled:opacity-40" disabled={page >= totalPages} onClick={() => void searchTrials(appliedQuery, page + 1)} type="button">Next →</button></nav> : null}
            </section>
          </div>
        </div>
      </section>

      {activeTrial && activeSummary ? <StudyDetailPanel detail={activeDetail} error={detailError} loading={detailLoading === activeTrial} onClose={closeDetails} onRefresh={() => void loadDetails(activeSummary, true)} onRetrySummary={() => void loadSummary(activeTrial)} summary={activeSummary} summaryError={summaryErrors[activeTrial] ?? null} summaryLoading={summaryLoading[activeTrial] === true} summaryText={generatedSummaries[activeTrial]?.plain_language_summary ?? activeDetail?.plain_language_summary ?? activeSummary.plain_language_summary ?? null} /> : null}
    </main>
  );
}

export function TrialCard({trial, onOpen}: {trial: TrialSummary; onOpen: () => void}) {
  const officialDescription = trial.source_description ?? trial.brief_summary ?? trial.detailed_description;
  const description = trial.plain_language_summary ?? officialDescription ?? fallbackStudyExplanation(trial);
  const descriptionLabel = trial.plain_language_summary ? "Plain-English summary" : officialDescription ? "Official study description" : "Source-grounded overview";
  return <article className="group rounded-3xl border border-slate-200 bg-white p-6 shadow-sm transition hover:-translate-y-0.5 hover:shadow-lg sm:p-8">
    <div className="min-w-0"><div className="flex flex-wrap items-center gap-2 text-[11px] font-bold uppercase tracking-wide"><span className={`rounded-full px-2.5 py-1 ${statusClass(trial.overall_status)}`}>{formatRegistryValue(trial.overall_status)}</span>{trial.phases.length ? trial.phases.slice(0, 2).map((value) => <span className="rounded-full bg-slate-100 px-2.5 py-1 text-slate-700" key={value}>{formatPhase(value)}</span>) : <span className="rounded-full bg-slate-100 px-2.5 py-1 text-slate-600">Phase not specified</span>}</div>
    <h3 className="mt-4 line-clamp-2 text-xl font-black leading-7 tracking-tight sm:text-2xl">{trial.title}</h3><p className="mt-2 text-sm font-semibold text-accent">{compactConditions(trial.conditions)}</p>
    <div className="mt-4"><p className="text-[11px] font-bold uppercase tracking-wide text-slate-500">{descriptionLabel}</p><p className="mt-1 line-clamp-4 text-[15px] leading-6 text-slate-700">{description}</p></div>
    <div className="mt-6 grid gap-4 rounded-2xl bg-slate-50 p-4 text-sm sm:grid-cols-2 lg:grid-cols-4"><InfoItem label="Published ages" value={ageLabel(trial.minimum_age, trial.maximum_age, trial.detail_status)} /><InfoItem label="Phase" value={trial.phases.length ? trial.phases.map(formatPhase).join(", ") : "Not specified by source"} /><InfoItem label="Massachusetts facilities" value={facilityCountLabel(trial, true)} /><InfoItem label="All source facilities" value={facilityCountLabel(trial, false)} /></div>
    <p className="mt-4 text-xs text-slate-400">ClinicalTrials.gov · {trial.nct_id} · retrieved {formatTimestamp(trial.retrieved_at)}</p></div>
    <div className="mt-5 flex justify-end"><button className="rounded-xl border border-slate-300 px-4 py-3 text-sm font-bold text-slate-700 hover:border-accent hover:text-accent" onClick={onOpen} type="button">Explore details</button></div>
  </article>;
}

export function StudyDetailPanel({summary, detail, loading, error, summaryText, summaryLoading, summaryError, onClose, onRefresh, onRetrySummary}: {summary: TrialSummary; detail?: TrialDetail; loading: boolean; error: string | null; summaryText: string | null; summaryLoading: boolean; summaryError: string | null; onClose: () => void; onRefresh: () => void; onRetrySummary: () => void}) {
  const displayed = detail ?? summary;
  const status = detail?.detail_status ?? summary.detail_status;
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const maLocations = detail ? partitionLocations(detail.locations).massachusetts : [];
  const otherLocations = detail ? partitionLocations(detail.locations).other : [];
  const officialBrief = displayed.brief_summary ?? displayed.source_description;
  const officialDetailed = displayed.detailed_description;
  const summarySections = summaryText ? parseSummarySections(summaryText) : null;
  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = [...panelRef.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), details > summary, [tabindex]:not([tabindex="-1"])',
      )].filter((element) => {
        const closedDetails = element.closest("details:not([open])");
        const style = window.getComputedStyle(element);
        return (!closedDetails || element.tagName === "SUMMARY") && style.display !== "none" && style.visibility !== "hidden";
      });
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);
  return <div aria-labelledby="study-detail-title" aria-modal="true" className="fixed inset-0 z-50 flex justify-end bg-ink/60 p-0 backdrop-blur-sm sm:p-4" role="dialog"><button aria-label="Close study details" className="absolute inset-0 cursor-default" onClick={onClose} tabIndex={-1} type="button" /><section className="relative h-full w-full overflow-y-auto bg-canvas shadow-2xl sm:max-w-4xl sm:rounded-3xl" ref={panelRef}><div className="sticky top-0 z-10 flex items-center justify-between gap-4 border-b border-slate-200 bg-white/95 px-5 py-4 backdrop-blur sm:px-7"><div className="min-w-0"><p className="text-xs font-bold uppercase tracking-[0.16em] text-accent">Official study details</p><p className="mt-1 truncate text-xs text-slate-500">ClinicalTrials.gov · {summary.nct_id}</p></div><div className="flex shrink-0 items-center gap-2"><a className="hidden rounded-xl bg-accent px-4 py-2.5 text-sm font-bold text-white transition hover:bg-teal-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent sm:inline-flex" href={displayed.source_url} rel="noreferrer" target="_blank">Official record ↗</a><button aria-label="Close study details" className="flex h-10 w-10 items-center justify-center rounded-full bg-slate-100 text-2xl transition hover:bg-slate-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" onClick={onClose} ref={closeRef} type="button">×</button></div></div><div className="p-5 sm:p-8">
    {loading ? <DetailSkeleton /> : null}{error ? <Alert message={error} onRetry={onRefresh} /> : null}{!loading ? <><div className="flex flex-wrap gap-2 text-[11px] font-bold uppercase tracking-wide"><span className={`rounded-full px-2.5 py-1 ${statusClass(displayed.overall_status)}`}>{formatRegistryValue(displayed.overall_status)}</span>{displayed.phases.length ? displayed.phases.map((value) => <span className="rounded-full bg-white px-2.5 py-1 text-slate-600 ring-1 ring-slate-200" key={value}>{formatPhase(value)}</span>) : <span className="rounded-full bg-white px-2.5 py-1 text-slate-600 ring-1 ring-slate-200">Phase not specified</span>}</div><h2 className="mt-4 text-2xl font-black leading-tight tracking-[-0.035em] sm:text-4xl" id="study-detail-title">{displayed.title}</h2><p className="mt-3 text-sm leading-6 text-slate-600">{compactConditions(displayed.conditions, 5)}</p><div className="mt-5 flex flex-col gap-3 rounded-2xl border border-slate-200 bg-white p-4 sm:flex-row sm:items-center sm:justify-between"><div><p className="text-xs font-bold uppercase tracking-wide text-slate-500">Source status</p><p className="mt-1 text-sm font-semibold text-slate-800">{detail?.detail_status === "retrieved" ? `Retrieved from ${formatRetrievalSource(detail.retrieval_source)}` : "Expanded official fields are not yet available"}</p><p className="mt-1 text-xs text-slate-500">Updated in TrialMatch {formatTimestamp(displayed.retrieved_at)}</p></div><a className="inline-flex w-full justify-center rounded-xl bg-accent px-4 py-3 text-sm font-bold text-white transition hover:bg-teal-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent sm:hidden" href={displayed.source_url} rel="noreferrer" target="_blank">View official ClinicalTrials.gov record ↗</a></div><nav aria-label="Study detail sections" className="mt-4 flex gap-2 overflow-x-auto pb-1 text-xs font-bold"><a className="whitespace-nowrap rounded-full bg-white px-3 py-2 ring-1 ring-slate-200 hover:ring-accent" href="#study-eligibility">Eligibility</a><a className="whitespace-nowrap rounded-full bg-white px-3 py-2 ring-1 ring-slate-200 hover:ring-accent" href="#study-interventions">Interventions</a><a className="whitespace-nowrap rounded-full bg-white px-3 py-2 ring-1 ring-slate-200 hover:ring-accent" href="#study-locations">Locations</a><a className="whitespace-nowrap rounded-full bg-white px-3 py-2 ring-1 ring-slate-200 hover:ring-accent" href="#study-contacts">Contacts</a></nav>
    <section className="mt-7 rounded-2xl border border-emerald-200 bg-white p-5" aria-labelledby="ai-summary-heading">
      <p className="text-xs font-bold uppercase tracking-[0.16em] text-accent">Participant overview</p>
      <h3 className="mt-2 text-lg font-black text-emerald-950" id="ai-summary-heading">AI-generated plain-language explanation</h3>
      <p className="mt-2 text-xs leading-5 text-slate-500">AI-generated explanation based on ClinicalTrials.gov study information. Verify details with the research team. This is not medical advice or a verified eligibility decision.</p>
      <div aria-live="polite" className="mt-4">
        {summaryLoading && !summaryText ? <div className="rounded-xl bg-emerald-50 p-4 text-sm font-semibold text-emerald-950" role="status">Generating the AI explanation… Official study information remains available below.</div> : null}
        {summaryError && !summaryText ? <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-950" role="alert"><p>{summaryError}</p><p className="mt-1">Use the original official description below while this feature is unavailable.</p><button className="mt-3 w-full rounded-lg bg-white px-3 py-2 font-bold ring-1 ring-amber-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent sm:w-auto" onClick={onRetrySummary} type="button">Try AI summary again</button></div> : null}
        {summaryText && summarySections ? <div className="grid gap-3 sm:grid-cols-2">{summarySections.map((section) => <section className="rounded-xl bg-emerald-50/70 p-4" key={section.heading}><h4 className="text-sm font-black text-emerald-950">{section.heading}</h4><p className="mt-2 text-sm leading-6 text-slate-700">{section.body}</p></section>)}</div> : null}
        {summaryText && !summarySections ? <p className="rounded-xl bg-emerald-50/70 p-4 text-sm leading-6 text-slate-700">{summaryText}</p> : null}
        {!summaryText && !summaryLoading && !summaryError ? <p className="rounded-xl bg-slate-50 p-4 text-sm leading-6 text-slate-600" role="status">The AI explanation has not loaded. The original official description is available below.</p> : null}
      </div>
      {officialBrief || officialDetailed ? <details className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4"><summary className="cursor-pointer text-sm font-bold text-slate-700">Read the original ClinicalTrials.gov description</summary>{officialBrief ? <div className="mt-4"><p className="text-xs font-bold uppercase tracking-wide text-slate-500">Official brief summary</p><p className="mt-2 whitespace-pre-line text-sm leading-6 text-slate-700">{officialBrief}</p></div> : null}{officialDetailed && officialDetailed !== officialBrief ? <div className="mt-5"><p className="text-xs font-bold uppercase tracking-wide text-slate-500">Official detailed description</p><p className="mt-2 whitespace-pre-line text-sm leading-6 text-slate-700">{officialDetailed}</p></div> : null}</details> : <p className="mt-3 text-xs leading-5 text-slate-500">ClinicalTrials.gov did not supply brief or detailed purpose text. No study purpose is inferred.</p>}
    </section>
    {detail ? <div className={`mt-6 rounded-2xl border p-4 text-sm leading-6 ${detail.detail_status === "retrieved" ? "border-emerald-200 bg-emerald-50 text-emerald-950" : "border-amber-200 bg-amber-50 text-amber-950"}`}><p className="font-bold">{detail.detail_status === "retrieved" ? "Official detail available" : "Official detail unavailable"}</p><p>{detail.retrieval_message}</p></div> : null}
    <div className="mt-7 grid gap-3 sm:grid-cols-3"><InfoPanel title="Age eligibility" value={ageLabel(displayed.minimum_age, displayed.maximum_age, status)} helper="Published source range; not a recommendation." /><InfoPanel title="Phase" value={displayed.phases.length ? displayed.phases.map(formatPhase).join(", ") : "Not specified by source"} helper="Not Applicable is shown when supplied by the source." /><InfoPanel title="Registry sex field" value={sourceValue(displayed.sex, status)} helper="This registry field may not describe gender identity." /></div>
    <section className="mt-8 scroll-mt-24" id="study-eligibility"><p className="text-xs font-bold uppercase tracking-[0.16em] text-accent">Published protocol</p><div className="mt-1 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between"><h3 className="text-xl font-black sm:text-2xl">Eligibility requirements</h3><span className="w-fit rounded-full bg-slate-100 px-3 py-1 text-[11px] font-bold text-slate-600">Published text · not an eligibility decision</span></div>{detail?.detail_status === "retrieved" && detail.eligibility_criteria ? <div className="mt-4 whitespace-pre-line rounded-2xl border border-slate-200 bg-white p-5 text-sm leading-7 text-slate-700 shadow-sm sm:p-6">{detail.eligibility_criteria}</div> : <MissingSource message={status === "retrieved" ? "Eligibility requirements were not specified in the retrieved official record." : "Eligibility requirements are unavailable because expanded official fields have not been retrieved. Missing criteria are never treated as requirements met."} />}</section>
    <section className="mt-8 scroll-mt-24" id="study-interventions"><p className="text-xs font-bold uppercase tracking-[0.16em] text-accent">Study treatment</p><h3 className="mt-1 text-xl font-black sm:text-2xl">Interventions</h3><MissingSource message="Intervention details are not included in the current TrialMatch study-detail data. No treatment or procedure is inferred from the title or description; review the official ClinicalTrials.gov record for published intervention information." /></section>
    <FacilitySection detail={detail} massachusetts={maLocations} other={otherLocations} status={status} />
    <section className="mt-8 scroll-mt-24" id="study-contacts"><p className="text-xs font-bold uppercase tracking-[0.16em] text-accent">Contact the study</p><h3 className="mt-1 text-xl font-black sm:text-2xl">Official contacts</h3><p className="mt-2 text-sm leading-6 text-slate-600">Use only the contact details published with this study. The study team can confirm current enrollment and answer protocol questions.</p>{detail?.detail_status === "retrieved" && detail.contacts.length ? <div className="mt-4 grid gap-3 sm:grid-cols-2">{detail.contacts.map((contact, index) => <article className="rounded-2xl border border-slate-200 bg-white p-5 text-sm shadow-sm" key={`${contact.name}-${contact.email}-${index}`}><div className="flex items-start justify-between gap-3"><p className="font-black text-slate-900">{contact.name ?? "Study contact"}</p>{contact.role ? <span className="rounded-full bg-slate-100 px-2.5 py-1 text-[10px] font-bold uppercase text-slate-600">{formatRegistryValue(contact.role)}</span> : null}</div>{contact.facility ? <p className="mt-2 text-slate-500">{contact.facility}</p> : null}<div className="mt-4 flex flex-col items-start gap-2">{contact.phone ? <a className="font-semibold text-accent hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" href={`tel:${contact.phone}`}>Call {contact.phone}</a> : null}{contact.email ? <a className="break-all font-semibold text-accent hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent" href={`mailto:${contact.email}`}>Email {contact.email}</a> : null}{!contact.phone && !contact.email ? <p className="text-xs leading-5 text-slate-500">No phone number or email was published for this contact.</p> : null}</div></article>)}</div> : <MissingSource message={status === "retrieved" ? "No contact details were provided in the retrieved official record. Use the official study page for updates." : "Contact details have not been retrieved."} />}</section>
    <details className="mt-8 rounded-2xl border border-slate-200 bg-white p-4 text-sm"><summary className="cursor-pointer font-bold">Source and refresh details</summary><dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2"><div><dt className="text-slate-500">Official identifier</dt><dd className="mt-1 font-semibold">{summary.nct_id}</dd></div><div><dt className="text-slate-500">Last source update in TrialMatch</dt><dd className="mt-1 font-semibold">{formatTimestamp(displayed.retrieved_at)}</dd></div>{detail ? <><div><dt className="text-slate-500">Detail came from</dt><dd className="mt-1 font-semibold">{formatRetrievalSource(detail.retrieval_source)}</dd></div><div><dt className="text-slate-500">Latest retrieval attempt</dt><dd className="mt-1 font-semibold">{formatTimestamp(detail.detail_attempted_at)}</dd></div></> : null}</dl><div className="mt-4 flex flex-wrap gap-3"><a className="rounded-xl bg-accent px-4 py-2.5 font-bold text-white" href={displayed.source_url} rel="noreferrer" target="_blank">View official ClinicalTrials.gov record ↗</a><button className="rounded-xl border border-slate-300 px-4 py-2.5 font-bold" disabled={loading} onClick={onRefresh} type="button">Refresh official data</button></div></details>
    </> : null}</div></section></div>;
}

function FacilitySection({detail, massachusetts, other, status}: {detail?: TrialDetail; massachusetts: TrialLocation[]; other: TrialLocation[]; status: string}) {
  if (detail?.detail_status !== "retrieved") return <section className="mt-8 scroll-mt-24" id="study-locations"><p className="text-xs font-bold uppercase tracking-[0.16em] text-accent">Published locations</p><h3 className="mt-1 text-xl font-black sm:text-2xl">Study locations</h3><MissingSource message={status === "retrieval_failed" ? "Location details could not be retrieved. This does not mean the study has no locations." : "Published location details have not been retrieved, so no location or availability can be claimed."} /></section>;
  return <section className="mt-8 scroll-mt-24" id="study-locations"><p className="text-xs font-bold uppercase tracking-[0.16em] text-accent">Published locations</p><div className="mt-1 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between"><h3 className="text-xl font-black sm:text-2xl">Massachusetts locations</h3><span className="w-fit rounded-full bg-emerald-100 px-3 py-1 text-[11px] font-bold text-emerald-900">Present in retrieved official record</span></div><p className="mt-2 text-sm leading-6 text-slate-600">Facility names, addresses, and statuses below are presented exactly from the retrieved study record.</p>{massachusetts.length ? <LocationList locations={massachusetts} /> : <MissingSource message="No Massachusetts location was specified in the retrieved official record. This is not proof that no local option exists." />}<h3 className="mt-8 text-lg font-black">Other published locations</h3>{other.length ? <LocationList locations={other} /> : <p className="mt-3 text-sm text-slate-600">No additional non-Massachusetts locations were specified in the retrieved record.</p>}<div className="mt-5 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-xs leading-5 text-amber-950"><strong>Verify before making plans.</strong> Presence in the official record is verified; current enrollment, site availability, travel distance, and accessibility are not independently verified by TrialMatch. Contact the study team to confirm.</div></section>;
}

function LocationList({locations}: {locations: TrialLocation[]}) {
  return <div className="mt-4 grid gap-3 sm:grid-cols-2">{locations.map((site, index) => <article className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" key={`${site.facility}-${site.city}-${index}`}><div className="flex items-start justify-between gap-3"><div><p className="font-black text-slate-900">{site.facility ?? "Facility name not specified"}</p><p className="mt-2 text-sm leading-6 text-slate-600">{[site.city, site.state, site.postal_code, site.country].filter(Boolean).join(", ") || "Address not specified by source"}</p></div>{site.recruiting_status ? <span className={`shrink-0 rounded-full px-2 py-1 text-[10px] font-bold uppercase ${statusClass(site.recruiting_status)}`}>{formatRegistryValue(site.recruiting_status)}</span> : null}</div><div className="mt-4 border-t border-slate-100 pt-3"><p className="text-[10px] font-bold uppercase tracking-wide text-slate-400">Published site status</p><p className="mt-1 text-xs font-semibold text-slate-600">{site.recruiting_status ? formatRegistryValue(site.recruiting_status) : "Not specified in official record"}</p></div></article>)}</div>;
}

function InfoItem({label, value}: {label: string; value: string}) { return <div><p className="text-[11px] font-bold uppercase tracking-wide text-slate-400">{label}</p><p className="mt-1 font-semibold text-slate-700">{value}</p></div>; }
function InfoPanel({title, value, helper}: {title: string; value: string; helper: string}) { return <div className="rounded-2xl border border-slate-200 bg-white p-4"><p className="text-xs font-bold uppercase tracking-wide text-slate-400">{title}</p><p className="mt-2 font-black">{value}</p><p className="mt-2 text-xs leading-5 text-slate-500">{helper}</p></div>; }
function MissingSource({message}: {message: string}) { return <div className="mt-4 rounded-2xl border border-dashed border-slate-300 bg-white/70 p-4 text-sm leading-6 text-slate-600">{message}</div>; }
function Alert({message, onRetry}: {message: string; onRetry: () => void}) { return <div className="mt-5 flex items-center justify-between gap-4 rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-900" role="alert"><span>{message}</span><button className="shrink-0 rounded-lg bg-white px-3 py-2 font-bold ring-1 ring-red-200" onClick={onRetry} type="button">Try again</button></div>; }
function SearchSkeleton() { return <div className="space-y-4" role="status"><span className="sr-only">Loading studies</span>{[0, 1, 2].map((item) => <div className="animate-pulse rounded-3xl border border-slate-200 bg-white p-7" key={item}><div className="h-4 w-1/3 rounded bg-slate-200" /><div className="mt-4 h-8 w-4/5 rounded bg-slate-200" /><div className="mt-4 h-4 w-2/3 rounded bg-slate-100" /></div>)}</div>; }
function DetailSkeleton() { return <div className="animate-pulse" role="status"><span className="sr-only">Retrieving official study details</span><div className="h-5 w-1/3 rounded bg-slate-200" /><div className="mt-5 h-10 w-5/6 rounded bg-slate-200" /><div className="mt-3 h-5 w-2/3 rounded bg-slate-100" /><div className="mt-8 h-36 rounded-2xl bg-white" /></div>; }
function compactConditions(conditions: string[], limit = 3): string {
  if (!conditions.length) return "Condition not specified in available source fields";
  const visible = conditions.slice(0, limit);
  const remaining = conditions.length - visible.length;
  return `${visible.join(" · ")}${remaining > 0 ? ` · +${remaining} more` : ""}`;
}
function fallbackStudyExplanation(trial: Pick<TrialSummary, "title" | "conditions">): string {
  const conditionText = trial.conditions.length
    ? `It is listed for ${trial.conditions.slice(0, 3).join(", ")}${trial.conditions.length > 3 ? ", and other listed conditions" : ""}.`
    : "The official record does not list a condition in the available fields.";
  return `ClinicalTrials.gov lists this research as “${trial.title}.” ${conditionText} A separate study-purpose description was not supplied, so no additional purpose is inferred.`;
}

const summaryHeadings = [
  "What is this study investigating?",
  "What might participation involve?",
  "Who is the study intended for?",
  "Important things to know.",
] as const;

export function parseSummarySections(value: string): Array<{heading: string; body: string}> | null {
  const starts = summaryHeadings.map((heading) => value.indexOf(heading));
  if (starts.some((start) => start < 0) || starts.some((start, index) => index > 0 && start <= starts[index - 1])) {
    return null;
  }
  return summaryHeadings.map((heading, index) => {
    const bodyStart = starts[index] + heading.length;
    const bodyEnd = index + 1 < starts.length ? starts[index + 1] : value.length;
    return {
      heading: heading === "Important things to know." ? "Important things to know" : heading,
      body: value.slice(bodyStart, bodyEnd).trim(),
    };
  });
}

function sortTrials(trials: TrialSummary[], sort: SortOption): TrialSummary[] { const copy = [...trials]; if (sort === "title") return copy.sort((a, b) => a.title.localeCompare(b.title)); if (sort === "status") return copy.sort((a, b) => a.overall_status.localeCompare(b.overall_status) || a.title.localeCompare(b.title)); if (sort === "ma_sites") return copy.sort((a, b) => maSiteCount(b.locations) - maSiteCount(a.locations) || a.title.localeCompare(b.title)); return copy; }
function partitionLocations(locations: TrialLocation[]): {massachusetts: TrialLocation[]; other: TrialLocation[]} { const massachusetts: TrialLocation[] = []; const other: TrialLocation[] = []; locations.forEach((site) => (isMassachusetts(site) ? massachusetts : other).push(site)); return {massachusetts, other}; }
function isMassachusetts(site: TrialLocation): boolean { const state = site.state?.trim().toLocaleLowerCase(); return state === "massachusetts" || state === "ma"; }
function maSiteCount(locations: TrialLocation[]): number { return locations.filter(isMassachusetts).length; }
function facilityCountLabel(trial: TrialSummary, maOnly: boolean): string { if (trial.detail_status === "retrieved") { const count = maOnly ? maSiteCount(trial.locations) : trial.locations.length; return count ? `${count} source location${count === 1 ? "" : "s"}` : maOnly ? "None specified in MA" : "None specified by source"; } return trial.detail_status === "retrieval_failed" ? "Retrieval failed — retry available" : "Open details to retrieve"; }
function ageLabel(minimum: string | null, maximum: string | null, status: string): string { if (minimum && maximum) return `${minimum} to ${maximum}`; if (minimum) return `From ${minimum}; upper bound not specified`; if (maximum) return `Up to ${maximum}; lower bound not specified`; return status === "retrieved" ? "Not specified by source" : status === "retrieval_failed" ? "Retrieval failed" : "Not yet retrieved"; }
function sourceValue(value: string | null, status: string): string { return value ? formatRegistryValue(value) : status === "retrieved" ? "Not specified by source" : status === "retrieval_failed" ? "Retrieval failed" : "Not yet retrieved"; }
function errorMessage(payload: ApiError, fallback: string): string { const message = payload.error?.message ?? fallback; const detail = payload.error?.details?.map((item) => item.reason).filter(Boolean).join("; "); return detail ? `${message} ${detail}` : message; }
function formatTimestamp(value: string | null): string { if (!value) return "not available"; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], {dateStyle: "medium", timeStyle: "short"}); }
function formatRegistryValue(value: string): string { return value.replaceAll("_", " ").toLocaleLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase()); }
function formatPhase(value: string): string { if (value === "NA" || value === "NOT_APPLICABLE") return "Not Applicable"; return formatRegistryValue(value).replace("Phase", "Phase "); }
function formatRetrievalSource(value: TrialDetail["retrieval_source"]): string { return {local_database: "Local validated database", official_cache: "Validated official-response cache", official_api: "ClinicalTrials.gov API"}[value]; }
function statusClass(status: string): string { const normalized = status.toLocaleUpperCase(); if (["RECRUITING", "ENROLLING_BY_INVITATION", "ACTIVE_NOT_RECRUITING"].includes(normalized)) return "bg-emerald-100 text-emerald-900"; if (["NOT_YET_RECRUITING", "UNKNOWN"].includes(normalized)) return "bg-amber-100 text-amber-950"; if (["SUSPENDED", "TERMINATED", "WITHDRAWN"].includes(normalized)) return "bg-red-100 text-red-900"; return "bg-slate-100 text-slate-700"; }
