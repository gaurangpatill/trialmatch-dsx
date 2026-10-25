import {renderToStaticMarkup} from "react-dom/server";
import {describe, expect, it, vi} from "vitest";

import type {SelectedLocation} from "./types";

vi.mock("./researcher-map", () => ({default: () => <div>Map</div>}));

import ResearcherDashboard, {Recommendation} from "./researcher-dashboard";

const location: SelectedLocation = {
  municipality_id: "Springfield",
  latitude: 42.1,
  longitude: -72.6,
  explanation: {
    raw_catchment_community_ids: ["Springfield", "Chicopee", "Holyoke"],
    raw_catchment_community_count: 3,
    raw_catchment_ej_subset_population_proxy: 125000,
    raw_catchment_objective: 0.7,
    uniquely_covered_community_ids: ["Springfield"],
    uniquely_covered_community_count: 1,
    uniquely_covered_ej_subset_population_proxy: 50000,
    uniquely_covered_objective: 0.3,
    leave_one_out_objective_loss: 0.2,
    leave_one_out_interpretation: "fixed-plan removal loss; other selected locations remain fixed; not causal impact or reoptimized marginal value",
    selected_municipality_ej_subset_population: 50000,
    selected_municipality_limited_english_household_percent_ej_subset: 12,
    selected_municipality_percent_minority_ej_subset: 45,
  },
};

describe("researcher workspace presentation", () => {
  it("renders compact, understandable workflow navigation and access controls", () => {
    const html = renderToStaticMarkup(<ResearcherDashboard />);

    expect(html).toContain("Make clinical research more accessible.");
    expect(html).toContain('role="tablist"');
    expect(html).toContain("Find Trial Access Gaps");
    expect(html).toContain("Plan Community Outreach");
    expect(html).toContain("Medical condition");
    expect(html).toContain("Search radius");
    expect(html).toContain("Analyzing trial access");
    expect(html).toContain("About the data");
    expect(html).toContain("Technical details");
    expect(html).not.toContain("condition-independent outreach");
  });

  it("presents recommendation reach and unique coverage without treating order as ranking", () => {
    const html = renderToStaticMarkup(<Recommendation location={location} displayIndex={2} reachMiles={15.5} active={false} onInspect={() => undefined} />);

    expect(html).toContain("Springfield");
    expect(html).toContain("3");
    expect(html).toContain("communities in modeled reach");
    expect(html).toContain("Communities only this area covers");
    expect(html).toContain("50,000");
    expect(html).toContain("not a best-to-worst ranking");
    expect(html).toContain("not a booked venue");
    expect(html).not.toContain("most patients");
  });
});
