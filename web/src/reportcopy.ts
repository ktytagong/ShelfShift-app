// Field reports on the store app (07-20; 07-CONTEXT D-04, D-13..D-16, D-20; 07-UI-SPEC surfaces 4 and 5): the Report
// shape and its fetchers (04-API-CONTRACT "Verify before you move (Phase 7)", the 07-20 row), and every word Business
// events, the Report an observation sheet and the report detail show. One module of its own, so the parallel Phase 7
// lanes never collide in api.ts or copy.ts (07-20 deviation); its words are page words (07-SCREENS-MAP G8).
// On-screen terms (G3, G4): "stock decision" for a case, "(scenario date)" on a business date; a person's action time
// is copy.recordedTime in the session's zone. The client never sorts or filters a list (G5): the server's query does.
// Erasable syntax only, so `node --test` runs this file as it is.
import { form, get, post, patch } from "./api.ts";
import { recordedTime } from "./copy.ts";
import { money, shortDate, units } from "./format.ts";
import type { IconName, Tone } from "./ui.tsx";
import { cityOf } from "./vocab.ts";

// --- the shapes (04-API-CONTRACT Shapes → Report; the 07-15 and 07-20 rows) ------------------------------------------

export type ReportKind = "CAPACITY" | "DEMAND_EVENT" | "TRANSPORT" | "STOCK_COUNT" | "EXPIRY" | "CONDITION" | "NOT_RELEVANT";
export type ReportState = "READ" | "CONFIRMED" | "DISMISSED" | "WITHDRAWN" | "REPLACED";
export type FieldValue = string | number | boolean | null;
export type ReportFields = Record<string, FieldValue>;
export type ReportOwner = { role: "manager" | "hq"; branch: string | null };

export type Report = {
  signal_id: string;
  report_id: string;
  revision: number;
  kind: ReportKind;
  state: ReportState;
  branch: string | null;
  reporter_account: string;
  reporter_role: "manager" | "hq";
  reporter_branch: string | null;
  owner: ReportOwner;
  text: string;
  fields: ReportFields;
  needs_input: { field: string }[];
  observed_at: string | null;
  created_at: string;
  case_id: string | null;
  corrects: string | null;
  file: { type: string; size: number | null } | null;
  provenance: "simulated" | "person";
  simulated_label: string | null;
  confirmed_at: string | null;
  confirmed_by: string | null;
  dismissed_at: string | null;
  dismissed_by: string | null;
  dismiss_reason: string | null;
  withdrawn_at: string | null;
  fact_id: string | null;
  fact_label: string | null;
  replaced_by: string | null;
  hold_id: string | null;
  task: { done_at: string | null; done_by: string | null; done_label: string | null } | null;
  impact_label: string | null;
  day: number;
  affects_count: number;
  sku: string | null;
  reading?: {reading_id: string; provenance: string; model_id: string; recorded_at: string; note: boolean; kind: string | null; suggestions: import("./api.ts").Suggestion[]} | null;
  field_sources?: Record<string, string>;
  field_corrections?: Record<string, {role: "manager" | "hq"; branch: string | null; revision: number}>;
  impact_summary?: import("./summaryview.ts").ImpactSummaryData | null;
};

export type Allowance = {
  reports_left: number; reports_limit: number; files_left: number; files_limit: number; resets: string | null;
};
export type ReportList = {
  signals: Report[]; page: number; page_size: number; total: number; allowance: Allowance; needs_my_confirmation: number;
};
export type PreviewRow = {
  case_id: string; key: string; state: string; rec_version: string | null; whatif_version: string | null;
  recommended_before: string | null; recommended_after: string | null; plan_now: string | null; plan_if: string | null;
  benefit_now: number | null; benefit_if: number | null; changed: boolean; allowed: boolean | null; error: string | null;
};
export type Preview = {
  preview_key?: string;
  signal_id: string | null; report_id: string | null; revision: number | null; kind: ReportKind; label: string;
  complete: boolean; message: string | null; cases: PreviewRow[]; more: number; more_text: string | null;
};
export type ReportDetail = Report & { revisions: Report[]; preview: Preview | null };
export type ReportConfirmed = {
  fact_id: string | null; labels: string[]; cases: { case_id: string; key: string; rec_version: string }[];
  flagged?: string[]; reassessed?: string[]; impact_label?: string | null;
};

