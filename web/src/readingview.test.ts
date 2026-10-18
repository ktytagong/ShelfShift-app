import { test } from "node:test";
import assert from "node:assert/strict";

// Missing public behavior is asserted inside each body, not treated as a
// successful RED caused by a module-load or collection failure.
const view = await import("./readingview.ts").catch((error: NodeJS.ErrnoException) => {
  if (error.code === "ERR_MODULE_NOT_FOUND" && error.message.includes("readingview.ts")) return {};
  throw error;
}) as Record<string, (...args: any[]) => any>;
const call = (name: string, ...args: unknown[]) => view[name]?.(...args);
const suggested = (field: string, value: unknown, extra = {}) => ({field, suggestion: {value, state: "suggested"}, ...extra});

test("either evidence input chooses the reading path and empty evidence stays manual", () => {
  assert.equal(call("sheetPath", "The chiller is broken", null), "ai");
  assert.equal(call("sheetPath", "", {name: "photo.png"}), "ai");
  assert.equal(call("sheetPath", "  ", null), "manual");
});

test("typed conflicts preserve the typed value until an explicit row choice", () => {
  const row = suggested("zone", "chilled", {typed: "ambient"});
  assert.deepEqual(call("rowState", row), {state: "conflict", value: "ambient", needsReview: true, reviewed: false});
  assert.deepEqual(call("rowState", {...row, choice: "keep"}), {state: "typed", value: "ambient", needsReview: true, reviewed: true});
  assert.deepEqual(call("rowState", {...row, choice: "accept"}), {state: "accepted", value: "chilled", needsReview: true, reviewed: true});
});

test("matching typed values and zero need no extra review", () => {
  assert.deepEqual(call("rowState", suggested("free_totes", 0, {typed: 0})),
    {state: "typed", value: 0, needsReview: false, reviewed: true});
  assert.deepEqual(call("rowState", {field: "zone", typed: "ambient"}),
    {state: "typed", value: "ambient", needsReview: false, reviewed: true});
});

test("suggested cleared restored and edited values have explicit review states", () => {
  const row = suggested("free_totes", 6);
  assert.deepEqual(call("rowState", row), {state: "suggested", value: 6, needsReview: true, reviewed: false});
  assert.deepEqual(call("rowState", {...row, choice: "clear"}), {state: "cleared", value: null, needsReview: true, reviewed: true});
  assert.deepEqual(call("rowState", {...row, choice: "accept"}), {state: "accepted", value: 6, needsReview: true, reviewed: true});
  assert.deepEqual(call("rowState", {...row, choice: "edit", edited: 4}), {state: "edited", value: 4, needsReview: true, reviewed: true});
});

test("missing and rejected suggestions stay blank and cannot be accepted", () => {
  const row = {field: "lot_id", suggestion: {state: "needs_input", value: "unknown"}};
  assert.deepEqual(call("rowState", row), {state: "needs_input", value: null, needsReview: false, reviewed: true});
  assert.deepEqual(call("rowState", {...row, choice: "accept"}), {state: "needs_input", value: null, needsReview: false, reviewed: true});
  assert.equal(call("rowState", {...suggested("free_totes", 6), choice: "edit", edited: ""})?.reviewed, false);
});

test("review progress counts suggested kind and conflicts but not matching typed or missing fields", () => {
  const rows = [suggested("kind", "CAPACITY", {choice: "accept"}), suggested("zone", "chilled", {typed: "ambient"}),
    suggested("free_totes", 6, {typed: 6}), {field: "valid_to"}];
  assert.deepEqual(call("reviewProgress", rows), {reviewed: 1, total: 2});
  assert.deepEqual(call("reviewProgress", []), {reviewed: 0, total: 0});
});

test("source highlights use only the exact nonempty substring", () => {
  assert.deepEqual(call("highlightRange", "The chiller: half the stock.", "half the stock"), [13, 27]);
  assert.equal(call("highlightRange", "The chiller: half the stock.", "Half the stock"), null);
  assert.equal(call("highlightRange", "Text", ""), null);
  assert.deepEqual(call("highlightRange", "<script>half</script>", "<script>"), [0, 8]);
});

test("field origins follow explicit choices and the final values the server validates", () => {
  const rows = [suggested("kind", "CAPACITY", {choice: "accept"}),
    suggested("zone", "chilled", {typed: "ambient", choice: "keep"}),
    suggested("free_totes", 6, {choice: "edit", edited: "4"}),
    suggested("valid_from", "2026-10-18", {choice: "clear"}), {field: "valid_to", typed: "2026-10-19"}];
  assert.deepEqual(call("fieldSources", rows), {kind: "gemini_accepted", zone: "typed", free_totes: "gemini_edited",
    valid_from: "cleared", valid_to: "typed"});
  assert.deepEqual(call("fieldSources", [suggested("free_totes", 6, {choice: "edit", edited: "6"})]),
    {free_totes: "gemini_accepted"});
});

test("unreviewed suggestions cannot produce submission origins", () => {
  assert.equal(typeof view.fieldSources, "function");
  assert.throws(() => view.fieldSources([suggested("kind", "CAPACITY")]), /Review each suggested field/);
});

test("origins compare normalized submitted numbers and trimmed codes", () => {
  assert.deepEqual(call("fieldSources", [suggested("free_totes", 6, {choice: "edit", edited: "6.0"}),
    suggested("lot_id", "LOT-1", {choice: "edit", edited: " LOT-1 "})]),
    {free_totes: "gemini_accepted", lot_id: "gemini_accepted"});
});

test("long exact excerpts retain their full range without treating markup as HTML", () => {
  const excerpt = "<script>" + "x".repeat(384) + "</script>";
  const message = "prefix " + excerpt + " suffix";
  assert.deepEqual(call("highlightRange", message, excerpt), [7, 7 + excerpt.length]);
});

test("optional blank edits are reviewed without converting required blanks into valid input", () => {
  assert.equal(call("rowState", {...suggested("sku", "MILK-1L"), required: false, choice: "edit", edited: ""})?.reviewed, true);
  assert.equal(call("rowState", {...suggested("free_totes", 6), required: true, choice: "edit", edited: ""})?.reviewed, false);
});
