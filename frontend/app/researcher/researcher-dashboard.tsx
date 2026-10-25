"use client";

import {useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent} from "react";

import {SiteHeader} from "../site-header";
import ResearcherMap from "./researcher-map";
import styles from "./researcher.module.css";
import type {BaselineResult, ConstraintSatisfaction, GeoCollection, GeoFeature, OptimizeResponse, SelectedLocation} from "./types";
import {
  DEFAULT_RADIUS_MILES,
  MAX_RADIUS_MILES,
  accessGapFeatures,
  accessQuery,
  clampRadiusMiles,
  clinicalFilterCounts,
  displayedPlanIsOutdated,
  formatMiles,
  formatRadiusInput,
  isUsablePlanStatus,
  kilometersToMiles,
  optimizationPayload,
  scoreDifference,
  type PlanningInputs,
} from "./researcher-utils";

const integer = new Intl.NumberFormat("en-US", {maximumFractionDigits: 0});
const decimal = new Intl.NumberFormat("en-US", {maximumFractionDigits: 2});
const percentMagnitude = new Intl.NumberFormat("en-US", {maximumFractionDigits: 1});
type WorkspaceMode = "access" | "planning";
type MapPlan = {result: OptimizeResponse; inputs: PlanningInputs};

function apiBase() { return (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/+$/, ""); }
function apiUrl(path: string) { return `${apiBase()}${path}`; }

async function errorMessage(response: Response) {
  try {
    const body = await response.json() as {detail?: string | {message?: string} | Array<{msg?: string}>; error?: {message?: string}};
    if (typeof body.detail === "string") return body.detail;
    if (body.detail && !Array.isArray(body.detail) && body.detail.message) return body.detail.message;
    if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg).filter(Boolean).join("; ");
    if (body.error?.message) return body.error.message;
  } catch { /* Use the HTTP status below. */ }
  return `Request failed (${response.status} ${response.statusText || "Unknown error"}).`;
}

function formatDate(value?: string | null) {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Unavailable" : date.toLocaleString("en-US", {dateStyle: "medium", timeStyle: "short"});
}
function nullableInteger(value: number | null | undefined) { return value == null ? "Unavailable" : integer.format(value); }
function nullablePercent(value: number | null | undefined) { return value == null ? "Unavailable" : `${decimal.format(value)}%`; }
function accessValue(feature: GeoFeature) {
  const nearby = feature.properties.distinct_nearby_recruiting_nct_count;
  if (nearby === null) return "Not available";
  if (nearby === 0) return "No nearby trial found in this dataset";
  return `${integer.format(nearby)} clinical ${nearby === 1 ? "trial" : "trials"}`;
}

function Metric({label, value, help}: {label: string; value: string; help?: string}) {
  return <div className={styles.metric} aria-label={help ? `${label}: ${value}. ${help}` : `${label}: ${value}`}><span>{label}</span><strong>{value}</strong>{help && <small>{help}</small>}</div>;
}
function ConstraintLabel({constraints}: {constraints: ConstraintSatisfaction}) {
  return <span className={constraints.all_satisfied ? styles.ok : styles.bad}>{constraints.all_satisfied ? "Validation passed" : "Validation needs review"}</span>;
}
function strategyLabel(strategy: BaselineResult["strategy"]) { return strategy === "population_first_greedy" ? "Maximize reach" : "Prioritize equity"; }
function strategyDescription(name: string) {
  if (name === "Balanced approach") return "Uses the selected balance of represented population reach and language-access priority.";
  if (name === "Maximize reach") return "Focus on covering more people.";
  return "Give greater weight to communities with documented language-access needs.";
}
function improvementLabel(optimized: number, baseline: number) {
  const comparison = scoreDifference(optimized, baseline);
  if (comparison.tied) return "Same modeled score.";
  if (comparison.percentage === null) return `${decimal.format(Math.abs(comparison.difference))} score points ${comparison.difference > 0 ? "higher" : "lower"}; a percentage is unavailable because the comparison score is zero.`;
  return `${decimal.format(Math.abs(comparison.difference))} points (${percentMagnitude.format(Math.abs(comparison.percentage))}%) ${comparison.difference > 0 ? "higher" : "lower"}.`;
}