// --- the fetchers ----------------------------------------------------------------------------------------------------

export type EventsQuery = {
  mine?: boolean; kind?: string; store?: string; state?: string; case?: string; page?: number; pageSize?: number;
};

// "/api/signals?mine=1&kind=CAPACITY&…": only the parameters given (blank ones left out; page 1 is the default).
export function eventsQuery(q: EventsQuery): string {
  const p = new URLSearchParams();
  const put = (k: string, v: string | number | boolean | undefined) => {
    if (v === undefined || v === false || (typeof v === "string" && v.trim() === "")) return;
    if (k === "page" && v === 1) return;
    p.set(k, v === true ? "1" : typeof v === "string" ? v.trim() : String(v));
  };
  put("mine", q.mine); put("kind", q.kind); put("store", q.store); put("state", q.state); put("case", q.case);
  put("page", q.page); put("page_size", q.pageSize);
  const text = p.toString();
  return text === "" ? "/api/signals" : `/api/signals?${text}`;
}

const at = (id: string) => `/api/signals/${encodeURIComponent(id)}`;
export const listReports = (q: EventsQuery) => get<ReportList>(eventsQuery(q));
export const getReport = (id: string) => get<ReportDetail>(at(id));
export const fileReport = (data: FormData, key: string) => form<Report>("/api/signals", data, key);
export const draftPreview = (body: { kind: ReportKind; fields: ReportFields; corrects?: string }, key: string) =>
  post<Preview>("/api/signals/preview", body, key);
export const previewReport = (id: string, revision: number, key: string) =>
  post<Preview>(`${at(id)}/preview`, { revision }, key);
export const confirmReport = (id: string, body: { revision: number; checked_both?: boolean }, key: string) =>
  post<ReportConfirmed>(`${at(id)}/confirm`, body, key);
export const dismissReport = (id: string, key: string) => post<Report>(`${at(id)}/dismiss`, {}, key);
export const withdrawReport = (id: string, key: string) => post<Report>(`${at(id)}/withdraw`, {}, key);
export const correctReport = (id: string, body: { revision: number; fields: ReportFields; kind?: ReportKind; observed?: string }, key: string) =>
  patch<Report>(at(id), body, key);
export const holdLot = (lotId: string, body: { reason: string; signal_id: string }, key: string) =>
  post<unknown>(`/api/lots/${encodeURIComponent(lotId)}/holds`, body, key);

// --- the kinds (07-UI-SPEC "Kind labels", Step 1 hints; HQ files transport only, spec §9) ---------------------------

export const KINDS: readonly ReportKind[] = ["CAPACITY", "DEMAND_EVENT", "TRANSPORT", "STOCK_COUNT", "EXPIRY", "CONDITION", "NOT_RELEVANT"];
export const KIND_LABEL: Readonly<Record<ReportKind, string>> = {
  CAPACITY: "Storage capacity",
  DEMAND_EVENT: "Demand event",
  TRANSPORT: "Transport",
  STOCK_COUNT: "Stock count",
  EXPIRY: "Printed expiry date",
  CONDITION: "Condition",
  NOT_RELEVANT: "Not relevant",
};
export const KIND_HINT: Readonly<Record<ReportKind, string>> = {
  CAPACITY: "A chiller or shelf space change",
  DEMAND_EVENT: "A local event or holiday",
  TRANSPORT: "A quote, a truck run not available, or a delay",
  STOCK_COUNT: "Units on the shelf and in the backroom",
  EXPIRY: "Dates on the packs",
  CONDITION: "Damage, a possible safety issue, or display",
  NOT_RELEVANT: "No operational fact",
};
// The kind marker's icon (no new icon is drawn in ui.tsx: the parallel lanes add theirs there).
export const KIND_ICON: Readonly<Record<ReportKind, IconName>> = {
  CAPACITY: "snow", DEMAND_EVENT: "trend", TRANSPORT: "truck", STOCK_COUNT: "box", EXPIRY: "today", CONDITION: "alert",
  NOT_RELEVANT: "info",
};
export const kindsFor = (role: "manager" | "hq"): readonly ReportKind[] => (role === "hq" ? ["TRANSPORT"] : KINDS.filter(k => k !== "NOT_RELEVANT"));
export const LOT_KINDS: readonly ReportKind[] = ["STOCK_COUNT", "EXPIRY", "CONDITION"];
export const CHECKED_KINDS: readonly ReportKind[] = ["STOCK_COUNT", "EXPIRY"]; // "When did you check?" (D-22)

