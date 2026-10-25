"use client";

import {useCallback, useEffect, useMemo, useRef, useState} from "react";

import {SiteHeader} from "../site-header";
import ResearcherMap from "./researcher-map";
import styles from "./researcher.module.css";
import type {BaselineResult, ConstraintSatisfaction, GeoCollection, GeoFeature, OptimizeResponse, SelectedLocation} from "./types";
import {
  DEFAULT_RADIUS_MILES,
  MAX_RADIUS_MILES,
  accessQuery,
  clampRadiusMiles,
  clinicalFilterCounts,
  displayedPlanIsOutdated,
  formatMiles,
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

function apiBase() {
  return (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/+$/, "");
}

function apiUrl(path: string) {
  return `${apiBase()}${path}`;
}

async function errorMessage(response: Response) {
  try {
    const body = await response.json() as {detail?: string | {message?: string} | Array<{msg?: string}>; error?: {message?: string}};
    if (typeof body.detail === "string") return body.detail;
    if (body.detail && !Array.isArray(body.detail) && body.detail.message) return body.detail.message;
    if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg).filter(Boolean).join("; ");
    if (body.error?.message) return body.error.message;
  } catch {
    // Fall back to the HTTP status when an upstream response is not JSON.
  }
  return `Request failed (${response.status} ${response.statusText || "Unknown error"}).`;
}

function formatDate(value?: string | null) {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Unavailable" : date.toLocaleString("en-US", {dateStyle: "medium", timeStyle: "short"});
}

function nullableInteger(value: number | null | undefined) {
  return value === null || value === undefined ? "Unavailable" : integer.format(value);
}

function nullablePercent(value: number | null | undefined) {
  return value === null || value === undefined ? "Unavailable" : `${decimal.format(value)}%`;
}

function Metric({label, value, help}: {label: string; value: string; help?: string}) {
  return <div className={styles.metric} aria-label={help ? `${label}: ${value}. ${help}` : `${label}: ${value}`}><span>{label}</span><strong>{value}</strong>{help && <small>{help}</small>}</div>;
}

function ConstraintLabel({constraints}: {constraints: ConstraintSatisfaction}) {
  return <span className={constraints.all_satisfied ? styles.ok : styles.bad}>{constraints.all_satisfied ? "Checks passed" : "Check failed"}</span>;
}

function strategyLabel(strategy: BaselineResult["strategy"]) {
  return strategy === "population_first_greedy" ? "Population-first plan" : "Equity-first plan";
}

function strategyDescription(name: string, status?: OptimizeResponse["status"]) {
  if (name === "Optimized plan") return status === "optimal"
    ? "The solver reports a proven best solution under this model and these limits."
    : "The mathematical solver searched the full model; see its reported status below.";
  if (name === "Population-first plan") return "A simpler method that repeatedly chooses areas for additional modeled population reach.";
  return "A simpler method that repeatedly chooses areas for additional equity-prioritized reach.";
}

function improvementLabel(optimized: number, baseline: number) {
  const comparison = scoreDifference(optimized, baseline);
  if (comparison.tied) return "Tie — the returned scores are the same.";
  if (comparison.percentage === null) return `${decimal.format(comparison.difference)} score points higher; percentage unavailable because the comparison score is zero.`;
  const direction = comparison.difference > 0 ? "higher" : "lower";
  const magnitude = Math.abs(comparison.percentage);
  const qualifier = magnitude < 1 ? "a small difference: " : "";
  return `${qualifier}${decimal.format(Math.abs(comparison.difference))} points (${percentMagnitude.format(magnitude)}%) ${direction}.`;
}

function accessValue(feature: GeoFeature) {
  const nearby = feature.properties.distinct_nearby_recruiting_nct_count;
  if (nearby === null) return "Unavailable — access was not calculated";
  if (nearby === 0) return "0 — no matching studies within the chosen distance";
  return integer.format(nearby);
}

