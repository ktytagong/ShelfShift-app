// node --test (no dependency): one plain action per contract error code, the dataset disclosure, the board's card
// copy. The currency codes below are test inputs; copy.ts formats with whatever currency the session gives.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  NETWORK as API_NETWORK, type AnswerSignal, type Badge, type Card, type FactCheck, type MarkdownCrossover,
  type OptionRow, type Request, type TransportCrossover, type WasteCrossover, type WasteFlip,
} from "./api.ts";
import {
  ABOUT_KINDS, ABOUT_LIMITS, ABOUT_MODIFICATIONS, ABOUT_SOURCE, DESIGNED_EXAMPLE, DESIGNED_TIP, DESIGNED_TIP_LABEL,
  EVALUATING, NEEDS_EVALUATION, NETWORK, PRODUCT_CODE, PUBLIC_DATASET, RISK_OPTIONS, STORE_PLACEHOLDER,
  TRANSFERS_UNAVAILABLE, bestIfLow, boundsHelp, confirmedLines, datasetMark, dayCount, dayWord, eligibilityLine,
  errorAction, exportFilename, factOutcome, factSentence, factTitle, factWhy, flagBody, flipLines, lotCountText,
  markdownLine, neededText, optionsFootnote, pagerText, recordedTime, responseNote, responseRows, responseStatus,
  restrictedFootnote, retryable, settingsChanges, statePill, textLinks, transportLine,
  VALUE_AT_FULL_PRICE, planBranches, riskFact, totalsScope, triggerReason, wasteSummary,
  CHECK_TIME, checkTimeLabel, checkTimeObserved, stateIcon,
} from "./copy.ts";
import { readFileSync } from "node:fs";
import { money } from "./format.ts";

test("NETWORK is the adapter's own sentence", () => {
  assert.equal(NETWORK, API_NETWORK);
});

test("a stock decision that moved on offers Reload the stock decision (stale_version, wrong_state, already_decided)", () => {
  for (const code of ["stale_version", "wrong_state", "already_decided"]) assert.equal(errorAction(code), "reload_case", code);
});

test("hold_expired offers Recheck and resend, sandbox_reset reloads the app, locked returns to the gate", () => {
  assert.equal(errorAction("hold_expired"), "recheck");
  assert.equal(errorAction("sandbox_reset"), "reload_app");
  assert.equal(errorAction("locked"), "gate");
});

test("a busy, timed-out or unreachable server offers Try again", () => {
  for (const code of ["engine_busy", "engine_timeout", "engine_unavailable", "db_busy", "preparing", "network"]) {
    assert.equal(errorAction(code), "retry", code);
    assert.equal(retryable(code), true, code);
  }
});

test("a cap is never retried: cap_reached has no action and is not retryable", () => {
  assert.equal(errorAction("cap_reached"), "none");
  assert.equal(retryable("cap_reached"), false);
});

test("forbidden, conflict and stale_version are never resent", () => {
  for (const code of ["conflict", "forbidden", "stale_version"]) assert.equal(retryable(code), false, code);
  assert.equal(errorAction("forbidden"), "none");
  assert.equal(errorAction("conflict"), "none");
});

test("only the retry group is retryable, and an unknown code has no action", () => {
  for (const code of ["hold_expired", "wrong_state", "already_decided", "sandbox_reset", "locked", "demo_complete",
    "bad_passcode", "idempotency_conflict", "too_many_requests", "needs_input"]) {
    assert.equal(retryable(code), false, code);
  }
  assert.equal(errorAction("something_new"), "none");
});

// --- the board's card copy (UI-SPEC "Card", "Badge tone map"; Amendments A-7, A-9, A-10) ----------------------------

const CAL = [
  { day: 3, date: "2026-10-15", weekday: "Thu" },
  { day: 4, date: "2026-10-16", weekday: "Fri" },
  { day: 5, date: "2026-10-17", weekday: "Sat" },
];

function card(over: Partial<Card> = {}): Card {
  return {
    case_id: "c1", key: "SUS-19", tag: "milk-story", origin: "designed", state: "OPEN", column: "New", sku: "MLK",
    product: "Fresh milk 1L", zone: "chilled", source: "Bang Na", dest: "Lat Phrao", lot_id: "MLK-038",
    suggested: "Transfer 80 to Lat Phrao", suggested_key: "tr:Lat Phrao:R-02:80:hold", at_risk_units: 120, at_risk_kg: 60,
    at_risk_value: 1234.5, est_benefit: 42.4,
    due: { day: 3, min: 600, date: "2026-10-15", weekday: "Thu", label: "Decide by Thu 10:00" },
    decide_by: { day: 3, latest_decision_min: 600, source: "run" }, markdown_step: null, act_before: null,
    wait: null, rec_version: "abc", next_owner: [{ role: "manager", branch: "Bang Na" }], decisions: {}, hold: null,
    badges: [], reason: null, waiting_until_day: null, ...over,
  };
}

// --- fact checks (UI-SPEC Copywriting "Fact check: …"; spec §6; research Pattern 7) ------------------------------

function fact(over: Partial<FactCheck> = {}): FactCheck {
  return {
    fact_id: "Lat Phrao:phantom", kind: "PHANTOM_STOCK", lot_id: "MLK-038", lot_ids: ["MLK-038"], branch: "Lat Phrao",
    recorded_value: 60, alt_value: 0, regret: 34.32, decision_regret: 34.32, unserved_units: 25.62,
    net_revenue_difference: 34.32, waste_policy_difference: 0, waste_kg_difference: 42.23,
    best_plan_if_alt: "tr:Lat Phrao:R-02:151:md20", best_plan_if_alt_label: "Transfer 151 to Lat Phrao (R-02), markdown rest 20%",
    pick_changed: true, pick_feasible_if_alt: true, changes_plan: true, ...over,
  };
}

const MONEY_FIGURE = /\$|USD|\d+\.\d\d/; // any amount: a sign, the code or a cents figure

test("factOutcome: not allowed whenever pick_feasible_if_alt is false, even with changes_plan true and regret 0", () => {
  assert.equal(factOutcome(fact({ pick_feasible_if_alt: false, regret: 0, decision_regret: 0 })), "not_allowed");
  assert.equal(factOutcome(fact({ pick_feasible_if_alt: false, changes_plan: false, pick_changed: false })), "not_allowed");
  assert.equal(factOutcome(fact()), "changes");
  assert.equal(factOutcome(fact({ changes_plan: false, regret: 0 })), "below_guard");
  assert.equal(factOutcome(fact({ changes_plan: false, pick_changed: false, regret: 0, decision_regret: 0 })), "no_change");
});

test("factSentence not allowed: the spec §6 sentence with the best plan, and no money figure even at regret 0", () => {
  const f = fact({ pick_feasible_if_alt: false, regret: 0, decision_regret: 0, net_revenue_difference: -12.5 });
  const s = factSentence(f, "USD", 0.5, CAL);
  assert.ok(s.includes("If this fact is true, the recommended plan is not allowed. Best plan then: "
    + "Transfer 151 to Lat Phrao (R-02), markdown rest 20%."), s);
  assert.doesNotMatch(s, MONEY_FIGURE);
});

test("factSentence changes, phantom: the lead with N = recorded − alt, then the regret split at a waste price above 0", () => {
  const f = fact({ net_revenue_difference: -5, waste_policy_difference: 12, waste_kg_difference: 24, decision_regret: 7 });
  assert.equal(factSentence(f, "USD", 0.5, CAL),
    "If Lat Phrao's 60 units aren't there, acting on the record leaves 26 units of demand unserved that the best plan "
    + "would serve. The best plan would be: Transfer 151 to Lat Phrao (R-02), markdown rest 20%. "
    + "The count changes the plan. Decision regret $7.00: net revenue −$5.00, waste policy +$12.00 "
    + "(24 kg less waste; a policy value, not cash).");
});

