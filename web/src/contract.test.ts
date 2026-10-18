// node --test (no dependency): the 06.1 client contract's query builders and calls (04-API-CONTRACT "Product mode
// (06.1)"). fetch is stubbed per test with a recorder that answers a real Response; nothing here reaches a server.
// The passcode is made per run and only ever compared as a boolean, so no failure message can print it.
import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import {
  boardQuery61, closeDecision, getExecution, getToday, lotsQuery, recordDispatch, scheduleQuery, signIn, signOut,
  takeOwnership, transfersQuery,
} from "./contract.ts";

type Call = { path: string; method: string; headers: Headers; body: unknown };

const realFetch = globalThis.fetch;

function stub(answer: (call: Call) => Response): Call[] {
  const calls: Call[] = [];
  globalThis.fetch = (async (input: unknown, init: RequestInit = {}) => {
    const call = { path: String(input), method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body };
    calls.push(call);
    return answer(call);
  }) as typeof fetch;
  return calls;
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => {
  globalThis.fetch = realFetch;
});

test("boardQuery61: HQ's filters in a fixed order; a manager's query never carries branch", () => {
  const f = { store: "S042", owner: "me", product: "P0123", deadline: "scenario_date", blocked: true, page: 2 } as const;
  assert.equal(boardQuery61({ role: "hq" }, f),
    "/api/cases?branch=S042&owner=me&product=P0123&deadline=scenario_date&blocked=1&page=2");
  assert.equal(boardQuery61({ role: "manager" }, f),
    "/api/cases?owner=me&product=P0123&deadline=scenario_date&blocked=1&page=2");
});

test("boardQuery61: empty and default values are omitted", () => {
  assert.equal(boardQuery61({ role: "hq" }, {}), "/api/cases");
  assert.equal(boardQuery61({ role: "hq" }, { store: "  ", owner: "", product: "", deadline: "", blocked: false, page: 1 }),
    "/api/cases");
  assert.equal(boardQuery61({ role: "hq" }, { deadline: "none" }), "/api/cases?deadline=none");
});

test("transfersQuery, scheduleQuery and lotsQuery build the contract's routes in a fixed key order", () => {
  assert.equal(transfersQuery({ direction: "incoming", state: "dispatched", page: 1 }),
    "/api/transfers?direction=incoming&state=dispatched");
  assert.equal(scheduleQuery({ date: "2026-10-17", view: "day", group: "work_order" }),
    "/api/schedule?date=2026-10-17&view=day&group=work_order");
  assert.equal(lotsQuery({ atRisk: true, product: "P0123" }), "/api/lots?product=P0123&at_risk=1");
});

test("getToday asks for HQ's later pages with ?page=; page 1 and a manager's call name no page", async () => {
  const calls = stub(() => json(200, { scenario_date: "2026-10-17" }));
  await getToday();
  await getToday(undefined, 1);
  await getToday(undefined, 3);
  await getToday("S042", 2);
  assert.deepEqual(calls.map((c) => c.path), ["/api/today", "/api/today", "/api/today?page=3", "/api/today?branch=S042&page=2"]);
});

test("signIn posts {account, passcode} to /api/session with a key and never puts the passcode in the URL", async () => {
  const passcode = crypto.randomUUID(); // made per run, compared only as booleans
  const calls = stub(() => json(200, { ok: true, reset_ms: 900, account: "S042", role: "manager", branch: "S042" }));
  const got = await signIn("S042", passcode, "k-sign-in");
  assert.deepEqual(got, { ok: true, reset_ms: 900, account: "S042", role: "manager", branch: "S042" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/session");
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].headers.get("Idempotency-Key"), "k-sign-in");
  assert.equal(calls[0].path.includes(passcode), false);
  const body = JSON.parse(String(calls[0].body)) as Record<string, unknown>;
  assert.deepEqual(Object.keys(body).sort(), ["account", "passcode"]);
  assert.equal(body.account, "S042");
  assert.equal(body.passcode === passcode, true);
});

test("signOut posts {} to /api/session/sign-out with a key", async () => {
  const calls = stub(() => json(200, { signed_out: true }));
  assert.deepEqual(await signOut("k-out"), { signed_out: true });
  assert.equal(calls[0].path, "/api/session/sign-out");
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].headers.get("Idempotency-Key"), "k-out");
  assert.equal(calls[0].body, "{}");
});

test("case writes post the contract's bodies with the caller's key; reads are plain GETs", async () => {
  const calls = stub(() => json(200, {}));
  await takeOwnership("SUS-19", "k1");
  await recordDispatch("SUS-19", { action_id: "a1", qty: 104, lot_id: "L-0042", condition: "as_expected" }, "k2");
  await closeDecision("SUS-19", { reason: "Received in full." }, "k3");
  await getExecution("SUS-19");
  assert.deepEqual(calls.map((c) => [c.method, c.path, c.headers.get("Idempotency-Key"), c.body ?? null]), [
    ["POST", "/api/cases/SUS-19/take-ownership", "k1", "{}"],
    ["POST", "/api/cases/SUS-19/dispatch", "k2",
      JSON.stringify({ action_id: "a1", qty: 104, lot_id: "L-0042", condition: "as_expected" })],
    ["POST", "/api/cases/SUS-19/close", "k3", JSON.stringify({ reason: "Received in full." })],
    ["GET", "/api/cases/SUS-19/execution", null, null],
  ]);
});