function Recommendation({location, displayIndex, reachMiles, onInspect}: {location: SelectedLocation; displayIndex: number; reachMiles: number; onInspect: () => void}) {
  const explanation = location.explanation;
  return <li className={styles.recommendation} id={`recommendation-${encodeURIComponent(location.municipality_id)}`}>
    <details>
      <summary>
        <span className={styles.displayIndex} aria-label={`Display item ${displayIndex}`}>{displayIndex}</span>
        <span className={styles.recommendationName}>{location.municipality_id}<small>Suggested outreach area · not a booked venue</small></span>
        <span><b>{integer.format(explanation.raw_catchment_community_count)}</b><small>communities in reach</small></span>
        <span><b>{integer.format(explanation.raw_catchment_ej_subset_population_proxy)}</b><small>represented people in reach</small></span>
        <span><b>{decimal.format(explanation.leave_one_out_objective_loss)}</b><small>fixed-plan score contribution</small></span>
      </summary>
      <div className={styles.recommendationDetails}>
        <p>This area helps the jointly selected plan by reaching the source-backed catchment below. Its usefulness can overlap with other selected areas, so the visible list order is not a priority ranking.</p>
        <dl>
          <dt>Modeled reach</dt><dd>{formatMiles(reachMiles)} straight-line</dd>
          <dt>All communities in reach</dt><dd>{integer.format(explanation.raw_catchment_community_count)}</dd>
          <dt>Communities only this selection covers</dt><dd>{integer.format(explanation.uniquely_covered_community_count)}</dd>
          <dt>Represented people only this selection covers</dt><dd>{integer.format(explanation.uniquely_covered_ej_subset_population_proxy)}</dd>
          <dt>Area's represented population</dt><dd>{integer.format(explanation.selected_municipality_ej_subset_population)}</dd>
          <dt>Documented minority share</dt><dd>{nullablePercent(explanation.selected_municipality_percent_minority_ej_subset)}</dd>
          <dt>Documented limited-English households</dt><dd>{nullablePercent(explanation.selected_municipality_limited_english_household_percent_ej_subset)}</dd>
        </dl>
        <p className={styles.caution}>Removing this area while holding every other selection fixed would reduce the current score by {decimal.format(explanation.leave_one_out_objective_loss)}. This is not causal impact and does not re-run the model.</p>
        <button type="button" className={styles.secondaryButton} onClick={onInspect}>Inspect this area on the map details</button>
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
  const geoRequestRef = useRef(0);
  const optimizeAbortRef = useRef<AbortController | null>(null);
  const optimizeRequestRef = useRef(0);

  const currentPlan = useMemo<PlanningInputs>(() => ({maxEvents, radiusMiles, equityWeight}), [equityWeight, maxEvents, radiusMiles]);
  const stale = Boolean(result) && displayedPlanIsOutdated(currentPlan, submittedPlan, refreshFailed);
  const planUsable = result ? isUsablePlanStatus(result.status) : false;
  const accessChanged = condition !== appliedCondition || accessRadiusMiles !== appliedAccessRadiusMiles;

  const loadGeo = useCallback(async (nextCondition: string, nextRadiusMiles: number) => {
    geoAbortRef.current?.abort();
    const controller = new AbortController();
    const requestId = ++geoRequestRef.current;
    geoAbortRef.current = controller;
    setGeoLoading(true);
    setGeoError(null);
    try {
      const response = await fetch(`${apiUrl("/api/geo")}?${accessQuery(nextCondition, nextRadiusMiles).toString()}`, {signal: controller.signal, headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(await errorMessage(response));
      const nextGeo = await response.json() as GeoCollection;
      if (requestId !== geoRequestRef.current) return;
      setGeo(nextGeo);
      if (!nextCondition) setPlanningGeo(nextGeo);
      setAppliedCondition(nextCondition);
      setAppliedAccessRadiusMiles(nextRadiusMiles);
      setActiveFeature(null);
    } catch (error) {
      if (controller.signal.aborted || requestId !== geoRequestRef.current) return;
      setGeoError(error instanceof Error ? error.message : "Unable to load municipality and recruiting-study data.");
    } finally {
      if (requestId === geoRequestRef.current) setGeoLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadGeo("", DEFAULT_RADIUS_MILES);
    return () => {
      geoAbortRef.current?.abort();
      optimizeAbortRef.current?.abort();
    };
  }, [loadGeo]);

  const runOptimization = async () => {
    optimizeAbortRef.current?.abort();
    const controller = new AbortController();
    const requestId = ++optimizeRequestRef.current;
    const inputs = {...currentPlan};
    optimizeAbortRef.current = controller;
    setOptimizing(true);
    setOptimizeError(null);
    setMapPlan(null);
    try {
      const response = await fetch(apiUrl("/api/optimize"), {
        method: "POST",
        signal: controller.signal,
        headers: {"Content-Type": "application/json", Accept: "application/json"},
        body: JSON.stringify(optimizationPayload(inputs)),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const nextResult = await response.json() as OptimizeResponse;
      if (requestId !== optimizeRequestRef.current) return;
      setResult(nextResult);
      setSubmittedPlan(inputs);
      setMapPlan({result: nextResult, inputs});
      setRefreshFailed(false);
      setActiveFeature(null);
    } catch (error) {
      if (controller.signal.aborted || requestId !== optimizeRequestRef.current) return;
      setRefreshFailed(Boolean(result));
      setOptimizeError(error instanceof Error ? error.message : "The outreach planning request failed.");
    } finally {
      if (requestId === optimizeRequestRef.current) setOptimizing(false);
    }
  };

  const selectMode = (nextMode: WorkspaceMode) => {
    setMode(nextMode);
    setActiveFeature(null);
  };
  const mapGeo = mode === "planning" ? planningGeo ?? geo : geo;
  const mapSelectedLocations = mode === "planning" ? mapPlan?.result.selected_locations ?? [] : [];
  const mapCoveredIds = useMemo(() => new Set(mode === "planning" ? mapPlan?.result.covered_municipality_ids ?? [] : []), [mapPlan, mode]);
  const comparison = result ? [
    {name: "Optimized plan", events: result.total_events, covered: result.covered_municipality_count, population: result.modeled_ej_subset_population_proxy_covered, objective: result.modeled_outreach_coverage_objective, constraints: result.constraint_satisfaction, selected: result.selected_locations.map((location) => location.municipality_id)},
    ...result.baselines.map((baseline) => ({name: strategyLabel(baseline.strategy), events: baseline.total_events, covered: baseline.covered_community_ids.length, population: baseline.modeled_ej_subset_population_proxy_covered, objective: baseline.modeled_outreach_coverage_objective, constraints: baseline.constraint_satisfaction, selected: baseline.selected_municipality_ids})),
  ] : [];
  const conditionLabel = appliedCondition || "All exported recruiting studies";
  const counts = clinicalFilterCounts(geo?.summary);
  const resolvedSites = geo?.recruiting_site_features.filter((feature) => feature.geometry).length ?? 0;
  const activeSelection = mapPlan?.result.selected_locations.find((location) => location.municipality_id === activeFeature?.id);

  return <div className={styles.shell}>
    <SiteHeader active="researcher" />

    <main className={styles.main}>
      <section className={styles.intro} aria-labelledby="workspace-title">
        <div><span className={styles.eyebrow}>Researcher Workspace</span><h1 id="workspace-title">Understand study access. Plan community outreach.</h1><p>Use two separate tools to explore the recruiting-study export and plan condition-independent outreach.</p></div>
        <div className={styles.freshness}>{geo ? <><strong>{integer.format(geo.summary.observed_feature_count)} represented municipalities</strong><br />MassGIS source retrieved {formatDate(geo.summary.retrieved_at)}</> : "Loading source summary…"}</div>
      </section>

      <nav className={styles.modeTabs} aria-label="Researcher workflows">
        <button type="button" aria-pressed={mode === "access"} className={mode === "access" ? styles.modeActive : ""} onClick={() => selectMode("access")}><span>1</span><b>Explore clinical trial access</b><small>Where are recruiting studies, and which communities may be farther away?</small></button>
        <button type="button" aria-pressed={mode === "planning"} className={mode === "planning" ? styles.modeActive : ""} onClick={() => selectMode("planning")}><span>2</span><b>Plan equitable community outreach</b><small>Where could limited outreach events reach more environmentally burdened areas?</small></button>
      </nav>

      <details className={styles.scopeStrip}>
        <summary><strong>Data scope:</strong> {geo ? geo.summary.clinical_site_data_available ? `${nullableInteger(geo.summary.clinical_site_source_record_count)} exported recruiting-site records across ${nullableInteger(geo.summary.clinical_site_exported_study_count)} exported studies; not every Massachusetts trial.` : "The clinical-site export is unavailable." : "Loading clinical export scope…"} <span>Read source limits</span></summary>
        <p>Current API counts remain visible below and may change with a condition filter. Site points can be approximate or city-level; straight-line distance is not drive time. Municipality points represent EJ census-block-group geography. Population estimates are neither patient counts nor expected enrollment.</p>
      </details>

      <div className={styles.workspace} id="workflow-panel">
        <aside className={styles.panel} aria-labelledby="controls-heading">
          {mode === "access" ? <>
            <div className={styles.panelHead}><div><span className={styles.eyebrow}>Workflow 1</span><h2 id="controls-heading">Explore clinical trial access</h2></div></div>
            <p className={styles.question}>Where are recruiting studies for this condition, and which represented communities may be farther from the locations we can identify?</p>
            <label className={styles.field}><span className={styles.fieldLabel}>Condition in the export</span><select value={condition} disabled={geoLoading || !geo?.summary.clinical_site_data_available} onChange={(event) => setCondition(event.target.value)}><option value="">All exported recruiting studies</option>{geo?.available_conditions?.map((item) => <option value={item} key={item}>{item}</option>)}</select><small className={styles.hint}>Choose an exact condition label from the exported cohort. Similar medical terms are not automatically grouped.</small></label>
            <label className={styles.field}><span className={styles.fieldLabel}>What counts as nearby? <output className={styles.value}>{formatMiles(accessRadiusMiles)}</output></span><span className={styles.dual}><input aria-label="Nearby study distance in miles" type="range" min={0} max={MAX_RADIUS_MILES} step={0.5} value={accessRadiusMiles} disabled={geoLoading} onChange={(event) => setAccessRadiusMiles(clampRadiusMiles(Number(event.target.value)))} /><input aria-label="Nearby study distance value in miles" type="number" min={0} max={MAX_RADIUS_MILES} step={0.5} value={accessRadiusMiles} disabled={geoLoading} onChange={(event) => setAccessRadiusMiles(clampRadiusMiles(Number(event.target.value)))} /></span><small className={styles.hint}>Changes each community's nearby distinct-study count. Distances are straight-line estimates.</small></label>
            <button className={styles.run} type="button" onClick={() => void loadGeo(condition, accessRadiusMiles)} disabled={geoLoading || !geo?.summary.clinical_site_data_available}>{geoLoading ? "Updating access view…" : "Update access view"}</button>
            {accessChanged && !geoLoading && <p className={styles.changed} role="status"><strong>Filters changed.</strong> Select Update access view to refresh map sites, counts, and distances.</p>}
            {!geo?.summary.clinical_site_data_available && !geoLoading && <p className={styles.changed}>The clinical export is unavailable. Study counts and distances are unavailable—not zero.</p>}
            {geoError && <p className={styles.error} role="alert"><strong>Access data failed to load.</strong><br />{geoError}</p>}
            {geo && <div className={styles.scopeCard} aria-label="Current clinical export filter counts"><strong>{conditionLabel}</strong><dl><dt>Matching site records</dt><dd>{nullableInteger(counts.siteRecords)}</dd><dt>Distinct studies</dt><dd>{nullableInteger(counts.distinctStudies)}</dd><dt>Mapped / unresolved sites</dt><dd>{nullableInteger(counts.resolved)} / {nullableInteger(counts.unresolved)}</dd><dt>Removed duplicates</dt><dd>{nullableInteger(geo.summary.clinical_site_duplicate_removed_count)}</dd></dl>{geo.summary.clinical_site_data_available && resolvedSites === 0 && <p className={styles.empty}><strong>No matching mapped sites.</strong> This exact filter has no resolved point to show.</p>}</div>}
          </> : <>
            <div className={styles.panelHead}><div><span className={styles.eyebrow}>Workflow 2</span><h2 id="controls-heading">Plan community outreach</h2></div></div>
            <p className={styles.question}>If we can organize a limited number of outreach events, which areas could help us reach more people represented in environmentally burdened communities?</p>
            <p className={styles.modeNote}><strong>Separate from the condition filter.</strong> This validated planner does not measure or fix disease-specific access gaps. No selected condition is sent to it.</p>
            <label className={styles.field}><span className={styles.fieldLabel}>Number of outreach events <output className={styles.value}>{maxEvents}</output></span><input disabled={optimizing} aria-label="Number of outreach events" type="number" min={0} max={20} step={1} value={maxEvents} onChange={(event) => setMaxEvents(Math.min(20, Math.max(0, Math.round(Number(event.target.value) || 0))))} /><small className={styles.hint}>Sets the maximum number of areas the model can select. Zero returns an empty plan.</small></label>
            <label className={styles.field}><span className={styles.fieldLabel}>How far each event could reach <output className={styles.value}>{formatMiles(radiusMiles)}</output></span><span className={styles.dual}><input disabled={optimizing} aria-label="Outreach reach in miles" type="range" min={0} max={MAX_RADIUS_MILES} step={0.5} value={radiusMiles} onChange={(event) => setRadiusMiles(clampRadiusMiles(Number(event.target.value)))} /><input disabled={optimizing} aria-label="Outreach reach value in miles" type="number" min={0} max={MAX_RADIUS_MILES} step={0.5} value={radiusMiles} onChange={(event) => setRadiusMiles(clampRadiusMiles(Number(event.target.value)))} /></span><small className={styles.hint}>Sets the same straight-line reach for every event—not travel time or a service area.</small></label>
            <label className={styles.field}><span className={styles.fieldLabel}>Priority for underserved communities <output className={styles.value}>{Math.round(equityWeight * 100)}%</output></span><input disabled={optimizing} aria-label="Priority for underserved communities" type="range" min={0} max={1} step={0.05} value={equityWeight} onChange={(event) => setEquityWeight(Number(event.target.value))} /><small className={styles.hint}>At 0%, the score emphasizes represented population. Higher values add more weight to the documented limited-English-household indicator.</small></label>
            <button className={styles.run} type="button" onClick={() => void runOptimization()} disabled={optimizing || geoLoading}>{optimizing ? "Updating outreach plan…" : result ? "Update outreach plan" : "Create outreach plan"}</button>
            {(stale || optimizing) && <p className={styles.changed} role="status"><strong>{optimizing ? "A fresh plan is being calculated." : "Settings changed—this result is out of date."}</strong> {optimizing ? "Old map overlays are cleared so they cannot be mistaken for the new plan." : "Select Update outreach plan before using the current recommendations."}</p>}
            {optimizeError && <p className={styles.error} role="alert"><strong>Outreach plan failed.</strong><br />{optimizeError}</p>}
          </>}
        </aside>

        <section className={styles.mapCard} aria-labelledby="map-heading">
          <div className={styles.mapTitle}><strong id="map-heading">{mode === "access" ? conditionLabel : "Condition-independent outreach plan"}</strong><span>{mode === "access" ? `Access within ${formatMiles(appliedAccessRadiusMiles)} · exact exported condition` : mapPlan ? `${formatMiles(mapPlan.inputs.radiusMiles)} reach · straight-line estimate` : "Create a plan to show selected areas and reach"}</span></div>
          {!geo && geoLoading && <p className={styles.loading} role="status">Loading municipality and recruiting-study geography…</p>}
          {!geo && geoError && <div className={styles.mapError} role="alert"><strong>Map data unavailable.</strong><br />{geoError}<br /><button type="button" onClick={() => void loadGeo(condition, accessRadiusMiles)}>Retry map data</button></div>}
          {mapGeo && <ResearcherMap data={mapGeo} selectedLocations={mapSelectedLocations} coveredIds={mapCoveredIds} submittedRadiusKm={mode === "planning" && mapPlan ? optimizationPayload(mapPlan.inputs).coverage_radius_km : null} visualizationMode={mode} onSelect={setActiveFeature} />}
          {geoLoading && geo && <div className={styles.mapLoading} role="status">Refreshing access data…</div>}
          {geo && <div className={styles.legend} aria-label={`${mode === "access" ? conditionLabel : "Outreach plan"} map legend`}><div className={styles.legendTitle}>{mode === "access" ? "Study access" : "Modeled outreach coverage"}</div>{mode === "access" ? <><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotAccessHigh}`} /> More nearby distinct studies</div><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotAccessZero}`} /> No matching study nearby</div><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotUnavailable}`} /> Access unavailable</div><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotSite}`} /> Recruiting site or cluster</div></> : <><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotLow}`} /> Modeled uncovered community</div><div className={styles.legendRow}><i className={styles.dot} /> Modeled covered community</div><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotSelected}`} /> Selected outreach area</div><div className={styles.legendRow}><i className={styles.legendCatchment} /> Event reach</div><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotSite}`} /> Exported recruiting site</div></>}</div>}
          {activeFeature && <div className={styles.details} role="region" aria-label={`${activeFeature.id} details`}><button type="button" aria-label="Close municipality details" onClick={() => setActiveFeature(null)}>×</button><strong>{activeFeature.id}</strong>{activeSelection && <span className={styles.selectedTag}>Selected outreach area</span>}<dl><dt>Represented population</dt><dd>{nullableInteger(activeFeature.properties.ej_subset_population)}</dd><dt>Documented minority share</dt><dd>{nullablePercent(activeFeature.properties.population_weighted_percent_minority_ej_subset)}</dd><dt>Limited-English households</dt><dd>{nullablePercent(activeFeature.properties.population_weighted_limited_english_household_percent_ej_subset)}</dd><dt>Nearby distinct studies</dt><dd>{accessValue(activeFeature)}</dd><dt>Nearest matching study</dt><dd>{activeFeature.properties.nearest_resolved_recruiting_site_distance_km === null ? (activeFeature.properties.distinct_nearby_recruiting_nct_count === null ? "Unavailable" : "No resolved matching site") : formatMiles(kilometersToMiles(activeFeature.properties.nearest_resolved_recruiting_site_distance_km))}</dd>{activeSelection && <><dt>Communities in event reach</dt><dd>{integer.format(activeSelection.explanation.raw_catchment_community_count)}</dd><dt>Event reach</dt><dd>{mapPlan ? formatMiles(mapPlan.inputs.radiusMiles) : "Unavailable"}</dd></>}</dl></div>}
        </section>
      </div>

      {mode === "access" ? <section className={styles.accessReport} aria-labelledby="scope-heading">
        <div><span className={styles.eyebrow}>Current export</span><h2 id="scope-heading">What this access view includes</h2><p>The filter updates real site records and distinct NCT study counts. One study may have several sites, and one facility may host several studies.</p></div>
        <div className={styles.scopeMetrics}><Metric label="Matching site records" value={nullableInteger(counts.siteRecords)} /><Metric label="Distinct matching studies" value={nullableInteger(counts.distinctStudies)} /><Metric label="Mapped site records" value={nullableInteger(counts.resolved)} /><Metric label="Unresolved site records" value={nullableInteger(counts.unresolved)} /></div>
        <details className={styles.methodology}><summary>Source details and limitations</summary><dl className={styles.wideDl}><dt>Full export</dt><dd>{nullableInteger(geo?.summary.clinical_site_source_record_count)} records / {nullableInteger(geo?.summary.clinical_site_exported_study_count)} exported studies</dd><dt>Export cohort</dt><dd>{geo?.summary.clinical_site_export_scope ?? "Unavailable"}</dd><dt>Coordinates</dt><dd>{geo?.summary.clinical_site_coordinate_precision_limitation ?? "Unavailable"}</dd><dt>Generated</dt><dd>{formatDate(geo?.summary.clinical_site_export_generated_at)}</dd></dl></details>
      </section> : <section className={styles.results} aria-label="Outreach planning results" aria-live="polite">
        <article className={styles.card}>
          <div className={styles.statusRow}><div><span className={styles.eyebrow}>{stale || optimizing ? "Previous plan · out of date" : "Current plan"}</span><h2>Outreach plan summary</h2></div>{result && <span className={`${styles.badge} ${planUsable ? "" : styles.badgeWarn}`}>{result.status}</span>}</div>
          {!result && <p className={styles.empty}>Choose settings and create an outreach plan. No locations are preselected.</p>}
          {result && planUsable && <><p className={styles.resultSentence}>With these settings, the model recommends <strong>{integer.format(result.total_events)} outreach {result.total_events === 1 ? "area" : "areas"}</strong> and estimates coverage of <strong>{integer.format(result.covered_municipality_count)} source-represented communities</strong>.</p><div className={styles.metricGrid}><Metric label="Events" value={integer.format(result.total_events)} /><Metric label="Distinct communities covered" value={integer.format(result.covered_municipality_count)} help="Counted once across overlapping event reaches." /><Metric label="Represented population covered" value={integer.format(result.modeled_ej_subset_population_proxy_covered)} help="A source-based planning estimate, not expected enrollment." /><Metric label="Equity-and-reach score" value={decimal.format(result.modeled_outreach_coverage_objective)} /></div><details className={styles.methodology}><summary>Solver and validation details</summary><p>{strategyDescription("Optimized plan", result.status)}</p><p>Solver: {result.solver.name} {result.solver.version}. MIP gap: {result.solver.mip_gap === null ? "Unavailable" : decimal.format(result.solver.mip_gap)}. {result.solver.message}</p><ConstraintLabel constraints={result.constraint_satisfaction} /></details></>}
          {result && !planUsable && <p className={styles.error} role="alert"><strong>No usable outreach plan was returned.</strong><br />The solver reported {result.status}. Review the solver details and try valid settings; no selected areas are presented as recommendations.</p>}
        </article>

        <article className={`${styles.card} ${styles.recommendationsCard}`}><span className={styles.eyebrow}>Joint selection</span><h2>Recommended outreach areas</h2>{!result ? <p className={styles.empty}>Compact recommendations and their source-backed reasons appear after the planner runs.</p> : !planUsable ? <p className={styles.empty}>Recommendations are unavailable because the solver did not return a usable plan.</p> : result.selected_locations.length === 0 ? <p className={styles.empty}>{submittedPlan?.maxEvents === 0 ? "No areas were selected because zero outreach events were requested." : "The usable result selected no outreach areas under these settings."}</p> : <><p className={styles.sectionIntro}>These municipalities are analytical starting areas selected together—not ranked venues. Expand an item to inspect overlap, unique coverage, reach, and uncertainty.</p><ol className={styles.recommendationList}>{result.selected_locations.map((location, index) => <Recommendation key={location.municipality_id} location={location} displayIndex={index + 1} reachMiles={submittedPlan?.radiusMiles ?? radiusMiles} onInspect={() => {const feature = planningGeo?.features.find((item) => item.id === location.municipality_id) ?? null; setActiveFeature(feature);}} />)}</ol></>}
        </article>

        <article className={`${styles.card} ${styles.compareCard}`}><span className={styles.eyebrow}>Same settings, same score</span><h2>Compare three planning approaches</h2>{!result ? <p className={styles.empty}>The optimized, population-first, and equity-first plans will be evaluated under the same objective and limits.</p> : !planUsable ? <p className={styles.empty}>A fair comparison is unavailable because the solver did not return a usable optimized plan.</p> : <><div className={styles.tableWrap}><table className={styles.compareTable}><caption className={styles.srOnly}>Planning approach comparison</caption><thead><tr><th>Approach</th><th>Events</th><th>Selected areas</th><th>Distinct communities</th><th>Represented population</th><th>Equity-and-reach score</th></tr></thead><tbody>{comparison.map((row) => <tr key={row.name}><th scope="row"><strong>{row.name}</strong><small>{strategyDescription(row.name, result.status)}</small><ConstraintLabel constraints={row.constraints} /></th><td>{integer.format(row.events)}</td><td><details><summary>{integer.format(row.selected.length)} areas</summary>{row.selected.length ? row.selected.join(", ") : "None"}</details></td><td>{integer.format(row.covered)}</td><td>{integer.format(row.population)}</td><td><strong>{decimal.format(row.objective)}</strong>{row.name !== "Optimized plan" && <small>{improvementLabel(result.modeled_outreach_coverage_objective, row.objective)}</small>}</td></tr>)}</tbody></table></div><details className={styles.methodology}><summary>How to interpret this comparison</summary><p>Both simpler approaches make one choice at a time; the optimized method considers combinations. All three use the same returned score and constraints. Small differences and ties are reported without rescaling. These values estimate modeled reach—not contacts, enrollment, or clinical outcomes.</p></details></>}
        </article>
      </section>}

      <details className={styles.guardrails}><summary>Important methodology and interpretation limits</summary><ul><li>Municipality points are analytical representatives of EJ census-block-group geography, not boundaries, venues, or patient locations.</li><li>Study-site points may be approximate or city-level, not verified building entrances.</li><li>All visible distances are straight-line miles, not roads, transit, or travel time.</li><li>Population estimates describe source-represented EJ areas, not patients or expected trial enrollment.</li><li>The condition-specific access view and condition-independent outreach planner answer separate questions.</li><li>Model optimality is conditional on the documented assumptions and input data.</li>{result?.limitations.map((item) => <li key={item}>{item}</li>)}</ul></details>

      <section className={styles.provenance} aria-labelledby="provenance-heading"><div><h2 id="provenance-heading">Sources and methodology</h2><p>Official persisted MassGIS input supports the community geography. The limited clinical export supports the study-site view.</p></div><dl><dt>Municipality source</dt><dd>{geo ? <a href={geo.summary.source_url} target="_blank" rel="noreferrer">{geo.summary.source_name}</a> : "Unavailable"}</dd><dt>Observed / resolved points</dt><dd>{geo ? `${geo.summary.observed_feature_count} / ${geo.summary.resolved_feature_count}` : "Unavailable"}</dd><dt>Clinical export</dt><dd>{geo?.summary.clinical_site_data_available ? `${nullableInteger(geo.summary.clinical_site_source_record_count)} records / ${nullableInteger(geo.summary.clinical_site_exported_study_count)} exported studies` : "Unavailable"}</dd><dt>Optimization generated</dt><dd>{result ? formatDate(result.generated_at) : "Not run"}</dd><dt>Optimization scope</dt><dd>Condition-independent; the condition filter is never included in this request.</dd></dl></section>
    </main>
  </div>;
}
