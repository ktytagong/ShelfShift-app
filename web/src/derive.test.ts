// node --test (no dependency): the key lifecycle, the signed-in store check and the count-request helpers.
import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError, type AnswerSignal, type CaseState, type FactCheck, type Request } from "./api.ts";
import {
  COUNT_KINDS, REPLANNABLE, canAskForCount, canReplan, canRequestRecount, canonical, countsBody, draftKey, invalidFields,
  isMe, nextAttempt, pageInRange, parseCount, planKind, productLabel, requestForFact, resultIsToday, waitShown,
  type Attempt,
} from "./derive.ts";

function keys(): () => string {
  let n = 0;
  return () => `key-${++n}`;
}

test("an unsettled attempt with an equal body keeps its key, even with its keys in another order", () => {
  const make = keys();
  const first = nextAttempt(null, { role: "manager", branch: "Lat Phrao" }, make);
  assert.equal(first.key, "key-1");
  assert.equal(first.settled, false);
  const again = nextAttempt(first, { branch: "Lat Phrao", role: "manager" }, make);
  assert.equal(again.key, "key-1");
  assert.deepEqual(again.body, { branch: "Lat Phrao", role: "manager" });
});

test("a settled attempt or a changed body makes a new key", () => {
  const make = keys();
  const first = nextAttempt(null, { expected_version: 3 }, make);
  const settled: Attempt<{ expected_version: number }> = { ...first, settled: true };
  assert.equal(nextAttempt(settled, { expected_version: 3 }, make).key, "key-2");
  assert.equal(nextAttempt(first, { expected_version: 4 }, make).key, "key-3");
  assert.equal(nextAttempt(first, {}, make).key, "key-4");
});

test("canonical JSON sorts nested keys and keeps arrays in order", () => {
  assert.equal(canonical({ b: 1, a: { d: [2, 1], c: null } }), '{"a":{"c":null,"d":[2,1]},"b":1}');
  assert.equal(canonical({}), "{}");
});

test("isMe: false for HQ, false for a branch that differs only in case or spacing, true for the exact branch", () => {
  assert.equal(isMe({ role: "hq", branch: null }, "Bang Na"), false);
  assert.equal(isMe({ role: "hq", branch: null }, null), false);
  assert.equal(isMe({ role: "manager", branch: "Bang Na" }, "bang na"), false);
  assert.equal(isMe({ role: "manager", branch: "Bang Na" }, "Bang Na "), false);
  assert.equal(isMe({ role: "manager", branch: "Bang Na" }, "Bang  Na"), false);
  assert.equal(isMe({ role: "manager", branch: "Bang Na" }, "Bang Na"), true);
});

test("planKind: keep is KEEP, md20 and md30 are MARKDOWN, a tr:… key is TRANSFER, null and an unknown key are null", () => {
  assert.equal(planKind("keep"), "KEEP");
  assert.equal(planKind("md20"), "MARKDOWN");
  assert.equal(planKind("md30"), "MARKDOWN");
  assert.equal(planKind("tr:Lat Phrao:R-02:120:hold"), "TRANSFER");
  assert.equal(planKind("tr:Lat Phrao:R-02:80:md20"), "TRANSFER");
  assert.equal(planKind(null), null);
  assert.equal(planKind("something"), null);
});

// --- count requests (05-CONTEXT D-01) -------------------------------------------------------------------------------

const ACTIVE = "rec-active-0001";

function fact(over: Partial<FactCheck> = {}): Pick<FactCheck, "fact_id" | "kind" | "changes_plan"> {
  return { fact_id: "F-1", kind: "PHANTOM_STOCK", changes_plan: true, ...over };
}

