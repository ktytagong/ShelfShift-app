import { test } from "node:test";
import assert from "node:assert/strict";
import type { EvalCell, EvalFailure, EvalReport, EvalSummaryPayload } from "./api.ts";
import { cellText, groupLine, noticeFor, failuresByGroup, usageLine } from "./evalview.ts";

const cell = (v: Partial<EvalCell> = {}): EvalCell => ({ run: 1, p: 5, a: 5, f: 0, x: 0, result: "PASS", ...v });
test("cellText preserves scored counts and never turns a partial result into Pass", () => {
  assert.deepEqual(cellText(cell()), { text: "5 of 5", result: "Pass" });
  assert.deepEqual(cellText(cell({ p: 3, f: 2, result: "FAIL" })), { text: "3 of 5 · 2 failed", result: "Fail" });
  assert.deepEqual(cellText(cell({ p: 4, result: "NOT_SCORED" })), { text: "4 of 5", result: "Not scored" });
  assert.deepEqual(cellText(cell({ p: 4 })), { text: "4 of 5", result: "Not scored" });
});
test("invalid runs and N/A items are never passes", () => {
  assert.deepEqual(cellText(cell({ result: "INVALID" })), { text: "Invalid · not scored", result: "Not scored" });
  assert.deepEqual(cellText(cell({ p: 0, a: 0, x: 5, result: "NA" })), { text: "N/A", result: "N/A" });
});
test("group and usage lines contain counts only", () => {
  assert.equal(groupLine({ p: 12, a: 15, f: 2, x: 3 }), "12 of 15 applicable items passed across 3 runs · 2 failed · 3 N/A");
  assert.equal(usageLine({ calls: 30, tokens: 1234 }), "30 Gemini calls · 1234 tokens · run under its own limit, separate from reviewers' daily AI allowance");
});
test("noticeFor dates only a newer unpublished run and preserves the reason", () => {
  const published: EvalReport = { label: "label", model_id: "model", assistant_version: "v1", build: "b", fingerprint: "f",
    date: "2026-10-17T10:00:00Z", reviewed_at: null, safety: [], groups: [], usage: { calls: 0, tokens: 0 } };
  const data: EvalSummaryPayload = { published, latest_unpublished: { date: "2026-10-18T10:00:00Z", reason: "Reason <literal>" }, current: null, version_mismatch: false };
  assert.equal(noticeFor(data), "The latest evaluation run (18 Oct 2026) is not published: Reason <literal>.");
  assert.equal(noticeFor({ ...data, latest_unpublished: { date: published.date, reason: "Older" } }), null);
  assert.equal(noticeFor({ ...data, latest_unpublished: null }), null);
  assert.equal(noticeFor({ ...data, published: null }), "The latest evaluation run is not published: Reason <literal>.");
  assert.equal(noticeFor({ ...data, published: null, latest_unpublished: { date: data.latest_unpublished!.date, reason: "Literal $& $$" } }),
    "The latest evaluation run is not published: Literal $& $$.");
});
test("failure order is run then scenario then item and leaves notes unchanged", () => {
  const row = (run: number, scenario: number, item_id: string): EvalFailure => ({ run, scenario, item_id, item: item_id, note: "Literal <note>", context: null });
  const input = [row(2, 1, "a"), row(1, 2, "a"), row(1, 1, "b"), row(1, 1, "a")];
  assert.deepEqual(failuresByGroup(input), [input[3], input[2], input[1], input[0]]);
  assert.equal(input[0].run, 2);
});
