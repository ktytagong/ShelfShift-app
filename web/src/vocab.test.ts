// node --test (no dependency): the words every 06.1 page shares (06.1-UI-SPEC "Status vocabulary", "Times and dates",
// "History", "On-screen terms"). Hand derivations sit beside each expected string.
import { test } from "node:test";
import assert from "node:assert/strict";
import * as vocab from "./vocab.ts";
import {
  BOOKING_CONFIRMED, BY_SYSTEM, CHECK_AVAILABILITY, DECISION_DATE_LINE, HOLD_EXPIRED, NEXT_ACTION_LABEL, PROFILE_WHO,
  ROLE_LABEL, TERMS, cityOf, contextLine, historyLine, holdHeld, markLines, ownerText, profileWho, shortShipment,
  type HistoryEntry,
} from "./vocab.ts";
import * as copy from "./copy.ts";

const TZ = "Asia/Bangkok";

// A contract-shaped activity entry (04-API-CONTRACT "Product mode (06.1)": activity entries add `actor` and
// `scenario_date`). 19:48 UTC on 16 Oct is 02:48 on 17 Oct in Bangkok (UTC+7, no daylight saving).
const dispatched = (over: Partial<HistoryEntry> = {}): HistoryEntry => ({
  type: "dispatched", at: "2026-10-16T19:48:00Z", day: 3, scenario_date: "2026-10-17",
  actor: { account: "S042", role: "manager", branch: "S042" },
  payload: { qty: 104, lot_id: "L-0042" }, group: "decisions", ...over,
});

test("historyLine: a dispatch entry gives both clocks, the actor and the detail in words (D-19, D-58, D-63)", () => {
  assert.deepEqual(historyLine(dispatched(), TZ), {
    title: "Dispatch recorded by S042",
    meta: "Recorded 17 Oct 2026, 02:48 Bangkok time · Scenario date: Sat 17 Oct 2026",
    detail: "104 units dispatched from L-0042",
    group: "decisions",
  });
});

test("historyLine: both clocks show even when the recorded date and the scenario date match", () => {
  // 03:00 UTC on 17 Oct is 10:00 on 17 Oct in Bangkok: the same calendar date as the scenario date.
  const line = historyLine(dispatched({ at: "2026-10-17T03:00:00Z" }), TZ);
  assert.equal(line.meta, "Recorded 17 Oct 2026, 10:00 Bangkok time · Scenario date: Sat 17 Oct 2026");
});

test("historyLine: no actor reads 'by ShelfShift' (proposed); an HQ actor reads 'by HQ'", () => {
  assert.equal(BY_SYSTEM, "ShelfShift");
  assert.equal(historyLine(dispatched({ actor: null }), TZ).title, "Dispatch recorded by ShelfShift");
  assert.equal(historyLine(dispatched({ actor: { account: "HQ", role: "hq", branch: null } }), TZ).title,
    "Dispatch recorded by HQ");
});

test("historyLine: titles come from the map, an unknown type reads in words, never the raw key", () => {
  assert.equal(historyLine(dispatched({ type: "price_applied", payload: {} }), TZ).title,
    "Price change recorded by S042");
  const unknown = historyLine(dispatched({ type: "some_new_event", payload: {} }), TZ).title;
  assert.equal(unknown, "Some new event by S042");
  assert.doesNotMatch(unknown, /_/);
  // decision entries take their verb from the payload
  const decision = (d: string) => historyLine(dispatched({ type: "decision", payload: { decision: d } }), TZ).title;
  assert.equal(decision("approve"), "Approved by S042");
  assert.equal(decision("accept"), "Accepted by S042");
  assert.equal(decision("decline"), "Declined by S042");
});

test("historyLine: a 64-character hash shows its first 12 characters only (D-39)", () => {
  const hash = "0123456789abcdef".repeat(4); // 64 hex characters
  const line = historyLine(dispatched({ type: "new_version", payload: { rec_version: hash } }), TZ);
  assert.match(line.detail, /0123456789ab/);
  assert.doesNotMatch(line.detail, /0123456789abc/); // exactly 12, never 13 or the full 64
  assert.doesNotMatch(line.detail, /rec_version/); // words, never the raw key
});

test("cityOf names the city of an IANA zone, never typed", () => {
  assert.equal(cityOf("Asia/Bangkok"), "Bangkok");
  assert.equal(cityOf("America/New_York"), "New York");
});

// --- Task 2: the status vocabulary, the D-61 terms, the context line and the mark's two lines -----------------------

test("booking holds: a static time in the session's city, never minutes left (D-57)", () => {
  // 07:35 UTC is 14:35 in Bangkok (UTC+7).
  assert.equal(holdHeld("2026-10-17T07:35:00Z", TZ), "Capacity held until 14:35 Bangkok time.");
  assert.equal(holdHeld("not a time", TZ), null);
  assert.equal(HOLD_EXPIRED, "Capacity hold expired. Check availability and book again.");
  assert.equal(BOOKING_CONFIRMED, "Booking confirmed.");
  assert.equal(CHECK_AVAILABILITY, "Check availability");
});

