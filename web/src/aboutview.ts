// Honest release presentation (11-CONTEXT D-06, D-08; 11-UI-SPEC R-9). Pure, erasable syntax for node --test.
import type { AboutPayload } from "./contract.ts";
import type { IconName, Tone } from "./ui.tsx";
import { markDate } from "./format.ts";
import { PROVIDER_NAMES } from "./vocab.ts";

export const WEEKCHECK_TEXT = [
  "Synthetic retail scenarios with a weekday profile derived from FreshRetailNet-50K and explicitly stated demand, ordering and markdown assumptions.",
  "In the synthetic 5-seed engineering check, delivery sizing from in-stock days only is biased low: when the order is 90–100% of the estimate, the in-stock days are the lower-demand days, which produces the slow rise in stockouts shown in the synthetic check.",
  "In public FreshRetailNet-50K histories, sales during stockout hours are censored and cannot by themselves establish uncensored demand; stockout-day imputation for forecast training is an explicit modeling assumption.",
] as const;
export const BENCHMARK_SCENARIOS = [
  "Modeled store rule with original records", "Modeled store rule with equivalent stock checks",
  "ShelfShift decisions with those checks", "Automatic markdowns only · no transfers or additional intervention",
] as const;
export const PUBLISHED = "Published";
export const DIFFERENT_VERSION = "Different version · not representative of the current app";
export const RUN_NOT_PUBLISHED = "Run recorded · not published";
export const IN_CODE = "In code · not run yet";
export const NOT_AVAILABLE = "Evaluation not yet available";
export const AI_NOT_PUBLISHED = "AI evaluation not yet published.";
export const NOT_IN_RELEASE_CHIP = "Not part of this release";

export type VersionRow = { key: string; label: string; value: string; note?: string };
export function versionRows(about: AboutPayload): VersionRow[] {
  const fc = about.forecast;
  return [
    { key: "commit", label: "Commit", value: about.git_commit && about.git_commit !== "dev" ? about.git_commit.slice(0, 12) : "Not recorded" },
    { key: "engine", label: "Engine", value: about.engine_version },
    { key: "dataset", label: "Dataset", value: about.dataset_version === "unseeded" ? "Not installed yet" : about.dataset_version },
    { key: "assistant", label: "Assistant", value: about.assistant_version ?? NOT_IN_RELEASE_CHIP },
    { key: "model", label: "Gemini model", value: about.gemini_model || "—",
      ...(about.gemini_model && about.assistant_version === null ? { note: "configured, not used in this release" } : {}) },
    { key: "location", label: "Gemini location", value: about.vertex_location || "—" },
    { key: "forecast", label: "Forecasts", value: fc === null ? "No forecast run installed" : fc.headline + "\n" +
      Object.entries(fc.providers).map(([code, n]) => `${PROVIDER_NAMES[code] ?? code} · ${n} series-runs`).join("\n") },
    { key: "run", label: "Forecast run", value: fc === null ? "—" : `${fc.run_id} · history to ${markDate(fc.cutoff_date)}` },
  ];
}

export type CheckRow = { key: "benchmark" | "ai-eval" | "weekcheck"; label: string; measures: string;
  status: string; tone: Tone; icon: IconName; details: string; runLine?: string };
export function checkRows(about: AboutPayload): CheckRow[] {
  return [
    { key: "benchmark", label: "Public-week benchmark", measures: "Three ways of deciding on the same public week, with a waste reference",
      status: about.benchmark ? (about.benchmark.version_match ? PUBLISHED : DIFFERENT_VERSION) : NOT_AVAILABLE,
      tone: about.benchmark ? (about.benchmark.version_match ? "good" : "warn") : "gray",
      icon: about.benchmark ? (about.benchmark.version_match ? "check" : "alert") : "circle", details: "benchmark",
      ...(about.benchmark ? {runLine: publishedRunLine(about.benchmark, about.benchmark.dataset_version)} : {}) },
    { key: "ai-eval", label: "AI evaluation", measures: "The Gemini assistant and report reading on fixed scenarios",
      status: about.ai_eval ? (about.ai_eval.version_match ? PUBLISHED : DIFFERENT_VERSION) : AI_NOT_PUBLISHED,
      tone: about.ai_eval ? (about.ai_eval.version_match ? "good" : "warn") : "gray",
      icon: about.ai_eval ? (about.ai_eval.version_match ? "check" : "alert") : "circle", details: "ai-evaluation" },
    { key: "weekcheck", label: "Synthetic 5-seed engineering check", measures: "The engine on five generated scenarios",
      status: about.weekcheck_status === "in_code" ? IN_CODE : NOT_IN_RELEASE_CHIP,
      tone: "gray", icon: about.weekcheck_status === "in_code" ? "circle" : "x", details: "weekcheck" },
  ];
}

