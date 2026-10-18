// The words every 06.1 page shares (06.1-UI-SPEC "Status vocabulary", "Times and dates", "History", "On-screen terms";
// 06.1-CONTEXT D-19, D-39, D-57, D-58, D-61, D-63). One vocabulary (R-2): each label is defined once, here, or by
// 05.2 in copy.ts (NEEDS_EVALUATION, TRANSFERS_UNAVAILABLE, restrictedFootnote), which pages import from there and
// this file never redefines. Strings in bold quotes in the UI-SPEC are the user's or locked upstream and are typed
// word for word; a string marked "(proposed)" below is the UI-SPEC's or Claude's wording, kept behind its own named
// constant so the user's change is one line.
// Times: a recorded time (when a person or the server acted) reads "Recorded {d Mon yyyy, HH:MM} {City} time" in the
// session's IANA zone; an operational date reads "Scenario date: {Ddd d Mon yyyy}". No helper here composes "today",
// "yesterday", "tomorrow" or "N hours ago" beside a date (D-58), and none shows minutes left on a hold (D-57).
// Pure, erasable syntax only, so `node --test` runs it.
import type { CaseState, Session } from "./api.ts";
import type { MovementState } from "./contract.ts";
import { datasetMark, recordedTime } from "./copy.ts";
import { planKind } from "./derive.ts";
import { LOCALE, markDate, units } from "./format.ts";
import type { IconName, Tone } from "./ui.tsx";

// --- the two clocks (UI-SPEC "Times and dates"; D-19, D-58, D-63) ---------------------------------------------------

// The city of an IANA zone as people say it: "Asia/Bangkok" → "Bangkok", "America/New_York" → "New York". Read from
// the session's time_zone, never typed, so another zone needs no code change.
export function cityOf(tz: string): string {
  const last = tz.split("/").pop() ?? tz;
  return last.replace(/_/g, " ");
}

// "Recorded 17 Oct 2026, 02:48 Bangkok time" from an ISO UTC instant in the session's zone; "" when the instant or the
// zone cannot be read (copy.recordedTime's rule), so no half line is ever shown.
export function recordedLine(iso: string, tz: string): string {
  const at = recordedTime(iso, tz);
  return at === "" ? "" : `Recorded ${at} ${cityOf(tz)} time`;
}

// "Scenario date: Sat 17 Oct 2026" from the ISO date of the public sales scenario (format.markDate: the UTC calendar,
// so the browser's own zone and clock never move it).
export function scenarioDateLine(iso: string): string {
  return `Scenario date: ${markDate(iso)}`;
}

// --- history (UI-SPEC "History (D-50, D-58, D-39)") -----------------------------------------------------------------

// The account behind an entry that no person wrote: the screen or the engine (proposed, UI-SPEC History).
export const BY_SYSTEM = "ShelfShift";

// A contract-shaped activity entry (04-API-CONTRACT "Product mode (06.1)" → activity entries add `actor` and
// `scenario_date`): `actor` is null for the screen and the engine; `scenario_date` is the ISO date of `day`.
export type HistoryActor = { account: string; role: "manager" | "hq"; branch: string | null };
export type HistoryEntry = {
  type: string;
  at: string;
  day: number | null;
  scenario_date: string | null;
  actor: HistoryActor | null;
  payload: Record<string, unknown>;
  group: string;
};
export type HistoryLine = { title: string; meta: string; detail: string; group: string };

