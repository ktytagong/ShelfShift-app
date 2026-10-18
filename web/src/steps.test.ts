// node --test (no dependency): the names of the nine steps and four stages for the server's `step`, `step_number` and
// `stage` keys (06.1-UI-SPEC "The nine steps and four stages"; D-24: the client names them, never derives them).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { STAGES, STEP_KEYS, stageName, stepName, stepText } from "./steps.ts";

test("STEP_KEYS are the nine server keys in order", () => {
  assert.deepEqual([...STEP_KEYS],
    ["TRIGGER", "CONTEXT", "OPTIONS", "COMPARE", "APPROVE", "COMMIT", "EXECUTE", "VERIFY", "ARCHIVE"]);
});

test("stepName and stageName name the server's keys", () => {
  assert.equal(stepName("COMPARE"), "Compare");
  assert.deepEqual(STEP_KEYS.map(stepName),
    ["Trigger", "Context", "Options", "Compare", "Approve", "Commit", "Execute", "Verify", "Archive"]);
  assert.equal(stageName("check"), "Check and close");
  assert.equal(stageName("decide"), "Decide");
  assert.equal(stageName("authorise"), "Authorise");
  assert.equal(stageName("do"), "Do");
});

test("stepText reads 'Step 4 of 9 · Compare' and is null without a step number", () => {
  assert.equal(stepText({ step: "COMPARE", step_number: 4 }), "Step 4 of 9 · Compare");
  assert.equal(stepText({ step: "TRIGGER", step_number: 0 }), null);
  assert.equal(stepText({ step: "TRIGGER", step_number: null }), null);
  assert.equal(stepText({ step: null, step_number: null }), null);
});

test("STAGES lists decide, authorise, do, check with their step hints", () => {
  assert.deepEqual(STAGES.map((s) => [s.key, s.name, s.hint]), [
    ["decide", "Decide", "Trigger · Context · Options · Compare"],
    ["authorise", "Authorise", "Approve · Commit"],
    ["do", "Do", "Execute"],
    ["check", "Check and close", "Verify · Archive"],
  ]);
});

test("steps.ts maps no case state to a step (D-24: the server derives steps)", () => {
  const src = readFileSync(new URL("./steps.ts", import.meta.url), "utf8");
  for (const state of ["PENDING_APPROVAL", "APPROVED", "PREPARED", "EXECUTED", "OBSERVED", "CLOSED", "REJECTED",
    "WAITING", "INVALIDATED"]) {
    assert.doesNotMatch(src, new RegExp(`\\b${state}\\b`), state);
  }
});
