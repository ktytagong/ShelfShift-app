// node --test: the field report words (07-20; 07-UI-SPEC surfaces 4 and 5): kind and field labels, the allowance line
// from the API's numbers (D-20), the state chips, the one-line summary's 90-character cut, and the filters' query.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FIELD_LABEL, KIND_LABEL, allowanceLine, eventsQuery, fieldsBody, fieldsOf, kindsFor, missingOf, moreAffected,
  stateChip, summaryLine, type Report,
} from "./reportcopy.ts";

const TZ = "Asia/Bangkok";

test("the kind labels are the UI-SPEC's, and HQ files transport only", () => {
  assert.deepEqual(Object.values(KIND_LABEL), [
    "Storage capacity", "Demand event", "Transport", "Stock count", "Printed expiry date", "Condition", "Not relevant"]);
  assert.deepEqual(kindsFor("hq"), ["TRANSPORT"]);
  assert.equal(kindsFor("manager").length, 6);
  assert.equal(kindsFor("manager").includes("NOT_RELEVANT"), false);
});

test("field labels use the store app's terms: no bare 'Run' (G3)", () => {
  assert.equal(FIELD_LABEL.run_code, "Scheduled truck run");
  assert.equal(FIELD_LABEL.free_totes, "Available totes");
  assert.equal(FIELD_LABEL.multiplier, "Expected demand multiplier");
});

test("the allowance line comes from the API's numbers", () => {
  const a = { reports_left: 17, reports_limit: 20, files_left: 9, files_limit: 10, resets: "2026-10-18T17:00:00+00:00" };
  assert.equal(allowanceLine(a, TZ), "17 of 20 reports left today · 9 with a file");
  assert.equal(allowanceLine({ ...a, reports_left: 0 }, TZ),
    "0 of 20 reports left today for this workspace · resets at 00:00 Bangkok time");
  assert.equal(allowanceLine({ ...a, reports_left: 0, resets: null }, TZ), "0 of 20 reports left today for this workspace");
});

test("a transport report shows and sends the fields of what happened only", () => {
  assert.deepEqual(fieldsOf("TRANSPORT", { what: "DELAY" }), ["run_code", "what", "delay_hours", "applies_on"]);
  assert.deepEqual(fieldsBody("TRANSPORT", { run_code: " R-1 ", what: "DELAY", delay_hours: "2", amount: "9" }, false),
    { run_code: "R-1", what: "DELAY", delay_hours: 2, applies_on: null });
  assert.deepEqual(missingOf("TRANSPORT", { run_code: "R-1", what: "QUOTE", amount: "9", pricing_basis: "PER_TRIP",
    includes_handling: "UNKNOWN", valid_until: "2026-10-18", applies_on: "2026-10-18" }), ["includes_handling"]);
  assert.deepEqual(fieldsBody("CONDITION", { lot_id: "L1", condition: "DAMAGE" }, true), { lot_id: "L1", condition: "DAMAGE", hold: true });
  assert.deepEqual(fieldsBody("CONDITION", { lot_id: "L1", condition: "DISPLAY" }, true), { lot_id: "L1", condition: "DISPLAY" });
});

test("a blank count is null, never 0", () => {
  assert.deepEqual(fieldsBody("STOCK_COUNT", { lot_id: "L1", shelf_units: "", backroom_units: "0" }, false),
    { lot_id: "L1", shelf_units: null, backroom_units: 0 });
});

const base = {
  state: "READ", owner: { role: "manager", branch: "S042" }, fact_label: null, confirmed_by: null, confirmed_at: null,
  dismissed_by: null, dismissed_at: null, withdrawn_at: null,
} as unknown as Report;

test("the state chips say who acts and who acted", () => {
  assert.equal(stateChip(base, TZ).text, "Awaiting confirmation by S042 manager");
  assert.equal(stateChip({ ...base, owner: { role: "hq", branch: null } }, TZ).text, "Awaiting confirmation by HQ planner");
  assert.equal(stateChip({ ...base, state: "DISMISSED", dismissed_by: "HQ", dismissed_at: "2026-10-16T13:30:00Z" }, TZ).text,
    "Dismissed by HQ planner · 16 Oct 2026, 20:30");
  assert.equal(stateChip({ ...base, state: "WITHDRAWN", withdrawn_at: "2026-10-16T13:30:00Z" }, TZ).text,
    "Withdrawn by reporter · 16 Oct 2026, 20:30");
  assert.equal(stateChip({ ...base, state: "CONFIRMED", fact_label: "Reported by S042 manager · x · field report" }, TZ).text,
    "Reported by S042 manager · x · field report");
});

test("the summary is one line of at most 90 characters", () => {
  assert.equal(summaryLine({ kind: "CAPACITY", fields: { zone: "chilled", free_totes: 6, valid_from: "2026-10-16", valid_to: "2026-10-17" } }, "USD"),
    "Available chilled totes: 6 · Fri 16 Oct – Sat 17 Oct");
  const long = summaryLine({ kind: "TRANSPORT", fields: { run_code: "R".repeat(120), what: "DELAY", delay_hours: 2, applies_on: null } }, "USD");
  assert.equal(long.length, 90);
  assert.ok(long.endsWith("…"));
});

test("affected stock decisions beyond the five shown, in the store app's terms (G3)", () => {
  assert.equal(moreAffected(1), "and 1 more affected stock decision");
  assert.equal(moreAffected(3), "and 3 more affected stock decisions");
});

test("the filters are the server's query", () => {
  assert.equal(eventsQuery({}), "/api/signals");
  assert.equal(eventsQuery({ mine: true, kind: "CAPACITY", store: " S042 ", case: "", page: 1 }),
    "/api/signals?mine=1&kind=CAPACITY&store=S042");
  assert.equal(eventsQuery({ mine: false, page: 2, pageSize: 1 }), "/api/signals?page=2&page_size=1");
});