// --- the fields (07-UI-SPEC Step 2; 04-API-CONTRACT POST /api/signals) -----------------------------------------------

export const FIELD_LABEL: Readonly<Record<string, string>> = {
  store: "Store",
  product: "Product",
  zone: "Zone",
  free_totes: "Available totes",
  valid_from: "Valid from",
  valid_to: "Valid to",
  sku: "Product (optional)",
  multiplier: "Expected demand multiplier",
  run_code: "Scheduled truck run",
  what: "What happened",
  amount: "Quote amount",
  pricing_basis: "Pricing basis",
  includes_handling: "Includes handling",
  applies_on: "Applies on",
  valid_until: "Valid until",
  delay_hours: "Delay (hours)",
  lot_id: "Lot",
  shelf_units: "Shelf",
  backroom_units: "Backroom",
  printed_date: "Printed date",
  condition: "Condition",
};
export const DATE_FIELDS: ReadonlySet<string> = new Set(["valid_from", "valid_to", "applies_on", "valid_until", "printed_date"]);
export const NUMBER_FIELDS: ReadonlySet<string> = new Set(["free_totes", "multiplier", "amount", "delay_hours", "shelf_units", "backroom_units"]);
export const CHOICES: Readonly<Record<string, readonly (readonly [string, string])[]>> = {
  zone: [["chilled", "Chilled"], ["ambient", "Ambient"]],
  what: [["QUOTE", "Price quote"], ["RUN_NOT_AVAILABLE", "Scheduled truck run not available"], ["DELAY", "Delay"]],
  pricing_basis: [["PER_TRIP", "Per trip"], ["PER_UNIT", "Per unit"]],
  includes_handling: [["YES", "Yes"], ["NO", "No"], ["UNKNOWN", "Unknown"]],
  condition: [["DAMAGE", "Damage"], ["SAFETY", "Possible safety issue"], ["DISPLAY", "Display issue"]],
};
export const choiceLabel = (field: string, v: FieldValue): string =>
  CHOICES[field]?.find(([k]) => k === v)?.[1] ?? (v === null ? NONE : String(v));
export const MIXED = "MIXED"; // the printed date's "Mixed dates in this lot" value (D-11)
export const MIXED_DATES = "Mixed dates in this lot";
export const REQUIRED = " (required)";
export const SCENARIO_DATE = " (scenario date)"; // G4: a business date is a scenario date
export const DATE_HELP = "In the dataset's calendar";
export const NOT_CHECKED = "Not checked";
export const NONE = "—";

// What a transport report needs besides the run, what happened and the day it applies on (reports.TRANSPORT_NEEDS).
const TRANSPORT_NEEDS: Readonly<Record<string, readonly string[]>> = {
  QUOTE: ["amount", "pricing_basis", "includes_handling", "valid_until"], RUN_NOT_AVAILABLE: [], DELAY: ["delay_hours"],
};

// The kind's fields the sheet shows, in order, given what is filled (a transport report by what happened).
export function fieldsOf(kind: ReportKind, f: ReportFields): string[] {
  switch (kind) {
    case "CAPACITY": return ["zone", "free_totes", "valid_from", "valid_to"];
    case "DEMAND_EVENT": return ["sku", "multiplier", "valid_from", "valid_to"];
    case "TRANSPORT": return ["run_code", "what", ...(TRANSPORT_NEEDS[String(f.what)] ?? []), "applies_on"];
    case "STOCK_COUNT": return ["lot_id", "shelf_units", "backroom_units"];
    case "EXPIRY": return ["lot_id", "printed_date"];
    case "CONDITION": return ["lot_id", "condition"];
    case "NOT_RELEVANT": return [];
  }
}

// The fields the kind needs before it can be previewed or confirmed (the server's `_required`; it decides).
export function requiredOf(kind: ReportKind, f: ReportFields): string[] {
  return fieldsOf(kind, f).filter((n) => !(kind === "DEMAND_EVENT" && n === "sku"));
}

const blank = (v: FieldValue | undefined) => v === null || v === undefined || (typeof v === "string" && v.trim() === "");

