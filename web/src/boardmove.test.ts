import { test } from "node:test";
import assert from "node:assert/strict";
import * as moves from "./boardmove.ts";
import { MOVE_TARGETS, moveChoice, moveDone, moveRefreshKey, sameMove, targetName, type MoveCard } from "./boardmove.ts";
import { mayAct } from "./decision/shared.ts";
import { prepareWrite, sendWrite } from "./decision/writes.ts";
import { STAGES } from "./steps.ts";
import type { NextActionKey } from "./vocab.ts";

const source = { role: "manager" as const, branch: "S1" };
const destination = { role: "manager" as const, branch: "S2" };
const hq = { role: "hq" as const, branch: null };
const card = (patch: Partial<MoveCard> = {}): MoveCard => ({
  case_id: "id/one", key: "SUS-1", stage: "decide", step: "COMPARE", state: "OPEN", closed_as: null,
  rec_version: "version-1", next_action: { key: "send", who: [source] }, blocked: false, ...patch,
});

test("a simple forward move uses the versioned workflow endpoint, with an encoded id", () => {
  assert.deepEqual(moveChoice(card(), "authorise", source), {
    kind: "direct", write: { path: "/api/cases/id%2Fone/send", body: { rec_version: "version-1" } },
  });
  const approved = card({ stage: "authorise", step: "COMMIT", state: "APPROVED", next_action: { key: "prepare", who: [source] } });
  assert.deepEqual(moveChoice(approved, "do", source), {
    kind: "direct", write: { path: "/api/cases/id%2Fone/prepare", body: {} },
  });
});

const KEYS: (NextActionKey | null)[] = [
  null, "evaluate", "take_ownership", "send", "check_availability", "approve", "accept", "prepare", "record_dispatch",
  "record_receipt", "record_price", "reconcile", "close", "answer_stock_check", "confirm_stock_check",
];
const WHO = [[source], [destination], [hq], [source, hq], []];

test("every direct move is exactly the stock decision page's Send for approval or Prepare write, and nothing else", () => {
  let direct = 0;
  for (const stage of STAGES.map((s) => s.key)) {
    for (const key of KEYS) {
      for (const who of WHO) {
        for (const blocked of [false, true]) {
          for (const rec_version of ["version-1", null]) {
            const c = card({ stage, next_action: { key, who }, blocked, rec_version });
            for (const target of MOVE_TARGETS) {
              for (const account of [source, destination, hq]) {
                const choice = moveChoice(c, target, account);
                if (choice.kind !== "direct") continue;
                direct += 1;
                const expected = key === "send" ? sendWrite(c) : prepareWrite(c);
                assert.deepEqual(choice.write, expected);
                assert.ok(
                  (key === "send" && stage === "decide" && target === "authorise" && rec_version !== null)
                  || (key === "prepare" && stage === "authorise" && target === "do"),
                  JSON.stringify({ stage, key, target }),
                );
                assert.ok(["/api/cases/id%2Fone/send", "/api/cases/id%2Fone/prepare"].includes(choice.write.path));
                assert.equal(blocked, false);
                assert.equal(mayAct(c, account), true);
              }
            }
          }
        }
      }
    }
  }
  assert.ok(direct > 0);
});

test("the move uses the stock decision page's act rule: HQ by role, a null action acts for nobody, a store for itself", () => {
  const hqWho = { role: "hq" as const, branch: "S9" }; // HQ is matched by role, whatever branch the server names
  assert.equal(mayAct({ next_action: { key: "send", who: [hqWho] } }, hq), true);
  assert.deepEqual(moveChoice(card({ next_action: { key: "send", who: [hqWho] } }), "authorise", hq).kind, "direct");
  assert.equal(mayAct({ next_action: { key: null, who: [source] } }, source), false);
  assert.equal(mayAct({ next_action: { key: "take_ownership", who: [source] } }, hq), true);
  assert.equal(mayAct({ next_action: { key: "send", who: [source] } }, destination), false);
  assert.equal(mayAct({ next_action: { key: "send", who: [hqWho] } }, source), false);
  assert.equal(mayAct({ next_action: { key: "send", who: [source] } }, hq), false);
  assert.deepEqual(moveChoice(card(), "authorise", hq), { kind: "guided" });
});

test("approval, physical stock records, and closing always open a guided action", () => {
  for (const key of ["approve", "accept"] as const) {
    const pending = card({ stage: "authorise", step: "APPROVE", state: "PENDING_APPROVAL", next_action: { key, who: [source] } });
    assert.deepEqual(moveChoice(pending, "do", source), { kind: "guided" });
  }
  for (const key of ["record_dispatch", "record_receipt", "record_price", "reconcile"] as const) {
    assert.deepEqual(moveChoice(card({ stage: "do", step: "EXECUTE", state: "PREPARED",
      next_action: { key, who: [source] } }), "check", source), { kind: "guided" });
  }
  const observed = card({ stage: "check", step: "VERIFY", state: "OBSERVED", next_action: { key: "close", who: [source] } });
  assert.deepEqual(moveChoice(observed, "closed", source), { kind: "guided" });
});