// Each activity type in words (UI-SPEC History's event titles; the researcher-written titles are proposed, UI-SPEC
// "Open questions" note). `decision` takes its verb from the payload (DECISION_TITLES) and `prepared` names the kind
// of work order from the plan (the D-61 term; never the bare internal "work order", UI-SPEC open question 3). The
// stock-check types use the D-61 term so the internal "evidence" name never reaches the screen. An unknown type
// reads as the type in words, never the raw key.
export const EVENT_TITLES: Readonly<Record<string, string>> = {
  opened: "Opened",
  ownership_taken: "Ownership taken",
  sent: "Sent for approval",
  committed: "Booking confirmed",
  prepared: "Transfer work order prepared",
  dispatched: "Dispatch recorded",
  received: "Receipt recorded",
  discrepancy_opened: "Discrepancy opened",
  discrepancy_reconciled: "Discrepancy reconciled",
  price_applied: "Price change recorded",
  closed: "Closed",
  rejected: "Rejected",
  new_version: "New version",
  approval_cancelled: "Approval cancelled · new version",
  hold_expired: "Capacity hold expired",
  evidence_requested: "Stock check requested",
  evidence_answered: "Stock check answered",
  count_confirmed: "Count confirmed",
  assumptions_adopted: "Assumptions adopted",
  settings_changed: "Policy settings changed",
};
export const DECISION_TITLES: Readonly<Record<string, string>> = {
  approve: "Approved", accept: "Accepted", reject: "Rejected", decline: "Declined",
};
// A decision entry whose verb this version does not know (proposed).
export const DECISION_RECORDED = "Decision recorded";
// A markdown plan's prepared work order (UI-SPEC open question 3, recommended default; proposed).
export const MARKDOWN_PREPARED = "Markdown work order prepared";

// "some_new_event" → "Some new event".
function inWords(key: string): string {
  const w = key.replace(/_/g, " ").trim();
  return w.charAt(0).toUpperCase() + w.slice(1);
}

function eventTitle(e: Pick<HistoryEntry, "type" | "payload">): string {
  if (e.type === "decision") {
    const d = e.payload.decision;
    return (typeof d === "string" ? DECISION_TITLES[d] : undefined) ?? DECISION_RECORDED;
  }
  if (e.type === "prepared" && typeof e.payload.plan === "string" && planKind(e.payload.plan) === "MARKDOWN") {
    return MARKDOWN_PREPARED;
  }
  return EVENT_TITLES[e.type] ?? inWords(e.type);
}

// The resources a send holds, in words (UI-SPEC History: "Held: stock, truck space, storage, price change").
const RESOURCE_WORDS: Readonly<Record<string, string>> = {
  SOURCE_LOT: "stock", TRUCK: "truck space", STORAGE: "storage", LOT_PRICE: "price change", DEMAND: "demand",
};
// The word for a hash-valued key: the recommendation version reads as the Options version line does.
const HASH_WORDS: Readonly<Record<string, string>> = { rec_version: "Recommendation" };
const HASH = /^[0-9a-f]{64}$/i;
const HASH_SHOWN = 12; // D-39: a 12-character hash, never 64

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v.trim() !== "" ? v : null);
const qty = (n: number) => `${units(n)} ${n === 1 ? "unit" : "units"}`;

// The detail line in words, from the keys the entry carries: the plan's label, the quantities and lots, the
// resources held, a person's reason, and every 64-character hash cut to 12. Raw keys, enums and plan keys never show.
function detailOf(e: Pick<HistoryEntry, "type" | "payload">): string {
  const p = e.payload;
  const parts: string[] = [];
  const label = str(p.label) ?? str(p.plan_label);
  if (label !== null) parts.push(label);
  const lot = str(p.lot_id);
  if (e.type === "dispatched" && num(p.qty) !== null) {
    parts.push(`${qty(num(p.qty) as number)} dispatched${lot === null ? "" : ` from ${lot}`}`);
  } else if (e.type === "received" && num(p.received_qty) !== null) {
    const into = str(p.new_lot_id);
    parts.push(`${qty(num(p.received_qty) as number)} received${into === null ? "" : ` into ${into}`}`);
    const damaged = num(p.damaged_qty);
    if (damaged !== null && damaged > 0) parts.push(`${qty(damaged)} damaged`);
  } else if (num(p.units) !== null) {
    parts.push(qty(num(p.units) as number));
  }
  if (e.type === "price_applied" && num(p.pct) !== null) {
    parts.push(`${units(num(p.pct))}% off${lot === null ? "" : ` ${lot}`}`);
  }
  if (Array.isArray(p.held) && p.held.length > 0) {
    parts.push(`Held: ${p.held.map((k) => RESOURCE_WORDS[String(k)] ?? inWords(String(k)).toLowerCase()).join(", ")}`);
  }
  const reason = str(p.reason);
  if (reason !== null) parts.push(reason);
  for (const [k, v] of Object.entries(p)) {
    if (typeof v === "string" && HASH.test(v)) parts.push(`${HASH_WORDS[k] ?? inWords(k)} ${v.slice(0, HASH_SHOWN)}`);
  }
  return parts.join(" · ");
}

