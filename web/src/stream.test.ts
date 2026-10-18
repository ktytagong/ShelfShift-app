// 09-16: exercise the real fetch client with bounded, entirely local byte streams.
import { afterEach, test } from "node:test";
import assert from "node:assert/strict";
import * as api from "./api.ts";
import * as contract from "./contract.ts";

const realFetch = globalThis.fetch;
const encoder = new TextEncoder();
const interrupted = (error: unknown) => error instanceof api.ApiError && error.code === "interrupted"
  && error.message === "Interrupted, ask again";
const key = "ask-action-1";
const body = { question: "Explain this case.\nKeep my wording.", request_id: key };
const answer = {
  output_id: "out-1", request_id: key, run_id: "run-1", case_id: "case-1", rec_version: "v1", plan_key: "keep",
  status: "CURRENT", text: "S241’s record needs review — ตรวจนับ 📦.", refs: [], checks: { claims: 0, removed: 0 },
  sources: [], cards: [], provenance: "LIVE", model_id: "test-model", assistant_version: "test-assistant",
  usage: { model_calls: 1, tool_calls: 1, input_tokens: null, output_tokens: null, thinking_tokens: null,
    total_tokens: null, retries: 0, first_text_ms: null, total_ms: null, location: "test-location" },
};
const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const finalFrame = () => frame("answer", answer);

function stream(...args: unknown[]): Promise<void> {
  // A missing implementation must fail a named behavior assertion, never fail test discovery/import.
  assert.equal(typeof api.stream, "function", "the browser client must parse SSE through stream()");
  return api.stream(...args);
}