import { NOT_IN_RELEASE, isShipped } from "./release.ts";
export function benchmarkInRelease(about: AboutPayload | null): boolean {
  return isShipped("reports") || about?.benchmark_status === "published";
}
export function realNowLines(about: AboutPayload | null, error: boolean): string[] {
  const current = error ? null : about;
  const lines = [current ? `The deterministic engine that scores every option (${current.engine_version}).`
    : "The deterministic engine that scores every option."];
  if (current?.forecast) lines.push(`Demand forecasts precomputed through Google Cloud BigQuery ML: ${current.forecast.headline}.`);
  if (current?.google_cloud) lines.push("Cloud Run serves the app, Cloud SQL for PostgreSQL keeps each workspace, and Cloud Storage holds the release files" +
    (isShipped("verification") ? " and uploaded evidence." : "."));
  const verbs = [isShipped("assistant") ? "writes decision briefs · answers questions" : null,
    isShipped("reading") ? "reads field reports" : null].filter(Boolean);
  if (current?.assistant_version && verbs.length) lines.push(`Gemini on Google Cloud (${current.gemini_model}, ${current.vertex_location}) ${verbs.join(" · ")}.`);
  lines.push("Each stock decision's version line names the forecast run behind it and its provider.");
  return lines;
}
export const notInReleaseLines = (): string[] => NOT_IN_RELEASE.map(surface => `${surface.name} is not part of this release.`);