// One history entry as two lines plus a detail (UI-SPEC History; D-63: both clocks always, even when the dates
// match): title "{Dispatch recorded} by {S042}" (the account code, "HQ", or BY_SYSTEM for no actor); meta
// "Recorded {17 Oct 2026, 02:48} Bangkok time · Scenario date: {Sat 17 Oct 2026}" with the city from the zone; detail
// in words. A part the entry cannot give is left out rather than shown as a placeholder.
export function historyLine(e: HistoryEntry, tz: string): HistoryLine {
  const by = e.actor === null ? BY_SYSTEM : e.actor.account;
  const meta = [recordedLine(e.at, tz), e.scenario_date === null ? "" : scenarioDateLine(e.scenario_date)]
    .filter((x) => x !== "").join(" · ");
  return { title: `${eventTitle(e)} by ${by}`, meta, detail: detailOf(e), group: e.group };
}

// --- who is signed in (UI-SPEC "Shell": context line, profile row and menu; D-08 as amended by D-63) ---------------

export type Role = Session["role"];
export type Account = { account: string; role: Role };

// The role under the account in the profile row and at the end of the context line.
export const ROLE_LABEL: Readonly<Record<Role, string>> = { manager: "Store manager", hq: "HQ planner" };

// The page header's context line: "S042 · Scenario date: Sat 17 Oct 2026 · Store manager"; HQ: "HQ · Scenario date:
// … · HQ planner". The date is the session's data_as_of (never the browser's clock); with none (the synthetic set) the
// date part is left out, never shown as a placeholder.
export function contextLine(s: Account & Pick<Session, "data_as_of">): string {
  return [s.account, s.data_as_of === null ? "" : scenarioDateLine(s.data_as_of), ROLE_LABEL[s.role]]
    .filter((x) => x !== "").join(" · ");
}

// The mark as two lines (UI-SPEC Shell → Head): copy.datasetMark's one line (the session's label, reference data,
// with "{date}" filled by markDate) split before " · Scenario date:", so line 1 is the dataset and scenario kind and
// line 2 the scenario date. A label with no scenario date (the synthetic set, or no data_as_of) gives one line. No
// part of the mark is typed here.
const DATE_PART = " · Scenario date:";
export function markLines(s: Pick<Session, "label" | "data_as_of">): string[] {
  const mark = datasetMark(s);
  const at = mark.indexOf(DATE_PART);
  return at < 0 ? [mark] : [mark.slice(0, at), mark.slice(at + " · ".length)];
}

// The owner of a stock decision someone else took: "Owner: S042 manager" or "Owner: HQ planner" (UI-SPEC Ownership).
export function ownerText(o: Account): string {
  return `Owner: ${ownerName(o)}`;
}
const ownerName = (o: Account) => (o.role === "hq" ? ROLE_LABEL.hq : `${o.account} manager`);

// The profile menu's who block (UI-SPEC Shell → Profile menu): the manager's line is the sketch shell's; HQ's is
// proposed.
export const PROFILE_WHO_HQ = "All branches · can take ownership across branches and change policies"; // proposed
export const PROFILE_WHO = {
  manager: (code: string) => `Branch: ${code} · can approve outgoing stock, accept incoming stock, request stock checks`,
  hq: () => PROFILE_WHO_HQ,
};
// [name, who]: ["S042 manager", "Branch: S042 · …"] or ["HQ planner", "All branches · …"].
export function profileWho(o: Account): [string, string] {
  return o.role === "hq" ? [ROLE_LABEL.hq, PROFILE_WHO.hq()] : [ownerName(o), PROFILE_WHO.manager(o.account)];
}

// --- first-stage labels (UI-SPEC "First-stage labels"; D-54). The flagged label is 05.2's copy.NEEDS_EVALUATION -
export const NEEDS_ACTION = "Needs action";
export const NEEDS_DECISION = "Needs decision"; // shown once Phase 7's lot_status exists
export const AWAITING_VERIFICATION = "Awaiting verification"; // shown once Phase 7's lot_status exists

// --- ownership (UI-SPEC "Ownership"; D-25, D-67) ------------------------------------------------------------------
export const NOT_TAKEN = "Not taken yet";
export const YOURS = "Yours";

