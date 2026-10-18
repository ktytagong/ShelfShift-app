// Release copy and honest empty states (11-CONTEXT D-06, D-08).
import { test } from "node:test";
import assert from "node:assert/strict";
import type { AboutPayload } from "./contract.ts";
import { BENCHMARK_SCENARIOS, WEEKCHECK_TEXT, checkRows, versionRows, realNowLines, notInReleaseLines, benchmarkInRelease, notYetProven, labelRows, ONLINE_GROCERY_CAVEAT, PROMOTION_CAVEAT, PILOT_LINES } from "./aboutview.ts";

export const EMPTY: AboutPayload = {
  git_commit: "dev", engine_version: "engine-test", dataset_version: "unseeded", assistant_version: null,
  gemini_model: "", vertex_location: "", label: null, data_as_of: null, forecast: null, error_model: null,
  contact: "", google_cloud: false, weekcheck_status: "not_in_release", benchmark_status: "not_published",
  benchmark: null, ai_eval_status: "not_published", ai_eval: null, latest_unpublished: null,
};

test("empty checks have three honest chips in contract order and no invented result figures", () => {
  const rows = checkRows(EMPTY);
  assert.deepEqual(rows.map(({key, status, tone, icon}) => [key, status, tone, icon]), [
    ["benchmark", "Evaluation not yet available", "gray", "circle"],
    ["ai-eval", "AI evaluation not yet published.", "gray", "circle"],
    ["weekcheck", "Not part of this release", "gray", "x"],
  ]);
  assert.ok(rows.every((row) => !/passed|%/i.test(row.status)));
  assert.equal(checkRows({ ...EMPTY, weekcheck_status: "in_code" })[2].status, "In code · not run yet");
});

test("versionRows always has eight rows with individual missing-value text", () => {
  const rows = versionRows(EMPTY);
  assert.deepEqual(rows.map((row) => row.label), ["Commit", "Engine", "Dataset", "Assistant", "Gemini model", "Gemini location", "Forecasts", "Forecast run"]);
  assert.deepEqual(rows.map((row) => row.value), ["Not recorded", "engine-test", "Not installed yet", "Not part of this release", "—", "—", "No forecast run installed", "—"]);
  const populated = versionRows({ ...EMPTY, git_commit: "1234567890abcdef", gemini_model: "configured-model",
    forecast: {headline: "Mixed providers", providers: {ai_forecast: 2, arima_plus: 1}, run_id: "fixture-run", cutoff_date: "2026-10-13"} });
  assert.equal(populated[0].value, "1234567890ab");
  assert.equal(populated[4].note, "configured, not used in this release");
  assert.match(populated[6].value, /^.+ · 2 series-runs$/m);
  assert.match(populated[6].value, /^.+ · 1 series-runs$/m);
  assert.equal(populated[7].value, "fixture-run · history to Tue 13 Oct 2026");
});

test("the three synthetic paragraphs and four benchmark names retain the locked wording", () => {
  assert.deepEqual(WEEKCHECK_TEXT, [
    "Synthetic retail scenarios with a weekday profile derived from FreshRetailNet-50K and explicitly stated demand, ordering and markdown assumptions.",
    "In the synthetic 5-seed engineering check, delivery sizing from in-stock days only is biased low: when the order is 90–100% of the estimate, the in-stock days are the lower-demand days, which produces the slow rise in stockouts shown in the synthetic check.",
    "In public FreshRetailNet-50K histories, sales during stockout hours are censored and cannot by themselves establish uncensored demand; stockout-day imputation for forecast training is an explicit modeling assumption.",
  ]);
  assert.deepEqual(BENCHMARK_SCENARIOS, ["Modeled store rule with original records", "Modeled store rule with equivalent stock checks", "ShelfShift decisions with those checks", "Automatic markdowns only · no transfers or additional intervention"]);
});

import { SHIPPED, NOT_IN_RELEASE, isShipped } from "./release.ts";

test("the release manifest has disjoint ids and always excludes branch orders", () => {
  assert.deepEqual(SHIPPED.filter(s => NOT_IN_RELEASE.some(n => n.id === s.id)), []);
  assert.equal(isShipped("orders"), false);
  assert.equal(isShipped("forecasts"), true);
  assert.ok(NOT_IN_RELEASE.some(s => s.id === "orders"));
  assert.doesNotMatch(JSON.stringify([...SHIPPED, ...NOT_IN_RELEASE]), /projected.stock/i);
  assert.deepEqual(notInReleaseLines(), NOT_IN_RELEASE.map(s => `${s.name} is not part of this release.`));
});

test("Real now names installed forecasts and only confirmed release capabilities", () => {
  const about = {...EMPTY, forecast: {headline:"Fixture provider mix", providers:{ai_forecast:2}, run_id:"fixture", cutoff_date:"2026-10-13"}};
  const lines = realNowLines(about, false);
  assert.ok(lines.some(s => s.includes("engine-test")));
  assert.ok(lines.some(s => s.includes("Fixture provider mix")));
  assert.ok(lines.some(s => s === "Each stock decision's version line names the forecast run behind it and its provider."));
  assert.doesNotMatch(lines.join(" "), /Gemini on Google Cloud|Cloud Run/);
  const cloud = realNowLines({...about, google_cloud:true}, false).join(" ");
  assert.match(cloud, /Cloud Run/);
  assert.doesNotMatch(cloud, /uploaded evidence/);
  assert.deepEqual(realNowLines(about, true), realNowLines(null, false));
  assert.equal(benchmarkInRelease(about), isShipped("reports"));
  assert.equal(benchmarkInRelease({...about, benchmark_status:"published"}), true);
});

test("Not yet proven keeps three groups and conditional assumptions without guessing", () => {
  const groups = notYetProven(EMPTY, false);
  assert.deepEqual(groups.map(g => g.title), ["Data and assumptions", "Decisions and checks", "Running it for a real customer"]);
  assert.ok(groups[0].lines.includes(ONLINE_GROCERY_CAVEAT));
  assert.ok(groups[0].lines.includes(PROMOTION_CAVEAT));
  assert.deepEqual(groups[2].lines, PILOT_LINES);
  assert.doesNotMatch(groups.flatMap(g => g.lines).join(" "), /sandbox|declared error model|one operational fact/i);
  assert.equal(groups[1].lines.some(s => s.includes("ignores inbound")), isShipped("reports"));
  const about = {...EMPTY, error_model: {share:0.2, range:[0.5, 0.9] as [number,number]}, benchmark_status:"published" as const};
  assert.match(notYetProven(about, false)[0].lines.join(" "), /about 20% of lots can hold between 0.5 and 0.9/);
  assert.match(notYetProven(about, false)[1].lines.join(" "), /ignores inbound deliveries/);
  assert.deepEqual(notYetProven(about, true), groups);
});

test("the label legend uses real tones and only shipped AI surfaces", () => {
  const rows = labelRows([]);
  assert.equal(rows.find(r => r.name === "Forecast · precomputed")?.tone, "plan");
  assert.equal(rows.find(r => r.name === "Assumption")?.tone, "gray");
  assert.doesNotMatch(rows.map(r => r.name).join(" "), /Gemini/);
  assert.ok(labelRows(["assistant"]).some(r => r.name === "Live · Gemini model ID"));
  assert.ok(labelRows(["reading"]).some(r => r.name === "Precomputed · Gemini model ID"));
});
