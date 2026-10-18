import { test } from "node:test";
import assert from "node:assert/strict";
import { confirmOptions, readingLines, shouldRetryOnOpen } from "./answerview.ts";
import type { AnswerReading, Request } from "./api.ts";

const reading = (over: Partial<AnswerReading> = {}) => ({ status: "DONE", attempt: 1, pending_counts: { L: { shelf_units: 0, backroom_units: 0 } },
  printed_dates: null, observations: [], conflicts: [], not_checked: null, note: false, after_confirmation: false, ...over } as AnswerReading);
const request = (counts = { L: { shelf_units: 4, backroom_units: 2 } }) => ({ lot_ids: ["L"], to_branch: "Store", part: "count",
  answer: { fields: { counts } } } as unknown as Request);

test("complete conflicting sources stay separate, with no default; matching sources need no choice", () => {
  assert.deepEqual(confirmOptions(request(), reading()).map((x) => x.source), ["typed", "reading"]);
  assert.deepEqual(confirmOptions(request(), null).map((x) => x.source), ["typed"]);
  assert.deepEqual(confirmOptions(request({ L: { shelf_units: 0, backroom_units: 0 } }), reading()).map((x) => x.source), ["typed"]);
  const partial = request({ L: { shelf_units: null, backroom_units: null } } as never);
  assert.deepEqual(confirmOptions(partial, reading()).map((x) => x.source), ["reading"]);
  assert.match(confirmOptions(partial, reading())[0].label, /Pending manager check/);
  assert.deepEqual(confirmOptions(partial, reading({ after_confirmation: true })), []);
});

test("partial lots and complementary sources cannot produce a complete confirmation", () => {
  const r = request({ L: { shelf_units: 4, backroom_units: null } } as never);
  assert.deepEqual(confirmOptions(r, reading({ pending_counts: { L: { shelf_units: null, backroom_units: 2 } } })), []);
  r.lot_ids.push("OTHER");
  assert.deepEqual(confirmOptions(r, reading()), []);
});

test("retry requires an explicit missing or interrupted server value, the manager, evidence and an unused attempt", () => {
  assert.equal(shouldRetryOnOpen(null, true, true, false), true);
  assert.equal(shouldRetryOnOpen(undefined, true, true, false), false);
  assert.equal(shouldRetryOnOpen(reading({ status: "INTERRUPTED" }), true, true, false), true);
  for (const status of ["FAILED", "REFUSED", "DONE", "GENERATING"] as const)
    assert.equal(shouldRetryOnOpen(reading({ status }), true, true, false), false);
  assert.equal(shouldRetryOnOpen(reading({ status: "INTERRUPTED", attempt: 2 }), true, true, false), false);
  assert.equal(shouldRetryOnOpen(null, false, true, false), false);
  assert.equal(shouldRetryOnOpen(null, true, false, false), false);
  assert.equal(shouldRetryOnOpen(null, true, true, true), false);
});

test("lines preserve observation, per-lot value, server sentence, conflict and instruction-note order", () => {
  const lines = readingLines(reading({ observations: ["visible slot empty"], not_checked: "Server sentence exactly.",
    conflicts: [{ field: "counts.L.shelf_units", typed: 4, read: 0 }], note: true }));
  assert.deepEqual(lines.map((x) => x.kind), ["observation", "value", "not_checked", "conflict", "note"]);
  assert.equal(lines[2].text, "Server sentence exactly.");
  assert.match(lines[1].text, /Lot L/);
});
