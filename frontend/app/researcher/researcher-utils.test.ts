import {describe, expect, it} from "vitest";

import type {GeoSummary} from "./types";
import {
  DEFAULT_RADIUS_MILES,
  KILOMETERS_PER_MILE,
  MAX_RADIUS_KM,
  MAX_RADIUS_MILES,
  accessQuery,
  clinicalFilterCounts,
  displayedPlanIsOutdated,
  formatMiles,
  isPlanStale,
  isUsablePlanStatus,
  kilometersToMiles,
  milesToKilometers,
  optimizationPayload,
  scoreDifference,
} from "./researcher-utils";

describe("researcher distance units", () => {
  it("converts zero, fractional, and boundary values with the exact factor", () => {
    expect(milesToKilometers(0)).toBe(0);
    expect(milesToKilometers(0.5)).toBe(KILOMETERS_PER_MILE / 2);
    expect(milesToKilometers(MAX_RADIUS_MILES)).toBeCloseTo(MAX_RADIUS_KM, 12);
    expect(kilometersToMiles(25)).toBe(DEFAULT_RADIUS_MILES);
  });

  it("preserves round trips across repeated UI changes", () => {
    let kilometers = 73.421;
    for (let change = 0; change < 100; change += 1) {
      kilometers = milesToKilometers(kilometersToMiles(kilometers));
    }
    expect(kilometers).toBeCloseTo(73.421, 12);
  });

  it("converts once at each API boundary and formats only for display", () => {
    const radiusMiles = kilometersToMiles(25);
    const query = accessQuery("Lung Cancer", radiusMiles);
    const payload = optimizationPayload({maxEvents: 4, radiusMiles, equityWeight: 0.65});

    expect(Number(query.get("access_radius_km"))).toBeCloseTo(25, 12);
    expect(query.get("condition")).toBe("Lung Cancer");
    expect(payload.coverage_radius_km).toBeCloseTo(25, 12);
    expect(formatMiles(kilometersToMiles(payload.coverage_radius_km))).toBe("15.5 mi");
  });
});

describe("researcher planning state", () => {
  const submitted = {maxEvents: 5, radiusMiles: DEFAULT_RADIUS_MILES, equityWeight: 0.5};

  it("marks every planning input change stale without cumulative conversion", () => {
    expect(isPlanStale(submitted, submitted)).toBe(false);
    expect(isPlanStale({...submitted, maxEvents: 6}, submitted)).toBe(true);
    expect(isPlanStale({...submitted, radiusMiles: submitted.radiusMiles + 0.5}, submitted)).toBe(true);
    expect(isPlanStale({...submitted, equityWeight: 0.55}, submitted)).toBe(true);
    expect(isPlanStale(submitted, null)).toBe(false);
  });

  it("keeps an older result outdated after a failed same-input refresh", () => {
    expect(displayedPlanIsOutdated(submitted, submitted, false)).toBe(false);
    expect(displayedPlanIsOutdated(submitted, submitted, true)).toBe(true);
    expect(displayedPlanIsOutdated({...submitted, maxEvents: 6}, submitted, false)).toBe(true);
  });

  it("only treats successful solver outcomes as usable plans", () => {
    expect(isUsablePlanStatus("optimal")).toBe(true);
    expect(isUsablePlanStatus("feasible")).toBe(true);
    expect(isUsablePlanStatus("infeasible")).toBe(false);
    expect(isUsablePlanStatus("unbounded")).toBe(false);
    expect(isUsablePlanStatus("solver_error")).toBe(false);
  });

  it("handles ties, zero baselines, and floating-point noise honestly", () => {
    expect(scoreDifference(10, 10).tied).toBe(true);
    expect(scoreDifference(10, 0).percentage).toBeNull();
    expect(scoreDifference(0.1 + 0.2, 0.3).tied).toBe(true);
  });
});

describe("condition-filter counts", () => {
  it("keeps site records, distinct studies, resolved, and unresolved separate", () => {
    const summary = {
      clinical_site_retained_count: 12,
      clinical_site_distinct_nct_count: 7,
      clinical_site_resolved_count: 10,
      clinical_site_unresolved_count: 2,
    } as GeoSummary;

    expect(clinicalFilterCounts(summary)).toEqual({
      siteRecords: 12,
      distinctStudies: 7,
      resolved: 10,
      unresolved: 2,
    });
    expect(clinicalFilterCounts(null)).toEqual({
      siteRecords: null,
      distinctStudies: null,
      resolved: null,
      unresolved: null,
    });
  });
});