test("missing ownership, a blocked plan, or another actor opens prerequisites without writing", () => {
  assert.deepEqual(moveChoice(card({ next_action: { key: "take_ownership", who: [source] } }), "authorise", source), { kind: "guided" });
  assert.deepEqual(moveChoice(card({ needs_evaluation: true, rec_version: null }), "authorise", source), { kind: "guided" });
  assert.deepEqual(moveChoice(card({ blocked: true }), "authorise", source), { kind: "guided" });
  assert.deepEqual(moveChoice(card(), "authorise", destination), { kind: "guided" });
  const expired = card({ next_action: { key: "check_availability", who: [source] } });
  assert.deepEqual(moveChoice(expired, "authorise", source), { kind: "guided" });
});

test("a move cannot skip stages, reverse completed work, reopen closed decisions, or silently close", () => {
  assert.equal(moveChoice(card(), "decide", source).kind, "none");
  assert.deepEqual(moveChoice(card(), "do", source), { kind: "blocked", reason: moves.MOVE_NOT_YET("SUS-1", "Do", "Review and send") });
  assert.equal(moveChoice(card(), "closed", source).kind, "blocked");
  assert.deepEqual(moveChoice(card({ stage: "authorise", step: "APPROVE" }), "decide", source),
    { kind: "blocked", reason: moves.MOVE_NO_UNDO });
  assert.deepEqual(moveChoice(card({ step: "ARCHIVE", state: "REJECTED" }), "decide", source),
    { kind: "blocked", reason: moves.MOVE_CLOSED });
  assert.equal(moveChoice(card({ closed_as: "COMPLETED" }), "do", source).kind, "blocked");
  assert.equal(targetName("closed"), moves.CLOSED_HEADING);
});

test("a guided move is done at its column, past it, or once closed; Closed only when archived", () => {
  assert.equal(moveDone({ stage: "decide", step: "COMPARE" }, "authorise"), false);
  assert.equal(moveDone({ stage: "authorise", step: "APPROVE" }, "authorise"), true);
  assert.equal(moveDone({ stage: "do", step: "EXECUTE" }, "authorise"), true); // another account moved it on
  assert.equal(moveDone({ stage: "check", step: "VERIFY" }, "do"), true);
  assert.equal(moveDone({ stage: "check", step: "VERIFY" }, "closed"), false);
  assert.equal(moveDone({ stage: "check", step: "ARCHIVE" }, "closed"), true);
  assert.equal(moveDone({ stage: "decide", step: "ARCHIVE" }, "authorise"), true); // rejected: nothing left to do
});

test("the guided drawer's refresh key changes with the card's state, step, version, action and blocks", () => {
  const live = {
    state: "PREPARED" as const, step: "EXECUTE" as const, rec_version: "version-1",
    next_action: { key: "record_dispatch" as const, who: [source] }, blocked: false, blocked_reasons: [],
  };
  const key = moveRefreshKey(live);
  assert.equal(moveRefreshKey({ ...live }), key);
  for (const patch of [
    { state: "EXECUTED" as const }, { step: "VERIFY" as const }, { rec_version: "version-2" },
    { next_action: { key: "record_receipt" as const, who: [destination] } },
    { blocked: true }, // a confirmed count can make the transfer infeasible with nothing else changing
    { blocked: true, blocked_reasons: ["dispatch_blocked" as const] },
  ]) assert.notEqual(moveRefreshKey({ ...live, ...patch }), key, JSON.stringify(patch));
  assert.notEqual(moveRefreshKey({ ...live, blocked: true, blocked_reasons: ["missing_data" as const] }),
    moveRefreshKey({ ...live, blocked: true, blocked_reasons: ["dispatch_blocked" as const] }));
});

test("a changed plan, action, state, step, stage, or id invalidates the card picked up", () => {
  const original = card();
  assert.equal(sameMove(original, card()), true);
  for (const patch of [
    { rec_version: "version-2" }, { case_id: "other" }, { state: "PENDING_APPROVAL" as const },
    { stage: "authorise" as const }, { step: "CONTEXT" as const }, { next_action: { key: "approve" as const, who: [source] } },
  ]) assert.equal(sameMove(original, card(patch)), false);
});

// 06.1 D-03, D-14, D-58: no demo words, no relative day and no countdown in a move's wording.
const BANNED = /\b(today|ago|demo|seats?|sandbox(es)?|next day|start here|minutes left)\b/i;

test("every move string and templated line holds no banned word", () => {
  const lines: string[] = [];
  for (const [name, value] of Object.entries(moves)) {
    if (!/^[A-Z][A-Z_]*$/.test(name)) continue;
    if (typeof value === "string") lines.push(value);
    if (typeof value === "function") {
      lines.push(String((value as (...a: (string | null)[]) => string)("SUS-1", "Authorise", "Review and send")));
      lines.push(String((value as (...a: (string | null)[]) => string)("SUS-1", "Authorise", null)));
    }
  }
  lines.push(...MOVE_TARGETS.map(targetName));
  assert.ok(lines.length >= 20, String(lines.length));
  for (const line of lines) assert.doesNotMatch(line, BANNED);
});