test("factSentence: the unserved units are an expectation, shown in whole units (G-05-2 item 10)", () => {
  assert.ok(factSentence(fact({ unserved_units: 25.6 }), "USD", 0, CAL).includes("26 units of demand unserved"));
  assert.ok(factSentence(fact({ unserved_units: 0.4 }), "USD", 0, CAL).includes(" 0 units of demand unserved"));
});

test("factSentence changes: 'more waste' when waste_kg_difference is negative, the policy clause kept at an amount of 0", () => {
  const more = factSentence(fact({ waste_policy_difference: -3.1, waste_kg_difference: -6.2 }), "USD", 0.5, CAL);
  assert.ok(more.includes("waste policy −$3.10 (6.2 kg more waste; a policy value, not cash)."), more);
  const zero = factSentence(fact({ waste_policy_difference: 0, waste_kg_difference: 0 }), "USD", 0.5, CAL);
  assert.ok(zero.includes("net revenue +$34.32, waste policy $0.00 (0 kg less waste; a policy value, not cash)."), zero);
});

test("factSentence changes at a waste price of 0: no policy clause (build/api b570cf5's rule), and never 'cash'", () => {
  const s = factSentence(fact(), "USD", 0, CAL);
  assert.ok(s.endsWith("The count changes the plan. Decision regret $34.32: net revenue +$34.32."), s);
  assert.doesNotMatch(s, /waste policy|cash/);
});

test("factSentence changes, stale: the lead's N is the units the count may lack, in whole units", () => {
  const f = fact({ fact_id: "MLK-340:stale", kind: "STALE_COUNT", lot_id: "MLK-340", lot_ids: ["MLK-340"], branch: "Thonburi",
    recorded_value: 41, alt_value: 28.7, unserved_units: 3 });
  assert.ok(factSentence(f, "USD", 0, CAL).startsWith("If Thonburi's 12 units aren't there, acting on the record leaves 3 "
    + "units of demand unserved that the best plan would serve."));
});

test("factSentence changes, estimated expiry: the expiry lead with both weekdays (plan assumption)", () => {
  const f = fact({ fact_id: "STR-201:expiry", kind: "ESTIMATED_EXPIRY", lot_id: "STR-201", lot_ids: ["STR-201"],
    branch: "Rangsit", recorded_value: 5, alt_value: 4, best_plan_if_alt_label: "Keep" });
  assert.ok(factSentence(f, "USD", 0, CAL).startsWith("If lot STR-201 expires Fri instead of Sat, the best plan would be: Keep. "
    + "The count changes the plan."));
});

test("factSentence below the noise guard (user wording, 2026-10-16; G-05-12 (c)) and with no change", () => {
  const BELOW = "A stock count could change the recommended plan, but the estimated impact is below the threshold for "
    + "prioritizing a check.";
  const below = fact({ changes_plan: false, regret: 0, decision_regret: 0.02, best_plan_if_alt_label: "Keep" });
  assert.equal(factSentence(below, "USD", 0, CAL), BELOW);
  assert.equal(factSentence({ ...below, decision_regret: 0 }, "USD", 5, CAL), BELOW);
  assert.ok(!/\$|Keep/.test(factSentence(below, "USD", 0, CAL)));
  const none = fact({ changes_plan: false, pick_changed: false, regret: 0, decision_regret: 0 });
  assert.equal(factSentence(none, "USD", 0, CAL), "Checked, wouldn't change the plan.");
});

test("factWhy: the UI-SPEC sentence per kind; stale N = session day − last_verified_day of the fact's lot", () => {
  const input = { lots: [{ lot_id: "MLK-340", last_verified_day: -5 }, { lot_id: "MLK-038", last_verified_day: 1 }] };
  assert.equal(factWhy(fact(), input, 3, CAL),
    "On record, but no sales were recorded here yesterday although the forecast expects sales.");
  assert.equal(factWhy(fact({ kind: "ESTIMATED_EXPIRY" }), input, 3, CAL),
    "The expiry date is estimated, not read from the label.");
  assert.equal(factWhy(fact({ kind: "STALE_COUNT", lot_id: "MLK-340" }), input, 3, CAL), "Not counted for 8 days.");
  assert.equal(factWhy(fact({ kind: "STALE_COUNT", lot_id: "MLK-340" }), input, 4, CAL), "Not counted for 9 days.");
  assert.equal(factWhy(fact({ kind: "STALE_COUNT", lot_id: "MLK-038" }), input, 2, CAL), "Not counted for 1 day.");
  assert.equal(factWhy(fact({ kind: "SOME_NEW_KIND" }), input, 3, CAL), "Some new kind");
});

test("dayCount and dayWord: 'day' only for exactly one (G-05-12 (e))", () => {
  assert.equal(dayCount(0), "0 days");
  assert.equal(dayCount(1), "1 day");
  assert.equal(dayCount(2), "2 days");
  assert.equal(dayCount(1234), "1,234 days");
  assert.equal(dayWord(1), "day");
  assert.equal(dayWord(7), "days");
  assert.equal(dayWord(0), "days");
});

test("factWhy: an input computed on another day gives no phantom or stale-count line (review CR-01)", () => {
  const lots = [{ lot_id: "MLK-340", last_verified_day: -5 }];
  const ranOn3 = { today: 3, lots };
  assert.equal(factWhy(fact({ kind: "STALE_COUNT", lot_id: "MLK-340" }), ranOn3, 3, CAL), "Not counted for 8 days.");
  assert.equal(factWhy(fact({ kind: "STALE_COUNT", lot_id: "MLK-340" }), ranOn3, 5, CAL), null);
  assert.equal(factWhy(fact(), ranOn3, 5, CAL), null);
  assert.equal(factWhy(fact({ kind: "ESTIMATED_EXPIRY" }), ranOn3, 5, CAL),
    "The expiry date is estimated, not read from the label.");
});

test("factTitle: branch, every lot id and the recorded units, or the recorded expiry date", () => {
  assert.equal(factTitle(fact({ lot_ids: ["MLK-038", "MLK-039"], recorded_value: 1250 }), CAL),
    "Lat Phrao · lot MLK-038, MLK-039: recorded 1,250 units");
  assert.equal(factTitle(fact({ kind: "ESTIMATED_EXPIRY", lot_ids: ["STR-201"], branch: "Rangsit", recorded_value: 5 }), CAL),
    "Rangsit · lot STR-201: recorded expiry Sat 17 Oct");
});

// --- the Options tab (UI-SPEC "Options (UI-04, ENG-04, D-08)") -----------------------------------------------------

function option(key: string, low: number): OptionRow {
  const t = { low, base: low + 10, high: low + 20 };
  return {
    key, label: key, kind: "MARKDOWN", pct: 20, qty: 100, dest: null, run_code: null, net: t, score: t, waste_policy: 0,
    delta_vs_keep: 0, sold: 90, wasted_units: 10, wasted_kg: 10, shortfall_filled: 0,
  };
}

test("bestIfLow: the row with the highest score.low when it is not the suggested one", () => {
  assert.equal(bestIfLow([option("a", 50), option("b", 70), option("c", 60)], "a"), "b");
});

test("bestIfLow: null when the best row by score.low is the suggested one", () => {
  assert.equal(bestIfLow([option("a", 50), option("b", 70)], "b"), null);
});

test("bestIfLow: a tie on score.low goes to the first row in the engine's order", () => {
  assert.equal(bestIfLow([option("s", 10), option("a", 70), option("b", 70)], "s"), "a");
  assert.equal(bestIfLow([option("a", 70), option("b", 70)], "a"), null); // the first of the tie is the suggested row
});

test("bestIfLow reads score.low, not net.low", () => {
  const a = { ...option("a", 50), score: { low: 80, base: 0, high: 0 } };
  assert.equal(bestIfLow([option("s", 60), a], "s"), "a");
  assert.equal(bestIfLow([], "s"), null);
});

// --- crossovers (UI-SPEC "Options"; Amendments A-1 to A-4) ---------------------------------------------------------

const RANGE: [number, number] = [0, 555.56];

