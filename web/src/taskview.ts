// Tasks as one list (07-19; 07-CONTEXT D-36 item 3, D-05, D-06, D-09, D-13; 07-UI-SPEC "Tasks", "DeadlineBar",
// "Lifecycle"): the GET /api/tasks shape the page reads, and the pure words and arithmetic its rows show. The server
// orders and filters the list (07-SCREENS-MAP G5); nothing here sorts or filters it. Page-only words live here, as
// 06.1 keeps them in its page modules (G8); shared status words stay in vocab.ts and copy.ts.
import type { Request } from "./api.ts";
import type { TransferItem } from "./contract.ts";
import { recordedTime } from "./copy.ts";
import { CONFIRM_COUNT } from "./vocab.ts"; // defined once there (06.1-30); re-exported for the rows

export { CONFIRM_COUNT };

// --- the API shape (04-API-CONTRACT GET /api/tasks, 07-19 row) ---------------------------------------------------------

export type TaskTile = "to_answer" | "to_confirm" | "lot_holds" | "advisory" | "dispatches" | "receipts";
export type TaskKind = "request" | "hold" | "advisory" | "dispatch" | "receipt";
export type TaskViewName = "open" | "closed";

export type HoldView = {
  hold_id: string; lot_id: string; branch: string; kind: string; reason: string; placed_by: string | null;
  placed_at: string; owner_branch: string; state: string; resolved_by: string | null; resolved_at: string | null;
  resolution: string | null; note: string | null; signal_id: string | null; request_id: string | null;
  cases: { case_id: string; key: string; state: string }[]; placed_label: string; resolved_label: string | null;
};

export type AdvisoryView = {
  signal_id: string; report_id?: string | null; branch: string; text: string; state: string;
  fields: Record<string, unknown>; file: { type: string; size: number | null } | null;
  confirmed_at: string | null; provenance?: string | null; simulated_label?: string | null;
  task: { done_at: string | null; done_by: string | null; done_label: string | null } | null;
};

export type BlockReason = {
  kind: string; id: string | null; lot_ids: string[]; owner_branch: string | null; reason: string; resolution: string;
};

export type TaskItem = {
  key: string;
  kind: TaskKind;
  tile: TaskTile;
  open: boolean;
  urgent: boolean;
  blocked: boolean;
  blocked_reasons: BlockReason[];
  owner_branch: string | null;
  due_at: string | null;
  at: string | null;
  sku: string | null;
  parts: Request[];
  confirmations: Record<string, { at: string; labels: string[] }>;
  exposure: Record<string, number | null>;
  hold: HoldView | null;
  advisory: AdvisoryView | null;
  transfer: TransferItem | null;
};

export type TasksView = {
  tiles: Record<TaskTile, number>;
  list: { view: TaskViewName; tile: TaskTile | null; items: TaskItem[]; page: number; page_size: number; total: number };
  server_now: string;
};

// GET /api/tasks for one view, tile and page; the server answers in its own order.
export function tasksPath(view: TaskViewName, tile: TaskTile | null, page: number): string {
  const q = new URLSearchParams({ view });
  if (tile !== null) q.set("tile", tile);
  if (page > 1) q.set("page", String(page));
  return `/api/tasks?${q.toString()}`;
}

// --- the words (07-UI-SPEC "Tasks", "Copywriting Contract"; D-36's tile names) -----------------------------------------

export const TILE_ORDER: readonly TaskTile[] = ["to_answer", "to_confirm", "lot_holds", "advisory", "dispatches", "receipts"];
export const TILE_LABEL: Readonly<Record<TaskTile, string>> = {
  to_answer: "Checks to answer",
  to_confirm: "Answers to confirm",
  lot_holds: "Lot holds",
  advisory: "Advisory tasks",
  dispatches: "Dispatches",
  receipts: "Receipts",
};
export const TILES_LABEL = "Filter tasks"; // the tiles' group name (proposed)
export const VIEW_LABEL = "Show"; // the Open | Closed control's name (proposed)
export const VIEW_OPTIONS: readonly (readonly [TaskViewName, string])[] = [["open", "Open"], ["closed", "Closed"]];

export const noOpenTasks = (store: string) => `No open tasks for ${store}`;
export const NO_OPEN_BODY = "Count and expiry checks addressed to your store appear here, each with its response "
  + "deadline. Lot holds you own and advisory tasks appear here too.";
export const NO_CLOSED = "No closed tasks yet.";
export const NONE_IN_TILE = "Nothing here right now."; // a tile with no open items (proposed)
// 07-SCREENS-MAP T5: one HQ line, "store managers" as 06.1 says it, plus the Business events pointer.
export const TASKS_HQ_P7 = "Tasks go to store managers. Transport reports for HQ are under Business events.";
export const BUSINESS_EVENTS = "Business events";