test("contextLine: account · scenario date · role (D-08 as amended by D-63)", () => {
  assert.equal(contextLine({ account: "S042", role: "manager", data_as_of: "2026-10-17" }),
    "S042 · Scenario date: Sat 17 Oct 2026 · Store manager");
  assert.equal(contextLine({ account: "HQ", role: "hq", data_as_of: "2026-10-17" }),
    "HQ · Scenario date: Sat 17 Oct 2026 · HQ planner");
  // the synthetic set has no scenario date: the part is left out, never a placeholder
  assert.equal(contextLine({ account: "S042", role: "manager", data_as_of: null }), "S042 · Store manager");
  assert.deepEqual(ROLE_LABEL, { manager: "Store manager", hq: "HQ planner" });
});

test("markLines splits the session's mark at the scenario date (the label is reference data, never typed)", () => {
  const label = "Public sales dataset · Simulated operational scenario · Scenario date: {date}";
  assert.deepEqual(markLines({ label, data_as_of: "2026-10-17" }),
    ["Public sales dataset · Simulated operational scenario", "Scenario date: Sat 17 Oct 2026"]);
  // the same composition as copy.datasetMark: joined back, the two lines are its one line
  assert.equal(markLines({ label, data_as_of: "2026-10-17" }).join(" · "),
    copy.datasetMark({ label, data_as_of: "2026-10-17" }));
  // a label without {date} gives one line; a public label with no date drops the date line
  assert.deepEqual(markLines({ label: "A generated test set", data_as_of: null }), ["A generated test set"]);
  assert.deepEqual(markLines({ label, data_as_of: null }), ["Public sales dataset · Simulated operational scenario"]);
});

test("shortShipment and ownerText", () => {
  assert.equal(shortShipment(104, 110), "Short shipment · 104 of 110");
  assert.equal(ownerText({ account: "S042", role: "manager" }), "Owner: S042 manager");
  assert.equal(ownerText({ account: "HQ", role: "hq" }), "Owner: HQ planner");
});

test("TERMS are the D-61 on-screen terms", () => {
  assert.equal(TERMS.STOCK_DECISION, "Stock decision");
  assert.equal(TERMS.BRANCH_TRANSFER, "Branch transfer");
  assert.equal(TERMS.TRANSFER_WORK_ORDER, "Transfer work order");
  assert.equal(TERMS.STOCK_CHECK, "Stock check");
  assert.equal(TERMS.SCHEDULED_TRUCK_RUN, "Scheduled truck run");
  assert.equal(TERMS.DECISION_BOARD, "Decision board");
  assert.equal(TERMS.OPEN_STOCK_DECISION, "Open stock decision");
  assert.equal(TERMS.VIEW_STOCK_DECISION, "View stock decision");
});

test("NEXT_ACTION_LABEL names every next_action key of the contract", () => {
  // 04-API-CONTRACT "Card and detail keys": next_action.key, with the two stock-check keys 06.1-13 added
  const keys = ["evaluate", "take_ownership", "send", "check_availability", "approve", "accept", "prepare",
    "record_dispatch", "record_receipt", "record_price", "reconcile", "close", "answer_stock_check",
    "confirm_stock_check"];
  assert.deepEqual(Object.keys(NEXT_ACTION_LABEL).sort(), [...keys].sort());
  assert.equal(NEXT_ACTION_LABEL.take_ownership, "Take ownership");
  assert.equal(NEXT_ACTION_LABEL.check_availability, CHECK_AVAILABILITY);
  assert.equal(NEXT_ACTION_LABEL.close, "Close stock decision");
  assert.equal(NEXT_ACTION_LABEL.answer_stock_check, "Answer stock check");
  assert.equal(NEXT_ACTION_LABEL.confirm_stock_check, "Confirm count");
});

test("the decision date line and the profile menu's who block", () => {
  assert.equal(DECISION_DATE_LINE("2026-10-17"), "This stock decision is for scenario date Sat 17 Oct 2026.");
  assert.equal(PROFILE_WHO.manager("S042"),
    "Branch: S042 · can approve outgoing stock, accept incoming stock, request stock checks");
  assert.deepEqual(profileWho({ account: "S042", role: "manager" }), ["S042 manager",
    "Branch: S042 · can approve outgoing stock, accept incoming stock, request stock checks"]);
  assert.deepEqual(profileWho({ account: "HQ", role: "hq" }), ["HQ planner", PROFILE_WHO.hq()]);
});

test("05.2's labels are imported where needed, never redefined here", () => {
  const exported = new Set(strings(vocab));
  for (const label of [copy.NEEDS_EVALUATION, copy.TRANSFERS_UNAVAILABLE, copy.restrictedFootnote]) {
    assert.equal(exported.has(label), false, label);
  }
});

// Every string reachable from the module's exports (constants, arrays, records) plus what each helper composes for
// sample inputs: the scan the D-58, D-61 and removed-strings rules run over. A chip's tone and icon are names, not
// words on screen (the "today" icon draws a calendar), so they are left out.
function strings(v: unknown, seen = new Set<unknown>()): string[] {
  if (typeof v === "string") return [v];
  if (v === null || typeof v !== "object" || seen.has(v)) return [];
  seen.add(v);
  return Object.entries(v as Record<string, unknown>).flatMap(([k, x]) => (k === "tone" || k === "icon" ? [] : strings(x, seen)));
}