test("transportLine (A-1): rises reads 'stays best until', the engine's threshold to the cent (G-05-12 (a))", () => {
  const c: TransportCrossover = { result: "threshold", value: 96.83, direction: "rises", run_code: "R-02", quote: 16.67, range: RANGE, method: "exact" };
  assert.equal(transportLine(c, "USD"), "This plan stays best until R-02 costs $96.83.");
  assert.equal(transportLine(c, "EUR"), "This plan stays best until R-02 costs €96.83.");
});

test("transportLine (A-1): falls reads 'A transfer would win if … cost below', the engine's cent, never a coarser step", () => {
  const c: TransportCrossover = { result: "threshold", value: 11.38, direction: "falls", run_code: "R-07", quote: 25, range: RANGE, method: "exact" };
  assert.equal(transportLine(c, "USD"), "A transfer would win if R-07 cost below $11.38.");
  assert.equal(transportLine({ ...c, run_code: "R-05", value: 10.51 }, "USD"), "A transfer would win if R-05 cost below $10.51.");
  assert.equal(transportLine({ ...c, run_code: "R-05", value: 10.51 }, "EUR"), "A transfer would win if R-05 cost below €10.51.");
});

test("transportLine: no change in range reads the crossover's own range; a null run_code gives no line", () => {
  const c: TransportCrossover = { result: "no_change_in_range", run_code: "R-02", quote: 16.67, range: [0, 83.35], method: "exact" };
  assert.equal(transportLine(c, "USD"), "No R-02 quote from $0.00 to $83.35 changes the plan.");
  assert.equal(transportLine({ ...c, range: RANGE }, "USD"), "No R-02 quote from $0.00 to $555.56 changes the plan.");
  assert.equal(transportLine({ result: "no_change_in_range", run_code: null, method: "exact" }, "USD"), null);
});

const MD_RANGE = { range: { m20: [1.3, 1.55] as [number, number], m30: [1.4, 1.8] as [number, number] }, method: "approx" as const, label: "approx." };

test("markdownLine (A-2): each result ends with '(approx.)'; the threshold shows ×value.m20 to two digits", () => {
  const t: MarkdownCrossover = { ...MD_RANGE, result: "threshold", value: { m20: 1.4321, m30: 1.66, winner: "md30" } };
  assert.equal(markdownLine(t), "The plan changes if the 20% markdown lifts sales by ×1.43 (approx.).");
  assert.equal(markdownLine({ ...MD_RANGE, result: "no_change_in_range" }),
    "No markdown response in the tested range changes the plan (approx.).");
  const m: MarkdownCrossover = { ...MD_RANGE, result: "multiple_changes", values: [
    { m20: 1.46, m30: 1.66, winner: "md30" }, { m20: 1.55, m30: 1.8, winner: "keep" }] };
  assert.equal(markdownLine(m), "The plan changes more than once across the tested markdown responses (approx.).");
});

const NONE: WasteFlip = { result: "no_change_in_range" };
const flip = (value: number, label: string): WasteFlip => ({ result: "threshold", value, key: label.toLowerCase(), label });
const waste = (price: number, rises: WasteFlip, falls: WasteFlip, max = 5): WasteCrossover =>
  ({ price, range: [0, max], method: "exact", rises_at: rises, falls_at: falls });

// G-05-12 (d), user wording 2026-10-16: a line names only the next change each way (never a half-line), or the
// boundary when the shown cent is the crossing itself; every change and boundary line carries its tooltip.
const RISE_TIP = "Higher waste prices may change the recommendation again.";
const FALL_TIP = "Lower waste prices may change the recommendation again.";
const rise = (x: string, label: string) =>
  ({ text: `The next recommendation change occurs at ${x}/kg: ${label} becomes preferred.`, tip: RISE_TIP });
const fall = (y: string, label: string) =>
  ({ text: `The next recommendation change below the current price occurs at ${y}/kg: ${label} becomes preferred.`, tip: FALL_TIP });
const plain = (text: string) => ({ text, tip: null });

test("flipLines (G-05-12 (d)): a rising threshold names the next change, rounded up to the cent, with its tooltip", () => {
  assert.deepEqual(flipLines(waste(0, flip(0.4101, "Transfer 120"), NONE), "USD"), [rise("$0.42", "Transfer 120")]);
  assert.deepEqual(flipLines(waste(0, flip(0.4201, "Transfer 120"), NONE), "USD"), [rise("$0.43", "Transfer 120")]);
  assert.deepEqual(flipLines(waste(0, flip(0.4200000001, "Markdown 30%"), NONE), "USD"), [rise("$0.43", "Markdown 30%")]);
});

test("flipLines (G-05-12 (d)): a falling threshold names the next change below, rounded down to the cent, with its tooltip", () => {
  assert.deepEqual(flipLines(waste(0.5, flip(0.9001, "Transfer 80"), flip(0.3099, "Keep")), "USD"), [
    rise("$0.91", "Transfer 80"),
    fall("$0.30", "Keep"),
  ]);
  assert.deepEqual(flipLines(waste(0.5, NONE, flip(0.4199999999, "Keep")), "USD")[1], fall("$0.41", "Keep"));
  assert.deepEqual(flipLines(waste(100, NONE, flip(40.123, "Keep"), 100), "USD"), [fall("$40.12", "Keep")]);
});

test("flipLines (tie rule, user decision 2): a shown cent equal to the value names the boundary of its direction", () => {
  assert.deepEqual(flipLines(waste(0, flip(0.5, "Transfer 120"), NONE), "USD"),
    [{ text: "The next recommendation boundary above the current price is $0.50/kg.", tip: RISE_TIP }]);
  assert.deepEqual(flipLines(waste(0, flip(0.42, "Markdown 30%"), NONE), "USD"),
    [{ text: "The next recommendation boundary above the current price is $0.42/kg.", tip: RISE_TIP }]);
  assert.deepEqual(flipLines(waste(1, NONE, flip(0.25, "Keep")), "USD")[1],
    { text: "The next recommendation boundary below the current price is $0.25/kg.", tip: FALL_TIP });
  assert.deepEqual(flipLines(waste(1, NONE, flip(0.1 + 0.2, "Keep")), "USD")[1], // within float precision of $0.30
    { text: "The next recommendation boundary below the current price is $0.30/kg.", tip: FALL_TIP });
  assert.deepEqual(flipLines(waste(5, NONE, flip(2, "Keep")), "USD"),
    [{ text: "The next recommendation boundary below the current price is $2.00/kg.", tip: FALL_TIP }]);
  // falling, the engine's one-float-step tie bump above a cent (0.5 + 2^-53) floors back onto it: still the crossing;
  // rising, the same value rounds up to the next cent, strictly past the crossing; a hundredth of a cent is no tie
  assert.deepEqual(flipLines(waste(1, NONE, flip(0.5 + Number.EPSILON / 2, "Keep")), "USD")[1],
    { text: "The next recommendation boundary below the current price is $0.50/kg.", tip: FALL_TIP });
  assert.deepEqual(flipLines(waste(0, flip(0.5 + Number.EPSILON / 2, "Keep"), NONE), "USD"), [rise("$0.51", "Keep")]);
  assert.deepEqual(flipLines(waste(0, flip(0.5001, "Keep"), NONE), "USD"), [rise("$0.51", "Keep")]);
  assert.deepEqual(flipLines(waste(1, NONE, flip(0.5001, "Keep")), "USD")[1], fall("$0.50", "Keep"));
});

test("flipLines (A-3): a falling value that floors to 0.00 reads 'At a waste policy of $0.00/kg', no tooltip", () => {
  assert.deepEqual(flipLines(waste(0.5, NONE, flip(5e-324, "Keep")), "USD"), [
    plain("No waste policy from $0.50/kg to $5.00/kg changes the plan."),
    plain("At a waste policy of $0.00/kg the engine would choose Keep."),
  ]);
  assert.deepEqual(flipLines(waste(0.5, NONE, flip(0.0099, "Keep")), "USD")[1],
    plain("At a waste policy of $0.00/kg the engine would choose Keep."));
});