// The required fields still blank (a quote's handling left Unknown counts, as the server's needs_input does).
export function missingOf(kind: ReportKind, f: ReportFields): string[] {
  return requiredOf(kind, f).filter((n) => blank(f[n]) || f[n] === "UNKNOWN" || (n === "printed_date" && f[n] === MIXED));
}

// The JSON `fields` part: the shown fields only, numbers as numbers, blanks as null (never 0, D-14).
export function fieldsBody(kind: ReportKind, f: ReportFields, hold: boolean): ReportFields {
  const out: ReportFields = {};
  for (const n of fieldsOf(kind, f)) {
    const v = f[n];
    if (blank(v)) out[n] = null;
    else if (NUMBER_FIELDS.has(n)) out[n] = Number(v);
    else out[n] = typeof v === "string" ? v.trim() : v;
  }
  if (kind === "CONDITION" && (f.condition === "DAMAGE" || f.condition === "SAFETY")) out.hold = hold;
  return out;
}

// --- the sheet's words (07-UI-SPEC surface 5; Consequential actions) -------------------------------------------------

export const REPORT_AN_OBSERVATION = "Report an observation";
export const STEPS = ["Kind", "Details", "Preview"] as const;
export const KIND_LEGEND = "Kind";
export const SUBMIT = "Submit report";
export const CLOSE_WITHOUT = "Close without submitting";
export const TEXT_LABEL = "What happened (optional)";
export const SAW_LABEL = "What you saw";
export const HOLD_BOX = "Hold this lot for review";
export const HOLD_HELP = "Places a lot hold now. Stock decisions involving the lot can't be committed or prepared until the lot's manager releases it.";
export const DISPLAY_HELP = "Creates an advisory task. No hold, no commitment block.";
export const ADD_FILE = "Add file";
export const FILE_HELP = "PNG, JPEG or PDF, up to 5 MB";
export const FILE_TOO_BIG = "This file is larger than 5 MB. Choose a smaller file.";
export const FILE_BAD_TYPE = "Only PNG, JPEG or PDF files can be attached.";
export const REMOVE = "Remove";
export const NO_FILES_LEFT = "No file attachments left today · you can still send the report without a file";
export const DISCARD_SENTENCE = "Discard this report? What you typed is lost.";
export const DISCARD = "Discard report";
export const KEEP_EDITING = "Keep editing";
export const PLACE_HOLD = "Place hold";
export const DONT_HOLD = "Don't hold lot";
export const SEND_AND_HOLD = "Send and hold lot";
export const GO_BACK = "Go back";
export const REPORT_SUBMITTED = "Report submitted";
export const FILE_TYPES = ["image/png", "image/jpeg", "application/pdf"] as const;
export const FILE_MAX = 5 * 1024 * 1024;

export const holdSentence = (lot: string, store: string) =>
  `Hold lot ${lot}? Stock decisions involving this lot can't be committed or prepared until the ${store} manager inspects and releases it.`;
export const mixedSentence = (lot: string, store: string) =>
  `This places a lot hold on lot ${lot}: Separate stock and verify before moving. Stock decisions involving this lot can't be committed or prepared until the ${store} manager resolves it.`;

// A chosen file's precheck (a courtesy: the server checks the content and the size), or null when it passes.
export function fileProblem(file: { size: number; type: string }): string | null {
  if (!(FILE_TYPES as readonly string[]).includes(file.type)) return FILE_BAD_TYPE;
  return file.size > FILE_MAX ? FILE_TOO_BIG : null;
}

// "report-photo…evidence.jpg" middle-truncated to `max` characters, so a long name never breaks the sheet (E8).
export function middle(name: string, max = 32): string {
  if (name.length <= max) return name;
  const keep = max - 1;
  return `${name.slice(0, Math.ceil(keep / 2))}…${name.slice(name.length - Math.floor(keep / 2))}`;
}