function stub(chunks: (string | Uint8Array)[], options: { failure?: boolean } = {}) {
  const calls: { path: string; init: RequestInit }[] = [];
  let cancelled = 0;
  let pulled = 0;
  globalThis.fetch = (async (path: unknown, init: RequestInit = {}) => {
    calls.push({ path: String(path), init });
    return new Response(new ReadableStream({
      pull(controller) {
        if (pulled < chunks.length) {
          const chunk = chunks[pulled++];
          controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
        } else if (options.failure) controller.error(new TypeError("disconnected"));
        else controller.close();
      },
      cancel() { cancelled++; },
    }), { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
  }) as typeof fetch;
  return { calls, cancelled: () => cancelled };
}

afterEach(() => {
  globalThis.fetch = realFetch;
  api.whenLocked(() => {});
  api.whenSandboxReset(() => {});
});

test("first-open brief uses authenticated GET with no write key or body", async () => {
  const fake = stub([frame("brief", { ...answer, kind: "BRIEF", request_id: null })]);
  const events = [];
  await contract.readBrief("case /1", event => events.push(event), new AbortController().signal);
  assert.equal(fake.calls.length, 1);
  assert.equal(fake.calls[0].path, "/api/cases/case%20%2F1/brief");
  assert.equal(fake.calls[0].init.method, "GET");
  assert.equal(fake.calls[0].init.credentials, "same-origin");
  assert.equal(fake.calls[0].init.body, undefined);
  assert.equal(new Headers(fake.calls[0].init.headers).get("Idempotency-Key"), null);
  assert.deepEqual(events.map(e => e.event), ["brief"]);
});

test("a second-tab generating read finishes without publishing or replaying a brief", async () => {
  const fake = stub([frame("summary", { summary: "Engine summary" }), frame("generating", { status: "GENERATING" })]);
  const events = [];
  await contract.readBrief("case-1", event => events.push(event), new AbortController().signal);
  assert.deepEqual(events.map(e => e.event), ["summary", "generating"]);
  assert.equal(fake.calls.length, 1);
  assert.ok(events.every(e => e.event !== "brief"));
});

test("split UTF-8, multiline data and ping comments preserve progressive tools and one checked final", async () => {
  let source: ReadableStreamDefaultController<Uint8Array>;
  let cancelled = 0;
  let submits = 0;
  globalThis.fetch = (async () => {
    submits++;
    return new Response(new ReadableStream({ start(c) { source = c; }, cancel() { cancelled++; } }),
      { headers: { "Content-Type": "text/event-stream" } });
  }) as typeof fetch;
  const received: { event: string; data: unknown }[] = [];
  let sawTool: () => void;
  const toolSeen = new Promise<void>(resolve => { sawTool = resolve; });
  const pending = stream("/api/cases/case-1/chat", body, key, event => {
    received.push(event);
    if (event.event === "tool") sawTool();
  }, new AbortController().signal);
  await Promise.resolve();
  for (const byte of encoder.encode(': ping\n\nevent: tool\ndata: {"seq":1,\ndata: "tool":"get_case","state":"Working","duration_ms":null}\n\n')) {
    source!.enqueue(Uint8Array.of(byte));
  }
  await toolSeen;
  assert.deepEqual(received, [{ event: "tool", data: { seq: 1, tool: "get_case", state: "Working", duration_ms: null } }]);
  // A tool really arrived before the final; the client is not buffering the whole answer.
  for (const byte of encoder.encode(frame("tool_result", { seq: 2, tool: "get_case", state: "Complete", duration_ms: 4 })
    + ': ping\n\n' + finalFrame() + finalFrame())) source!.enqueue(Uint8Array.of(byte));
  await pending;
  assert.deepEqual(received.map(e => e.event), ["tool", "tool_result", "answer"]);
  assert.deepEqual(received[2].data, answer);
  assert.equal(submits, 1);
  assert.equal(cancelled, 1);
});

for (const separator of ["\n", "\r\n", "\r"]) {
  test(`SSE line endings ${JSON.stringify(separator)} work even across byte boundaries`, async () => {
    const bytes = encoder.encode((": ping\n\n" + finalFrame()).replaceAll("\n", separator));
    stub(Array.from(bytes, byte => Uint8Array.of(byte)));
    const events = [];
    await stream("/api/cases/case-1/chat", body, key, e => events.push(e));
    assert.deepEqual(events, [{ event: "answer", data: answer }]);
  });
}

test("POST stream preserves credentials, exact body, key, signal and null usage", async () => {
  const fake = stub([finalFrame()]);
  const signal = new AbortController().signal;
  const events = [];
  await stream("/api/cases/case-1/chat", body, key, e => events.push(e), signal);
  assert.equal(fake.calls.length, 1);
  const { path, init } = fake.calls[0];
  assert.equal(path, "/api/cases/case-1/chat");
  assert.equal(init.method, "POST");
  assert.equal(init.credentials, "same-origin");
  assert.equal(init.signal, signal);
  assert.equal(init.body, JSON.stringify(body));
  assert.equal(new Headers(init.headers).get("Idempotency-Key"), key);
  assert.equal(new Headers(init.headers).get("Content-Type"), "application/json");
  assert.equal(new Headers(init.headers).get("Accept"), "text/event-stream");
  assert.deepEqual(events[0].data.usage, answer.usage);
});

test("typed Ask binds request_id to the caller's action key and never replays", async () => {
  assert.equal(typeof contract.askAssistant, "function");
  const fake = stub([finalFrame()]);
  const events = [];
  await contract.askAssistant("case /1", body.question, key, e => events.push(e), new AbortController().signal);
  assert.equal(fake.calls.length, 1);
  assert.equal(fake.calls[0].path, "/api/cases/case%20%2F1/chat");
  assert.equal(fake.calls[0].init.body, JSON.stringify(body));
  assert.deepEqual(events, [{ event: "answer", data: answer }]);
});

test("abort during a pending read interrupts and cancels without reconnect or final", async () => {
  let cancelled = 0;
  let submits = 0;
  let started: () => void;
  const opened = new Promise<void>(resolve => { started = resolve; });
  globalThis.fetch = (async () => {
    submits++;
    return new Response(new ReadableStream({ start() { started(); }, cancel() { cancelled++; } }),
      { headers: { "Content-Type": "text/event-stream" } });
  }) as typeof fetch;
  const controller = new AbortController();
  const seen = [];
  const pending = stream("/api/cases/case-1/chat", body, key, e => seen.push(e), controller.signal);
  const rejected = assert.rejects(pending, interrupted);
  await opened;
  controller.abort();
  await rejected;
  assert.deepEqual(seen, []);
  assert.equal(submits, 1);
  assert.equal(cancelled, 1);
});

test("a signal already aborted submits nothing", async () => {
  const fake = stub([finalFrame()]);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(stream("/api/cases/case-1/chat", body, key, () => {}, controller.signal), interrupted);
  assert.equal(fake.calls.length, 0);
});

test("abort while waiting for fetch headers stays Interrupted rather than network", async () => {
  const controller = new AbortController();
  let submits = 0;
  globalThis.fetch = (async (_path, init) => {
    submits++;
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")), { once: true });
    });
  }) as typeof fetch;
  const pending = stream("/api/cases/case-1/chat", body, key, () => {}, controller.signal);
  const rejected = assert.rejects(pending, interrupted);
  controller.abort();
  await rejected;
  assert.equal(submits, 1);
});