test("flipLines (A-4): a rising no-change reads from the price to range[1], omitted at or above range[1]", () => {
  assert.deepEqual(flipLines(waste(0, NONE, NONE), "USD"), [plain("No waste policy from $0.00/kg to $5.00/kg changes the plan.")]);
  assert.deepEqual(flipLines(waste(1.25, NONE, flip(0.7501, "Keep")), "USD"), [
    plain("No waste policy from $1.25/kg to $5.00/kg changes the plan."),
    fall("$0.75", "Keep"),
  ]);
  // the waste price may exceed waste_flip_max, and then range[1] is the price itself (engine/crossover.py)
  assert.deepEqual(flipLines(waste(100, NONE, flip(40.123, "Keep"), 100), "USD"), [fall("$40.12", "Keep")]);
});

test("flipLines (A-4): a falling no-change reads from $0 to the price only above 0", () => {
  assert.deepEqual(flipLines(waste(0.5, flip(0.9001, "Transfer 80"), NONE), "USD"), [
    rise("$0.91", "Transfer 80"),
    plain("No waste policy from $0.00/kg to $0.50/kg changes the plan."),
  ]);
  assert.deepEqual(flipLines(waste(0, flip(0.9001, "Transfer 80"), NONE), "USD"), [rise("$0.91", "Transfer 80")]);
});

test("flipLines (A-4): both directions unchanged above $0 merge into one line from $0 to range[1]", () => {
  assert.deepEqual(flipLines(waste(0.5, NONE, NONE), "USD"), [plain("No waste policy from $0.00/kg to $5.00/kg changes the plan.")]);
  assert.deepEqual(flipLines(waste(0.5, NONE, NONE, 7.5), "USD"), [plain("No waste policy from $0.00/kg to $7.50/kg changes the plan.")]);
  assert.deepEqual(flipLines(waste(100, NONE, NONE, 100), "USD"), [plain("No waste policy from $0.00/kg to $100.00/kg changes the plan.")]);
});

test("flipLines: no line claims a winner for a whole half-line of waste prices", () => {
  const all = [flip(0.4101, "A"), flip(0.5, "B"), flip(0.3099, "C"), flip(0.25, "D"), flip(0.0099, "E")]
    .flatMap((f) => [...flipLines(waste(0.6, f, NONE), "USD"), ...flipLines(waste(0.6, NONE, f), "USD")]);
  for (const l of all) assert.doesNotMatch(l.text, /or more|waste policy below/, l.text);
});

test("flipLines: amounts follow the session currency", () => {
  assert.deepEqual(flipLines(waste(0, flip(0.4201, "Keep"), NONE), "EUR"), [rise("€0.43", "Keep")]);
  assert.deepEqual(flipLines(waste(0, flip(42, "Keep"), NONE), "JPY"),
    [{ text: "The next recommendation boundary above the current price is ¥42/kg.", tip: RISE_TIP }]);
  assert.deepEqual(flipLines(waste(0, flip(42.2, "Keep"), NONE), "JPY"), [rise("¥43", "Keep")]);
});

test("optionsFootnote: the score sentence only above $0, the shortlist label only when the engine gives one", () => {
  const base = "Scenarios are not probabilities: low and high are the forecast's 80% interval. ";
  const tail = " One destination per plan. Figures are simulated or estimated.";
  assert.equal(optionsFootnote({ candidates_checked: 345 }, 0), `${base}345 plans searched.${tail}`);
  assert.equal(optionsFootnote({ candidates_checked: 1842, shortlist: { step: 5, label: "transfer quantities in steps of 5" } }, 0),
    `${base}1,842 plans searched; transfer quantities in steps of 5.${tail}`);
  assert.equal(optionsFootnote({ candidates_checked: 345 }, 0.5), `${base}345 plans searched.${tail} `
    + "Score = net − waste price × kg wasted. The waste price is a retailer policy, not a cash cost.");
});

test("optionsFootnote: says what is simulated or estimated and never calls a figure synthetic (05.2 D-03, D-04)", () => {
  for (const price of [0, 0.5]) {
    const text = optionsFootnote({ candidates_checked: 3 }, price);
    assert.match(text, /Figures are simulated or estimated\.( Score = |$)/);
    assert.doesNotMatch(text, /synthetic/i);
  }
  assert.ok(optionsFootnote({ candidates_checked: 3 }, 0).endsWith("Figures are simulated or estimated."));
});

// --- flagged items, evaluation on open, eligibility (05.2 D-24, D-25, D-27, D-31; 05-UI-SPEC A-33..A-35, A-40) --------

test("the flagged-item words, word for word (05.2 D-24, D-25, D-27, D-31)", () => {
  assert.equal(NEEDS_EVALUATION, "Needs evaluation");
  assert.equal(EVALUATING, "Evaluating this item…");
  assert.equal(restrictedFootnote, "Best among the plans evaluated.");
  assert.equal(TRANSFERS_UNAVAILABLE, "Transfers unavailable: location or route data missing");
  assert.doesNotMatch(TRANSFERS_UNAVAILABLE, /benefi/i); // an unmapped store is never worded as "no benefit" (D-31)
});

test("eligibilityLine: one '<dest>: <reason>' line per excluded destination, in the order given", () => {
  const excluded = [
    { run_code: "R-12-3", dest: "S031", reason: "too late to sell" },
    { run_code: "R-12-1", dest: "S007", reason: "cold chain" },
    { run_code: "R-12-2", dest: "S019", reason: "no receiving capacity" },
  ];
  assert.deepEqual(eligibilityLine({ excluded }), ["S031: too late to sell", "S007: cold chain", "S019: no receiving capacity"]);
  assert.deepEqual(eligibilityLine({ excluded: [] }), []);
});

test("optionsFootnote: a restricted search ends with 'Best among the plans evaluated.'; unrestricted is unchanged", () => {
  const r = { candidates_checked: 345 };
  for (const price of [0, 0.5]) {
    const plain = optionsFootnote(r, price);
    assert.equal(optionsFootnote(r, price, false), plain);
    assert.equal(optionsFootnote(r, price, true), `${plain} Best among the plans evaluated.`);
  }
});

test("flagBody: the screen's figures only, no plan, amount or deadline (05.2 D-24)", () => {
  const cal = [{ day: 3, date: "2026-10-17", weekday: "Sat" }, { day: 4, date: "2026-10-18", weekday: "Sun" }];
  const rows = flagBody({ risk: { a: true, b: false, keep_waste: 41.6, stock_at_step: 120.4, step_day: 4 } }, cal);
  assert.deepEqual(rows, [
    { kind: "kv", label: "At risk of waste", value: "42 units" },
    { kind: "kv", label: "Stock at markdown step", value: "120 units · Sun 18 Oct" },
  ]);
  const text = JSON.stringify(rows);
  assert.doesNotMatch(text, /Suggested|benefit|Decide|Due|\$|USD|transfer|markdown \d|keep/i);
  // Missing data: the screen gave no figures, so none are shown as numbers.
  assert.deepEqual(flagBody({ risk: { a: null, b: null, keep_waste: null, stock_at_step: null, step_day: null } }, cal), [
    { kind: "kv", label: "At risk of waste", value: "—" },
    { kind: "kv", label: "Stock at markdown step", value: "—" },
  ]);
  assert.deepEqual(flagBody({}, cal), [
    { kind: "kv", label: "At risk of waste", value: "—" },
    { kind: "kv", label: "Stock at markdown step", value: "—" },
  ]);
});

test("the Options copy never calls an amount cash, savings or loss (only 'not a cash cost' for the waste price)", () => {
  const lines = [
    ...flipLines(waste(0.5, flip(0.9, "Transfer 80"), flip(0.2, "Keep")), "USD").flatMap((l) => [l.text, l.tip ?? ""]),
    optionsFootnote({ candidates_checked: 3 }, 0.5).replace("not a cash cost", ""),
    markdownLine({ ...MD_RANGE, result: "no_change_in_range" }),
  ];
  for (const s of lines) assert.doesNotMatch(s, /cash|saving|loss/i, s);
});

// --- count requests: the verification line, request status, Requests pills (UI-SPEC "Verification line", "Requests";
// Amendments A-8, A-9, A-10) ----------------------------------------------------------------------------------------