// "1.2 MB" / "640 KB".
export function fileSize(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

// The allowance line from the API's numbers (D-20; never a literal): "17 of 20 reports left today · 9 with a file";
// at none left "0 of 20 reports left today for this workspace · resets at 00:00 Bangkok time".
export function allowanceLine(a: Allowance, timeZone: string): string {
  if (a.reports_left <= 0) {
    const reset = a.resets === null ? "" : recordedTime(a.resets, timeZone).split(", ")[1] ?? "";
    const when = reset === "" ? "" : ` · resets at ${reset} ${cityOf(timeZone)} time`;
    return `0 of ${a.reports_limit} reports left today for this workspace${when}`;
  }
  return `${a.reports_left} of ${a.reports_limit} reports left today · ${a.files_left} with a file`;
}

// --- the preview's words (07-UI-SPEC Step 3; D-16) -------------------------------------------------------------------

export const PREVIEW_IMPACT = "Preview impact";
export const PREVIEWING = "Previewing impact";
export const INCOMPLETE = "Complete report details to preview impact.";
export const NO_AFFECTED = "No open stock decisions are affected by this report.";
export const PREVIEW_STALE = "Details changed · preview again";
export const PLAN_CHANGES = "Plan changes";
export const NOT_ALLOWED_IF = "Plan not allowed if confirmed";
export const PREVIEW_HEAD = ["Stock decision", "Plan now", "Plan if confirmed", "Est. benefit now → if confirmed"] as const;
export const moreAffected = (n: number) => `and ${n} more affected stock decision${n === 1 ? "" : "s"}`;
export const nothingChanges = (owner: string) => `Nothing changes until the ${owner} confirms this report.`;
export const benefitLine = (now: number | null, after: number | null, currency: string) =>
  `${now === null ? NONE : money(now, currency)} → ${after === null ? NONE : money(after, currency)}`;

// --- who: "S042 manager", "HQ planner" --------------------------------------------------------------------------------

export const ownerName = (o: ReportOwner) => (o.role === "hq" ? "HQ planner" : `${o.branch ?? NONE} manager`);
export const accountName = (account: string | null) => (account === null || account === "HQ" ? "HQ planner" : `${account} manager`);
// The owner a report of `kind` filed at `store` waits for (its confirm footer before it exists).
export const ownerOf = (kind: ReportKind, store: string | null): ReportOwner =>
  (kind === "TRANSPORT" ? { role: "hq", branch: null } : { role: "manager", branch: store });

export function owns(s: { role: "manager" | "hq"; branch: string | null }, r: Pick<Report, "owner">): boolean {
  return r.owner.role === s.role && (s.role === "hq" || r.owner.branch === s.branch);
}
export function reported(s: { role: "manager" | "hq"; branch: string | null }, r: Pick<Report, "reporter_role" | "reporter_branch">): boolean {
  return r.reporter_role === s.role && r.reporter_branch === s.branch;
}

// --- the state chip (07-UI-SPEC "Holds, flags and reports") ----------------------------------------------------------

export type Chip = { tone: Tone; icon: IconName; text: string };

export function stateChip(r: Report, timeZone: string): Chip {
  const t = (iso: string | null) => (iso === null ? "" : ` · ${recordedTime(iso, timeZone)}`);
  switch (r.state) {
    case "READ": return { tone: "info", icon: "inbox", text: `Awaiting confirmation by ${ownerName(r.owner)}` };
    case "CONFIRMED": return {
      tone: "good", icon: "check", text: r.fact_label ?? `Confirmed by ${accountName(r.confirmed_by)}${t(r.confirmed_at)}`,
    };
    case "DISMISSED": return { tone: "gray", icon: "x", text: `Dismissed by ${accountName(r.dismissed_by)}${t(r.dismissed_at)}` };
    case "WITHDRAWN": return { tone: "gray", icon: "x", text: `Withdrawn by reporter${t(r.withdrawn_at)}` };
    default: return { tone: "gray", icon: "redo", text: REPLACED };
  }
}
export const REPLACED = "Replaced by a later revision"; // (proposed)
export const NEEDS_REPORTER = "Needs the reporter's input";
export const affectsChip = (n: number) => `Affects ${n} stock decision${n === 1 ? "" : "s"}`;

// --- the timeline row ------------------------------------------------------------------------------------------------

// "Storage capacity · S042 · P0123" (the product, else —).
export const titleLine = (r: Pick<Report, "kind" | "branch" | "sku">) =>
  `${KIND_LABEL[r.kind]} · ${r.branch ?? NONE} · ${r.sku ?? NONE}`;

const SUMMARY_MAX = 90;
export const clip = (s: string, max = SUMMARY_MAX) => (s.length <= max ? s : `${s.slice(0, max - 1)}…`);
const day = (v: FieldValue) => (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v) ? shortDate(v) : NONE);
const val = (v: FieldValue) => (v === null || v === undefined || v === "" ? NONE : typeof v === "number" ? units(v) : String(v));

