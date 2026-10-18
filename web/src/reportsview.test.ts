import { test } from "node:test";
import assert from "node:assert/strict";
import { axisFor, combinedLine, horizonLabel, runningTotals, transfersText, wasteChange } from "./reportsview.ts";
import * as reportView from "./reportsview.ts";
import { reportsBenchmarkQuery, reportsWorkspaceQuery, type BenchmarkSummary } from "./contract.ts";

test("block result lines fit ninety characters, retain zero, and use singular forms", () => {
  const checks = { requests: 1, lots_counted: 1, corrected_up: 1, corrected_down: 0, nothing_to_correct: 0 };
  assert.match(reportView.checksLine(checks), /^1 request · 1 lot counted/);
  assert.match(reportView.branchLine(1), /^1 store ·/);
  assert.equal(reportView.checksLine(null), "No stock checks in this scenario");
  const f = { wape: null, baseline_wape: 0.3, coverage_80: 0.75, scored_days: 1 } as BenchmarkSummary["forecast_quality"];
  assert.equal(reportView.forecastLine(f), "WAPE — / 30% baseline · 75% range coverage · 1 day scored");
  assert.equal(reportView.percent(0), "0%");
  for (const n of [0, 1, 1240, 999999999999]) {
    const lines = [reportView.checksLine({ ...checks, requests: n, lots_counted: n, corrected_up: n, corrected_down: n }),
      reportView.qualityLine({ approved: n, rejected: n, waiting: n, limit_breaks: { shelfshift: n } } as BenchmarkSummary["decision_quality"]),
      reportView.forecastLine({ ...f, scored_days: n }), reportView.branchLine(n)];
    for (const line of lines) assert.ok(line.length <= 90, line);
  }
});

test("pre-window dates are placed from the session calendar, not the computer clock", () => {
  const cal = [{ day: 0, date: "2026-10-12", weekday: "Mon" }];
  assert.equal(reportView.scenarioDate(cal, -1), "11 Oct");
  assert.equal(reportView.scenarioDate(cal, 0), "Mon 12 Oct");
  assert.equal(reportView.scenarioDate([], -7), "Scenario day -7");
});

test("waste cumulative series preserve scenario order and all-zero lines", () => {
  const view = reportView as unknown as { cumulative?: (rows: Record<string, number>[], ids: string[]) => number[][] };
  assert.equal(typeof view.cumulative, "function", "daily waste needs cumulative placement");
  const ids = ["store_rule", "store_rule_checked", "shelfshift", "markdowns_only"];
  assert.deepEqual(view.cumulative!([{ day: 0, store_rule: 0, store_rule_checked: 2, shelfshift: 1, markdowns_only: 3 },
    { day: 1, store_rule: 1, store_rule_checked: 0, shelfshift: 2, markdowns_only: 4 }], ids),
  [[0, 1], [2, 2], [1, 3], [3, 7]]);
  assert.deepEqual(view.cumulative!([{ day: 0, ...Object.fromEntries(ids.map((k) => [k, 0])) }], ids), [[0], [0], [0], [0]]);
});

test("waste sentence names only the automatic markdown reference and retains increases", () => {
  const text = reportView.wasteSentence({ markdowns_only: { units: 7 }, shelfshift: { units: 9 } }, { shelfshift: { units: 2 } });
  assert.match(text, /\+2 units vs Automatic markdowns only/);
  assert.doesNotMatch(text, /[Mm]odeled store rule/);
  assert.match(reportView.wasteSentence({ markdowns_only: { units: 0 }, shelfshift: { units: 0 } }, { shelfshift: { units: 0 } }), /0 units wasted/);
});