export const COUNT_STOCK = "Count stock";
export const CHECK_EXPIRY = "Check printed expiry date";
export const ANSWER_REQUEST = "Answer request";
export const CONFIRM_DATE = "Confirm printed date";
export const INSPECT_RELEASE = "Inspect and release";
export const MARK_DONE = "Mark done";
export const SIMULATED_REQUEST = "Simulated request";
export const PHOTO_NOT_COUNT = "Photo · not a count";
export const PHOTO_ALONE_COUNT = "A photo alone doesn't confirm a count. Enter both numbers to complete it.";
export const PHOTO_ALONE_DATE = "A photo alone doesn't confirm the date.";
export const MIXED_DATES = "Mixed dates in this lot";
export const PRINTED_DATE = "Printed date";
export const PRINTED_DATE_BOX = "Printed expiry date (business date)";
export const PRINTED_DATE_HELP = "As printed on the pack, in the dataset's calendar";
export const CHECKED_LOT = "I checked the specified lot";
export const SEND_AND_HOLD = "Send and hold lot";
export const CANT_CHECK = "Can't check";
export const KEEP_OPEN = "Keep request open";
export const GO_BACK = "Go back";
export const CANT_CHECK_SENTENCE = "Close this request without an answer? The fact stays unverified, and the requester can ask again.";
export const RELEASE_HOLD = "Release hold";
export const KEEP_HOLD = "Keep hold";
// The server asks for an inspection note on every release (POST /api/holds/{id}/release `note`).
export const RELEASE_NOTE = "What did you find?"; // (proposed)
export const ADD_NOTE = "Add a note"; // (proposed) the disabled confirm's reason
export const ADVISORY_LINE = "Advisory · no hold, no commitment block";
export const ADVISORY_CHIP = "Advisory task";
export const LOT_HOLD = "Lot hold";
export const WHY_IT_MATTERS = "Why it matters:";
export const HISTORY = "History";
export const LIFECYCLE_LABEL = "Request progress";
// D-61 / G3: the 07-UI-SPEC note with "case" written "stock decision".
export const CONFIRM_NOTE_P7 = "Confirming approves nothing. It re-plans every affected open stock decision, which "
  + "cancels any approval in progress. Committed stock decisions in scope are flagged for review.";
export const MIXED_HOLD_HELP = "After separating the stock, report its printed date with Report an observation under "
  + "Business events and confirm it; or inspect the lot and release the hold."; // (proposed)

// The polite status line after the visitor's own write moved a row to Closed (07-UI-SPEC "Success"; proposed).
export const cantCheckDone = (lot: string) => `Request for lot ${lot}: Can't check. The fact stays unverified.`;
export const unchangedDone = (lot: string) => `Confirmed for lot ${lot}. It matches the confirmed evidence, so nothing changed.`;
export const CLOSED_HINT = "It is now under Closed."; // (proposed)

export const partVerb = (part: Request["part"]) => (part === "expiry" ? CHECK_EXPIRY : COUNT_STOCK);
export const partLane = (part: Request["part"]) => (part === "expiry" ? "Expiry" : "Count");
export const sendPart = (part: Request["part"]) => (part === "expiry" ? "Send expiry answer" : "Send count answer");

// "Separate stock and verify before moving" lot hold sentences (07-UI-SPEC "Consequential actions").
export const mixedSentence = (lot: string, store: string) => `This places a lot hold on lot ${lot}: Separate stock and `
  + `verify before moving. Stock decisions involving this lot can't be committed or prepared until the ${store} `
  + "manager resolves it.";
export const releaseSentence = (lot: string) => `Release the hold on lot ${lot}? Release it only after inspecting the `
  + "stock. Stock decisions involving this lot can then be committed again.";
export const markDoneNote = (store: string) => `Closes this task for ${store}. A lot hold, if any, stays.`;
export const tickError = (lot: string) => `Tick the box to confirm you checked lot ${lot}.`;
export const forDecision = (key: string) => `for stock decision ${key}`;
export const ownerLine = (store: string | null) => (store === null ? "" : `Owner: ${store} manager`);
export const replacedBy = (id: string) => `Replaced by request ${id}`;
export const countSource = (store: string, checked: string) => `Count reported by ${store}${checked === "" ? "" : ` · checked ${checked}`}`;
export const dateSource = (store: string, checked: string) => `Printed date reported by ${store}${checked === "" ? "" : ` · checked ${checked}`}`;

// D-36 item 1: a blocked dispatch names its reason and its action.
export function blockedLine(kind: string): string {
  if (kind === "lot_hold") return "Dispatch blocked · Resolve the lot hold.";
  if (kind === "review_flag") return "Dispatch blocked · Review new confirmed information.";
  return "Dispatch blocked";
}

// Titles stay within 90 characters collapsed (R-5); the full text is the element's title.
export const TITLE_MAX = 90;
export function clip(s: string, max = TITLE_MAX): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

