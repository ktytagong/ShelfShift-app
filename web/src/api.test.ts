// node --test (no dependency): the adapter's key-per-action rule, the error envelope and the badge shapes.
// fetch is stubbed per test with a recorder that answers a real Response; nothing here reaches a server.
import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import {
  ApiError, NETWORK, form, get, newKey, patch, post, whenLocked, whenPaused, whenSandboxReset,
  type Badge, type Card,
} from "./api.ts";

type Call = { path: string; method: string; headers: Headers; body: unknown; credentials: unknown };

const realFetch = globalThis.fetch;

function stub(answer: (call: Call) => Response | Promise<Response>): Call[] {
  const calls: Call[] = [];
  globalThis.fetch = (async (input: unknown, init: RequestInit = {}) => {
    const call = { path: String(input), method: init.method ?? "GET", headers: new Headers(init.headers),
      body: init.body, credentials: init.credentials };
    calls.push(call);
    return answer(call);
  }) as typeof fetch;
  return calls;
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => {
  globalThis.fetch = realFetch;
  whenLocked(() => {});
  whenSandboxReset(() => {});
  whenPaused(() => {});
});

test("post sends exactly the caller's Idempotency-Key, and a retry resends the same key", async () => {
  const calls = stub(() => json(200, { state: "PENDING_APPROVAL", round: 1 }));
  const key = newKey();
  const body = { rec_version: "v1" };
  await post("/api/cases/c1/send", body, key);
  await post("/api/cases/c1/send", body, key); // Try again: the same user action
  assert.equal(calls.length, 2);
  for (const call of calls) {
    assert.equal(call.method, "POST");
    assert.equal(call.headers.get("Idempotency-Key"), key);
    assert.equal(call.headers.get("Content-Type"), "application/json");
    assert.equal(call.body, JSON.stringify(body));
    assert.equal(call.credentials, "same-origin");
  }
});

test("newKey returns a different key on each call", () => {
  const keys = new Set(Array.from({ length: 20 }, () => newKey()));
  assert.equal(keys.size, 20);
});

test("patch sends PATCH with JSON and the caller's key", async () => {
  const calls = stub(() => json(200, { version: 2, values: {}, bounds: {}, reevaluated: [] }));
  await patch("/api/settings", { expected_version: 1, changes: { waste_price: 0.5 } }, "k-settings");
  assert.equal(calls[0].method, "PATCH");
  assert.equal(calls[0].headers.get("Idempotency-Key"), "k-settings");
  assert.equal(calls[0].headers.get("Content-Type"), "application/json");
  assert.equal(calls[0].body, JSON.stringify({ expected_version: 1, changes: { waste_price: 0.5 } }));
});

test("form sets the Idempotency-Key and never sets Content-Type itself", async () => {
  const calls = stub(() => json(200, { signal_id: "s1", revision: 1 }));
  const data = new FormData();
  data.set("text", "");
  data.set("counts", JSON.stringify({ L1: { shelf_units: 3, backroom_units: null } }));
  await form("/api/requests/r1/answer", data, "k-answer");
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].headers.get("Idempotency-Key"), "k-answer");
  assert.equal(calls[0].headers.has("Content-Type"), false); // the browser writes the multipart boundary
  assert.equal(calls[0].body, data);
});

test("a 401 locked envelope calls the locked handler once and rejects with code locked", async () => {
  stub(() => json(401, { error: "locked", message: "Enter the demo passcode" }));
  let locked = 0;
  whenLocked(() => { locked += 1; });
  await assert.rejects(get("/api/session"), (e: unknown) =>
    e instanceof ApiError && e.status === 401 && e.code === "locked" && e.message === "Enter the demo passcode");
  assert.equal(locked, 1);
});

test("a 409 sandbox_reset envelope calls the reset handler once", async () => {
  stub(() => json(409, { error: "sandbox_reset", message: "Your sandbox was reset. Reload to continue." }));
  let reset = 0;
  let locked = 0;
  whenSandboxReset(() => { reset += 1; });
  whenLocked(() => { locked += 1; });
  await assert.rejects(post("/api/cases/c1/send", { rec_version: "v1" }, "k"), (e: unknown) =>
    e instanceof ApiError && e.code === "sandbox_reset");
  assert.equal(reset, 1);
  assert.equal(locked, 0);
});

