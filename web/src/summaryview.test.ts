import { test } from "node:test";
import assert from "node:assert/strict";

// Preparation only. Copy to the assigned code lane for semantic RED before
// adding summaryview.ts. A missing behavior fails an assertion, not discovery.
const view = await import("./summaryview.ts").catch((error: NodeJS.ErrnoException) => {
  if (error.code === "ERR_MODULE_NOT_FOUND" && error.message.includes("summaryview.ts")) return {};
  throw error;
}) as Record<string, (...args: any[]) => any>;
const call = (name: string, ...args: unknown[]) => {
  assert.equal(typeof view[name], "function", `${name} must expose its planned behavior`);
  return view[name](...args);
};

const row = { case_id: "case-a", key: "A", rec_version: "v1", whatif_version: "w1",
  benefit_now: 20, benefit_if: 35, changed: true,
  benefits: { now: { net: 20 }, if_confirmed: { net: 35 } } };
const ref = { result_id: "result-old", path: "cases.0.benefits.if_confirmed.net", kind: "money", value: 35, text: "$35.00" };
const summary = { output_id: "output-old", signal_id: "signal-a", revision: 1,
  status: "CURRENT", text: "The proposed result is {{0}}.", refs: [ref],
  rows_result_id: "result-old", preview_key: "preview-old",
  rows: { cases: [row], affected: 1, previewed: 1 }, checks: { numbers_checked: 1, removed: 0 },
  usage: { model_calls: 1, tool_calls: 1, total_ms: 1250 }, provenance: "LIVE", reason: null,
  written_for_unconfirmed: false };
const preview = { preview_key: "preview-old", cases: [row] };

test("summary states preserve historical and outdated text while generation runs", () => {
  assert.equal(call("summaryState", null), "absent");
  assert.equal(call("summaryState", null, "being_written"), "being_written");
  assert.equal(call("summaryState", null, "checking"), "checking");
  assert.equal(call("summaryState", summary), "current");
  assert.equal(call("summaryState", { ...summary, status: "GENERATING" }), "being_written");
  assert.equal(call("summaryState", { ...summary, status: "OUT_OF_DATE" }, "being_written"), "out_of_date");
  assert.equal(call("summaryState", { ...summary, status: "HISTORICAL" }), "historical");
  assert.equal(call("summaryState", { ...summary, written_for_unconfirmed: true }), "historical");
  for (const reason of ["error", "timeout", "tool_limit", "cap_reached", "interrupted"]) {
    assert.equal(call("summaryState", { ...summary, status: "FALLBACK", provenance: "FALLBACK", reason }), "unavailable");
    assert.equal(call("summaryState", { ...summary, status: "OUT_OF_DATE", provenance: "FALLBACK", reason }), "out_of_date");
  }
});

test("check lines use measured singular counts and omit zero removed", () => {
  assert.equal(call("checkLine", summary), "1 number checked · 1 Gemini call · 1 tool call · 1.3 s");
  assert.equal(call("checkLine", { ...summary, checks: { numbers_checked: 2, removed: 1 },
    usage: { model_calls: 2, tool_calls: 3, total_ms: 2000 } }),
    "2 numbers checked · 1 removed · 2 Gemini calls · 3 tool calls · 2 s");
  assert.equal(call("checkLine", { ...summary, checks: { numbers_checked: 0, removed: 0 },
    usage: { model_calls: 0, tool_calls: 0, total_ms: 0 } }),
    "0 numbers checked · 0 Gemini calls · 0 tool calls · 0 s");
});

test("precomputed check lines show no load calls without replacing stored generation counts", () => {
  const saved = { ...summary, provenance: "PRECOMPUTED" };
  assert.equal(call("checkLine", saved), "1 number checked · 0 Gemini calls on page load");
  assert.equal(call("checkLine", saved, { stored: true }), "1 number checked · 1 Gemini call · 1 tool call · 1.3 s");
});

test("missing metrics are missing rather than invented zero calls or seconds", () => {
  assert.equal(call("checkLine", { ...summary, usage: null }),
    "1 number checked · Missing Gemini calls · Missing tool calls · Missing s");
});