function request(over: Partial<Request> = {}): Request {
  return {
    request_id: "REQ-1", case_id: "C-1", case_key: "SUS-19", rec_version: ACTIVE, fact_id: "F-1",
    fact_kind: "PHANTOM_STOCK", lot_ids: ["MLK-038"], to_branch: "Lat Phrao", ask_text: "Please count", due_day: 3,
    due_min: 585, state: "OPEN", partial_note: null, answer_signal_id: null, replaced_by: null, case_source: "Bang Na",
    recountable: false, confirmable: false, answerable: true, answer: null, ...over,
  };
}

const detail = (requests: Request[] = [], state: CaseState = "OPEN") => ({ source: "Bang Na", rec_version: ACTIVE, requests, state });
const source = { role: "manager" as const, branch: "Bang Na" };

test("COUNT_KINDS mirrors api/signals.py: phantom stock and stale counts only", () => {
  assert.deepEqual([...COUNT_KINDS], ["PHANTOM_STOCK", "STALE_COUNT"]);
});

test("requestForFact matches the fact id on the case's active version only", () => {
  const mine = request();
  const older = request({ request_id: "REQ-0", rec_version: "rec-older" });
  const other = request({ request_id: "REQ-2", fact_id: "F-2" });
  assert.equal(requestForFact(detail([older, mine, other]), "F-1"), mine);
  assert.equal(requestForFact(detail([older]), "F-1"), null);
  assert.equal(requestForFact(detail([other]), "F-1"), null);
});

test("requestForFact returns the newest request for the fact on the active version (the detail lists oldest first)", () => {
  const original = request({ state: "SUPERSEDED", replaced_by: "REQ-2" });
  const recount = request({ request_id: "REQ-2" });
  assert.equal(requestForFact(detail([original, recount]), "F-1"), recount);
  assert.equal(requestForFact(detail([original, recount, request({ request_id: "REQ-9", rec_version: "rec-older" })]), "F-1"),
    recount);
});

test("REPLANNABLE mirrors api/cases.py LIVE_STATES: a confirmed count re-plans only OPEN, WAITING and PENDING_APPROVAL", () => {
  assert.deepEqual([...REPLANNABLE], ["OPEN", "WAITING", "PENDING_APPROVAL"]);
  for (const state of REPLANNABLE) assert.equal(canReplan(state), true, state);
  for (const state of ["REJECTED", "APPROVED", "PREPARED", "EXECUTED", "OBSERVED", "CLOSED", "INVALIDATED"] as const) {
    assert.equal(canReplan(state), false, state);
  }
});

test("canAskForCount only while the case can be re-planned (G-05-2 item 11): OPEN, WAITING, PENDING_APPROVAL", () => {
  for (const state of ["OPEN", "WAITING", "PENDING_APPROVAL"] as const) {
    assert.equal(canAskForCount(fact(), source, detail([], state)), true, state);
  }
  for (const state of ["REJECTED", "APPROVED", "PREPARED", "EXECUTED", "OBSERVED", "CLOSED", "INVALIDATED"] as const) {
    assert.equal(canAskForCount(fact(), source, detail([], state)), false, state);
  }
});

test("canRequestRecount: an ANSWERED request the server calls recountable, for the manager of its case_source only", () => {
  const ok = request({ state: "ANSWERED", recountable: true, case_source: "Bang Na" });
  assert.equal(canRequestRecount(ok, source), true);
  assert.equal(canRequestRecount(ok, { role: "hq", branch: null }), false);
  assert.equal(canRequestRecount(ok, { role: "manager", branch: "Lat Phrao" }), false); // the addressed branch
  assert.equal(canRequestRecount(request({ state: "ANSWERED", recountable: false }), source), false);
  for (const state of ["OPEN", "SUPERSEDED", "CONFIRMED", "EXPIRED"] as const) {
    assert.equal(canRequestRecount(request({ state, recountable: true }), source), false, state);
  }
});

test("canAskForCount: a plan-changing phantom or stale fact, the source manager, no request yet on this version", () => {
  assert.equal(canAskForCount(fact(), source, detail()), true);
  assert.equal(canAskForCount(fact({ kind: "STALE_COUNT" }), source, detail()), true);
});