test("no exported string says today, yesterday, tomorrow, ago, minutes left, an internal term or the demo", () => {
  const composed = [
    historyLine(dispatched(), TZ), holdHeld("2026-10-17T07:35:00Z", TZ), contextLine({ account: "S042",
      role: "manager", data_as_of: "2026-10-17" }), shortShipment(1, 2), ownerText({ account: "HQ", role: "hq" }),
    DECISION_DATE_LINE("2026-10-17"), PROFILE_WHO.manager("S042"), PROFILE_WHO.hq(),
    ...["opened", "prepared", "decision", "evidence_requested", "evidence_answered"]
      .map((type) => historyLine(dispatched({ type, payload: { plan: "md20" } }), TZ)),
  ];
  const all = [...strings(vocab), ...strings(composed)];
  assert.ok(all.length > 60, `scanned ${all.length} strings`);
  const exempt = new Set([TERMS.TRANSFER_WORK_ORDER, vocab.MARKDOWN_WORK_ORDER]);
  for (const s of all) {
    assert.doesNotMatch(s, /\b(today|yesterday|tomorrow)\b/i, s);
    for (const bad of [" ago", "min left", "Case ", "Evidence request", "Next day", "sandbox", "demo"]) {
      assert.equal(s.includes(bad), false, `${JSON.stringify(s)} holds ${JSON.stringify(bad)}`);
    }
    if (!exempt.has(s)) assert.equal(s.includes("Work order "), false, s);
  }
});


test("each series names its validated provider codes, including unknown and empty lists", async () => {
  const {providerShort} = await import("./vocab.ts");
  assert.equal(providerShort(["ai_forecast", "arima_plus"]), "TimesFM 2.5 + ARIMA_PLUS");
  assert.equal(providerShort(["new_provider"]), "new_provider");
  assert.equal(providerShort([]), "");
});

test("Assistant draft history names only authorized provenance and stays read-only", () => {
  const event = dispatched({ type: "evidence_drafted", actor: null, group: "Assistant", payload: {
    draft_id: "D-long-id", class: "Required", owner: "S042", to_branch: "S017", sku: "P007",
    lot_ids: ["L-0042"], model_id: "configured-model", assistant_version: "prior-instructions",
    rec_version: "0123456789abcdef".repeat(4), plan_key: "keep",
    question: "Private question sentinel", answer_text: "Private answer sentinel",
    tool_results: [{ result: "Private source sentinel" }], original_text: "Unprojected draft sentinel",
  } });
  const line = historyLine(event, TZ);
  assert.equal(line.title, "Stock check drafted by ShelfShift");
  for (const text of ["Drafted by Gemini", "Read-only record", "Required", "S017", "P007", "L-0042", "D-long-id",
    "configured-model", "prior-instructions", "0123456789ab", "keep"]) assert.ok(line.detail.includes(text), text);
  assert.doesNotMatch(line.detail, /Private|Unprojected|0123456789abc/);
  assert.equal(line.group, "Assistant");
  assert.equal(historyLine({ ...event, group: "Data" }, TZ).group, "Data");
});

test("partial draft metadata is Missing and a person-sent request retains its server Data group", () => {
  const missing = historyLine(dispatched({ type: "evidence_drafted", actor: null, at: "", scenario_date: null,
    payload: {}, group: "Assistant" }), TZ);
  assert.match(missing.detail, /Draft: Missing/);
  assert.match(missing.detail, /Model: Missing/);
  assert.match(missing.detail, /Lots: Missing/);
  assert.match(missing.meta, /Recorded time: Missing/);
  assert.match(missing.meta, /Scenario date: Missing/);
  assert.doesNotMatch(missing.detail, /\b0\b/);
  const sent = historyLine(dispatched({ type: "evidence_requested", group: "Data", payload: {
    request_id: "REQ-001", to_branch: "S017", lot_ids: ["L-0042"], question: "Private sentinel",
  } }), TZ);
  assert.equal(sent.group, "Data");
  assert.match(sent.detail, /Sent by S042 manager · Recorded 17 Oct 2026, 02:48 Bangkok time/);
  assert.match(sent.detail, /REQ-001/);
  assert.doesNotMatch(sent.detail, /Private/);
});

test("history empty/count/disclosure grammar handles zero, one, many and complete long text", () => {
  assert.equal(vocab.ASSISTANT_HISTORY_EMPTY, "No assistant activity for this stock decision yet.");
  assert.equal(vocab.historyCount(0), "0 events");
  assert.equal(vocab.historyCount(1), "1 event");
  assert.equal(vocab.historyCount(4), "4 events");
  assert.equal(vocab.historySummary("Short detail"), "Short detail");
  const long = "Long dataset/result identifier ".repeat(8);
  assert.equal(vocab.historySummary(long), long.slice(0, 90) + "…");
  assert.equal(vocab.historySummary("x".repeat(90)), "x".repeat(90));
});
