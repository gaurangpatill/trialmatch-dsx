import {renderToStaticMarkup} from "react-dom/server";
import {describe, expect, it} from "vitest";

import type {TrialDetail, TrialSummary} from "@/lib/clinical-contracts";
import {PatientPortal, StudyDetailPanel, TrialCard, parseSummarySections} from "./patient-portal";

const summary: TrialSummary = {
  nct_id: "NCT01521741",
  title: "Prospective Screening for Breast Cancer-related Lymphedema: Analysis of Objective Measurements, Symptoms, Functionality, and Quality of Life Questionnaires",
  overall_status: "RECRUITING",
  conditions: ["Lymphedema", "Breast Cancer", "Arm Symptoms", "Quality of Life"],
  phases: ["NOT_APPLICABLE"],
  source_description: "The official study measures arm changes and symptoms during and after breast cancer treatment.",
  brief_summary: "The official study measures arm changes and symptoms during and after breast cancer treatment.",
  detailed_description: "Participants complete questionnaires and objective arm measurements during follow-up.",
  plain_language_summary: "What is this study investigating? Researchers are studying arm symptoms and swelling during and after breast cancer treatment. What might participation involve? Participants complete questionnaires and arm measurements. Who is the study intended for? The study is intended for adults with breast cancer. Important things to know. ClinicalTrials.gov lists the study as recruiting.",
  minimum_age: "18 Years",
  maximum_age: null,
  sex: "FEMALE",
  locations: [],
  source_url: "https://clinicaltrials.gov/study/NCT01521741",
  retrieved_at: "2026-10-24T20:47:11Z",
  detail_status: "retrieved",
  screening_coverage_status: "partial",
};

const detail: TrialDetail = {
  ...summary,
  eligibility_criteria: "Inclusion Criteria:\nConfirmed diagnosis of breast cancer\nAge 18 years or older",
  locations: [{
    facility: "Massachusetts General Hospital",
    recruiting_status: "RECRUITING",
    city: "Boston",
    state: "Massachusetts",
    postal_code: "02114",
    country: "United States",
    latitude: 42.35843,
    longitude: -71.05977,
  }],
  contacts: [{
    name: "Clinical Trials Office",
    role: "CENTRAL_CONTACT",
    phone: "617-555-0100",
    email: "trials@example.org",
    facility: "Massachusetts General Hospital",
  }],
  detail_attempted_at: "2026-10-24T20:47:11Z",
  retrieval_source: "official_cache",
  retrieval_message: "Official study detail is available.",
  screening_coverage: {
    status: "partial",
    protocol_item_count: 2,
    accounted_item_count: 1,
    evaluable_criterion_count: 1,
    unsupported_protocol_requirement_count: 0,
    uncovered_protocol_requirement_count: 1,
    independently_verified: false,
    verification_method: null,
    reason: "Some published requirements still need review.",
  },
};

describe("participant study discovery and details", () => {
  it("renders a plain-English card with details access and no screening controls", () => {
    const html = renderToStaticMarkup(
      <TrialCard trial={summary} onOpen={() => undefined} />,
    );

    expect(html).toContain("Plain-English summary");
    expect(html).toContain("Researchers are studying arm symptoms");
    expect(html).toContain("+1 more");
    expect(html).toContain("line-clamp-2");
    expect(html).toContain("Explore details");
    expect(html).not.toContain("+ Select");
    expect(html).not.toContain("✓ Selected");
    expect(html).not.toContain("Requirements ready to check");
    expect(html).not.toContain("Requirements need review");
  });

  it("renders source-faithful study details without screening support", () => {
    const html = renderToStaticMarkup(
      <StudyDetailPanel summary={summary} detail={detail} loading={false} error={null} summaryText={summary.plain_language_summary ?? null} summaryLoading={false} summaryError={null} onClose={() => undefined} onRefresh={() => undefined} onRetrySummary={() => undefined} />,
    );

    expect(html).toContain("AI-generated plain-language explanation");
    expect(html).toContain("What is this study investigating?");
    expect(html).toContain("What might participation involve?");
    expect(html).toContain("Who is the study intended for?");
    expect(html).toContain("Important things to know");
    expect(html).toContain("Read the original ClinicalTrials.gov description");
    expect(html).toContain("AI-generated explanation based on ClinicalTrials.gov study information");
    expect(html).toContain("Verify details with the research team");
    expect(html).toContain("not medical advice or a verified eligibility decision");
    expect(html).toContain("Official brief summary");
    expect(html).toContain("Official detailed description");
    expect(html).toContain("Participants complete questionnaires");
    expect(html).toContain("Eligibility requirements");
    expect(html).toContain("Confirmed diagnosis of breast cancer");
    expect(html).toContain("Interventions");
    expect(html).toContain("Intervention details are not included in the current TrialMatch study-detail data");
    expect(html).toContain("Massachusetts General Hospital");
    expect(html).toContain("Presence in the official record is verified");
    expect(html).toContain("current enrollment, site availability");
    expect(html).toContain("Central Contact");
    expect(html).toContain("Call 617-555-0100");
    expect(html).toContain("Email trials@example.org");
    expect(html).toContain('href="https://clinicaltrials.gov/study/NCT01521741"');
    expect(html).toContain('target="_blank"');
    expect(html).not.toContain("Preliminary screening support");
  });

  it("renders explicit AI loading and error fallbacks without hiding official text", () => {
    const loadingHtml = renderToStaticMarkup(
      <StudyDetailPanel summary={summary} detail={detail} loading={false} error={null} summaryText={null} summaryLoading={true} summaryError={null} onClose={() => undefined} onRefresh={() => undefined} onRetrySummary={() => undefined} />,
    );
    expect(loadingHtml).toContain("Generating and validating the AI explanation");
    expect(loadingHtml).toContain("Official brief summary");

    const errorHtml = renderToStaticMarkup(
      <StudyDetailPanel summary={summary} detail={detail} loading={false} error={null} summaryText={summary.plain_language_summary ?? null} summaryLoading={false} summaryError="AI explanation is temporarily unavailable. You can still read the official study description." onClose={() => undefined} onRefresh={() => undefined} onRetrySummary={() => undefined} />,
    );
    expect(errorHtml).toContain("Try AI summary again");
    expect(errorHtml).toContain("AI explanation is temporarily unavailable");
    expect(errorHtml).toContain("You can still read the official study description");
    expect(errorHtml).toContain("What is this study investigating?");
    expect(errorHtml).toContain("Use the original official description");
    expect(errorHtml).toContain("Official detailed description");
    expect(parseSummarySections("unexpected legacy text")).toBeNull();
  });

  it("omits the fictional example comparison sidebar while keeping discovery", () => {
    const html = renderToStaticMarkup(<PatientPortal />);

    expect(html).toContain("Explore recruiting studies");
    expect(html).toContain("Filter and sort results");
    expect(html).not.toContain("<aside");
    expect(html).not.toContain("Studies selected for review");
    expect(html).not.toContain("Fictional profile and requirement comparison controls");
    expect(html).not.toContain("Select a study first");
    expect(html).not.toContain("Preliminary research screening only");
    expect(html).toContain("Research information only");
  });
});