test("canAskForCount is false for an estimated expiry, a fact that changes nothing, HQ and another branch's manager", () => {
  assert.equal(canAskForCount(fact({ kind: "ESTIMATED_EXPIRY" }), source, detail()), false);
  assert.equal(canAskForCount(fact({ changes_plan: false }), source, detail()), false);
  assert.equal(canAskForCount(fact(), { role: "hq", branch: null }, detail()), false);
  assert.equal(canAskForCount(fact(), { role: "manager", branch: "Lat Phrao" }, detail()), false);
});

test("canAskForCount is false while a request exists on this version, true again when it is on an older one", () => {
  for (const state of ["OPEN", "ANSWERED", "CONFIRMED", "EXPIRED", "SUPERSEDED"] as const) {
    assert.equal(canAskForCount(fact(), source, detail([request({ state })])), false, state);
  }
  assert.equal(canAskForCount(fact(), source, detail([request({ rec_version: "rec-older" })])), true);
});

test("parseCount: a blank field is null, never 0; a typed 0 is 0", () => {
  assert.equal(parseCount(""), null);
  assert.equal(parseCount("   "), null);
  assert.equal(parseCount("0"), 0);
  assert.equal(parseCount("12"), 12);
  assert.equal(parseCount("2.5"), 2.5);
  assert.equal(parseCount("abc"), null);
});

test("countsBody lists every requested lot with both parts; a blank part is null, never 0", () => {
  const body = countsBody(["MLK-038", "MLK-039"], { "MLK-038": { shelf: "0", backroom: "" } });
  assert.deepEqual(body, {
    "MLK-038": { shelf_units: 0, backroom_units: null },
    "MLK-039": { shelf_units: null, backroom_units: null },
  });
  assert.ok(!JSON.stringify(countsBody(["A"], {})).includes(":0"));
});

// --- the verification line, needs_input fields and drafts (05-UI-SPEC "Verification line", "Requests") -------------

test("invalidFields: a needs_input error's fields become 'lotId:field'; any other error or none gives an empty set", () => {
  const e = new ApiError(409, "needs_input", "Fill in every count before confirming", {
    fields: [{ lot_id: "MLK-038", field: "shelf_units" }, { lot_id: "MLK-039", field: "backroom_units" }],
  });
  assert.deepEqual([...invalidFields(e)].sort(), ["MLK-038:shelf_units", "MLK-039:backroom_units"]);
  assert.equal(invalidFields(new ApiError(409, "scope_tick", "Tick")).size, 0);
  assert.equal(invalidFields(new ApiError(409, "needs_input", "x", { fields: "nope" })).size, 0);
  assert.equal(invalidFields(null).size, 0);
});

test("draftKey: the request id and the answer revision, 0 before any answer", () => {
  assert.equal(draftKey(request()), "REQ-1:0");
  const answer = { revision: 3 } as AnswerSignal;
  assert.equal(draftKey(request({ answer })), "REQ-1:3");
});

// --- the day a stored result describes (review CR-01) ------------------------------------------------------------

test("resultIsToday: true only when the stored input was computed on the session day", () => {
  assert.equal(resultIsToday({ engine_input: { today: 3 } }, 3), true);
  assert.equal(resultIsToday({ engine_input: { today: 3 } }, 4), false); // an APPROVED case after Next day
  assert.equal(resultIsToday({ engine_input: {} }, 3), false);
  assert.equal(resultIsToday({ engine_input: null }, 3), false);
});

// --- the plan's own deadline and the wait sentence (G-05-2 item 4) ----------------------------------------------

const WAIT = { run_code: "R-03", day: 3, latest_decision_min: 660, departs_min: 810 };
const FINISHED: CaseState[] = ["APPROVED", "PREPARED", "EXECUTED", "OBSERVED", "CLOSED", "REJECTED", "INVALIDATED"];