function req(over: Partial<Request> = {}): Request {
  return {
    request_id: "REQ-1", case_id: "c1", case_key: "SUS-19", rec_version: "abc", fact_id: "f1",
    fact_kind: "PHANTOM_STOCK", lot_ids: ["MLK-038"], to_branch: "Lat Phrao", ask_text: "Please count", due_day: 3,
    due_min: 585, state: "OPEN", partial_note: null, answer_signal_id: null, replaced_by: null, case_source: "Bang Na",
    recountable: false, confirmable: false, answerable: true, answer: null, ...over,
  };
}

test("statePill: Open grey, 'Answered · confirm the count' amber, Confirmed green, Expired grey", () => {
  assert.deepEqual(statePill(req({ state: "OPEN" })), { tone: "gray", text: "Open" });
  assert.deepEqual(statePill(req({ state: "ANSWERED" })), { tone: "warn", text: "Answered · confirm the count" });
  assert.deepEqual(statePill(req({ state: "CONFIRMED" })), { tone: "good", text: "Confirmed" });
  assert.deepEqual(statePill(req({ state: "EXPIRED" })), { tone: "gray", text: "Expired" });
});

test("statePill SUPERSEDED (user decision 6): grey 'Replaced by a recount' for a count request, 'Replaced by a new request' otherwise", () => {
  for (const kind of ["PHANTOM_STOCK", "STALE_COUNT"]) {
    assert.deepEqual(statePill(req({ state: "SUPERSEDED", fact_kind: kind })), { tone: "gray", text: "Replaced by a recount" }, kind);
  }
  assert.deepEqual(statePill(req({ state: "SUPERSEDED", fact_kind: "ESTIMATED_EXPIRY" })),
    { tone: "gray", text: "Replaced by a new request" });
});

// --- the response summary (user wording 2026-10-16; the user's decisions 3 to 6 of the 05 plan revision) ------------

function answered(over: Partial<AnswerSignal> = {}): AnswerSignal {
  return {
    signal_id: "SIG-1", revision: 1, kind: "STOCK_COUNT", state: "READ", request_id: "REQ-1", branch: "Lat Phrao",
    text: "", fields: { lot_ids: ["MLK-038"], counts: { "MLK-038": { shelf_units: 4, backroom_units: 2 } }, needs_input: [] },
    day: 3, reporter_role: "manager", reporter_branch: "Lat Phrao", created_at: "2026-10-16T13:30:00Z", ...over,
  };
}

test("lotCountText: 'Shelf 4 · Backroom 2 · Total 6 units'; a blank part reads 'not checked' with no total and no unit", () => {
  assert.equal(lotCountText({ shelf_units: 4, backroom_units: 2 }), "Shelf 4 · Backroom 2 · Total 6 units");
  assert.equal(lotCountText({ shelf_units: 0, backroom_units: 0 }), "Shelf 0 · Backroom 0 · Total 0 units");
  assert.equal(lotCountText({ shelf_units: 4, backroom_units: null }), "Shelf 4 · Backroom: not checked");
  assert.equal(lotCountText({ shelf_units: null, backroom_units: 2 }), "Shelf: not checked · Backroom 2");
  assert.equal(lotCountText({ shelf_units: null, backroom_units: null }), "Shelf: not checked · Backroom: not checked");
});

test("recordedTime: a recorded server time as '16 Oct 2026, 20:30' in the zone given, whatever the machine's zone", () => {
  const machine = process.env.TZ;
  try {
    for (const tz of ["America/New_York", "UTC", "Asia/Tokyo"]) {
      process.env.TZ = tz; // the machine's own zone changes; the result does not
      assert.equal(recordedTime("2026-10-16T13:30:00Z", "Asia/Bangkok"), "16 Oct 2026, 20:30", tz);
      assert.equal(recordedTime("2026-10-16T13:30:00Z", "UTC"), "16 Oct 2026, 13:30", tz);
      assert.equal(recordedTime("2026-10-16T02:05:00Z", "Asia/Bangkok"), "16 Oct 2026, 09:05", tz);
    }
  } finally {
    if (machine === undefined) delete process.env.TZ;
    else process.env.TZ = machine;
  }
  assert.equal(recordedTime("2026-10-16T13:30:00.123456+00:00", "Asia/Bangkok"), "16 Oct 2026, 20:30"); // the API's form
  assert.equal(recordedTime("2026-10-16T17:30:00Z", "Asia/Bangkok"), "17 Oct 2026, 00:30"); // past midnight, never 24:30
  assert.equal(recordedTime("not a time", "Asia/Bangkok"), "");
  assert.equal(recordedTime("", "Asia/Bangkok"), "");
});

test("responseStatus: Awaiting confirmation, Confirmed, and SUPERSEDED by kind", () => {
  assert.equal(responseStatus(req({ state: "ANSWERED" })), "Awaiting confirmation");
  assert.equal(responseStatus(req({ state: "CONFIRMED" })), "Confirmed");
  assert.equal(responseStatus(req({ state: "SUPERSEDED", fact_kind: "PHANTOM_STOCK" })), "Not confirmed · replaced by a recount");
  assert.equal(responseStatus(req({ state: "SUPERSEDED", fact_kind: "STALE_COUNT" })), "Not confirmed · replaced by a recount");
  assert.equal(responseStatus(req({ state: "SUPERSEDED", fact_kind: "ESTIMATED_EXPIRY" })),
    "Not confirmed · replaced by a new request");
});

test("responseNote: 'Answered after deadline' when the answer's sandbox day is after the due day, in every state", () => {
  for (const state of ["ANSWERED", "CONFIRMED", "SUPERSEDED"] as const) {
    assert.equal(responseNote(req({ state, due_day: 3, answer: answered({ day: 4 }) })), "Answered after deadline", state);
    assert.equal(responseNote(req({ state, due_day: 3, answer: answered({ day: 3 }) })), null, state);
    assert.equal(responseNote(req({ state, due_day: 4, answer: answered({ day: 3 }) })), null, state);
  }
  assert.equal(responseNote(req({ state: "OPEN" })), null); // no answer, no note
});

test("responseRows: per lot, Reply when given, Submitted by the recorded seat, Submitted in the zone, Status", () => {
  const r = req({ state: "ANSWERED", answer: answered() });
  assert.deepEqual(responseRows(r, "Asia/Bangkok"), [
    ["Lot MLK-038", "Shelf 4 · Backroom 2 · Total 6 units"],
    ["Submitted by", "Lat Phrao manager"],
    ["Submitted", "16 Oct 2026, 20:30"],
    ["Status", "Awaiting confirmation"],
  ]);
  const replied = req({ state: "ANSWERED", lot_ids: ["A", "B"], answer: answered({
    text: "Counted after the delivery",
    fields: { lot_ids: ["A", "B"], needs_input: [],
      counts: { A: { shelf_units: 4, backroom_units: 2 }, B: { shelf_units: 0, backroom_units: 1 } } },
  }) });
  assert.deepEqual(responseRows(replied, "Asia/Bangkok"), [
    ["Lot A", "Shelf 4 · Backroom 2 · Total 6 units"],
    ["Lot B", "Shelf 0 · Backroom 1 · Total 1 units"],
    ["Reply", "Counted after the delivery"],
    ["Submitted by", "Lat Phrao manager"],
    ["Submitted", "16 Oct 2026, 20:30"],
    ["Status", "Awaiting confirmation"],
  ]);
});

test("responseRows: the recorded reporter, never the request's to_branch; an unreadable time leaves Submitted out", () => {
  const r = req({ state: "CONFIRMED", to_branch: "Lat Phrao", answer: answered({ reporter_branch: "Bang Na" }) });
  assert.deepEqual(responseRows(r, "Asia/Bangkok")[1], ["Submitted by", "Bang Na manager"]);
  const rows = responseRows(req({ state: "ANSWERED", answer: answered({ created_at: "garbled" }) }), "Asia/Bangkok");
  assert.equal(rows.some(([label]) => label === "Submitted"), false);
  assert.deepEqual(rows.at(-1), ["Status", "Awaiting confirmation"]);
  assert.deepEqual(responseRows(req({ state: "SUPERSEDED", answer: answered() }), "UTC").slice(-2), [
    ["Submitted", "16 Oct 2026, 13:30"], ["Status", "Not confirmed · replaced by a recount"],
  ]);
});