// --- booking holds (UI-SPEC "Booking holds"; D-57: a static time, no countdown) ------------------------------------

// "14:35" for an ISO instant in the zone, 24-hour; null when the instant or the zone cannot be read.
function clockIn(iso: string, tz: string): string | null {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  try {
    const parts = new Intl.DateTimeFormat(LOCALE, { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
      .formatToParts(at);
    const part = (t: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === t)?.value ?? "";
    return `${part("hour")}:${part("minute")}`;
  } catch {
    return null;
  }
}

// "Capacity held until 14:35 Bangkok time." from the hold's `until` in the session's zone; null when it cannot be
// read. Never minutes left: the hold refreshes at its time from the server's clock (hooks.ts), it never counts down.
export function holdHeld(until: string, tz: string): string | null {
  const t = clockIn(until, tz);
  return t === null ? null : `Capacity held until ${t} ${cityOf(tz)} time.`;
}
export const HOLD_EXPIRED = "Capacity hold expired. Check availability and book again.";
export const CHECK_AVAILABILITY = "Check availability";
export const BOOKING_CONFIRMED = "Booking confirmed.";

// --- dispatch, movement and receipt (UI-SPEC "Dispatch, movement and receipt"; D-33, D-48, D-49, D-52, D-65) --------
// Decision status and movement status are separate chips and never merge (D-33).
export const READY_TO_DISPATCH = "Ready to dispatch"; // proposed
export const DISPATCH_BLOCKED = "Dispatch blocked"; // proposed
export const PLANNED = "Planned";
export const DISPATCHED = "Dispatched";
export const RECEIVED = "Received";
export const PARTIALLY_RECEIVED = "Partially received";
export const RECEIPT_DISCREPANCY = "Receipt discrepancy—reconciliation required.";
export const DISCREPANCY_RECONCILED = "Discrepancy reconciled"; // proposed
export const EXPIRED = "Expired";
export const CANCELLED = "Cancelled";
export const NO_TRANSPORT = "No transport assigned.";
export const NO_TRANSFERS = "No transfers assigned";
export const REVIEW_BEFORE_DISPATCH = "Review before dispatch";
export const NEW_CONFIRMED_INFO = "New confirmed information · review required";
export const AFTER_DISPATCH_REVIEW = "New confirmed information · operational exception · review"; // proposed
export const DISPATCH_CHECKS_UNAVAILABLE = "Dispatch checks unavailable";

// "Short shipment · 104 of 110": dispatched below the committed quantity.
export function shortShipment(dispatched: number, committed: number): string {
  return `Short shipment · ${units(dispatched)} of ${units(committed)}`;
}

// --- data kinds (UI-SPEC "Data kinds"; D-15). Public values carry no chip; "Designed example" is 05.2's
// copy.DESIGNED_EXAMPLE -----------------------------------------------------------------------------------------------
export const DATA_KIND = {
  simulated: "Simulated",
  estimated: "Estimated",
  forecast: "Forecast · precomputed",
  assumption: "Assumption",
} as const;

// --- planned times (UI-SPEC "Times and dates"; D-18) ----------------------------------------------------------------
export const UNSCHEDULED = "Unscheduled";
export const NOT_YET_RECORDED = "Not yet recorded";
export const PLANNED_NOTE = "Planned times. Actual times appear when someone records the work.";

// The stock decision page's date line (UI-SPEC open question 1, recommended default; proposed): "This stock decision
// is for scenario date Sat 17 Oct 2026." from the ISO scenario date.
export function DECISION_DATE_LINE(iso: string): string {
  return `This stock decision is for scenario date ${markDate(iso)}.`;
}

// --- on-screen terms (UI-SPEC "On-screen terms"; D-61; API and database names unchanged) ----------------------------
// A markdown's work order (UI-SPEC open question 3, recommended default; proposed).
export const MARKDOWN_WORK_ORDER = "Markdown work order";
export const TERMS = {
  STOCK_DECISION: "Stock decision",
  BRANCH_TRANSFER: "Branch transfer",
  TRANSFER_WORK_ORDER: "Transfer work order",
  MARKDOWN_WORK_ORDER,
  STOCK_CHECK: "Stock check",
  SCHEDULED_TRUCK_RUN: "Scheduled truck run",
  DECISION_BOARD: "Decision board",
  OPEN_STOCK_DECISION: "Open stock decision",
  VIEW_STOCK_DECISION: "View stock decision",
} as const;
export const BACK_TO_BOARD = "Back to the Decision board";

// --- the next action (04-API-CONTRACT "Card and detail keys": next_action.key) --------------------------------------
export const EVALUATE_LABEL = "Evaluate this item"; // proposed
export const PREPARE_LABEL = "Prepare work order"; // proposed: the key names no plan kind
export const RECONCILE_LABEL = "Reconcile discrepancy"; // proposed
export const ANSWER_STOCK_CHECK = "Answer stock check"; // (proposed, 06.1-20) answer_stock_check: the request is OPEN
export const CONFIRM_COUNT = "Confirm count"; // confirm_stock_check: the Phase 5 action's name, kept
export type NextActionKey =
  | "evaluate" | "take_ownership" | "send" | "check_availability" | "approve" | "accept" | "prepare"
  | "record_dispatch" | "record_receipt" | "record_price" | "reconcile" | "close"
  | "answer_stock_check" | "confirm_stock_check"; // the two stock-check keys (06.1-13)
export const NEXT_ACTION_LABEL: Readonly<Record<NextActionKey, string>> = {
  evaluate: EVALUATE_LABEL,
  take_ownership: "Take ownership",
  send: "Review and send",
  check_availability: CHECK_AVAILABILITY,
  approve: "Approve plan",
  accept: "Accept transfer",
  prepare: PREPARE_LABEL,
  record_dispatch: "Record dispatch",
  record_receipt: "Record receipt",
  record_price: "Record price change",
  reconcile: RECONCILE_LABEL,
  close: "Close stock decision",
  answer_stock_check: ANSWER_STOCK_CHECK,
  confirm_stock_check: CONFIRM_COUNT,
};

// --- the status vocabulary (R-2: one tone, icon and label per state, the same on every page; 06.1-30 hoist) --------
// Each label is defined here once and imported by the pages; "(proposed)" marks wording the user has not confirmed.

export type ChipLook = { tone: Tone; icon: IconName; text: string };

// A card's exception badges (06.1-17; D-61: these replace the Phase 5 "Hold expired" and "Evidence requested").
export const CAPACITY_HOLD_EXPIRED = "Capacity hold expired";
export const RECEIPT_DISCREPANCY_BADGE = "Receipt discrepancy";
export const STOCK_CHECK_REQUESTED = "Stock check requested";
export const MISSING_DATA = "Missing data";

// The decision's own state in words (06.1-29, proposed: the UI-SPEC names the Decision chip, not its words).
// "Committed" is the work order prepared after both sides approved: committed, not departed.
export const DECISION_CHIP: Readonly<Record<CaseState, ChipLook>> = {
  OPEN: { tone: "gray", icon: "circle", text: "Not sent" },
  WAITING: { tone: "gray", icon: "pause", text: "Waiting" },
  PENDING_APPROVAL: { tone: "info", icon: "clock", text: "Waiting for approvals" },
  APPROVED: { tone: "good", icon: "check", text: "Approved" },
  PREPARED: { tone: "good", icon: "check", text: "Committed" },
  EXECUTED: { tone: "good", icon: "check", text: "Executed" },
  OBSERVED: { tone: "gray", icon: "check", text: "Results recorded" },
  CLOSED: { tone: "gray", icon: "check", text: "Closed" },
  REJECTED: { tone: "bad", icon: "x", text: "Rejected" },
  INVALIDATED: { tone: "gray", icon: "redo", text: "Replaced" },
};
export const decisionLook = (state: string): ChipLook =>
  DECISION_CHIP[state as CaseState] ?? { tone: "gray", icon: "circle", text: state.replace(/_/g, " ").toLowerCase() };

// The movement's state, from what was recorded (06.1-29; UI-SPEC "Dispatch, movement and receipt"). "Discrepancy" is
// the Movement status filter's own word; the full sentence shows in the Movement box.
export const DISCREPANCY = "Discrepancy";
export const MOVEMENT_CHIP: Readonly<Record<MovementState, ChipLook>> = {
  planned: { tone: "plan", icon: "clock", text: PLANNED },
  dispatched: { tone: "info", icon: "truck", text: DISPATCHED },
  partially_received: { tone: "bad", icon: "alert", text: PARTIALLY_RECEIVED },
  received: { tone: "good", icon: "check", text: RECEIVED },
  discrepancy: { tone: "bad", icon: "alert", text: DISCREPANCY },
  expired: { tone: "gray", icon: "clock", text: EXPIRED },
  cancelled: { tone: "gray", icon: "x", text: CANCELLED },
};

// The planned schedule's recorded states in words (06.1-21). The labels above are reused, so a state reads the same on
// every page; the others are the schedule's (proposed).
export const SCHEDULE_STATUS: Readonly<Record<string, ChipLook>> = {
  open: { tone: "info", icon: "circle", text: "Open" },
  not_requested: { tone: "gray", icon: "circle", text: "Not requested" },
  answered: { tone: "info", icon: "inbox", text: "Answered" },
  confirmed: { tone: "good", icon: "check", text: "Confirmed" },
  expired: { tone: "gray", icon: "clock", text: EXPIRED },
  superseded: { tone: "gray", icon: "redo", text: "Replaced" },
  not_started: { tone: "gray", icon: "circle", text: "Not started" },
  scheduled: { tone: "plan", icon: "today", text: "Scheduled" },
  dispatched: { tone: "info", icon: "truck", text: DISPATCHED },
  partially_received: { tone: "bad", icon: "alert", text: PARTIALLY_RECEIVED },
  received: { tone: "good", icon: "check", text: RECEIVED },
  discrepancy: { tone: "bad", icon: "alert", text: DISCREPANCY },
  done: { tone: "good", icon: "check", text: "Done" },
  rejected: { tone: "bad", icon: "x", text: "Rejected" },
  closed: { tone: "gray", icon: "check", text: "Closed" },
  cancelled: { tone: "gray", icon: "x", text: CANCELLED },
  voided: { tone: "gray", icon: "x", text: "Voided" },
};
export const scheduleStatus = (s: string): ChipLook =>
  SCHEDULE_STATUS[s] ?? { tone: "gray", icon: "circle", text: s.replace(/_/g, " ") };

// --- words more than one page uses (06.1-30 hoist; each defined once) -----------------------------------------------

export const TAKE_OWNERSHIP_HELP = "Records you as the owner. It approves nothing."; // (proposed, 06.1-17, -18)
export const OWNED_BY = (label: string) => `Owned by the ${label}.`; // (proposed, 06.1-17, -18)
export const RELOAD_DECISION = "Reload the stock decision"; // stale_version's action (UI-SPEC; 05 copy, D-61 term)
export const CLEAR_FILTERS = "Clear filters";
export const ALL_BRANCHES = "all branches"; // (proposed, 06.1-20, -29) HQ's store where a line names one
export const IN_STORE = "in store"; // "{S042} · in store" for a decision with no destination
export const LOGISTICS = (run: string) => `Logistics (${run})`;
// A receipt's quantities (UI-SPEC Record receipt and Open discrepancy), each already formatted.
export const receiptLine = (dispatched: string, received: string, damaged: string, missing: string) =>
  `Dispatched ${dispatched} · Received ${received} · Damaged ${damaged} · Missing ${missing}`;
// The provider codes the forecast pipeline writes and the names it gives them (data/forecast.py PROVIDERS; 06.1-25).
// An unknown code is shown as sent, never guessed into a BigQuery name.
export const PROVIDER_NAMES: Readonly<Record<string, string>> = {
  ai_forecast: "TimesFM 2.5 via BigQuery AI.FORECAST",
  arima_plus: "ARIMA_PLUS via BigQuery ML.FORECAST",
  "local-seasonal-naive": "local seasonal-naive stand-in (development and tests only)",
};

// Per-series codes are supplied and ordered by the API (11-CONTEXT D-12); never infer a provider from the run.
export const PROVIDER_SHORT: Readonly<Record<string, string>> = {
  ai_forecast: "TimesFM 2.5", arima_plus: "ARIMA_PLUS", "local-seasonal-naive": "local seasonal-naive stand-in",
};
export const providerShort = (codes: string[]): string => codes.map(code => PROVIDER_SHORT[code] ?? code).join(" + ");