test("a current reference focuses its matching case in the panel", () => {
  assert.deepEqual(call("refTarget", summary, ref, preview), {
    scope: "panel", caseId: "case-a", path: ref.path, value: 35,
  });
  // DOM order is not identity: the current panel may contain another case first.
  assert.equal(call("refTarget", summary, ref, { ...preview, cases: [{ ...row, case_id: "other" }, row] }).caseId, "case-a");
});

test("outdated and historical references always target their stored rows", () => {
  for (const status of ["OUT_OF_DATE", "HISTORICAL"]) {
    assert.deepEqual(call("refTarget", { ...summary, status }, ref, preview), {
      scope: "stored", caseId: "case-a", path: ref.path, value: 35,
    });
  }
  assert.equal(call("refTarget", { ...summary, written_for_unconfirmed: true }, ref, preview).scope, "stored");
});

test("a changed preview key or row cannot receive the old reference", () => {
  const alternatives = [
    { ...preview, preview_key: "preview-new" },
    { ...preview, cases: [] },
    { ...preview, cases: [{ ...row, rec_version: "v2" }] },
    { ...preview, cases: [{ ...row, whatif_version: "w2" }] },
    { ...preview, cases: [{ ...row, benefit_if: 99 }] },
  ];
  for (const current of alternatives) assert.equal(call("refTarget", summary, ref, current).scope, "stored");
});

test("references must belong to the saved result and match an actual numeric value", () => {
  for (const invalid of [
    { ...ref, result_id: "different-result" }, { ...ref, value: 99 },
    { ...ref, path: "cases.9.benefits.if_confirmed.net" },
    { ...ref, path: "__proto__.polluted" }, { ...ref, path: "constructor.prototype.value" },
    { ...ref, path: "cases.0.key", value: "A" }, { ...ref, value: NaN },
  ]) assert.equal(call("refTarget", summary, invalid, preview), null);
});

test("an aggregate reference resolves its stored preview metric without inventing a case", () => {
  assert.deepEqual(call("refTarget", summary,
    { result_id: "result-old", path: "affected", kind: "number", value: 1, text: "1" }, preview),
    { scope: "stored", caseId: null, path: "affected", value: 1 });
});

const terminal = 'event: summary\ndata: {"text":"Complete {{0}}.","refs":[{"value":35}]}\n\n';
function chunks(parts: string[]) {
  let pending = "";
  const events: unknown[] = [];
  for (const part of parts) {
    const parsed = call("parseSse", pending + part);
    events.push(...parsed.events);
    pending = parsed.rest;
  }
  return { events, rest: pending };
}

test("SSE split frames and heartbeat comments yield exactly one complete summary", () => {
  const payload = ': ping\n\nevent: status\ndata: {"state":"checking"}\n\n' + terminal;
  assert.deepEqual(chunks([...payload]), { events: [
    { event: "status", data: { state: "checking" } },
    { event: "summary", data: { text: "Complete {{0}}.", refs: [{ value: 35 }] } },
  ], rest: "" });
});

test("SSE multiline JSON and split CRLF retain the terminal frame", () => {
  const payload = 'event: summary\r\ndata: {"text":\r\ndata: "ครบ 📦", "refs":[]}\r\n\r\n';
  assert.deepEqual(chunks([...payload]), { events: [
    { event: "summary", data: { text: "ครบ 📦", refs: [] } },
  ], rest: "" });
});

test("SSE preserves an unfinished frame without rendering any partial text", () => {
  const unfinished = 'event: summary\ndata: {"text":"Not complete"}\n';
  assert.deepEqual(call("parseSse", unfinished), { events: [], rest: unfinished });
  assert.deepEqual(chunks([unfinished, "\n"]).events,
    [{ event: "summary", data: { text: "Not complete" } }]);
});

test("SSE rejects malformed payloads and ignores unrecognized extension events", () => {
  assert.equal(typeof view.parseSse, "function");
  assert.throws(() => view.parseSse('event: summary\ndata: {broken}\n\n'), /JSON|stream/i);
  assert.throws(() => view.parseSse('event: summary\ndata: null\n\n'), /stream|object/i);
  assert.deepEqual(call("parseSse", 'event: extension\ndata: ignored\n\n'), { events: [], rest: "" });
});