test("409 envelopes keep every extra key: conflict, stale_version, needs_input", async () => {
  const envelopes: Record<string, Record<string, unknown>> = {
    conflict: { reasons: ["truck capacity 8 totes, needs 9"], resources: [{ kind: "TRUCK", label: "R-02", needed: 9,
      available: 8, unit: "totes" }] },
    stale_version: { active_rec_version: "abc123" },
    needs_input: { fields: [{ lot_id: "L1", field: "backroom_units" }] },
  };
  for (const [code, extra] of Object.entries(envelopes)) {
    stub(() => json(409, { error: code, message: `the ${code} message`, ...extra }));
    await assert.rejects(post("/api/x", {}, "k"), (e: unknown) => {
      assert.ok(e instanceof ApiError);
      assert.equal(e.code, code);
      assert.equal(e.message, `the ${code} message`);
      assert.deepEqual(e.extra, extra);
      return true;
    });
  }
});

test("a fetch rejection becomes ApiError code network with the NETWORK message", async () => {
  globalThis.fetch = (async () => { throw new TypeError("Failed to fetch"); }) as typeof fetch;
  await assert.rejects(get("/api/cases"), (e: unknown) =>
    e instanceof ApiError && e.status === 0 && e.code === "network" && e.message === NETWORK);
});

test("get returns the unverified badge exactly as sent, in both contract shapes; null stays null", async () => {
  const withAmounts: Badge = { kind: "unverified", text: "Unverified", fact_ids: ["Lat Phrao:phantom"],
    not_allowed_fact_ids: [], decision_regret: 23.33, net_revenue_difference: 20, waste_policy_difference: 3.33 };
  const notAllowed: Badge = { kind: "unverified", text: "Unverified", fact_ids: ["Lat Phrao:phantom", "L7:stale"],
    not_allowed_fact_ids: ["Lat Phrao:phantom", "L7:stale"], decision_regret: null, net_revenue_difference: null,
    waste_policy_difference: null };
  for (const badge of [withAmounts, notAllowed]) {
    stub(() => json(200, { day: 3, version: 1, cases: [{ case_id: "c1", badges: [badge] }] }));
    const board = await get<{ day: number; version: number; cases: Card[] }>("/api/cases");
    const got = board.cases[0].badges[0];
    assert.deepEqual(got, badge); // every key, every value, null included
  }
  stub(() => json(200, { cases: [{ badges: [notAllowed] }] }));
  const got = (await get<{ cases: Card[] }>("/api/cases")).cases[0].badges[0];
  assert.ok(got.kind === "unverified");
  assert.strictEqual(got.decision_regret, null);
  assert.strictEqual(got.net_revenue_difference, null);
  assert.strictEqual(got.waste_policy_difference, null);
  assert.deepEqual(got.not_allowed_fact_ids, ["Lat Phrao:phantom", "L7:stale"]);
});

test("a 503 paused calls whenPaused with the contact and still throws the API error", async () => {
  stub(() => json(503, { ok: false, db: "unreachable", error: "paused",
    message: "ShelfShift is temporarily unavailable.", contact: "help@example.org" }));
  const pauses: { contact: string }[] = [];
  whenPaused((p) => pauses.push(p));
  await assert.rejects(get("/api/session"), (e: unknown) => e instanceof ApiError && e.status === 503 && e.code === "paused");
  assert.deepEqual(pauses, [{ contact: "help@example.org" }]);
});

test("503 db_busy does not call whenPaused", async () => {
  stub(() => json(503, { error: "db_busy", message: "The server is busy. Try again in a moment." }));
  let paused = 0;
  whenPaused(() => { paused += 1; });
  await assert.rejects(get("/api/cases"), ApiError);
  assert.equal(paused, 0);
});

test("a paused envelope without a string contact uses an empty contact", async () => {
  const contacts: string[] = [];
  whenPaused((p) => contacts.push(p.contact));
  for (const contact of [undefined, null, 42, {}]) {
    stub(() => json(503, { error: "paused", message: "ShelfShift is temporarily unavailable.", contact }));
    await assert.rejects(get("/api/session"), ApiError);
  }
  assert.deepEqual(contacts, ["", "", "", ""]);
});