// The one-line summary from the typed fields, at most 90 characters with an ellipsis (R-5); the full text is on the
// detail route. "Available chilled totes: 6 · Fri 16 Oct – Sat 17 Oct".
export function summaryLine(r: Pick<Report, "kind" | "fields">, currency: string): string {
  const f = r.fields;
  let s: string;
  switch (r.kind) {
    case "NOT_RELEVANT":
      s = "Not relevant · no impact preview";
      break;
    case "CAPACITY":
      s = `Available ${f.zone === null || f.zone === undefined ? "" : `${String(f.zone)} `}totes: ${val(f.free_totes)} · ${day(f.valid_from)} – ${day(f.valid_to)}`;
      break;
    case "DEMAND_EVENT":
      s = `Demand ×${val(f.multiplier)} · ${f.sku === null || f.sku === undefined ? "every product" : String(f.sku)} · ${day(f.valid_from)} – ${day(f.valid_to)}`;
      break;
    case "TRANSPORT": {
      const run = val(f.run_code);
      if (f.what === "RUN_NOT_AVAILABLE") s = `${run} not available · ${day(f.applies_on)}`;
      else if (f.what === "DELAY") s = `${run} delayed ${val(f.delay_hours)} h · ${day(f.applies_on)}`;
      else s = `${run} · quote ${typeof f.amount === "number" ? money(f.amount, currency) : NONE} ${choiceLabel("pricing_basis", f.pricing_basis ?? null).toLowerCase()} · handling ${choiceLabel("includes_handling", f.includes_handling ?? null).toLowerCase()}`;
      break;
    }
    case "STOCK_COUNT":
      s = `Lot ${val(f.lot_id)}: shelf ${f.shelf_units === null ? NOT_CHECKED.toLowerCase() : val(f.shelf_units)} + backroom ${f.backroom_units === null ? NOT_CHECKED.toLowerCase() : val(f.backroom_units)}`;
      break;
    case "EXPIRY":
      s = `Lot ${val(f.lot_id)}: ${f.printed_date === MIXED ? MIXED_DATES.toLowerCase() : `printed ${day(f.printed_date)}`}`;
      break;
    case "CONDITION":
      s = `Lot ${val(f.lot_id)}: ${choiceLabel("condition", f.condition ?? null)}`;
      break;
  }
  return clip(s);
}

// The real-date group header and the time column, both in the session's zone: "16 Oct 2026" and "20:30".
export function groupOf(iso: string, timeZone: string): { date: string; time: string } {
  const [date = "", time = ""] = recordedTime(iso, timeZone).split(", ");
  return { date, time };
}

// --- Business events' words (07-UI-SPEC surface 4; Copywriting) ------------------------------------------------------

export const EVENTS_TITLE = "Business events";
export const EVENTS_LOADING = "Loading reports…";
export const EVENTS_FAILED = "Business events didn't load"; // (proposed)
export const VIEW_LABEL = "Show"; // (proposed) the segmented control's accessible name
export const needsMine = (n: number) => `Needs my confirmation (${n})`;
export const ALL = "All";
export const ANY = "Any";
export const FILTER = { kind: "Kind", store: "Store", state: "State", case: "Stock decision" } as const;
export const STORE_HINT = "Type a store code";
export const CASE_HINT = "Type a stock decision key";
export const APPLY = "Apply";
export { CLEAR_FILTERS } from "./vocab.ts"; // the shared word (G8)
export const STATE_OPTIONS: readonly (readonly [string, string])[] = [
  ["READ", "Awaiting confirmation"], ["CONFIRMED", "Confirmed"], ["DISMISSED", "Dismissed"], ["WITHDRAWN", "Withdrawn"],
];
export const EMPTY_TITLE = "No field reports yet";
export const EMPTY_BODY = "Use Report an observation to record something that changed at your store, such as a broken chiller or a local event. Nothing applies until its owner confirms it.";
export const NO_MATCH = "No reports match these filters.";
export const NOTHING_MINE = "Nothing waiting for your confirmation.";
export const SHOW_ALL = "Show all reports";
export const REVIEW_REPORT = "Review report";

// --- the report detail's words (07-UI-SPEC "Report detail"; Consequential actions) -----------------------------------

