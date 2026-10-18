// node --test (no dependency): Stock & lots' pure parts (06.1-UI-SPEC "Stock & lots"; D-15, D-31, D-56): the cover bar
// against the forecast to expiry, the row's decision action (the server's `action`, never derived), and the cell texts.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  NO_FORECAST, NO_STOCK, NOT_COUNTED, countText, coverParts, discrepancyText, expiryText, lotHistory, markdownText,
  rowAction,
} from "./stockview.ts";
import { historyFilter } from "./bars.ts";
import { shortDate } from "./format.ts";

test("coverParts: the forecast to expiry fills the recorded track and the rest is surplus", () => {
  assert.deepEqual(coverParts({ qty: 300, forecast_to_expiry: 120 }), { fill: 0.4, surplus: 0.6, text: "120 of 300 sell by expiry" });
});

test("coverParts: a forecast above the stock fills the whole track and never claims more than the stock", () => {
  assert.deepEqual(coverParts({ qty: 40, forecast_to_expiry: 75.6 }), { fill: 1, surplus: 0, text: "40 of 40 sell by expiry" });
});

test("coverParts: expected units are whole, recorded stock keeps one decimal", () => {
  assert.equal(coverParts({ qty: 12.5, forecast_to_expiry: 6.4 }).text, "6 of 12.5 sell by expiry");
});

test("coverParts: no stock on record draws no bar", () => {
  assert.deepEqual(coverParts({ qty: 0, forecast_to_expiry: 120 }), { fill: 0, surplus: 0, text: NO_STOCK });
  assert.equal(NO_STOCK, "No stock on record");
});

test("coverParts: a missing forecast is said, never drawn as zero demand", () => {
  assert.deepEqual(coverParts({ qty: 300, forecast_to_expiry: null }), { fill: 0, surplus: 0, text: NO_FORECAST });
});

test("rowAction follows the server's action: view a live decision, open a flag, nothing otherwise", () => {
  assert.deepEqual(rowAction({ case_key: "SUS-19", flag_key: null, action: "view" }),
    { label: "View stock decision", href: "#/cases/SUS-19", primary: false });
  assert.deepEqual(rowAction({ case_key: null, flag_key: "SUS-31", action: "open" }),
    { label: "Open stock decision", href: "#/cases/SUS-31", primary: true });
  assert.equal(rowAction({ case_key: null, flag_key: null, action: null }), null);
});

test("rowAction never offers Open for a lot whose server action is view, and needs the key it links to", () => {
  assert.equal(rowAction({ case_key: "SUS-19", flag_key: "SUS-31", action: "view" })?.href, "#/cases/SUS-19");
  assert.equal(rowAction({ case_key: null, flag_key: null, action: "view" }), null);
  assert.equal(rowAction({ case_key: null, flag_key: null, action: "open" }), null);
  assert.equal(rowAction({ case_key: "A/B", flag_key: null, action: "view" })?.href, "#/cases/A%2FB");
});

test("shortDate and expiryText: the compact scenario date and how the expiry is known", () => {
  assert.equal(shortDate("2026-10-17"), "Sat 17 Oct");
  assert.equal(expiryText({ expiry_date: "2026-10-18", expiry_source: "PRINTED" }), "Sun 18 Oct · printed");
  assert.equal(expiryText({ expiry_date: "2026-10-18", expiry_source: "ESTIMATED" }), "Sun 18 Oct · estimated");
  assert.equal(expiryText({ expiry_date: null, expiry_source: "PRINTED" }), "—");
});

test("countText: counted on the scenario date, or how many days since the last count", () => {
  assert.equal(countText({ last_verified_day: 5, last_verified_date: "2026-10-17" }, 5), "Counted Sat 17 Oct");
  assert.equal(countText({ last_verified_day: 2, last_verified_date: "2026-10-14" }, 5), "Not counted for 3 days");
  assert.equal(countText({ last_verified_day: 4, last_verified_date: "2026-10-16" }, 5), "Not counted for 1 day");
  assert.equal(countText({ last_verified_day: null, last_verified_date: null }, 5), NOT_COUNTED);
});

test("markdownText and discrepancyText", () => {
  assert.equal(markdownText({ day: 6, date: "2026-10-18", pct: 20 }), "20% on Sun 18 Oct");
  assert.equal(markdownText(null), "—");
  assert.equal(discrepancyText(0), null);
  assert.equal(discrepancyText(1), "1 open");
  assert.equal(discrepancyText(2), "2 open");
});

test("lotHistory: decisions and movements with both clocks, grouped for the All · Decisions · Data filter", () => {
  const lines = lotHistory([
    { type: "stock_decision", id: "c1", case_id: "c1", key: "SUS-19", state: "OPEN", closed_as: null,
      recorded_at: "2026-10-16T19:45:00Z", scenario_day: 5, scenario_date: "2026-10-17" },
    { type: "movement", id: "m-1", movement_id: "m-1", kind: "DISPATCH", case_id: "c1", key: "SUS-19", action_id: "a1",
      lot_id: "L-1", qty: 12, damaged_qty: 0, condition: null, reason: null,
      actor: { account: "S042", role: "manager", branch: "S042" },
      recorded_at: "2026-10-16T19:48:00Z", scenario_day: 5, scenario_date: "2026-10-17" },
  ], "Asia/Bangkok");
  assert.deepEqual(lines, [
    { title: "Stock decision SUS-19 opened", meta: "Recorded 17 Oct 2026, 02:45 Bangkok time · Scenario date: Sat 17 Oct 2026",
      detail: "", group: "Decisions" },
    { title: "Dispatch recorded by S042", meta: "Recorded 17 Oct 2026, 02:48 Bangkok time · Scenario date: Sat 17 Oct 2026",
      detail: "12 units dispatched from L-1 · SUS-19", group: "Data" },
  ]);
  // The drawer's Decisions and Data filters (bars.historyFilter, the API's group names) each keep their own line.
  assert.deepEqual(historyFilter(lines, "Decisions").map((l) => l.title), ["Stock decision SUS-19 opened"]);
  assert.deepEqual(historyFilter(lines, "Data").map((l) => l.title), ["Dispatch recorded by S042"]);
});

test("lotHistory: a closed decision says how it closed; a receipt names what was received and damaged", () => {
  const [closed, receipt] = lotHistory([
    { type: "stock_decision", id: "c2", case_id: "c2", key: "SUS-07", state: "CLOSED", closed_as: "COMPLETED",
      recorded_at: null, scenario_day: 3, scenario_date: "2026-10-15" },
    { type: "movement", id: "m-2", movement_id: "m-2", kind: "RECEIPT", case_id: "c2", key: null, action_id: "a2",
      lot_id: "L-2", qty: 10, damaged_qty: 2, condition: "damaged", reason: "Crushed tote",
      actor: { account: "S017", role: "manager", branch: "S017" },
      recorded_at: "2026-10-16T20:00:00Z", scenario_day: 5, scenario_date: "2026-10-17" },
  ], "Asia/Bangkok");
  assert.equal(closed.detail, "Completed");
  assert.equal(closed.meta, "Scenario date: Thu 15 Oct 2026"); // a missing recorded time is never invented
  assert.equal(receipt.title, "Receipt recorded by S017");
  assert.equal(receipt.detail, "10 units received · 2 units damaged · Crushed tote");
});