test("waitShown: true only with a wait, a case that can be decided and today's result", () => {
  for (const state of REPLANNABLE) {
    assert.equal(waitShown({ wait: WAIT, state, engine_input: { today: 3 } }, 3), true, state);
  }
  assert.equal(waitShown({ wait: null, state: "OPEN", engine_input: { today: 3 } }, 3), false);
  for (const state of FINISHED) {
    assert.equal(waitShown({ wait: WAIT, state, engine_input: { today: 3 } }, 3), false, state);
  }
  assert.equal(waitShown({ wait: WAIT, state: "WAITING", engine_input: { today: 3 } }, 4), false);
});

// --- the public dataset's board and codes (05.2 D-02, D-26; UI-SPEC A-37, A-38) --------------------------------------

test("productLabel: the code alone without a name; 'Name · Code' only when the API gives a name (05.2 D-02)", () => {
  assert.equal(productLabel("P0123", null), "P0123");
  assert.equal(productLabel("MILK-1L", "Fresh milk 1 L"), "Fresh milk 1 L · MILK-1L");
  assert.equal(productLabel("P0123", ""), "P0123"); // an empty name is no name: never an invented one
});

// --- a list that shrank past the current page (quick 261017-wzs finding 5; 06.1 D-17) ----------------------------------

test("pageInRange: null while the page holds records or is the first; else the last page with records", () => {
  assert.equal(pageInRange(1, 20, 0), null); // the first page is never past the last
  assert.equal(pageInRange(2, 20, 40), null);
  assert.equal(pageInRange(2, 20, 21), null);
  assert.equal(pageInRange(3, 20, 40), 2); // the list shrank: ask for the last page with records
  assert.equal(pageInRange(2, 20, 20), 1);
  assert.equal(pageInRange(5, 20, 0), 1); // nothing left: page 1
  assert.equal(pageInRange(4, 0, 10), null); // no page size: nothing to correct
});

// --- 07-21: the fact checks' actions, the blockers and the final-approval rule (07-CONTEXT D-07, D-11, D-12) ---------
import * as d21 from "./derive.ts";

const SRC = { role: "manager" as const, branch: "S1" };
const fact21 = (over: Record<string, unknown> = {}) => ({
  fact_id: "S1:phantom", kind: "PHANTOM_STOCK", branch: "S1", lot_ids: ["L-1"], class: "REQUIRED" as const,
  changes_plan: true, requests_left: 2, blocked_label: null, ...over,
});
const holder21 = (requests: Partial<Request>[] = [], state: CaseState = "OPEN") => ({
  rec_version: "v1", source: "S1", state, requests: requests.map((r) => ({ rec_version: "v1", fact_id: "S1:phantom", ...r }) as Request),
});

test("07-21: the row's action: ask, ask again (the server's askable_again), the limit, or nothing", () => {
  assert.deepEqual(d21.factAction(fact21(), SRC, holder21()), { kind: "ask", part: "count", disabled: false });
  assert.deepEqual(d21.factAction(fact21({ kind: "ESTIMATED_EXPIRY" }), SRC, holder21()), { kind: "ask", part: "expiry", disabled: false });
  assert.deepEqual(d21.factAction(fact21({ requests_left: 0 }), SRC, holder21()), { kind: "ask", part: "count", disabled: true });
  const cant = { request_id: "R1", state: "CANT_CHECK" as const, askable_again: true };
  assert.equal(d21.factAction(fact21({ requests_left: 1 }), SRC, holder21([cant])).kind, "again");
  const used = d21.factAction(fact21({ requests_left: 0 }), SRC, holder21([{ ...cant, askable_again: false }]));
  assert.equal(used.kind === "again" && used.disabled, true);
  assert.equal(d21.factAction(fact21(), SRC, holder21([{ request_id: "R1", state: "OPEN", overdue: false }])).kind, "none");
  assert.equal(d21.factAction(fact21(), { role: "manager", branch: "S2" }, holder21()).kind, "none"); // not the source
  assert.equal(d21.factAction(fact21(), SRC, holder21([], "APPROVED")).kind, "none"); // no longer re-planned
  assert.equal(d21.factAction(fact21({ class: null }), SRC, holder21()).kind, "none"); // changes nothing
  assert.equal(d21.factAction(fact21({ kind: "DEMAND_EVENT" }), SRC, holder21()).kind, "none"); // no check for the kind
});