export function Recommendation({location, displayIndex, reachMiles, active, onInspect}: {location: SelectedLocation; displayIndex: number; reachMiles: number; active: boolean; onInspect: () => void}) {
  const explanation = location.explanation;
  return <li className={`${styles.recommendation} ${active ? styles.recommendationActive : ""}`} id={`recommendation-${encodeURIComponent(location.municipality_id)}`}>
    <details>
      <summary onClick={onInspect}>
        <span className={styles.displayIndex} aria-label={`Map marker ${displayIndex}`}>{displayIndex}</span>
        <span className={styles.recommendationName}>{location.municipality_id}<small>Suggested outreach area — not a booked venue</small></span>
        <span className={styles.reachMetric}><b>{integer.format(explanation.raw_catchment_community_count)}</b><small>communities in modeled reach</small></span>
        <span className={styles.cardChevron} aria-hidden="true">＋</span>
      </summary>
      <div className={styles.recommendationDetails}>
        <p>This area contributes to the combined plan by reaching the source-backed catchment shown below. Selections work together; marker numbers identify them and are not a best-to-worst ranking.</p>
        <dl>
          <dt>Event reach radius</dt><dd>{formatMiles(reachMiles)} straight-line</dd>
          <dt>Represented population in reach</dt><dd>{integer.format(explanation.raw_catchment_ej_subset_population_proxy)}</dd>
          <dt>Communities only this area covers</dt><dd>{integer.format(explanation.uniquely_covered_community_count)}</dd>
          <dt>Represented population only this area covers</dt><dd>{integer.format(explanation.uniquely_covered_ej_subset_population_proxy)}</dd>
          <dt>Fixed-plan score contribution</dt><dd>{decimal.format(explanation.leave_one_out_objective_loss)}</dd>
        </dl>
        <p className={styles.caution}>Unique contribution is calculated within this selected plan. Removing this area while holding the others fixed reduces the score by the amount shown; this is not causal impact or a re-optimized result.</p>
        <button type="button" className={styles.secondaryButton} onClick={onInspect}>Show this area on the map</button>
      </div>
    </details>
  </li>;
}