export const TRAINING_INPUTS_TEXT = "Up to each run's cutoff, public sale_amount is scaled by the assumed units factor. Full-price in-stock days are kept as recorded. Discounted in-stock days are converted to estimated full-price units using the store's assumed markdown response; days with an unknown price stay at base price. Stockout days (any hour out of stock between 06:00 and 22:00) are imputed from the same weekday's in-stock days, never below the day's own converted units.";
export const SALES_REPLAY_TEXT = "The evaluation uses recorded units sold as a demand proxy. It does not estimate unmet demand during stockouts or remove historical promotion effects.";
export const ONLINE_GROCERY_CAVEAT = "This dataset comes from online grocery. Its stockout patterns may differ from those in other retail settings.";
export const PROMOTION_CAVEAT = "The extra sales a markdown brings are an assumption: the dataset's discounts are promotions, not markdowns for stock near expiry.";
export const PILOT_LINES = [
  "Reviewers each get their own workspace in their own browser, with shared prototype accounts per store and for HQ. A real pilot needs a personal sign-in for every user and one shared organization workspace across devices, with access limited to that organization.",
  "The prototype's reference data can be rebuilt from release files, but that does not bring back counts, approvals, uploaded evidence or activity recorded here. A real pilot needs automatic database backups, point-in-time recovery, a tested restore and agreed recovery targets.",
  "The prototype runs on one small server and one small database. What-if runs started at the same time wait their turn and can take a few seconds.",
];
export const ON_THIS_PAGE = [
  ["versions", "Versions"], ["checks", "Checks and evaluations"], ["ai-evaluation", "AI evaluation"],
  ["weekcheck", "Synthetic 5-seed engineering check"], ["real-now", "Real now"], ["simulated", "Simulated"],
  ["not-yet-proven", "Not yet proven"], ["labels", "Labels"], ["dataset", "The dataset"],
] as const;
export function notYetProven(about: AboutPayload | null, error: boolean): {title:string; lines:string[]}[] {
  const current = error ? null : about;
  const data = ["There is no real retailer data.", "Stock, lots and deliveries are simulated from the public sales.",
    "The markdown response and the cost of a trip that ships nothing are assumptions.",
    "Costs, shelf life, markdown recovery, holding and transfer costs are assumed parameters.", ONLINE_GROCERY_CAVEAT, PROMOTION_CAVEAT];
  if (current?.error_model) {
    const {share, range} = current.error_model;
    data.push(`Stock records follow a declared error model: about ${Number((share * 100).toFixed(2))}% of lots can hold between ${range[0]} and ${range[1]} of the recorded units. It is an assumption, not an error measured at a store.`);
  }
  data.push("The forecast does not learn from what happens in the app.");
  const decisions = ["Execution is simulated.", "There is no live point-of-sale, ordering or supplier data feed and no store trial."];
  if (benchmarkInRelease(current)) decisions.push("The modeled store rule ignores inbound deliveries when estimating surplus. Differences reflect this limitation as well as ShelfShift's planning.",
    "Waste reductions are compared only with automatic markdowns and no other action, never claimed lower than the modeled store rule.");
  if (isShipped("verification")) decisions.push("Each what-if tests one operational fact at a time while holding the other facts fixed.",
    "A lot with mixed expiry dates is held for staff to separate; the app never splits it into quantities. A typed check time stops silent misdating but cannot prove the time is accurate.");
  return [{title:"Data and assumptions", lines:data}, {title:"Decisions and checks", lines:decisions}, {title:"Running it for a real customer", lines:[...PILOT_LINES]}];
}
export type LabelRow = {name:string; meaning:string; tone:Tone | null; icon?:IconName};
export function labelRows(shipped: string[], about: AboutPayload | null = null): LabelRow[] {
  const providers = about?.forecast ? Object.keys(about.forecast.providers).map(code => PROVIDER_NAMES[code] ?? code) : [];
  const rows: LabelRow[] = [
    {name:"Calculated by the engine", tone:"gray", meaning:"A figure the deterministic engine computed for this version of the stock decision."},
    {name:"Template summary · from the engine's figures", tone:"gray", meaning:"The stock decision summary: a fixed template filled in with the engine's figures."},
    {name:"Forecast · precomputed", tone:"plan", icon:"trend", meaning:"Demand forecasts computed in advance, each with its run and cutoff." + (providers.length ? ` Each row names its provider: ${providers.join(" or ")}.` : "")},
    {name:"Simulated", tone:"plan", icon:"info", meaning:"Built for this scenario from the public sales. Never observed."},
    {name:"Estimated", tone:"plan", icon:"info", meaning:"Calculated from simulated stock and the forecast, such as potential waste."},
    {name:"Assumption", tone:"gray", icon:"tag", meaning:"A value the scenario assumes, with its source."},
    {name:"Public sales dataset", tone:null, meaning:"Public sales from FreshRetailNet-50K with simulated stock and labelled assumptions. Nothing on screen was observed at a retailer's store."},
  ];
  if (shipped.includes("assistant") || shipped.includes("reading")) rows.push(
    {name:"Precomputed · Gemini model ID", tone:"gray", icon:"clock", meaning:"Written by Gemini before you opened the page, with the model that wrote it."},
    {name:"Live · Gemini model ID", tone:"gray", meaning:"Written by Gemini in your workspace after your question or change."},
    {name:"Fallback · deterministic engine summary", tone:"gray", icon:"info", meaning:"Gemini was unavailable, so the summary uses the deterministic engine's figures."},
  );
  if (about?.benchmark || about?.ai_eval) rows.push({name:DIFFERENT_VERSION, tone:"warn", meaning:"A published result from a different engine, dataset, model or assistant version."});
  return rows;
}

export function publishedRunLine(row: {published_at:string; git_commit:string}, dataset: string): string {
  return `run ${markDate(row.published_at.slice(0,10)).replace(/^\w+ /, "")} · commit ${row.git_commit.slice(0,12)} · dataset ${dataset}`;
}
export function benchmarkText(about: AboutPayload): string {
  const b=about.benchmark;
  if (!b) return NOT_AVAILABLE;
  return ["Simulated evaluation", `${markDate(b.first_day)}–${markDate(b.last_day)}`,
    b.stores === null ? null : `${b.stores} stores`, b.series === null ? null : `${b.series} series`].filter(Boolean).join(" · ") + ".";
}

export function aiEvalNotice(about: AboutPayload | null): string | null {
  const n=about?.latest_unpublished;
  return n ? `The latest evaluation run (${markDate(n.run_date.slice(0,10)).replace(/^\w+ /, "")}) is not published: ${n.reason}${/[.!?]$/.test(n.reason) ? "" : "."}` : null;
}
