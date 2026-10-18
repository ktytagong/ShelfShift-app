// node --test (no dependency): the arithmetic every 06.1 bar draws (step bars, quantity bars, scaled bars, chart tables).
import { test } from "node:test";
import assert from "node:assert/strict";
import { historyFilter, qtyParts, scaled, segmentStates, tableRows } from "./bars.ts";

test("segmentStates marks the steps before the current one done, the current one current and the rest later", () => {
  assert.deepEqual(segmentStates(4, 9), ["done", "done", "done", "current", "later", "later", "later", "later", "later"]);
  const last = segmentStates(9, 9);
  assert.equal(last.length, 9);
  assert.deepEqual(last.map((s, i) => (s === "current" ? i : -1)).filter((i) => i >= 0), [8]);
  assert.deepEqual(segmentStates(0, 9), Array(9).fill("later")); // a flag before evaluation: no step yet
  assert.throws(() => segmentStates(10, 9), RangeError);
  assert.throws(() => segmentStates(-1, 9), RangeError);
  assert.throws(() => segmentStates(2.5, 9), RangeError);
});

// The server never sends Options as the step (06.1-01 contract: "OPTIONS is always done once a recommendation exists"),
// so with a recommendation the Options segment is done above step 3 and is never drawn as the current step.
test("segmentStates with optionsDone marks Options (step 3) done above step 3 and never current", () => {
  assert.deepEqual(segmentStates(5, 9, true), ["done", "done", "done", "done", "current", "later", "later", "later", "later"]);
  assert.equal(segmentStates(4, 9, true)[2], "done");
  assert.equal(segmentStates(3, 9, true)[2], "done");
  assert.ok(segmentStates(3, 9, true).every((s) => s !== "current"));
  assert.deepEqual(segmentStates(1, 9, true), ["current", "later", "done", "later", "later", "later", "later", "later", "later"]);
  assert.equal(segmentStates(3, 9)[2], "current"); // without a recommendation the flag changes nothing
  assert.deepEqual(segmentStates(0, 9, true), Array(9).fill("later")); // a flag before evaluation has no recommendation
});

test("qtyParts splits a short, partly received transfer into whole units and fractions of the committed quantity", () => {
  const p = qtyParts({ committed: 110, dispatched: 104, received: 100, damaged: 0 });
  assert.deepEqual(p.units, { committed: 110, dispatched: 104, received: 100, damaged: 0, missing: 4, short: 6 });
  assert.deepEqual(p.widths, { dispatched: 104 / 110, received: 100 / 110, damaged: 0, missing: 4 / 110, short: 6 / 110 });
});

test("qtyParts with nothing dispatched gives only the committed track", () => {
  const p = qtyParts({ committed: 110, dispatched: null, received: null, damaged: null });
  assert.deepEqual(p.units, { committed: 110, dispatched: null, received: null, damaged: null, missing: null, short: null });
  assert.deepEqual(p.widths, { dispatched: 0, received: 0, damaged: 0, missing: 0, short: 0 });
});

test("qtyParts in transit knows the short part but not yet the missing one; damaged units are their own part", () => {
  const moving = qtyParts({ committed: 110, dispatched: 104, received: null, damaged: null });
  assert.equal(moving.units.missing, null);
  assert.equal(moving.units.short, 6);
  assert.equal(moving.widths.received, 0);
  const hurt = qtyParts({ committed: 10, dispatched: 10, received: 7, damaged: 2 });
  assert.deepEqual(hurt.units, { committed: 10, dispatched: 10, received: 7, damaged: 2, missing: 1, short: 0 });
});

test("scaled divides by the largest value, and a zero maximum gives zeros, never NaN", () => {
  assert.deepEqual(scaled([2, 5, 0], 5), [0.4, 1, 0]);
  assert.deepEqual(scaled([2, 5, 0]), [0.4, 1, 0]); // the maximum defaults to the largest value
  assert.deepEqual(scaled([0, 0], 0), [0, 0]);
  assert.deepEqual(scaled([], 0), []);
});

// The groups are the API's own (GET /api/cases/{id}/activity: "Decisions", "Data", "Assistant"), matched exactly: the
// client never re-maps a server group (06.1-28; the lower-case keys before it made both filters empty).
test("historyFilter keeps the entries of the API's group, or every entry in the given order", () => {
  const entries = [
    { title: "Opened", group: "Decisions" }, { title: "Count recorded", group: "Data" },
    { title: "Stock check requested", group: "Assistant" },
    { title: "Sent for approval", group: "Decisions" }, { title: "Receipt recorded", group: "Data" },
  ];
  assert.deepEqual(historyFilter(entries, "Data").map((e) => e.title), ["Count recorded", "Receipt recorded"]);
  assert.deepEqual(historyFilter(entries, "Decisions").map((e) => e.title), ["Opened", "Sent for approval"]);
  assert.deepEqual(historyFilter(entries, "All"), entries);
});

test("historyFilter matches the group name exactly, never a lower-cased or re-mapped one", () => {
  const entries = [{ title: "Opened", group: "decisions" }, { title: "Count recorded", group: "data" }];
  assert.deepEqual(historyFilter(entries, "Decisions"), []);
  assert.deepEqual(historyFilter(entries, "Data"), []);
});

test("tableRows turns a chart's labels and series into a Date column and one column per series", () => {
  const t = tableRows({ labels: ["Mon", "Tue"], series: [{ name: "Units", values: [3, 4] }, { name: "Hours out of stock", values: [0, 2] }] });
  assert.deepEqual(t.head, ["Date", "Units", "Hours out of stock"]);
  assert.deepEqual(t.rows, [["Mon", 3, 0], ["Tue", 4, 2]]);
});

test("tableRows gives a band series two columns, low and high, and names the first column when asked", () => {
  const t = tableRows({
    labels: ["Wed"],
    series: [{ name: "Forecast", values: [12] }, { name: "Forecast 80% range", low: [9], high: [15] }],
  }, "Day");
  assert.deepEqual(t.head, ["Day", "Forecast", "Forecast 80% range low", "Forecast 80% range high"]);
  assert.deepEqual(t.rows, [["Wed", 12, 9, 15]]);
});

test("tableRows keeps a missing value as null, never zero", () => {
  const t = tableRows({ labels: ["Mon", "Tue"], series: [{ name: "Units", values: [3] }] });
  assert.deepEqual(t.rows, [["Mon", 3], ["Tue", null]]);
});

test("Assistant filtering follows server groups, including person-sent Data records, without regrouping", () => {
  const entries = [
    { type: "evidence_drafted", group: "Assistant", id: "draft" },
    { type: "evidence_requested", group: "Data", id: "person-sent" },
    { type: "evidence_drafted", group: "Data", id: "server-classified-data" },
    { type: "opened", group: "assistant", id: "different-case" },
  ];
  assert.deepEqual(historyFilter(entries, "Assistant").map(e => e.id), ["draft"]);
  assert.deepEqual(historyFilter(entries, "Data").map(e => e.id), ["person-sent", "server-classified-data"]);
  assert.deepEqual(historyFilter([], "Assistant"), []);
  assert.deepEqual(historyFilter(entries, "All"), entries);
});