export const COLUMNS = { source: "Source", fields: "Fields", impact: "Potential impact" } as const;
export const BACK_TO_EVENTS = "Back to Business events";
export const REPORT_LOADING = "Loading the report…";
export const REPORT_FAILED = "This report didn't load"; // (proposed)
export const CONFIRM_FACT = "Confirm as reported fact";
export const CORRECT_FIELDS = "Correct fields";
export const SAVE_CORRECTIONS = "Save corrections";
export const KEEP_FIELDS = "Keep fields as they are"; // (proposed) leaves the correction without saving
export const DISMISS = "Dismiss";
export const DISMISS_SENTENCE = "Dismiss this report? It stays in the history and is never applied.";
export const DISMISS_REPORT = "Dismiss report";
export const KEEP_REPORT = "Keep report";
export const WITHDRAW = "Withdraw";
export const WITHDRAW_SENTENCE = "Withdraw this report? It stays in the history and changes nothing. A lot hold placed with it stays until the lot's manager releases it.";
export const WITHDRAW_REPORT = "Withdraw report";
export const CHOOSE_TRANSPORT = "Choose the pricing basis and handling to confirm.";
export const REPORT_CORRECTION = "Report a correction";
export const CHECKED_BOTH = "I checked the shelf and the backroom for this lot";
export const VIEW_FILE = "View file";
export const HOLD_PLACED = "Lot hold placed"; // (proposed) the hold line once a hold is placed with the report
export const TIMES = { reported: "Reported", checked: "Checked", confirmed: "Confirmed", dismissed: "Dismissed", withdrawn: "Withdrawn" } as const;
export const quoteExpired = (date: string) => `This quote expired on ${date}.`;
export const validRange = (from: string, to: string) => `Valid ${from} – ${to}`;
export const reportedBy = (r: Pick<Report, "reporter_account" | "created_at">, timeZone: string) =>
  `Reported by ${accountName(r.reporter_account)} · ${recordedTime(r.created_at, timeZone)}`;
export const confirmNote = (n: number) =>
  `Confirming applies this fact: ${n} open stock decision${n === 1 ? "" : "s"} get a new version and pending approvals on them are cancelled. Committed stock decisions in scope are flagged for review.`;
export const replanned = (n: number) => `Re-planned ${n} stock decision${n === 1 ? "" : "s"}`;
export const flaggedLine = (tags: string[]) => `Flagged for review: ${tags.join(", ")}`;
export const holdReason = (r: Pick<Report, "kind" | "fields">) =>
  (r.kind === "CONDITION" ? `Held for review: ${choiceLabel("condition", r.fields.condition ?? null)}` : "Held for review");

// The validity the owner confirms ("Valid Fri 16 Oct – Sat 17 Oct"), by kind; null when the kind has none.
export function validity(r: Pick<Report, "kind" | "fields">): string | null {
  const f = r.fields;
  if (r.kind === "CAPACITY" || r.kind === "DEMAND_EVENT") return validRange(day(f.valid_from), day(f.valid_to));
  if (r.kind === "TRANSPORT" && f.what === "QUOTE") return validRange(day(f.applies_on), day(f.valid_until));
  if (r.kind === "TRANSPORT") return validRange(day(f.applies_on), day(f.applies_on));
  return null;
}

// Why Confirm is disabled before the server is asked (07-UI-SPEC Actions), or null: a quote without its pricing basis
// or handling chosen; a quote whose valid-until scenario date is before today's.
export function confirmBlock(r: Pick<Report, "kind" | "fields">, todayIso: string | null): string | null {
  const f = r.fields;
  if (r.kind !== "TRANSPORT" || f.what !== "QUOTE") return null;
  if (blank(f.pricing_basis) || blank(f.includes_handling) || f.includes_handling === "UNKNOWN") return CHOOSE_TRANSPORT;
  if (todayIso !== null && typeof f.valid_until === "string" && f.valid_until < todayIso) return quoteExpired(day(f.valid_until));
  return null;
}

// A field's shown value in the detail's Kv.
export function fieldText(name: string, v: FieldValue, currency: string): string {
  if (v === null || v === undefined || v === "") return NONE;
  if (name === "printed_date" && v === MIXED) return MIXED_DATES;
  if (CHOICES[name] !== undefined) return choiceLabel(name, v);
  if (DATE_FIELDS.has(name)) return day(v);
  if (name === "amount" && typeof v === "number") return money(v, currency);
  return typeof v === "number" ? units(v) : String(v);
}