// A file name middle-truncated to `max` characters (07-UI-SPEC FilePick), the full name in `title`.
export function middle(s: string, max = 32): string {
  if (s.length <= max) return s;
  const keep = max - 1;
  const head = Math.ceil(keep / 2);
  return `${s.slice(0, head)}…${s.slice(s.length - (keep - head))}`;
}

// "Count stock · lot L-1", "Check printed expiry date · lot L-1", "Count stock + Check printed expiry date · lot L-1".
export function requestTitle(parts: Pick<Request, "part" | "lot_ids">[]): string {
  const verbs = parts.map((p) => partVerb(p.part));
  const lots = parts[0]?.lot_ids ?? [];
  return `${[...new Set(verbs)].join(" + ")} · lot ${lots.join(", ")}`;
}
export const holdTitle = (lot: string) => `Lot hold · lot ${lot}`;
export const advisoryTitle = (sku: string | null) => `Advisory task${sku === null ? "" : ` · ${sku}`}`;

// --- the deadline (07-CONTEXT D-06; 07-UI-SPEC DeadlineBar) -----------------------------------------------------------

// Milliseconds left before `due`, from the server's time in the same response plus the time elapsed on this device
// since that response arrived (`receivedAt`, `nowMs` from the same device clock): a wrong device clock never moves
// it. null when a time cannot be read.
export function msLeft(due: string, serverNow: string, receivedAt: number, nowMs: number): number | null {
  const d = Date.parse(due);
  const s = Date.parse(serverNow);
  if (Number.isNaN(d) || Number.isNaN(s)) return null;
  return d - (s + Math.max(0, nowMs - receivedAt));
}

// "{N} min left" (floored), "Less than 1 min left" under a minute, "Overdue" from the deadline on (display only).
export function barText(ms: number): string {
  if (ms >= 60_000) return `${Math.floor(ms / 60_000)} min left`;
  if (ms > 0) return "Less than 1 min left";
  return "Overdue";
}

// The fill: the share of asked → due that has passed at the server-adjusted now, 0 to 1.
export function barShare(asked: string, due: string, ms: number): number {
  const span = Date.parse(due) - Date.parse(asked);
  if (!(span > 0)) return 1;
  return Math.min(1, Math.max(0, (span - ms) / span));
}

// Where a time falls on asked → due, 0 to 1 (an answer marker); above 1 for a late answer.
export function barAt(asked: string, due: string, at: string): number {
  const span = Date.parse(due) - Date.parse(asked);
  if (!(span > 0)) return 1;
  return Math.max(0, (Date.parse(at) - Date.parse(asked)) / span);
}

// "42 minutes left" for the Lifecycle's Respond by item (the bar is aria-hidden).
export function minutesWords(ms: number): string {
  if (ms >= 120_000) return `${Math.floor(ms / 60_000)} minutes left`;
  if (ms >= 60_000) return "1 minute left";
  if (ms > 0) return "less than 1 minute left";
  return "overdue";
}

// "Asked: {t} · Respond by: {t} · Checked: {t} · Answered: {t} · Confirmed: {t}" (07-CONTEXT Specifics), "—" for a
// time that has not happened; times in the session's zone.
export type Stamps = { asked: string | null; due: string | null; checked: string | null; answered: string | null; confirmed: string | null };
export function stampsLine(t: Stamps, timeZone: string): string {
  const f = (iso: string | null) => (iso === null ? "—" : recordedTime(iso, timeZone) || "—");
  return `Asked: ${f(t.asked)} · Respond by: ${f(t.due)} · Checked: ${f(t.checked)} · Answered: ${f(t.answered)} · `
    + `Confirmed: ${f(t.confirmed)}`;
}

// "20:30" (HH:MM in the session's zone) for a Lifecycle caption; "—" when it has not happened.
export function hhmmIn(iso: string | null, timeZone: string): string {
  if (iso === null) return "—";
  const full = recordedTime(iso, timeZone);
  return full === "" ? "—" : full.slice(-5);
}

// The row's one primary for the signed-in store (R-4), or null when the row has none.
export function primaryLabel(item: Pick<TaskItem, "kind" | "tile" | "open" | "parts">): string | null {
  if (!item.open) return null;
  if (item.kind === "request") {
    if (item.tile === "to_answer") return item.parts.some((p) => p.answerable) ? ANSWER_REQUEST : null;
    const waiting = item.parts.find((p) => p.state === "ANSWERED" && p.confirmable);
    if (waiting === undefined) return null;
    return waiting.part === "expiry" ? CONFIRM_DATE : CONFIRM_COUNT;
  }
  if (item.kind === "hold") return INSPECT_RELEASE;
  if (item.kind === "advisory") return MARK_DONE;
  return null;
}
