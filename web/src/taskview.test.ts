// node --test (no dependency): the Tasks words and the deadline arithmetic (07-19; 07-UI-SPEC "Tasks", "DeadlineBar",
// "Lifecycle"; 07-CONTEXT D-06, D-09, D-36). Hand derivations sit beside each expected value.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  barAt, barShare, barText, blockedLine, clip, hhmmIn, middle, minutesWords, msLeft, primaryLabel, requestTitle,
  stampsLine, tasksPath, TILE_LABEL, TILE_ORDER, type TaskItem,
} from "./taskview.ts";
import type { Request } from "./api.ts";

const TZ = "Asia/Bangkok";

test("the six tiles in D-36's order and words", () => {
  assert.deepEqual(TILE_ORDER.map((t) => TILE_LABEL[t]),
    ["Checks to answer", "Answers to confirm", "Lot holds", "Advisory tasks", "Dispatches", "Receipts"]);
});

test("the list path carries view, tile and page; the client never filters", () => {
  assert.equal(tasksPath("open", null, 1), "/api/tasks?view=open");
  assert.equal(tasksPath("closed", "lot_holds", 2), "/api/tasks?view=closed&tile=lot_holds&page=2");
});

test("minutes left come from the server's clock plus the time elapsed here, never the device's clock", () => {
  // due 13:30Z, the server said 12:48Z (42 min); 30 s have passed on this device since the response arrived.
  const ms = msLeft("2026-10-16T13:30:00Z", "2026-10-16T12:48:00Z", 1_000_000, 1_030_000);
  assert.equal(ms, 41.5 * 60_000);
  assert.equal(barText(ms!), "41 min left"); // floored
  // A device clock a day wrong changes nothing: only the elapsed difference enters.
  assert.equal(msLeft("2026-10-16T13:30:00Z", "2026-10-16T12:48:00Z", 9e12, 9e12 + 30_000), ms);
  assert.equal(barText(60_000), "1 min left");
  assert.equal(barText(59_999), "Less than 1 min left");
  assert.equal(barText(0), "Overdue");
  assert.equal(barText(-5), "Overdue");
  assert.equal(msLeft("not a time", "2026-10-16T12:48:00Z", 0, 0), null);
});

test("the bar is drawn to scale between asked and due", () => {
  const asked = "2026-10-16T12:30:00Z";
  const due = "2026-10-16T13:30:00Z";
  assert.equal(barShare(asked, due, 45 * 60_000), 0.25); // 15 of 60 minutes passed
  assert.equal(barShare(asked, due, -1), 1);
  assert.equal(barShare(asked, due, 2 * 3_600_000), 0); // clamped
  assert.equal(barAt(asked, due, "2026-10-16T13:00:00Z"), 0.5);
  assert.ok(barAt(asked, due, "2026-10-16T13:40:00Z") > 1); // a late answer sits past the end tick
  assert.equal(minutesWords(42 * 60_000), "42 minutes left");
  assert.equal(minutesWords(90_000), "1 minute left");
});

test("the timestamps line in Bangkok time, a dash for what hasn't happened", () => {
  // 13:30Z is 20:30 in Bangkok (UTC+7).
  assert.equal(stampsLine({ asked: "2026-10-16T13:30:00Z", due: "2026-10-16T14:30:00Z", checked: "2026-10-16T13:40:00Z",
    answered: null, confirmed: null }, TZ),
  "Asked: 16 Oct 2026, 20:30 · Respond by: 16 Oct 2026, 21:30 · Checked: 16 Oct 2026, 20:40 · Answered: — · Confirmed: —");
  assert.equal(hhmmIn("2026-10-16T13:30:00Z", TZ), "20:30");
  assert.equal(hhmmIn(null, TZ), "—");
});

test("titles by kind, one title for a combined request, clipped at 90 characters", () => {
  const p = (part: "count" | "expiry", lots = ["L-1"]) => ({ part, lot_ids: lots });
  assert.equal(requestTitle([p("count")]), "Count stock · lot L-1");
  assert.equal(requestTitle([p("expiry")]), "Check printed expiry date · lot L-1");
  assert.equal(requestTitle([p("count"), p("expiry")]), "Count stock + Check printed expiry date · lot L-1");
  const long = "x".repeat(120);
  assert.equal(clip(long).length, 90);
  assert.ok(clip(long).endsWith("…"));
  assert.equal(clip("short"), "short");
});

test("file names middle-truncate to 32 characters", () => {
  const name = "a-very-long-photo-name-from-the-chiller-shelf.jpeg";
  const cut = middle(name);
  assert.equal(cut.length, 32);
  assert.ok(cut.startsWith("a-very-long-phot") && cut.endsWith("shelf.jpeg") && cut.includes("…"));
  assert.equal(middle("crate.png"), "crate.png");
});

test("one primary per row, from the server's flags", () => {
  const part = (over: Partial<Request>) => ({ state: "OPEN", answerable: true, confirmable: false, part: "count", ...over }) as Request;
  const row = (over: Partial<TaskItem>) => ({ kind: "request", tile: "to_answer", open: true, parts: [part({})], ...over }) as TaskItem;
  assert.equal(primaryLabel(row({})), "Answer request");
  assert.equal(primaryLabel(row({ tile: "to_confirm", parts: [part({ state: "ANSWERED", confirmable: true })] })), "Confirm count");
  assert.equal(primaryLabel(row({ tile: "to_confirm", parts: [part({ state: "ANSWERED", confirmable: true, part: "expiry" })] })),
    "Confirm printed date");
  assert.equal(primaryLabel(row({ tile: "to_confirm", parts: [part({ state: "ANSWERED", confirmable: false })] })), null);
  assert.equal(primaryLabel(row({ open: false })), null);
  assert.equal(primaryLabel(row({ kind: "hold", tile: "lot_holds" })), "Inspect and release");
  assert.equal(primaryLabel(row({ kind: "advisory", tile: "advisory" })), "Mark done");
});

test("a blocked dispatch names its reason and its action (D-36 item 1)", () => {
  assert.equal(blockedLine("lot_hold"), "Dispatch blocked · Resolve the lot hold.");
  assert.equal(blockedLine("review_flag"), "Dispatch blocked · Review new confirmed information.");
});