test("waterfall places negative and zero changes without turning them into totals", () => {
  const view = reportView as unknown as { waterfallBars?: (steps: { kind: string; value: number }[], min: number) => number[][] };
  assert.equal(typeof view.waterfallBars, "function", "saved money steps need a placement helper");
  assert.deepEqual(view.waterfallBars!([
    { kind: "total", value: 100 }, { kind: "change", value: -12 },
    { kind: "change", value: 0 }, { kind: "total", value: 88 },
  ], 80), [[80, 100], [100, 88], [88, 88], [80, 88]]);
});

test("waterfall running totals respect the saved closing total", () => {
  assert.deepEqual(runningTotals([{ kind: "total", value: 100 }, { kind: "change", value: 10 },
    { kind: "change", value: -5 }, { kind: "total", value: 104 }]), [100, 110, 105, 104]);
});
test("waterfall axis is zero at nonpositive edges and the fifty percent boundary", () => {
  assert.deepEqual(axisFor([]), { min: 0, note: false });
  assert.deepEqual(axisFor([{ kind: "total", value: 5 }, { kind: "change", value: -5 }]), { min: 0, note: false });
  assert.deepEqual(axisFor([{ kind: "total", value: 50 }, { kind: "change", value: 50 }]), { min: 0, note: false });
  assert.deepEqual(axisFor([{ kind: "total", value: -10 }]), { min: 0, note: false });
});
test("waterfall axis truncation rounds down with room for negative and near-zero changes", () => {
  const result = axisFor([{ kind: "total", value: 498120 }, { kind: "change", value: -1210 },
    { kind: "change", value: 4350 }, { kind: "total", value: 501260 }]);
  assert.equal(result.note, true);
  assert.ok(result.min < 496910 - 435);
  const flat = axisFor([{ kind: "total", value: 100 }, { kind: "change", value: 0 }]);
  assert.ok(flat.min < 100 && flat.note);
  const near = axisFor([{ kind: "total", value: 100 }, { kind: "change", value: 0.01 }]);
  assert.ok(near.min < 100 && near.note);
});

test("horizon reads both endpoints from the session calendar without inventing missing dates", () => {
  const cal = [{ day: 1, date: "2026-10-17", weekday: "Sat" }, { day: 4, date: "2026-10-20", weekday: "Tue" }];
  assert.equal(horizonLabel(cal, { first_day: 1, last_day: 4 }), "Sat 17 Oct – Tue 20 Oct");
  assert.equal(horizonLabel(cal, { first_day: 1, last_day: 1 }), "Sat 17 Oct – Sat 17 Oct");
  assert.equal(horizonLabel(cal, { first_day: 1, last_day: 8 }), "—");
});
test("transfer wording distinguishes zero, one and many", () => {
  assert.deepEqual([0, 1, 2].map(transfersText), ["0 transfers", "1 transfer", "2 transfers"]);
});
test("combined wording uses only the server additive flag and reason", () => {
  assert.equal(combinedLine(null), null);
  assert.equal(combinedLine({ additive: false, plans: 2, reason: "overlap" }), "not_additive");
  assert.equal(combinedLine({ additive: false, plans: 1, reason: "not_computed" }), "not_computed");
  assert.equal(combinedLine({ additive: true, plans: 2, net_change: 999, waste_change_units: 0,
    waste_change_kg: 0, transport_and_handling: 0 }), "total");
});
test("waste changes retain missing figures, signs and shared expectation rounding", () => {
  assert.equal(wasteChange(-42.4, -43.1), "−42 units · −43.1 kg");
  assert.equal(wasteChange(0, 0), "0 units · 0 kg");
  assert.equal(wasteChange(1, 2), "+1 units · +2 kg");
  assert.equal(wasteChange(null, null), "— units · — kg");
  assert.equal(wasteChange(null, 2), "— units · +2 kg");
  assert.equal(wasteChange(-0.1, -0.01), "0 units · 0 kg");
});

