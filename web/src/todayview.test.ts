// node --test (no dependency): Today's pure parts (06.1-UI-SPEC "Today"; D-18, D-22): where a planned minute sits on
// the 06:00–18:00 day strip, how markers that would overlap are stacked, and which server tile key a summary tile
// filters by. Planned times only: nothing here reads a clock.
import { test } from "node:test";
import assert from "node:assert/strict";
import { filterKey, laneElements, position, stackMarkers, type Lane } from "./todayview.ts";

test("position: 10:00 sits a third of the way along 06:00–18:00", () => {
  assert.deepEqual(position(10 * 60, 6 * 60, 18 * 60), { x: 1 / 3, outside: false });
});

test("position: the window's ends are inside it", () => {
  assert.deepEqual(position(6 * 60, 6 * 60, 18 * 60), { x: 0, outside: false });
  assert.deepEqual(position(18 * 60, 6 * 60, 18 * 60), { x: 1, outside: false });
});

test("position: a minute before the window clamps to 0 and is flagged outside", () => {
  assert.deepEqual(position(5 * 60, 6 * 60, 18 * 60), { x: 0, outside: true });
});

test("position: a minute after the window clamps to 1 and is flagged outside", () => {
  assert.deepEqual(position(20 * 60, 6 * 60, 18 * 60), { x: 1, outside: true });
});

test("stackMarkers: two markers closer than 4% apart sit at 28% and 74% of the lane height", () => {
  assert.deepEqual(stackMarkers([0.5, 0.52]), [0.28, 0.74]);
});

test("stackMarkers: lone markers stay in the middle of the lane", () => {
  assert.deepEqual(stackMarkers([0.1, 0.5, 0.9]), [0.5, 0.5, 0.5]);
});

test("stackMarkers: rows follow the input order, whatever order the positions come in", () => {
  assert.deepEqual(stackMarkers([0.52, 0.9, 0.5]), [0.74, 0.5, 0.28]);
});

test("stackMarkers: markers 5% apart do not overlap", () => {
  assert.deepEqual(stackMarkers([0.2, 0.25]), [0.5, 0.5]);
});

test("filterKey: each summary tile filters by the server's tile key", () => {
  assert.equal(filterKey("decisions_due"), "decisions_due");
  assert.equal(filterKey("approvals_waiting"), "approvals_waiting");
  assert.equal(filterKey("stock_checks"), "stock_checks");
  assert.equal(filterKey("transfers_at_risk"), "transfers_at_risk");
});

// --- the lanes (Task 2) -----------------------------------------------------------------------------------------------

const floor: Lane = {
  key: "floor", kind: "floor", branch: "S042",
  items: [
    { kind: "pick_and_pack", case_key: "SUS-19", case_id: "SUS-19", action_id: null, run_code: "R-02", qty: 110,
      start: { day: 3, min: 600 }, end: { day: 3, min: 720 }, conditional: true },
    { kind: "delivery", inbound_id: "BRD-303", sku: "P0007", qty: 230, day: 3, min: 360, status: "received" },
  ],
};

test("laneElements: a conditional pick-and-pack bar is dashed, labelled 'if sent', and placed from position()", () => {
  const [bar] = laneElements(floor, 3);
  assert.equal(bar.shape, "bar");
  assert.equal(bar.dashed, true);
  assert.equal(bar.label, "Pick & pack 110 · if sent");
  assert.ok(Math.abs(bar.left - (100 / 3)) < 1e-9);
  assert.ok(Math.abs(bar.width - (100 / 6)) < 1e-9);
  assert.equal(bar.caseKey, "SUS-19");
});

test("laneElements: a work order's pick-and-pack bar is solid", () => {
  const solid: Lane = { ...floor, items: [{ ...(floor.items[0] as Extract<Lane["items"][number], { kind: "pick_and_pack" }>),
    action_id: "SUS-19#1:T1", conditional: false }] };
  const [bar] = laneElements(solid, 3);
  assert.equal(bar.dashed, false);
  assert.equal(bar.label, "Pick & pack 110");
});

test("laneElements: a delivery is a marker at its planned minute with a planned accessible name", () => {
  const [, delivery] = laneElements(floor, 3);
  assert.equal(delivery.shape, "marker");
  assert.equal(delivery.left, 0);
  assert.equal(delivery.width, 0);
  assert.equal(delivery.label, "Delivery 06:00");
  assert.match(delivery.name, /, planned$/);
});

test("laneElements: a decision deadline is named '{key} decide by HH:MM, planned'", () => {
  const lane: Lane = { key: "decisions", kind: "decisions", branch: "S042",
    items: [{ kind: "decision_deadline", case_key: "SUS-19", case_id: "SUS-19", day: 3, min: 600 }] };
  const [m] = laneElements(lane, 3);
  assert.equal(m.label, "SUS-19 decide by 10:00");
  assert.equal(m.name, "SUS-19 decide by 10:00, planned");
  assert.equal(m.row, 0.5);
});

test("laneElements: two decision deadlines minutes apart stack at 28% and 74%", () => {
  const lane: Lane = { key: "decisions", kind: "decisions", branch: "S042", items: [
    { kind: "decision_deadline", case_key: "SUS-19", case_id: "SUS-19", day: 3, min: 600 },
    { kind: "decision_deadline", case_key: "SUS-20", case_id: "SUS-20", day: 3, min: 610 },
  ] };
  assert.deepEqual(laneElements(lane, 3).map((m) => m.row), [0.28, 0.74]);
});

test("laneElements: a run lane has its cutoff marker and its travel bar", () => {
  const lane: Lane = { key: "run:R-02", kind: "run", branch: "S042", run_code: "R-02", dest: "S017", cold_chain: true,
    items: [{ kind: "loading_cutoff", day: 3, min: 720 }, { kind: "travel", start: { day: 3, min: 750 }, end: { day: 3, min: 800 } }] };
  const [cutoff, travel] = laneElements(lane, 3);
  assert.equal(cutoff.label, "cutoff 12:00");
  assert.equal(cutoff.name, "R-02 loading cutoff 12:00, planned");
  assert.equal(travel.shape, "bar");
  assert.equal(travel.label, "departs 12:30 · arrives 13:20");
  assert.equal(travel.dashed, false);
});

test("laneElements: a time on the next day clamps to the strip's end and is flagged outside", () => {
  const lane: Lane = { key: "decisions", kind: "decisions", branch: "S042",
    items: [{ kind: "decision_deadline", case_key: "SUS-30", case_id: "SUS-30", day: 4, min: 600 }] };
  const [m] = laneElements(lane, 3);
  assert.equal(m.left, 100);
  assert.equal(m.outside, true);
});