test("confirmedLines: one line per counted lot from the stored answer; an uncounted lot adds none", () => {
  const answer = {
    revision: 1, state: "CONFIRMED",
    fields: {
      lot_ids: ["A", "B"], needs_input: [],
      counts: { A: { shelf_units: 4, backroom_units: 2 }, B: { shelf_units: 0, backroom_units: null } },
    },
  } as unknown as AnswerSignal;
  assert.deepEqual(confirmedLines(req({ state: "CONFIRMED", lot_ids: ["A", "B"], answer })), [
    "Count confirmed by Lat Phrao manager · shelf 4 + backroom 2 = 6 · the plan was recomputed",
  ]);
  assert.deepEqual(confirmedLines(req({ state: "CONFIRMED" })), []);
});

// --- the Approvals tab (UI-SPEC "Approvals (UI-04)", Copywriting "Hold") -------------------------------------------

const before = (min: number, sec = 0) => new Date(2026, 9, 15, 14, 30, 0).getTime() - min * 60_000 - sec * 1000;

test("neededText: a resource's amount with its unit, singular for one; a null available is an em dash", () => {
  assert.equal(neededText(120, "units"), "120 units");
  assert.equal(neededText(1, "units"), "1 unit");
  assert.equal(neededText(3, "totes"), "3 totes");
  assert.equal(neededText(1, "totes"), "1 tote");
  assert.equal(neededText(1, "right"), "1 right");
  assert.equal(neededText(0, "right"), "0 rights");
  assert.equal(neededText(1250, "units"), "1,250 units");
  assert.equal(neededText(null, "units"), "—");
});

// --- the rejection on the lot's history (UI-SPEC "Header" item 5; Amendments A-6, A-7; 05-02 follow-up (b)) --------

const AT = "2026-10-16T13:30:00Z"; // 20:30 in Asia/Bangkok
const at = (type: string, day: number | null, payload: Record<string, unknown> = {}) => ({ ...logged(type, day, payload), at: AT });

// --- Export case (UI-SPEC "Header" item 6; contract Export; T-05-24) --------------------------------------------------

test("exportFilename: the case key plus .json", () => {
  assert.equal(exportFilename("SUS-19"), "SUS-19.json");
});

test("exportFilename: keeps only letters, digits and hyphens from the key, so no path or other character reaches the file name", () => {
  assert.equal(exportFilename("SUS 19/../x"), "SUS19x.json");
  assert.equal(exportFilename("a\\b:c*d?e\"f<g>h|i.j"), "abcdefghij.json");
});

test("exportFilename: a key with nothing left falls back to case.json", () => {
  assert.equal(exportFilename(""), "case.json");
  assert.equal(exportFilename("../.."), "case.json");
});

// --- Policy settings (UI-SPEC "Policy settings (D-06, D-07)") ---------------------------------------------------------
// The bounds below are test inputs in the API's shape; the page reads them from GET /api/settings.

const BOUNDS = {
  waste_price: { min: 0, max: 100, unit: "EUR/kg" },
  min_sellable_days: { min: 1, max: 7, unit: "days" },
  shortlist_step: { allowed: [0, 5, 10] },
};

test("boundsHelp: each field's min, max and unit from the bounds object, in the UI-SPEC wording", () => {
  assert.equal(boundsHelp("waste_price", BOUNDS), "Between 0 and 100 EUR/kg. A retailer policy, not a cash cost.");
  assert.equal(boundsHelp("min_sellable_days", BOUNDS), "Between 1 and 7 days on arrival.");
  const other = { ...BOUNDS, waste_price: { min: 0.5, max: 12.25, unit: "GBP/kg" }, min_sellable_days: { min: 2, max: 9, unit: "days" } };
  assert.equal(boundsHelp("waste_price", other), "Between 0.5 and 12.25 GBP/kg. A retailer policy, not a cash cost.");
  assert.equal(boundsHelp("min_sellable_days", other), "Between 2 and 9 days on arrival.");
});

test("settingsChanges: both fields, each at its edited value or its loaded value, as numbers", () => {
  const loaded = { waste_price: 0, min_sellable_days: 2, shortlist_step: 5 };
  assert.deepEqual(settingsChanges(loaded, {}), { waste_price: 0, min_sellable_days: 2 });
  assert.deepEqual(settingsChanges(loaded, { waste_price: "0.37" }), { waste_price: 0.37, min_sellable_days: 2 });
  assert.deepEqual(settingsChanges(loaded, { min_sellable_days: "4" }), { waste_price: 0, min_sellable_days: 4 });
  assert.deepEqual(settingsChanges(loaded, { waste_price: "1.05", min_sellable_days: "3" }), { waste_price: 1.05, min_sellable_days: 3 });
  const out = settingsChanges(loaded, { waste_price: "12.5" });
  assert.equal(typeof out.waste_price, "number");
  assert.equal(Number.isInteger(out.min_sellable_days), true);
  assert.equal("shortlist_step" in out, false, "shortlist_step is not shown and never sent");
});

test("settingsChanges: an empty field is never sent as 0 (the browser blocks it; the server would refuse it)", () => {
  const out = settingsChanges({ waste_price: 0.5, min_sellable_days: 2, shortlist_step: 0 }, { waste_price: " " });
  assert.equal(Number.isNaN(out.waste_price), true);
});

// --- the dataset mark (05.2 D-03, D-04 amended 2026-10-17) -------------------------------------------------------------

const PUBLIC_LABEL = "Public sales dataset · Simulated operational scenario · Scenario date: {date}";

test("datasetMark: the session's label with {date} filled from data_as_of by markDate", () => {
  assert.equal(datasetMark({ label: PUBLIC_LABEL, data_as_of: "2026-10-17" }),
    "Public sales dataset · Simulated operational scenario · Scenario date: Sat 17 Oct 2026");
});

test("datasetMark: a label without {date} is shown unchanged (the synthetic test set's own label)", () => {
  const generated = "Synthetic test set · generated data"; // any label with no {date}; the real one comes from the API
  assert.equal(datasetMark({ label: generated, data_as_of: null }), generated);
  assert.equal(datasetMark({ label: generated, data_as_of: "2026-10-17" }), generated);
});

test("datasetMark: with no data_as_of the date part is left out, never a placeholder or a browser date", () => {
  const text = datasetMark({ label: PUBLIC_LABEL, data_as_of: null });
  assert.equal(text, "Public sales dataset · Simulated operational scenario");
  assert.doesNotMatch(text, /\{date\}|Scenario date/);
});

test("PUBLIC_DATASET: the gate's line, where no session exists yet", () => {
  assert.equal(PUBLIC_DATASET, "Public sales dataset");
});

// --- About: the dataset's disclosure (05.2 DATA-16; D-03, D-04; CC BY 4.0 §3(a), §2(a)(6)) ---------------------------
// The same paragraphs stand in DATASET.md word for word (tests/data/test_dataset_notice.py reads this file as text).

test("ABOUT_SOURCE: creator, dataset link, paper, copyright, license link, no warranties, modified, not affiliated", () => {
  for (const part of [
    "FreshRetailNet-50K by Dingdong-Inc (Wang, Gu, Long, Li, Shen, Fu, Zhou, Jiang, 2025)",
    "https://huggingface.co/datasets/Dingdong-Inc/FreshRetailNet-50K", "arXiv:2505.16319",
    "© Dingdong-Inc, licensed under CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/",
    "provided as is, without warranties", "changed it as listed under Modifications",
    "ShelfShift is not affiliated with or endorsed by Dingdong-Inc.",
  ]) assert.ok(ABOUT_SOURCE.includes(part), part);
});