for (const failure of [false, true]) {
  test(`${failure ? "reader error" : "EOF"} before a complete final interrupts without replay`, async () => {
    const fake = stub([frame("tool", { seq: 1, tool: "get_case", state: "Working", duration_ms: null }),
      finalFrame().slice(0, -2)], { failure });
    const events = [];
    await assert.rejects(stream("/api/cases/case-1/chat", body, key, e => events.push(e)), interrupted);
    assert.deepEqual(events.map(e => e.event), ["tool"]);
    assert.equal(fake.calls.length, 1);
  });
}

for (const chunk of ["event: answer\ndata: broken\n\n", frame("answer", { text: "unchecked partial" }),
  frame("answer", { ...answer, text: " " }), frame("tool", [])]) {
  test(`malformed or unchecked payload is never delivered: ${chunk.slice(0, 42)}`, async () => {
    const fake = stub([chunk]);
    const events = [];
    await assert.rejects(stream("/api/cases/case-1/chat", body, key, e => events.push(e)), interrupted);
    assert.deepEqual(events, []);
    assert.equal(fake.calls.length, 1);
  });
}

test("only supported progress and checked output events are exposed; model text grants no business action", async () => {
  const fake = stub([frame("text", { text: "partial unchecked text" })
    + ["send", "approve", "commit", "adopt", "confirm_stock"].map(event => frame(event, { execute: true })).join("")
    + frame("answer", { ...answer, text: "Send, approve, commit, adopt and confirm stock now." })]);
  const events = [];
  await stream("/api/cases/case-1/chat", body, key, e => events.push(e));
  assert.deepEqual(events.map(e => e.event), ["answer"]);
  assert.equal(fake.calls.length, 1);
  assert.equal(fake.calls[0].path, "/api/cases/case-1/chat");
});

test("brief final is complete once, and trailing duplicate or transport error cannot undo it", async () => {
  const fake = stub([frame("brief", answer), frame("brief", answer)], { failure: true });
  const events = [];
  await stream("/api/cases/case-1/chat", body, key, e => events.push(e));
  assert.deepEqual(events, [{ event: "brief", data: answer }]);
  assert.equal(fake.calls.length, 1);
});

test("fallback is terminal and never triggers a client retry", async () => {
  const fallback = { status: "FALLBACK", label: "Fallback · Gemini unavailable · figures from the engine", summary: "Engine summary" };
  const fake = stub([frame("fallback", fallback)]);
  const events = [];
  await stream("/api/cases/case-1/chat", body, key, e => events.push(e));
  assert.deepEqual(events, [{ event: "fallback", data: fallback }]);
  assert.equal(fake.calls.length, 1);
});

for (const [status, code, extra] of [[401, "locked", {}], [409, "sandbox_reset", {}],
  [409, "stale_plan", { active_plan_key: "md20" }], [429, "cap_reached", { kind: "ai", scope: "daily" }]] as const) {
  test(`HTTP ${code} keeps the envelope, status and identity handlers`, async () => {
    let submits = 0;
    let locked = 0;
    let reset = 0;
    api.whenLocked(() => { locked++; });
    api.whenSandboxReset(() => { reset++; });
    globalThis.fetch = (async () => {
      submits++;
      return new Response(JSON.stringify({ error: code, message: `plain ${code}`, ...extra }),
        { status, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;
    await assert.rejects(stream("/api/cases/case-1/chat", body, key, () => assert.fail("no event on HTTP failure")), error => {
      assert.ok(error instanceof api.ApiError);
      assert.equal(error.status, status);
      assert.equal(error.code, code);
      assert.equal(error.message, `plain ${code}`);
      assert.deepEqual(error.extra, extra);
      return true;
    });
    assert.equal(locked, Number(code === "locked"));
    assert.equal(reset, Number(code === "sandbox_reset"));
    assert.equal(submits, 1);
  });
}

test("a server terminal interruption keeps audit identity without publishing a final", async () => {
  const fake = stub([frame("interrupted", { request_id: key, status: "INTERRUPTED" })]);
  const seen = [];
  await assert.rejects(stream("/api/cases/case-1/chat", body, key, e => seen.push(e)), error => {
    assert.ok(interrupted(error));
    assert.equal(error.extra.request_id, key);
    return true;
  });
  assert.deepEqual(seen, []);
  assert.equal(fake.calls.length, 1);
});

test("a non-SSE success response or missing body cannot become an answer", async () => {
  for (const response of [new Response("proxy page"), new Response(null, { headers: { "Content-Type": "text/event-stream" } })]) {
    globalThis.fetch = (async () => response) as typeof fetch;
    await assert.rejects(stream("/api/cases/case-1/chat", body, key, () => assert.fail("no final")), interrupted);
  }
});