test("07-21: the combined request offers the other check of the same lot, count first in the body (D-11)", () => {
  const count = fact21();
  const expiry = fact21({ fact_id: "S1:expiry", kind: "ESTIMATED_EXPIRY" });
  const elsewhere = fact21({ fact_id: "S2:expiry", kind: "ESTIMATED_EXPIRY", branch: "S2" });
  const d = { ...holder21(), fact_checks: [count, expiry, elsewhere] };
  assert.equal(d21.companionFact(d, count)?.fact_id, "S1:expiry");
  assert.equal(d21.companionFact(d, expiry)?.fact_id, "S1:phantom");
  assert.equal(d21.companionFact({ ...d, fact_checks: [count, elsewhere] }, count), null);
  const outside = fact21({ fact_id: "S1:expiry", kind: "ESTIMATED_EXPIRY", lot_ids: ["L-9"] });
  assert.equal(d21.companionFact({ ...d, fact_checks: [count, outside] }, count), null); // lots outside the count's
  assert.equal(d21.companionFact({ ...holder21([{ fact_id: "S1:expiry" }]), fact_checks: [count, expiry] }, count), null);
  assert.deepEqual(d21.requestBody("v1", [expiry, count]), { rec_version: "v1", fact_ids: ["S1:phantom", "S1:expiry"] });
  assert.deepEqual(d21.requestBody("v1", [count]), { rec_version: "v1", fact_id: "S1:phantom" });
  assert.deepEqual(d21.requestBody("v1", [count], { request_id: "R1" }), { rec_version: "v1", fact_id: "S1:phantom", recount_of: "R1" });
});

test("07-21: blockers in the server's order, each with its owner and the server's Blocked label", () => {
  const d = {
    fact_checks: [fact21({ blocked_label: "Blocked · stock/expiry verification required" })],
    commitment: {
      blocks: [
        { kind: "lot_hold", hold_id: "H1", lot_ids: ["L-1"], owner_branch: "S1" },
        { kind: "required_check", fact_id: "S1:phantom", lot_ids: ["L-1"], owner_branch: "S1" },
      ],
      advisory: [], ack: null, next_actions: [],
    },
  };
  assert.deepEqual(d21.blockersOf(d, (f) => `title of ${f.fact_id}`), [
    { kind: "lot_hold", lot: "L-1", owner: "S1" },
    { kind: "required_check", fact_id: "S1:phantom", title: "title of S1:phantom", owner: "S1",
      blocked: "Blocked · stock/expiry verification required" },
  ]);
  assert.deepEqual(d21.blockersOf({ fact_checks: [] }, () => ""), []); // no commitment: nothing claimed
});

test("07-21: the final approval is the one after every other side the plan needs has decided (D-12)", () => {
  const transfer = { approvals: { required: ["source", "destination"] as ("source" | "destination")[], decisions: {} } };
  assert.equal(d21.isFinalDecision(transfer, "source"), false);
  assert.equal(d21.isFinalDecision(transfer, "destination"), false);
  const accepted = { approvals: { ...transfer.approvals, decisions: { destination: "accept" } } };
  assert.equal(d21.isFinalDecision(accepted, "source"), true);
  const keep = { approvals: { required: ["source"] as ("source" | "destination")[], decisions: {} } };
  assert.equal(d21.isFinalDecision(keep, "source"), true);
});