test("ABOUT_KINDS: Public, Assumption and Simulated in that order, each with what it covers; simulated is never observed", () => {
  assert.match(ABOUT_KINDS, /^Public: .+ Assumption: .+ Simulated: .+$/);
  for (const part of ["daily sales", "hours out of stock", "prices, costs, shelf life", "units factor",
    "labelled Assumption with its source", "stock on hand, lots, expiry dates and deliveries", "never shown as observed"]) {
    assert.ok(ABOUT_KINDS.includes(part), part);
  }
});

test("ABOUT_MODIFICATIONS and ABOUT_LIMITS: every change and every limit, in words", () => {
  for (const part of ["keeps its weekday", "units factor", "day-level fields", "hourly arrays", "display prefix",
    "unknown price", "Stock, lots, expiry and deliveries are simulated"]) {
    assert.ok(ABOUT_MODIFICATIONS.includes(part), part);
  }
  for (const part of ["front warehouses in China in 2024", "holiday flags follow that calendar", "understate demand",
    "stock is simulated", "locations and routes are assumed"]) {
    assert.ok(ABOUT_LIMITS.includes(part), part);
  }
});

test("ABOUT_KINDS, ABOUT_MODIFICATIONS and ABOUT_LIMITS type no figure: the shift, the factor and the dates are reference data", () => {
  // The dataset's name and the year its sales were recorded are the dataset card's facts, not figures of ours.
  for (const text of [ABOUT_KINDS, ABOUT_MODIFICATIONS, ABOUT_LIMITS]) {
    assert.doesNotMatch(text.replaceAll("FreshRetailNet-50K", "").replace("in China in 2024", ""), /\d/);
  }
});

test("textLinks: each web address becomes a link, trailing punctuation stays text", () => {
  assert.deepEqual(textLinks("See https://a.example/x/. Then (https://b.example/y), done."), [
    { text: "See ", href: null },
    { text: "https://a.example/x/", href: "https://a.example/x/" },
    { text: ". Then (", href: null },
    { text: "https://b.example/y", href: "https://b.example/y" },
    { text: "), done.", href: null },
  ]);
  assert.deepEqual(textLinks("No address here."), [{ text: "No address here.", href: null }]);
  assert.ok(textLinks(ABOUT_SOURCE).filter((s) => s.href).length === 2);
});

test("pagerText: 'Page N of M · T items'; a single page (or none) hides the pager (05.2 D-26)", () => {
  assert.equal(pagerText(2, 10, 37), "Page 2 of 4 · 37 items");
  assert.equal(pagerText(1, 25, 1234), "Page 1 of 50 · 1,234 items");
  assert.equal(pagerText(1, 10, 10), null);
  assert.equal(pagerText(1, 10, 0), null);
});

test("the product field is labelled 'Product code' (05.2 D-02)", () => {
  assert.equal(PRODUCT_CODE, "Product code");
});

test("the public board's filter words (05.2 D-26)", () => {
  assert.deepEqual(RISK_OPTIONS, [["all", "All"], ["waste", "Waste risk"], ["stock", "Stock at step"]]);
  assert.equal(STORE_PLACEHOLDER, "Type a store code");
});

test("the designed example's label and tooltip, word for word (05.2 D-18, D-40)", () => {
  assert.equal(DESIGNED_EXAMPLE, "Designed example");
  assert.equal(DESIGNED_TIP, "A record error placed on purpose to show verify before you move; see About");
  assert.equal(DESIGNED_TIP_LABEL, "More about the designed example");
});

// --- quick 261018-h9t: the case lot's own projected waste, and the plan totals' scope ---------------------------------

const BANNED = /\b(today|loss|cost|cash|demo)\b/i;

test("the trigger names the lot's own 18 units, its kg and its value at full price, projected to expire unsold", () => {
  const value = money(48.24, "USD");
  const line = triggerReason({ units: 18.27, kg: 9.14, value, lot: "S229-P38-2-r", by: "Sat 17 Oct" });
  assert.equal(line, `If nothing changes, 18 units of lot S229-P38-2-r (9 kg; ${value} at full price) are projected to `
    + "expire unsold by Sat 17 Oct, even with the store's markdown schedule.");
  assert.doesNotMatch(line ?? "", BANNED);
  // a part the detail cannot give is left out
  assert.equal(triggerReason({ units: 18.27, kg: null, value: null, lot: "S229-P38-2-r", by: null }),
    "If nothing changes, 18 units of lot S229-P38-2-r are projected to expire unsold, even with the store's markdown schedule.");
  assert.equal(triggerReason({ units: null, kg: 9.14, value, lot: "S229-P38-2-r", by: null }), null);
});

test("the risk fact and the analytics summary say projected to expire unsold, valued at full price", () => {
  const value = money(48.24, "USD");
  assert.equal(riskFact(18.27, value), `18 units · ${value} at full price, projected to expire unsold`);
  assert.equal(riskFact(18.27, null), "18 units, projected to expire unsold");
  assert.equal(wasteSummary(18.27, 1), "18 units in 1 lots are projected to expire unsold if nothing changes.");
  assert.equal(VALUE_AT_FULL_PRICE, "Value at full price");
  for (const x of [riskFact(18.27, value), wasteSummary(40, 3), VALUE_AT_FULL_PRICE]) assert.doesNotMatch(x, BANNED);
});

test("the totals line names the branches the plan totals cover, the source first", () => {
  const input = {
    lots: [{ branch: "S241" }, { branch: "S229" }, { branch: "S189" }, { branch: "S229" }],
    inbound: [{ branch: "S300" }], // a delivery alone does not put a branch in the engine's totals
  };
  assert.deepEqual(planBranches(input, "S229"), ["S229", "S189", "S241"]);
  assert.equal(totalsScope(planBranches(input, "S229")), "Sold and wasted figures are totals for S229, S189 and S241 together.");
  assert.equal(totalsScope(planBranches({ lots: [{ branch: "S229" }] }, "S229")), "Sold and wasted figures are totals for S229.");
  assert.deepEqual(planBranches(null, "S229"), ["S229"]);
  // a destination with a forecast and no stock yet is simulated, so it is named (Codex check h9t, finding 1)
  assert.deepEqual(planBranches({ lots: [{ branch: "S229" }], forecasts: [{ branch: "S229" }, { branch: "S017" }] }, "S229"),
    ["S229", "S017"]);
  assert.doesNotMatch(totalsScope(["S229", "S189"]), BANNED);
});

// --- Phase 7 request states, the check time and the exposure wording (07-17; 07-CONTEXT D-06, D-10, D-22) ------------

test("statePill: an open request reads the server's sentence word for word, in its tone", () => {
  const due = "Awaiting response · respond by 16 Oct 2026, 21:30";
  assert.deepEqual(statePill(req({ state: "OPEN", status_kind: "AWAITING", status: due })), { tone: "info", text: due });
  assert.deepEqual(statePill(req({ state: "OPEN", status_kind: "UNANSWERED", status: "Unanswered · Unverified" })),
    { tone: "warn", text: "Unanswered · Unverified" });
  assert.deepEqual(statePill(req({ state: "OPEN", status_kind: "PHOTO_PARTIAL", status: "Photo received · count missing · overdue" })),
    { tone: "warn", text: "Photo received · count missing · overdue" });
  assert.deepEqual(statePill(req({ state: "OPEN", status_kind: "TEXT_PARTIAL", status: "Reply received · count missing" })),
    { tone: "warn", text: "Reply received · count missing" });
});

test("statePill: Can't check and Superseded gray, a mixed-dates hold bad, a legacy EXPIRED row still reads", () => {
  assert.deepEqual(statePill(req({ state: "CANT_CHECK", status_kind: "CANT_CHECK", status: "Can't check" })),
    { tone: "gray", text: "Can't check" });
  assert.deepEqual(statePill(req({ state: "SUPERSEDED", status_kind: "SUPERSEDED", status: "Superseded" })),
    { tone: "gray", text: "Replaced by a recount" });
  assert.deepEqual(statePill(req({ state: "HELD", status_kind: "HELD", status: "Held" })),
    { tone: "bad", text: "Mixed dates · lot held" });
  assert.deepEqual(statePill(req({ state: "EXPIRED", status_kind: "EXPIRED", status: null })), { tone: "gray", text: "Expired" });
});

