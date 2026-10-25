import type {GeoFeature, GeoSummary} from "./types";

export const KILOMETERS_PER_MILE = 1.609344;
export const MAX_RADIUS_KM = 200;
export const MAX_RADIUS_MILES = MAX_RADIUS_KM / KILOMETERS_PER_MILE;
export const DEFAULT_RADIUS_MILES = 25 / KILOMETERS_PER_MILE;

export type PlanningInputs = {
  maxEvents: number;
  radiusMiles: number;
  equityWeight: number;
};

export function milesToKilometers(miles: number) {
  return miles * KILOMETERS_PER_MILE;
}

export function kilometersToMiles(kilometers: number) {
  return kilometers / KILOMETERS_PER_MILE;
}

export function clampRadiusMiles(value: number) {
  if (!Number.isFinite(value)) return 0;
  return Math.min(MAX_RADIUS_MILES, Math.max(0, value));
}

export function formatMiles(value: number) {
  return `${new Intl.NumberFormat("en-US", {maximumFractionDigits: 1}).format(value)} mi`;
}

export function accessQuery(condition: string, radiusMiles: number) {
  const parameters = new URLSearchParams();
  if (condition) parameters.set("condition", condition);
  parameters.set("access_radius_km", String(milesToKilometers(radiusMiles)));
  return parameters;
}

export function optimizationPayload(inputs: PlanningInputs) {
  return {
    max_events: inputs.maxEvents,
    coverage_radius_km: milesToKilometers(inputs.radiusMiles),
    equity_weight: inputs.equityWeight,
  };
}

export function planningInputsEqual(left: PlanningInputs, right: PlanningInputs) {
  return left.maxEvents === right.maxEvents
    && left.radiusMiles === right.radiusMiles
    && left.equityWeight === right.equityWeight;
}

export function isPlanStale(current: PlanningInputs, submitted: PlanningInputs | null) {
  return submitted !== null && !planningInputsEqual(current, submitted);
}

export function displayedPlanIsOutdated(current: PlanningInputs, submitted: PlanningInputs | null, refreshFailed: boolean) {
  return refreshFailed || isPlanStale(current, submitted);
}

export function isUsablePlanStatus(status: string) {
  return status === "optimal" || status === "feasible";
}

export function clinicalFilterCounts(summary: GeoSummary | null | undefined) {
  return {
    siteRecords: summary?.clinical_site_retained_count ?? null,
    distinctStudies: summary?.clinical_site_distinct_nct_count ?? null,
    resolved: summary?.clinical_site_resolved_count ?? null,
    unresolved: summary?.clinical_site_unresolved_count ?? null,
  };
}

export function scoreDifference(optimized: number, baseline: number) {
  const rawDifference = optimized - baseline;
  const tolerance = Number.EPSILON * Math.max(1, Math.abs(optimized), Math.abs(baseline)) * 16;
  const difference = Math.abs(rawDifference) <= tolerance ? 0 : rawDifference;
  return {
    difference,
    percentage: baseline === 0 ? null : (difference / baseline) * 100,
    tied: difference === 0,
  };
}

/** Keeps number controls readable without changing the full-precision API value. */
export function formatRadiusInput(value: number) {
  return Number(value.toFixed(1));
}

/**
 * Returns transparent access-gap candidates: zero nearby studies, with the
 * farthest known nearest recruiting location first. Null distances sort last.
 */
export function accessGapFeatures(features: GeoFeature[]) {
  return features
    .filter((feature) => feature.properties.distinct_nearby_recruiting_nct_count === 0)
    .sort((left, right) => {
      const leftDistance = left.properties.nearest_resolved_recruiting_site_distance_km;
      const rightDistance = right.properties.nearest_resolved_recruiting_site_distance_km;
      if (leftDistance === null && rightDistance === null) return left.id.localeCompare(right.id);
      if (leftDistance === null) return 1;
      if (rightDistance === null) return -1;
      return rightDistance - leftDistance || left.id.localeCompare(right.id);
    });
}