export default function ResearcherDashboard() {
  const [mode, setMode] = useState<WorkspaceMode>("access");
  const [geo, setGeo] = useState<GeoCollection | null>(null);
  const [planningGeo, setPlanningGeo] = useState<GeoCollection | null>(null);
  const [geoLoading, setGeoLoading] = useState(true);
  const [geoError, setGeoError] = useState<string | null>(null);
  const [condition, setCondition] = useState("");
  const [accessRadiusMiles, setAccessRadiusMiles] = useState(DEFAULT_RADIUS_MILES);
  const [appliedCondition, setAppliedCondition] = useState("");
  const [appliedAccessRadiusMiles, setAppliedAccessRadiusMiles] = useState(DEFAULT_RADIUS_MILES);
  const [maxEvents, setMaxEvents] = useState(5);
  const [radiusMiles, setRadiusMiles] = useState(DEFAULT_RADIUS_MILES);
  const [equityWeight, setEquityWeight] = useState(0.5);
  const [result, setResult] = useState<OptimizeResponse | null>(null);
  const [submittedPlan, setSubmittedPlan] = useState<PlanningInputs | null>(null);
  const [mapPlan, setMapPlan] = useState<MapPlan | null>(null);
  const [optimizing, setOptimizing] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [optimizeError, setOptimizeError] = useState<string | null>(null);
  const [activeFeature, setActiveFeature] = useState<GeoFeature | null>(null);
  const geoAbortRef = useRef<AbortController | null>(null);
  const optimizeAbortRef = useRef<AbortController | null>(null);
  const geoRequestRef = useRef(0);
  const optimizeRequestRef = useRef(0);
  const resultsRef = useRef<HTMLElement>(null);

  const currentPlan = useMemo<PlanningInputs>(() => ({maxEvents, radiusMiles, equityWeight}), [maxEvents, radiusMiles, equityWeight]);
  const stale = Boolean(result) && displayedPlanIsOutdated(currentPlan, submittedPlan, refreshFailed);
  const planUsable = result ? isUsablePlanStatus(result.status) : false;
  const accessChanged = condition !== appliedCondition || accessRadiusMiles !== appliedAccessRadiusMiles;

  const loadGeo = useCallback(async (nextCondition: string, nextRadiusMiles: number) => {
    geoAbortRef.current?.abort();
    const controller = new AbortController();
    const requestId = ++geoRequestRef.current;
    geoAbortRef.current = controller;
    setGeoLoading(true); setGeoError(null);
    try {
      const response = await fetch(`${apiUrl("/api/geo")}?${accessQuery(nextCondition, nextRadiusMiles)}`, {signal: controller.signal, headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(await errorMessage(response));
      const nextGeo = await response.json() as GeoCollection;
      if (requestId !== geoRequestRef.current) return;
      setGeo(nextGeo);
      if (!nextCondition) setPlanningGeo(nextGeo);
      setAppliedCondition(nextCondition); setAppliedAccessRadiusMiles(nextRadiusMiles); setActiveFeature(null);
    } catch (error) {
      if (controller.signal.aborted || requestId !== geoRequestRef.current) return;
      setGeoError(error instanceof Error ? error.message : "Unable to load community and recruiting-location data.");
    } finally { if (requestId === geoRequestRef.current) setGeoLoading(false); }
  }, []);

  useEffect(() => {
    void loadGeo("", DEFAULT_RADIUS_MILES);
    return () => { geoAbortRef.current?.abort(); optimizeAbortRef.current?.abort(); };
  }, [loadGeo]);

  const runOptimization = async () => {
    optimizeAbortRef.current?.abort();
    const controller = new AbortController();
    const requestId = ++optimizeRequestRef.current;
    const inputs = {...currentPlan};
    optimizeAbortRef.current = controller;
    setOptimizing(true); setOptimizeError(null);
    try {
      const response = await fetch(apiUrl("/api/optimize"), {method: "POST", signal: controller.signal, headers: {"Content-Type": "application/json", Accept: "application/json"}, body: JSON.stringify(optimizationPayload(inputs))});
      if (!response.ok) throw new Error(await errorMessage(response));
      const nextResult = await response.json() as OptimizeResponse;
      if (requestId !== optimizeRequestRef.current) return;
      setResult(nextResult); setSubmittedPlan(inputs); setMapPlan({result: nextResult, inputs}); setRefreshFailed(false); setActiveFeature(null);
      requestAnimationFrame(() => resultsRef.current?.focus({preventScroll: true}));
    } catch (error) {
      if (controller.signal.aborted || requestId !== optimizeRequestRef.current) return;
      setRefreshFailed(Boolean(result));
      setOptimizeError(error instanceof Error ? error.message : "The outreach planning request failed.");
    } finally { if (requestId === optimizeRequestRef.current) setOptimizing(false); }
  };

  const selectMode = (nextMode: WorkspaceMode) => { setMode(nextMode); setActiveFeature(null); };
  const inspectFeature = (id: string) => setActiveFeature((planningGeo ?? geo)?.features.find((feature) => feature.id === id) ?? null);
  const mapGeo = mode === "planning" ? planningGeo ?? geo : geo;
  const mapSelectedLocations = mode === "planning" ? mapPlan?.result.selected_locations ?? [] : [];
  const mapCoveredIds = useMemo(() => new Set(mode === "planning" ? mapPlan?.result.covered_municipality_ids ?? [] : []), [mapPlan, mode]);
  const counts = clinicalFilterCounts(geo?.summary);
  const accessDataAvailable = geo?.summary.clinical_site_data_available === true;
  const accessGaps = useMemo(() => accessDataAvailable ? accessGapFeatures(geo?.features ?? []) : [], [accessDataAvailable, geo]);
  const conditionLabel = appliedCondition || "All medical conditions";
  const resolvedSites = geo?.recruiting_site_features.filter((feature) => feature.geometry).length ?? 0;
  const activeSelection = mapPlan?.result.selected_locations.find((location) => location.municipality_id === activeFeature?.id);
  const comparison = result ? [
    {name: "Balanced approach", events: result.total_events, covered: result.covered_municipality_count, population: result.modeled_ej_subset_population_proxy_covered, objective: result.modeled_outreach_coverage_objective, constraints: result.constraint_satisfaction, selected: result.selected_locations.map((location) => location.municipality_id)},
    ...result.baselines.map((baseline) => ({name: strategyLabel(baseline.strategy), events: baseline.total_events, covered: baseline.covered_community_ids.length, population: baseline.modeled_ej_subset_population_proxy_covered, objective: baseline.modeled_outreach_coverage_objective, constraints: baseline.constraint_satisfaction, selected: baseline.selected_municipality_ids})),
  ] : [];
  const bestPopulation = comparison.length ? comparison.reduce((best, row) => row.population > best.population ? row : best) : null;
  const bestCoverage = comparison.length ? comparison.reduce((best, row) => row.covered > best.covered ? row : best) : null;
  const handleTabKey = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight" && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const nextMode: WorkspaceMode = event.key === "Home" ? "access" : event.key === "End" ? "planning" : mode === "access" ? "planning" : "access";
    selectMode(nextMode);
    requestAnimationFrame(() => document.getElementById(`${nextMode}-tab`)?.focus());
  };

  return <div className={styles.shell}>
    <SiteHeader active="researcher" />
    <main className={styles.main}>
      <section className={styles.intro} aria-labelledby="workspace-title">
        <div><span className={styles.eyebrow}>Making clinical research more accessible</span><h1 id="workspace-title">Make clinical research more accessible.</h1><p>Discover gaps in trial access and find where community outreach could help.</p></div>
        <div className={styles.freshness}>{geo ? <><strong>{integer.format(geo.summary.observed_feature_count)} communities mapped</strong><span>Community data retrieved {formatDate(geo.summary.retrieved_at)}</span></> : "Loading community data…"}</div>
      </section>

      <nav className={styles.modeTabs} role="tablist" aria-label="Researcher workflows" onKeyDown={handleTabKey}>
        <button id="access-tab" role="tab" type="button" aria-selected={mode === "access"} aria-controls="workflow-panel" tabIndex={mode === "access" ? 0 : -1} className={mode === "access" ? styles.modeActive : ""} onClick={() => selectMode("access")}><b>Find Trial Access Gaps</b><small>Where are clinical trials harder to reach?</small></button>
        <button id="planning-tab" role="tab" type="button" aria-selected={mode === "planning"} aria-controls="workflow-panel" tabIndex={mode === "planning" ? 0 : -1} className={mode === "planning" ? styles.modeActive : ""} onClick={() => selectMode("planning")}><b>Plan Community Outreach</b><small>Where should researchers reach out next?</small></button>
      </nav>

      <div className={styles.workspace} id="workflow-panel" role="tabpanel" aria-labelledby={`${mode}-tab`}>
        <aside className={styles.panel} aria-labelledby="controls-heading">
          {mode === "access" ? <>
            <div className={styles.panelHead}><span className={styles.step}>Step 1 · Understand access</span><h2 id="controls-heading">Find Trial Access Gaps</h2></div>
            <p className={styles.question}>Select a medical condition to see where trials are recruiting and which communities have fewer nearby options.</p>
            <label className={styles.field}><span className={styles.fieldLabel}>Medical condition</span><select value={condition} disabled={geoLoading || !geo?.summary.clinical_site_data_available} onChange={(event) => setCondition(event.target.value)}><option value="">All medical conditions</option>{geo?.available_conditions?.map((item) => <option value={item} key={item}>{item}</option>)}</select><small className={styles.hint}>Conditions use the labels available in this exported study dataset.</small></label>
            <label className={styles.field}><span className={styles.fieldLabel}>Search radius <output>{formatMiles(accessRadiusMiles)}</output></span><span className={styles.dual}><input aria-label="Search radius in miles" type="range" min={0} max={MAX_RADIUS_MILES} step={0.5} value={accessRadiusMiles} disabled={geoLoading} onChange={(event) => setAccessRadiusMiles(clampRadiusMiles(Number(event.target.value)))} /><input aria-label="Search radius value in miles" type="number" min={0} max={MAX_RADIUS_MILES} step={0.5} value={formatRadiusInput(accessRadiusMiles)} disabled={geoLoading} onChange={(event) => setAccessRadiusMiles(clampRadiusMiles(Number(event.target.value)))} /></span><small className={styles.hint}>Nearby means straight-line distance, not driving time.</small></label>
            <button className={styles.run} type="button" onClick={() => void loadGeo(condition, accessRadiusMiles)} disabled={geoLoading || !geo?.summary.clinical_site_data_available}>{geoLoading ? "Analyzing trial access…" : "Find access gaps"}</button>
            {accessChanged && !geoLoading && <p className={styles.changed} role="status"><strong>Settings changed.</strong> Select Find access gaps to update the map and results.</p>}
            {!geo?.summary.clinical_site_data_available && !geoLoading && <p className={styles.changed}>Recruiting-location data is unavailable. Counts and distances are unknown—not zero.</p>}
            {geoError && <p className={styles.error} role="alert"><strong>Trial access could not be updated.</strong><br />{geoError}</p>}
            {geo && <div className={styles.sidebarSummary}><strong>Your search results</strong><dl><dt>Clinical trials</dt><dd>{nullableInteger(counts.distinctStudies)}</dd><dt>Recruiting locations</dt><dd>{nullableInteger(counts.siteRecords)}</dd><dt>Locations found on map</dt><dd>{nullableInteger(counts.resolved)}</dd><dt>Communities without a nearby trial</dt><dd>{accessDataAvailable ? integer.format(accessGaps.length) : "Unavailable"}</dd></dl></div>}
          </> : <>
            <div className={styles.panelHead}><span className={styles.step}>Step 2 · Plan engagement</span><h2 id="controls-heading">Plan Community Outreach</h2></div>
            <p className={styles.question}>Imagine your research team can host {maxEvents} outreach {maxEvents === 1 ? "event" : "events"}. Where should {maxEvents === 1 ? "it" : "they"} go to reach communities that are often overlooked?</p>
            <p className={styles.modeNote}>This planner uses represented population and documented language-access data. In this model, “underserved” refers only to greater documented language-access needs; it is not a general measure of disadvantage. The planner does not target patients with the selected medical condition.</p>
            <label className={styles.field}><span className={styles.fieldLabel}>How many events can you host?</span><input disabled={optimizing} aria-label="How many events can you host?" type="number" min={0} max={20} step={1} value={maxEvents} onChange={(event) => setMaxEvents(Math.min(20, Math.max(0, Math.round(Number(event.target.value) || 0))))} /><small className={styles.hint}>Choose 0–20 event areas. Zero produces an empty plan.</small></label>
            <label className={styles.field}><span className={styles.fieldLabel}>Event reach radius <output>{formatMiles(radiusMiles)}</output></span><span className={styles.dual}><input disabled={optimizing} aria-label="Event reach radius in miles" type="range" min={0} max={MAX_RADIUS_MILES} step={0.5} value={radiusMiles} onChange={(event) => setRadiusMiles(clampRadiusMiles(Number(event.target.value)))} /><input disabled={optimizing} aria-label="Event reach radius value in miles" type="number" min={0} max={MAX_RADIUS_MILES} step={0.5} value={formatRadiusInput(radiusMiles)} onChange={(event) => setRadiusMiles(clampRadiusMiles(Number(event.target.value)))} /></span><small className={styles.hint}>The same straight-line radius is modeled for every event.</small></label>
            <label className={styles.field}><span className={styles.fieldLabel}>Prioritize underserved communities <output>{Math.round(equityWeight * 100)}%</output></span><input disabled={optimizing} aria-label="Prioritize underserved communities" type="range" min={0} max={1} step={0.05} value={equityWeight} onChange={(event) => setEquityWeight(Number(event.target.value))} /><small className={styles.hint}>Move this slider higher to give more priority to communities with greater documented language-access needs.</small></label>
            <button className={styles.run} type="button" onClick={() => void runOptimization()} disabled={optimizing || geoLoading}>{optimizing ? "Finding outreach locations…" : result ? "Update recommendations" : "Find the best locations"}</button>
            {(stale || optimizing) && <p className={styles.changed} role="status"><strong>{optimizing ? "Finding a new plan…" : "These recommendations use earlier settings."}</strong> {optimizing ? "Current results remain available while the map updates." : "Update recommendations before acting on them."}</p>}
            {optimizeError && <p className={styles.error} role="alert"><strong>Recommendations could not be updated.</strong><br />{optimizeError}</p>}
          </>}
        </aside>

        <section className={styles.mapCard} aria-labelledby="map-heading">
          <div className={styles.mapTitle}><strong id="map-heading">{mode === "access" ? conditionLabel : "Community outreach plan"}</strong><span>{mode === "access" ? `Access within ${formatMiles(appliedAccessRadiusMiles)}` : mapPlan ? `${formatMiles(mapPlan.inputs.radiusMiles)} modeled event reach` : "Run the planner to see recommended areas"}</span></div>
          {!geo && geoLoading && <p className={styles.loading} role="status">Loading community and recruiting-location data…</p>}
          {!geo && geoError && <div className={styles.mapError} role="alert"><strong>Map data unavailable.</strong><br />{geoError}<br /><button type="button" onClick={() => void loadGeo(condition, accessRadiusMiles)}>Try again</button></div>}
          {mapGeo && <ResearcherMap data={mapGeo} selectedLocations={mapSelectedLocations} coveredIds={mapCoveredIds} submittedRadiusKm={mode === "planning" && mapPlan ? optimizationPayload(mapPlan.inputs).coverage_radius_km : null} visualizationMode={mode} activeFeatureId={activeFeature?.id ?? null} onSelect={setActiveFeature} />}
          {geoLoading && geo && <div className={styles.mapLoading} role="status">Analyzing trial access…</div>}
          {activeFeature && <div className={styles.details} role="region" aria-label={`${activeFeature.id} details`}><button type="button" aria-label="Close community details" onClick={() => setActiveFeature(null)}>×</button><strong>{activeFeature.id}</strong>{activeSelection && <span className={styles.selectedTag}>Recommended outreach area</span>}<p className={styles.detailMeaning}>{mode === "access" ? activeFeature.properties.distinct_nearby_recruiting_nct_count === 0 ? `No matching recruiting location was found within ${formatMiles(appliedAccessRadiusMiles)} in the available dataset.` : activeFeature.properties.distinct_nearby_recruiting_nct_count === null ? "Trial access could not be calculated for this community." : "At least one matching recruiting location is within the selected straight-line radius." : activeSelection ? "This area is part of the combined outreach plan; it is not a verified event venue." : mapCoveredIds.has(activeFeature.id) ? "This community is within the modeled reach of at least one suggested event area." : "This community is outside the modeled reach of the current plan."}</p><dl><dt>Nearby clinical trials</dt><dd>{accessValue(activeFeature)}</dd><dt>Nearest recruiting location</dt><dd>{activeFeature.properties.nearest_resolved_recruiting_site_distance_km === null ? (activeFeature.properties.distinct_nearby_recruiting_nct_count === null ? "Unavailable" : "No resolved matching location") : `${formatMiles(kilometersToMiles(activeFeature.properties.nearest_resolved_recruiting_site_distance_km))} straight-line`}</dd><dt>Represented population</dt><dd>{nullableInteger(activeFeature.properties.ej_subset_population)}</dd><dt>Documented language-access need</dt><dd>{nullablePercent(activeFeature.properties.population_weighted_limited_english_household_percent_ej_subset)}</dd>{activeSelection && <><dt>Communities in modeled reach</dt><dd>{integer.format(activeSelection.explanation.raw_catchment_community_count)}</dd><dt>Represented population in reach</dt><dd>{integer.format(activeSelection.explanation.raw_catchment_ej_subset_population_proxy)}</dd></>}</dl></div>}
        </section>
      </div>

      {mode === "access" ? <section className={styles.accessResults} aria-live="polite" aria-labelledby="findings-heading">
        <article className={styles.findingCard}><span className={styles.eyebrow}>{geoLoading ? "Analyzing access" : geoError ? "Update failed" : accessDataAvailable ? "Analysis complete" : "Data unavailable"}</span><h2 id="findings-heading">What we found</h2>{geo ? accessDataAvailable ? <><p className={styles.resultSentence}>We found <strong>{nullableInteger(counts.distinctStudies)} clinical trials</strong> recruiting at <strong>{nullableInteger(counts.resolved)} mapped locations</strong> for {appliedCondition ? <strong>{appliedCondition}</strong> : "all medical conditions"}. <strong>{integer.format(accessGaps.length)} communities</strong> have no matching recruiting location within {formatMiles(appliedAccessRadiusMiles)} in our dataset.</p>{resolvedSites === 0 && <p className={styles.empty}><strong>No matching recruiting locations were found on the map.</strong> Try another condition or a broader search, while remembering that this is a limited exported dataset.</p>}</> : <p className={styles.empty}><strong>Trial access is unavailable.</strong> The clinical export could not be loaded, so community gaps are unknown—not zero.</p> : <p className={styles.empty}>Results will appear after community data loads.</p>}<div className={styles.metricGrid}><Metric label="Clinical trials" value={nullableInteger(counts.distinctStudies)} /><Metric label="Recruiting locations" value={nullableInteger(counts.siteRecords)} /><Metric label="Locations found on map" value={nullableInteger(counts.resolved)} /><Metric label="Duplicate locations removed" value={nullableInteger(geo?.summary.clinical_site_duplicate_removed_count)} /></div></article>
        <article className={styles.gapsCard}><div className={styles.sectionHeading}><div><span className={styles.eyebrow}>Next step</span><h2>Communities worth investigating</h2></div><button type="button" className={styles.secondaryButton} onClick={() => selectMode("planning")}>Explore community outreach planning</button></div><p className={styles.sectionIntro}>These communities have zero matching trials within the current radius. They are sorted by straight-line distance to the nearest mapped recruiting location when available—not by disease burden or patient need.</p>{!accessDataAvailable ? <p className={styles.empty}>Community access gaps cannot be identified until recruiting-location data is available.</p> : accessGaps.length ? <ul className={styles.gapList}>{accessGaps.slice(0, 8).map((feature) => <li key={feature.id}><button type="button" className={activeFeature?.id === feature.id ? styles.gapActive : ""} onClick={() => setActiveFeature(feature)}><strong>{feature.id}</strong><span>{feature.properties.nearest_resolved_recruiting_site_distance_km == null ? "No resolved matching location" : `${formatMiles(kilometersToMiles(feature.properties.nearest_resolved_recruiting_site_distance_km))} to nearest location`}</span></button></li>)}</ul> : <p className={styles.empty}>No zero-trial communities were identified at this radius. Select any community marker to inspect its nearby options.</p>}<p className={styles.nextAction}>Select a community to inspect its nearest recruiting locations.</p></article>
      </section> : <section className={styles.results} ref={resultsRef} tabIndex={-1} aria-label="Outreach planning results" aria-live="polite">
        <article className={styles.planSummary}><div className={styles.statusRow}><div><span className={styles.eyebrow}>{stale || optimizing ? "Previous plan" : result ? "Recommendations ready" : "Plan your events"}</span><h2>{result && planUsable ? "Your outreach plan is ready" : "Your recommended outreach plan"}</h2></div>{result && planUsable && <span className={styles.badge}>Optimized plan</span>}</div>
          {!result && <p className={styles.empty}>Choose your settings, then find locations. No areas are selected until the optimizer returns a result.</p>}
          {result && planUsable && <><p className={styles.resultSentence}>With <strong>{integer.format(result.total_events)} outreach {result.total_events === 1 ? "event" : "events"}</strong>, your plan could cover <strong>{integer.format(result.covered_municipality_count)} communities represented in our data</strong>.</p><p className={styles.qualification}>Modeled geographic reach, not confirmed attendance or patient enrollment.</p><div className={styles.metricGrid}><Metric label="Suggested outreach areas" value={integer.format(result.total_events)} /><Metric label="Unique communities covered" value={integer.format(result.covered_municipality_count)} help="Each community is counted once across overlapping event reaches." /><Metric label="Represented population in reach" value={integer.format(result.modeled_ej_subset_population_proxy_covered)} help="A source-based planning estimate, not expected enrollment." /></div><p className={styles.overlap}>Some communities may be covered by multiple events. Each community is counted only once in the combined coverage total.</p></>}
          {result && !planUsable && <p className={styles.error} role="alert"><strong>No feasible outreach recommendations were returned.</strong><br />The solver reported {result.status}. Adjust the settings and try again; no locations are presented as recommendations.</p>}
        </article>
        <article className={styles.recommendationsCard}><span className={styles.eyebrow}>Recommended Locations</span><h2>Where to host your events</h2>{!result ? <p className={styles.empty}>Recommended areas will appear here after the planner runs.</p> : !planUsable ? <p className={styles.empty}>Recommendations are unavailable because the optimizer did not return a usable plan.</p> : result.selected_locations.length === 0 ? <p className={styles.empty}>{submittedPlan?.maxEvents === 0 ? "No areas were selected because zero outreach events were requested." : "No feasible locations were selected under these settings."}</p> : <><p className={styles.sectionIntro}>These areas were selected together. Marker numbers connect each card to the map; they do not rank locations from best to worst.</p><ol className={styles.recommendationList}>{result.selected_locations.map((location, index) => <Recommendation key={location.municipality_id} location={location} displayIndex={index + 1} reachMiles={submittedPlan?.radiusMiles ?? radiusMiles} active={activeFeature?.id === location.municipality_id} onInspect={() => inspectFeature(location.municipality_id)} />)}</ol></>}</article>
        <article className={styles.compareCard}><span className={styles.eyebrow}>Which strategy works best?</span><h2>Compare outreach strategies</h2>{!result ? <p className={styles.empty}>Run the planner to compare three approaches using the same event limits and modeled score.</p> : !planUsable ? <p className={styles.empty}>A comparison is unavailable because the optimizer did not return a usable plan.</p> : <><div className={styles.strategyGrid}>{comparison.map((row) => <section key={row.name} className={row.name === "Balanced approach" ? styles.strategyPrimary : ""}><h3>{row.name}</h3><p>{strategyDescription(row.name)}</p><dl><dt>Unique communities</dt><dd>{integer.format(row.covered)}</dd><dt>Represented population</dt><dd>{integer.format(row.population)}</dd><dt>Events</dt><dd>{integer.format(row.events)}</dd></dl><ConstraintLabel constraints={row.constraints} /><details><summary>{row.selected.length} selected areas</summary>{row.selected.length ? row.selected.join(", ") : "None"}</details>{row.name !== "Balanced approach" && <small>{improvementLabel(result.modeled_outreach_coverage_objective, row.objective)}</small>}</section>)}</div><div className={styles.insight}><strong>What this means</strong><p>{bestCoverage && bestPopulation ? `${bestCoverage.name} covers the most communities in this result. ${bestPopulation.name} reaches the largest represented population. Compare these outcomes with the language-access priority you selected before deciding where to engage.` : "Compare modeled community and population reach before deciding which approach fits your outreach goals."}</p></div></>}</article>
      </section>}

      <section className={styles.disclosures} aria-label="Data and methodology disclosures">
        <details><summary>About the data</summary><p>Results use a limited set of exported recruiting studies and Massachusetts community geographic data—not every existing clinical trial. {geo?.summary.clinical_site_data_available ? `The current source includes ${nullableInteger(geo.summary.clinical_site_source_record_count)} recruiting-location records across ${nullableInteger(geo.summary.clinical_site_exported_study_count)} exported studies.` : "The clinical export is currently unavailable."}</p><p>{geo?.summary.clinical_site_export_scope ?? "Export scope is unavailable."}</p></details>
        <details><summary>How this analysis works</summary><p>Trial access uses the existing straight-line proximity calculation between representative community points and mapped recruiting locations. Outreach planning uses community population and documented language-access data; it does not target a selected condition or identify eligible patients.</p></details>
        <details><summary>Technical details</summary><dl className={styles.wideDl}><dt>Community source</dt><dd>{geo ? <a href={geo.summary.source_url} target="_blank" rel="noreferrer">{geo.summary.source_name}</a> : "Unavailable"}</dd><dt>Communities observed / mapped</dt><dd>{geo ? `${geo.summary.observed_feature_count} / ${geo.summary.resolved_feature_count}` : "Unavailable"}</dd><dt>Coordinates</dt><dd>{geo?.summary.clinical_site_coordinate_precision_limitation ?? "Unavailable"}</dd><dt>Clinical export generated</dt><dd>{formatDate(geo?.summary.clinical_site_export_generated_at)}</dd>{result && <><dt>Solver</dt><dd>{result.solver.name} {result.solver.version}: {result.solver.message}</dd><dt>Proof status</dt><dd>{result.status === "optimal" ? "The solver reported an optimal solution under the model and constraints." : `The solver returned ${result.status}.`} MIP gap: {result.solver.mip_gap === null ? "unavailable" : decimal.format(result.solver.mip_gap)}.</dd><dt>Objective score</dt><dd>{decimal.format(result.modeled_outreach_coverage_objective)} (technical equity-and-reach objective)</dd><dt>Validation</dt><dd><ConstraintLabel constraints={result.constraint_satisfaction} /></dd><dt>Scenario</dt><dd>{result.scenario_construction.scenario_label}; {result.scenario_construction.included_municipality_count} communities included and {result.scenario_construction.excluded_municipality_count} excluded.</dd></>}</dl></details>
        <details><summary>Limitations</summary><ul><li>Community points are analytical representatives of EJ census-block-group geography—not municipal boundaries, venues, or patient locations.</li><li>Recruiting-location points may be approximate or city-level and are not verified building entrances.</li><li>Distances and event reach are straight-line estimates, not roads, transit, or travel time.</li><li>Population values represent source-covered EJ areas, not patient counts, attendance, eligibility, enrollment, or disease prevalence.</li><li>Suggested outreach areas require local validation and community partnership before use.</li><li>The model cannot guarantee equitable recruitment or clinical access.</li>{result?.limitations.map((item) => <li key={item}>{item}</li>)}</ul></details>
      </section>
    </main>
  </div>;
}