test("waste styles follow the scenario id, never the legend position (UI-SPEC chart colours)", () => {
  const styles = (reportView as unknown as { WASTE_STYLE?: Record<string, unknown> }).WASTE_STYLE;
  assert.ok(styles, "the waste chart needs a style per scenario id");
  assert.deepEqual(styles, {
    store_rule: { pointStyle: "triangle", tone: "plan", colour: "#8b93b8", mark: "▲" },
    store_rule_checked: { pointStyle: "rect", tone: "plan", colour: "#5b6487", mark: "■" },
    shelfshift: { pointStyle: "circle", tone: "public", colour: "#13207a", mark: "●" },
    markdowns_only: { pointStyle: "rectRot", tone: "waste", colour: "#b54708", mark: "◆" },
  });
});

test("recorded assumptions read as plain labels, never JSON or internal key names", () => {
  const view = reportView as unknown as { plainValue?: (v: unknown) => string; checksSetting?: (v: unknown) => string };
  assert.equal(typeof view.plainValue, "function");
  assert.equal(typeof view.checksSetting, "function");
  const plain = view.plainValue!, checks = view.checksSetting!;
  const texts = [
    plain({ recorded_multiplier: 1.25 }), plain({ m20: [1.3, 1.4333333333333333, 1.55], m30: [1.4, 1.6, 1.8] }),
    plain(1), plain("Illustrative errors"), plain(null), plain(true), plain(["PHANTOM_STOCK"]), plain({}),
    checks({ evidence: ["PHANTOM_STOCK", "STALE_COUNT"], cap_per_version: 2, lot_once_per_day: true,
      answers: "exact, from each world's own physical stock" }),
  ];
  assert.deepEqual(texts, [
    "Recorded multiplier: 1.25", "20% markdown: 1.3, 1.433, 1.55 · 30% markdown: 1.4, 1.6, 1.8",
    "1", "Illustrative errors", "Not recorded in this run", "Yes", "Phantom stock", "None recorded",
    "Phantom stock and stale count checks only · at most 2 per recommendation version · each lot counted at most once a day · answers exact, from each world's own physical stock",
  ]);
  for (const text of texts) assert.doesNotMatch(text, /[{}[\]"_]|PHANTOM_STOCK|STALE_COUNT/, text);
  assert.equal(checks(null), "Not recorded in this run");
});

test("Reports query builders keep the saved ID and leave out empty scope and the first page", () => {
  assert.equal(reportsWorkspaceQuery(), "/api/reports/workspace");
  // A manager's page sends no branch: an empty branch and the first page are left out.
  assert.equal(reportsWorkspaceQuery({ branch: "", page: 1 }), "/api/reports/workspace");
  assert.equal(reportsWorkspaceQuery({ branch: " S042 ", page: 2, page_size: 50 }),
    "/api/reports/workspace?branch=S042&page=2&page_size=50");
  assert.equal(reportsBenchmarkQuery({ benchmark_id: "bm-0123456789ab", breakdown: "branch", chart: "money", page: 2 }),
    "/api/reports/benchmark?benchmark_id=bm-0123456789ab&breakdown=branch&chart=money&page=2");
  assert.throws(() => reportsBenchmarkQuery({ breakdown: "day", chart: "waste" } as never), /requires benchmark_id/);
});

test("provider mix reads the saved report's per-provider series counts with the pipeline's names (08-21)", () => {
  const names = { ai_forecast: "TimesFM 2.5 via BigQuery AI.FORECAST", arima_plus: "ARIMA_PLUS via BigQuery ML.FORECAST" };
  assert.equal(typeof reportView.providerMix, "function");
  assert.equal(reportView.providerMix({ ai_forecast: 49989, arima_plus: 11 }, names),
    "TimesFM 2.5 via BigQuery AI.FORECAST 49,989 series · ARIMA_PLUS via BigQuery ML.FORECAST 11 series");
  assert.equal(reportView.providerMix({ "new-code": 1 }, names), "new-code 1 series"); // an unknown code as sent
  assert.equal(reportView.providerMix({}, names), "");
});