test("statePill: a state the page does not know is a gray pill with the server's text, never undefined", () => {
  const odd = req({ state: "ESCALATED" as Request["state"], status_kind: "ESCALATED", status: "Escalated to HQ" });
  assert.deepEqual(statePill(odd), { tone: "gray", text: "Escalated to HQ" });
  assert.deepEqual(statePill(req({ state: "ESCALATED" as Request["state"], status: null })), { tone: "gray", text: "ESCALATED" });
  assert.equal(typeof stateIcon(odd), "string");
  for (const state of ["OPEN", "ANSWERED", "CONFIRMED", "EXPIRED", "SUPERSEDED", "CANT_CHECK", "HELD"] as const) {
    assert.equal(typeof stateIcon(req({ state })), "string", state);
  }
});

test("responseNote: Phase 7 reads the server's `late`; a row without a real deadline compares sandbox days", () => {
  const a = answered({ day: 3 });
  assert.equal(responseNote(req({ state: "ANSWERED", due_at: "2026-10-16T14:30:00Z", late: true, answer: a })), "Answered after deadline");
  assert.equal(responseNote(req({ state: "CONFIRMED", due_at: "2026-10-16T14:30:00Z", late: true, answer: a })), "Answered after deadline");
  assert.equal(responseNote(req({ state: "ANSWERED", due_at: "2026-10-16T14:30:00Z", late: false, due_day: 1, answer: a })), null);
});

test("checkTimeObserved: no choice, Checked just now, a typed Bangkok time and a future one", () => {
  const now = Date.parse("2026-10-16T14:00:00Z"); // 21:00 in Bangkok
  assert.deepEqual(checkTimeObserved({ mode: "", at: "" }, "Asia/Bangkok", now), { error: CHECK_TIME.missing });
  assert.deepEqual(checkTimeObserved({ mode: "now", at: "2026-10-16T23:00" }, "Asia/Bangkok", now), { observed: "now" });
  assert.deepEqual(checkTimeObserved({ mode: "at", at: "2026-10-16T20:40" }, "Asia/Bangkok", now),
    { observed: "2026-10-16T13:40:00.000Z" });
  assert.deepEqual(checkTimeObserved({ mode: "at", at: "2026-10-16T21:00" }, "Asia/Bangkok", now),
    { observed: "2026-10-16T14:00:00.000Z" }); // equal to now is accepted, as the server accepts it
  assert.deepEqual(checkTimeObserved({ mode: "at", at: "2026-10-16T21:01" }, "Asia/Bangkok", now), { error: CHECK_TIME.later });
  assert.deepEqual(checkTimeObserved({ mode: "at", at: "" }, "Asia/Bangkok", now), { error: CHECK_TIME.missing });
});

test("the CheckTime words are the UI-SPEC's", () => {
  assert.equal(CHECK_TIME.legend, "When did you check?");
  assert.equal(CHECK_TIME.now, "Checked just now");
  assert.equal(CHECK_TIME.at, "At a date and time");
  assert.equal(CHECK_TIME.missing, "Choose when you checked: Checked just now, or a date and time.");
  assert.equal(CHECK_TIME.later, "The check time can't be later than now.");
  assert.equal(checkTimeLabel("Asia/Bangkok"), "Date and time checked (Bangkok time)");
});

test("no string in copy.ts calls the exposure 'at stake' (D-10)", () => {
  assert.doesNotMatch(readFileSync(new URL("./copy.ts", import.meta.url), "utf8"), /at stake/);
});

// --- 07-21: the stock decision's verification words (07-UI-SPEC surface 1; 07-CONTEXT D-10, D-12, D-23, D-36) --------
import * as v21 from "./copy.ts";

test("07-21: the request panel and the fact checks state the server's numbers, never a constant", () => {
  assert.equal(v21.requestsLeftLine(2, 2), "2 of 2 requests left on this version");
  assert.equal(v21.requestsLeftLine(0, 3), "0 of 3 requests left on this version");
  assert.equal(v21.requestsUsedLine(2), "2 of 2 requests used on this version.");
  assert.equal(v21.responseMinutesLine(60), "The branch has 60 minutes to respond.");
  assert.equal(v21.oneRequestLine(2), "One request · counts once toward the 2 per version");
  assert.equal(v21.SUPERSEDE_NOTE, "The earlier request becomes Superseded. A late answer to it is kept but can't be confirmed.");
  assert.deepEqual([v21.COUNT_STOCK, v21.CHECK_PRINTED_EXPIRY, v21.SEND_REQUEST, v21.DONT_SEND_REQUEST],
    ["Count stock", "Check printed expiry date", "Send request", "Don't send request"]);
});

test("07-21: the deadline's minutes are display words only: minutes, under one minute, overdue", () => {
  assert.equal(v21.minutesLeftText(42, false), "42 min left");
  assert.equal(v21.minutesLeftText(0, false), "Less than 1 min left");
  assert.equal(v21.minutesLeftText(5, true), "Overdue");
});

test("07-21: blocker lines name the fact or the lot and its owner; the Blocked label is the server's", () => {
  assert.equal(v21.blockerText({ kind: "required_check", fact_id: "f", title: "S1 · lot L-1: recorded 9 units", owner: "S1", blocked: null }),
    "Required: S1 · lot L-1: recorded 9 units · owner S1 manager");
  assert.equal(v21.blockerText({ kind: "required_check", fact_id: "f", title: "T", owner: "S1",
    blocked: "Blocked · stock/expiry verification required" }), "Required: T · owner S1 manager · Blocked · stock/expiry verification required");
  assert.equal(v21.blockerText({ kind: "lot_hold", lot: "L-2", owner: "S2" }), "Lot hold: lot L-2 · owner S2 manager");
  assert.equal(v21.cantApproveYet("X"), "Can't approve yet: X");
  assert.equal(v21.commitmentWaits("X"), "Your approval is recorded. Commitment waits for: X");
  assert.equal(v21.moreBlockers(1), "+1 more");
  assert.equal(v21.advisoryToAcknowledge(2), "2 advisory to acknowledge at approval");
});

test("07-21: nothing calls a plan with unresolved facts safe, verified or clear (VER-06)", () => {
  for (const s of [v21.NO_BLOCKING_CHECKS, v21.BLOCKS_COMMITMENT, v21.REQUIRED_CHIP, v21.ADVISORY_CHIP]) {
    assert.doesNotMatch(s, /\b(safe|verified|clear|cash|savings)\b/i, s);
  }
});

test("07-21: the ways forward: select's confirm sentence, the selected plan and the cancelled lines (D-23, D-25)", () => {
  assert.equal(v21.selectConfirmLine("Keep"), "Switch this stock decision to Keep? It goes through the normal feasibility "
    + "checks again, any pending approvals are cancelled, and approvals start over.");
  assert.equal(v21.selectOptionName("Markdown 20%"), "Select Markdown 20%");
  assert.equal(v21.ALSO_NEEDS_THIS_FACT, "Also needs this fact verified");
  assert.equal(v21.selectedPlanLine("Keep", "S017", "2026-10-18T07:05:00Z", "Asia/Bangkok"),
    "Selected plan · Keep · by S017 · 18 Oct 2026, 14:05 Bangkok time");
  assert.equal(v21.selectedPlanLine("Keep", null, null, "Asia/Bangkok"), "Selected plan · Keep");
  assert.equal(v21.cancelledLine("S017", "2026-10-18T07:05:00Z", "No stock", "Asia/Bangkok"),
    "Cancelled by S017 · 18 Oct 2026, 14:05 Bangkok time · No stock");
  assert.equal(v21.cancelledLine(null, "not a time", null, "Asia/Bangkok"), "Cancelled");
  assert.doesNotMatch(v21.selectConfirmLine("Keep"), /\bcase\b/); // 06.1 D-61: "stock decision" on screen
});
