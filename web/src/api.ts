// The typed adapter for 04-API-CONTRACT.md v1: every response shape the web app reads, and the fetch helpers.
// Every write helper takes the caller's Idempotency-Key: the caller makes one key per user action and resends the same
// key when it retries that action, so a retry is never a second submission (contract Conventions; research Pattern 1).
// Erasable syntax only (no enums, namespaces or parameter properties), so `node --test` runs this file as it is.

// (proposed; 06.1-UI-SPEC "Interaction states", replaces the demo-server wording, D-03)
export const NETWORK = "Could not reach ShelfShift. Check your connection and try again.";

// A failed call: the envelope's code and message word for word, with every other envelope key kept in `extra`.
// `status` 0 with code "network" means the request never got an answer.
export class ApiError extends Error {
  status: number;
  code: string;
  extra: Record<string, unknown>;

  constructor(status: number, code: string, message: string, extra: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.extra = extra;
  }
}

export type Paused = { contact: string };
type Handler = () => void;
let onPaused: (p: Paused) => void = () => {};
export function whenPaused(f: (p: Paused) => void): void { onPaused = f; }
let onLocked: Handler = () => {};
let onSandboxReset: Handler = () => {};

// 401 `locked` anywhere sends the visitor back to the sign-in card; 409 `sandbox_reset` reloads the app's session.
export function whenLocked(f: Handler): void {
  onLocked = f;
}
export function whenSandboxReset(f: Handler): void {
  onSandboxReset = f;
}

async function send(path: string, init: RequestInit): Promise<Response> {
  let r: Response;
  try {
    r = await fetch(path, { ...init, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "network", NETWORK);
  }
  if (r.ok) return r;
  let body: unknown = null;
  try {
    body = await r.json();
  } catch {
    body = null; // not the API's envelope (for example a proxy page): no message to show but the network one
  }
  const envelope = body !== null && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
  const { error, message, ...extra } = envelope;
  const code = typeof error === "string" ? error : "error";
  const text = typeof message === "string" ? message : NETWORK;
  if (r.status === 401 && code === "locked") onLocked();
  if (r.status === 409 && code === "sandbox_reset") onSandboxReset();
  if (r.status === 503 && code === "paused") onPaused({ contact: typeof extra.contact === "string" ? extra.contact : "" });
  throw new ApiError(r.status, code, text, extra);
}

// One key per user action; the caller keeps it while the action is pending and for Try again.
export function newKey(): string {
  return crypto.randomUUID();
}

export async function get<T>(path: string): Promise<T> {
  return (await send(path, {})).json() as Promise<T>;
}

// A file download (the case export): the response body exactly as the API sent it, never parsed or re-serialised, so
// the saved file is the API's own. A failure is the same ApiError as any read.
export async function getBlob(path: string): Promise<Blob> {
  return (await send(path, {})).blob();
}

export async function post<T>(path: string, body: unknown, key: string): Promise<T> {
  const headers = { "Content-Type": "application/json", "Idempotency-Key": key };
  return (await send(path, { method: "POST", headers, body: JSON.stringify(body) })).json() as Promise<T>;
}

export async function patch<T>(path: string, body: unknown, key: string): Promise<T> {
  const headers = { "Content-Type": "application/json", "Idempotency-Key": key };
  return (await send(path, { method: "PATCH", headers, body: JSON.stringify(body) })).json() as Promise<T>;
}

// A multipart write (the count answer). Only the key is set: the browser writes Content-Type with its boundary.
export async function form<T>(path: string, data: FormData, key: string): Promise<T> {
  return (await send(path, { method: "POST", headers: { "Idempotency-Key": key }, body: data })).json() as Promise<T>;
}

// The closed set of `error` codes the API returns (api/errors.py CODES; contract "Errors"). ApiError.code may also
// be "network" (no answer) or "error" (an answer that was not the API's envelope).
export type ErrorCode =
  | "idempotency_key" | "bad_request" | "bad_input" | "bad_override" | "unknown_fact" | "out_of_bounds" | "not_writable"
  | "locked" | "bad_passcode"
  | "forbidden"
  | "not_found"
  | "stale_version" | "sandbox_reset" | "conflict" | "incomplete_shipment" | "idempotency_conflict" | "hold_expired"
  | "wrong_state" | "already_decided" | "missing_data" | "demo_complete" | "too_many_requests" | "needs_input"
  | "count_mismatch" | "scope_tick" | "engine_version"
  | "bad_file"
  | "cap_reached"
  | "paused" | "internal" // 11-CONTEXT D-02: database outage and unexpected server fault
  | "preparing" | "engine_busy" | "engine_timeout" | "engine_unavailable" | "db_busy";

// --- shapes (contract "Shapes"; every listed key is always present, `null` when it does not apply) -----------------
// The engine's `text` on decide_by, act_before and wait is never typed: labels are composed from day, minute and the
// session calendar, so client code cannot read the engine wording (contract "Decisions recorded here"; Pitfall 8).

export type Seat = { role: "manager" | "hq"; branch: string | null };

export type Session = {
  role: "manager" | "hq";
  branch: string | null;
  seats: Seat[];
  run_id: string;
  day: number;
  version: number;
  clock: string;
  calendar: { day: number; date: string; weekday: string }[];
  demo_complete: boolean;
  last_reset_ms: number | null;
  open_requests: number;
  currency: string;
  // The dataset this sandbox runs on (05.2 D-04; contract Session row of 05.2-14): "public" for FreshRetailNet-50K with
  // simulated stock, "synthetic" for the generated test set. `label` is the mark's template from reference data: the
  // public one holds "{date}", which the UI fills from `data_as_of` (an ISO date, null on the synthetic set) with
  // format.markDate; copy.datasetMark composes it.
  kind: "public" | "synthetic";
  data_as_of: string | null;
  label: string;
  // The IANA zone every recorded time is shown in ("Asia/Bangkok", settings caps.time_zone; spec rev 6.7 §5), never a
  // client constant and never the browser's own zone (contract row 2026-10-16, Request recount; plan 05-17).
  time_zone: string;
};

export type Due = { day: number; min: number; date: string; weekday: string; label: string };
export type DecideBy = { day: number; latest_decision_min: number; source: "run" | "markdown" };
export type ActBefore = { day: number; latest_decision_min: number };
export type Wait = { run_code: string; day: number; latest_decision_min: number; departs_min: number | null };
export type MarkdownStep = { day: number; pct: number; days_until: number };
export type Hold = { until: string | null; minutes_left: number | null };

// One badge per kind (contract "Badges"). `unverified` is a decision regret, never cash: its three amounts are summed
// over the counted facts whose plan stays allowed and are null (never 0) when every counted fact is not allowed
// (contract row 2026-10-15, quick 261015-sxs, build/api 999efbc).
export type UnverifiedBadge = {
  kind: "unverified";
  text: string;
  fact_ids: string[];
  not_allowed_fact_ids: string[];
  decision_regret: number | null;
  net_revenue_difference: number | null;
  waste_policy_difference: number | null;
};
export type HoldExpiringBadge = { kind: "hold_expiring"; text: string; minutes_left: number };
export type PlainBadge = {
  kind: "missing_data" | "hold_expired" | "re_review" | "second_look" | "evidence_requested";
  text: string;
};
export type Badge = UnverifiedBadge | HoldExpiringBadge | PlainBadge;

export type CaseState =
  | "OPEN" | "WAITING" | "PENDING_APPROVAL" | "APPROVED" | "PREPARED" | "EXECUTED" | "OBSERVED" | "CLOSED" | "REJECTED"
  | "INVALIDATED";
export type ColumnName = "New" | "Waiting" | "Awaiting approval" | "Ready to dispatch" | "Tracking results" | "Resolved";

export type Owner = { role: "manager"; branch: string };

// Each entry of GET /api/cases `cases`.
export type Card = {
  case_id: string;
  key: string;
  tag: string | null;
  origin: string | null;
  state: CaseState;
  column: ColumnName;
  sku: string;
  product: string | null; // null when the dataset names no product (public product codes; 05.2 D-02)
  zone: string;
  source: string;
  dest: string | null;
  lot_id: string;
  suggested: string | null;
  suggested_key: string | null;
  at_risk_units: number | null; // the case lot's own projected waste under Keep (quick 261018-h9t)
  at_risk_kg: number | null;
  at_risk_value: number | null; // at_risk_units x the product's full price
  est_benefit: number | null;
  due: Due | null;
  decide_by: DecideBy | null;
  markdown_step: MarkdownStep | null;
  act_before: ActBefore | null;
  wait: Wait | null;
  rec_version: string | null;
  next_owner: Owner[];
  decisions: { source?: "approve"; destination?: "accept" };
  hold: Hold | null;
  badges: Badge[];
  reason: string | null;
  waiting_until_day: number | null;
  // A lot the morning screen flagged on the public dataset that the engine has not evaluated yet (05.2 D-23..D-25;
  // contract rows of 05.2-15): `needs_evaluation` true, the screen's own figures in `risk`, and no plan, amount or
  // deadline. Absent on the synthetic set and on every evaluated case.
  needs_evaluation?: boolean;
  risk?: Risk;
};

// The screen's figures for a flagged lot (api/screen.py): `a` the lot's own waste rule, `b` the stock-at-step rule,
// `keep_waste` the units Keep is expected to waste, `stock_at_step` the stock expected on `step_day` (the lot's next
// markdown step). Null where the screen had missing data.
export type Risk = {
  a: boolean | null;
  b: boolean | null;
  keep_waste: number | null;
  stock_at_step: number | null;
  step_day: number | null;
};

// GET /api/cases. On the public dataset the server scopes, filters, orders and pages the list (05.2 D-26; contract
// rows of 05.2-15): `page` (from 1), `page_size`, `total` (every item in scope, all pages) and `scope` (the store the
// list is limited to, null for every store, and whether that is the seat's default). The synthetic set answers page
// 1 with every case. The four keys arrive with 05.2-15, so they are optional here.
export type Board = {
  day: number;
  version: number;
  cases: Card[];
  page?: number;
  page_size?: number;
  total?: number;
  scope?: { branch: string | null; default: boolean };
};

// Stage 1 of the local candidate network at case time (05.2 D-27, D-29..D-31; contract rows of 05.2-14). `applies` is
// false on the synthetic set. `status`: "available" (some destination passed), "none_eligible" (every one was
// excluded, each with its reason) or "unavailable_unmapped" (the source store has no location or route data, so it
// has no transfer at all; keep and markdown work as usual). `restricted` is true when stage 1 excluded a run or the
// shortlist is on: the result is then the best among the plans evaluated.
export type Eligibility = {
  applies: boolean;
  status: "available" | "none_eligible" | "unavailable_unmapped";
  considered: number;
  eligible: number;
  excluded: { run_code: string; dest: string; reason: string }[];
  restricted: boolean;
};

// The case lot as the sandbox records it (`qty` is the record). Once the lot is gone the API answers its seed row,
// with the record fields null.
export type Lot = {
  lot_id: string;
  branch: string;
  qty: number | null;
  expiry_day: number | null;
  expiry_source: string | null;
  markdown_pct: number | null;
  last_verified_day: number | null;
  verified_by: string | null;
  origin: string | null;
  tag: string | null;
};

// An engine adjustment (an override or a reported fact), as the engine names its fields.
export type Adjustment = { kind: string } & Record<string, unknown>;

export type Adopted = {
  override: Adjustment;
  trip: { run_code: string; day: number } | null;
  branch: string | null;
  note: string;
  adopted_day: number;
};

// SUPERSEDED: a recount replaced it; it stays readable with its answer and can be neither answered nor confirmed
// (contract row 2026-10-16, Request recount; Count path 9).
export type RequestState = "OPEN" | "ANSWERED" | "CONFIRMED" | "EXPIRED" | "SUPERSEDED";

export type LotCount = { shelf_units: number | null; backroom_units: number | null };
export type NeedsInput = { lot_id: string; field: "shelf_units" | "backroom_units" };

// The answer to a count request; it changes nothing until it is confirmed. `day` is the sandbox day it was submitted,
// `reporter_role` and `reporter_branch` the seat that answered, as recorded, and `created_at` the server time this
// revision was written (the recorded answer time, ISO-8601 UTC; one per revision) (contract row 2026-10-16).
export type AnswerSignal = {
  signal_id: string;
  revision: number;
  kind: "STOCK_COUNT";
  state: "READ" | "CONFIRMED" | "DISMISSED";
  request_id: string;
  branch: string;
  text: string;
  fields: { lot_ids: string[]; counts: Record<string, LotCount>; needs_input: NeedsInput[] };
  day: number;
  reporter_role: Session["role"];
  reporter_branch: string | null;
  created_at: string;
};

export type Request = {
  request_id: string;
  case_id: string;
  case_key: string;
  rec_version: string;
  fact_id: string;
  fact_kind: string;
  lot_ids: string[];
  to_branch: string;
  ask_text: string;
  due_day: number;
  due_min: number;
  state: RequestState;
  partial_note: string | null;
  answer_signal_id: string | null;
  // Contract row 2026-10-16 (Request recount): the request that replaced this one (null unless SUPERSEDED), the case's
  // source branch, and whether the server allows a recount now (ANSWERED, active version, live case, version not
  // full); the seat check stays the route's.
  replaced_by: string | null;
  case_source: string;
  recountable: boolean;
  // 05-REVIEW WR-10 (contract row 2026-10-17): `confirmable` is the server's own word on whether confirm() would take
  // the current answer now (OPEN or ANSWERED, complete and READ, given today, a late one only on a live case, every lot
  // still on the record); `answerable` says whether an answer sent today could be confirmed today. The page offers
  // Confirm count and the late-answer form only on them, never on a rule of its own.
  confirmable: boolean;
  answerable: boolean;
  answer: AnswerSignal | null;
};

export type Triple = { low: number; base: number; high: number };

// Each entry of result.options. `net` and `delta_vs_keep` never include the waste-policy amount; `score` is net
// minus it and `waste_policy` is the base scenario's (a policy value, not cash).
export type OptionRow = {
  key: string;
  label: string;
  kind: "KEEP" | "MARKDOWN" | "TRANSFER";
  pct: number;
  qty: number;
  dest: string | null;
  run_code: string | null;
  net: Triple;
  score: Triple;
  waste_policy: number;
  delta_vs_keep: number;
  sold: number;
  wasted_units: number;
  wasted_kg: number;
  shortfall_filled: number;
};

// Each entry of result.rejected (engine/limits.py candidates): the reasons exactly as the engine words them.
export type RejectedRow = { label: string; dest: string | null; run_code: string | null; reasons: string[] };

// engine/crossover.py transport_crossover: exact. With no transfer at all there is no run, quote or range.
export type TransportCrossover =
  | { result: "no_change_in_range"; run_code: null; method: "exact" }
  | { result: "no_change_in_range"; run_code: string; quote: number; range: [number, number]; method: "exact" }
  | {
    result: "threshold"; value: number; direction: "rises" | "falls"; run_code: string; quote: number;
    range: [number, number]; method: "exact";
  };

// engine/crossover.py markdown_crossover: approximate, on a grid; `winner` is a plan key.
export type MarkdownChange = { m20: number; m30: number; winner: string };
type MarkdownRange = { range: { m20: [number, number]; m30: [number, number] }; method: "approx"; label: string };
export type MarkdownCrossover =
  | (MarkdownRange & { result: "no_change_in_range" })
  | (MarkdownRange & { result: "threshold"; value: MarkdownChange })
  | (MarkdownRange & { result: "multiple_changes"; values: MarkdownChange[] });

// engine/crossover.py waste_price_crossover: exact, both directions; values are never rounded by the engine.
export type WasteFlip = { result: "threshold"; value: number; key: string; label: string } | { result: "no_change_in_range" };
export type WasteCrossover = {
  price: number;
  range: [number, number];
  method: "exact";
  rises_at: WasteFlip;
  falls_at: WasteFlip;
};

// The engine result for the active version, as returned, with wait's text removed. The deadline keys and the
// shortlist appear only when the engine gives them.
export type Result = {
  engine_version: string;
  recommended: string;
  recommended_label: string;
  options: OptionRow[];
  rejected: RejectedRow[];
  scenario_wins: { wins: number; by_scenario: Record<string, string> };
  wait: { run_code: string; day: number; latest_decision_min: number } | null;
  candidates_checked: number;
  effective_version: string;
  markdown_step?: MarkdownStep;
  act_before?: ActBefore;
  decide_by?: DecideBy;
  shortlist?: { step: number; label: string };
  crossovers?: { transport: TransportCrossover; markdown_response: MarkdownCrossover; waste_price: WasteCrossover };
};

// Each entry of fact_checks, in the engine's order (not-allowed checks first). `kind` is PHANTOM_STOCK, STALE_COUNT
// or ESTIMATED_EXPIRY today; an unknown kind is shown in words.
export type FactCheck = {
  fact_id: string;
  kind: string;
  lot_id: string;
  lot_ids: string[];
  branch: string;
  recorded_value: number;
  alt_value: number;
  regret: number;
  decision_regret: number;
  unserved_units: number;
  net_revenue_difference: number;
  waste_policy_difference: number;
  waste_kg_difference: number;
  best_plan_if_alt: string;
  best_plan_if_alt_label: string;
  pick_changed: boolean;
  pick_feasible_if_alt: boolean;
  changes_plan: boolean;
};

// The version line. With no recommendation the API answers null for every recommendation-specific key.
export type Versions = {
  rec_version: string | null;
  effective_version: string | null;
  engine_version: string | null;
  dataset_version: string;
  forecast_run_id: string | null;
  forecast_providers: Record<string, number> | null;
  forecast_series_providers: string[] | null;
  forecast_headline: string;
  forecast_cutoff: string | null;
  day: number | null;
  synthetic: true;
  label: string;
};

export type Resource = {
  kind: "DEMAND" | "STORAGE" | "TRUCK" | "SOURCE_LOT" | "LOT_PRICE";
  label: string;
  needed: number;
  available: number | null;
  unit: "units" | "totes" | "right";
};

export type Approvals = {
  round: number;
  required: ("source" | "destination")[];
  decisions: { source?: string; destination?: string };
  hold: Hold | null;
  resources: Resource[];
  next_owner: Owner[];
};

// GET /api/cases/{id}: the card plus these keys. A case with no recommendation has result and engine_input null.
// A flag the engine has not evaluated yet (`needs_evaluation` true; 05.2-15 flag_detail) carries the card, `lot` and
// `risk` with no result and no fact checks: the case page shows it as Needs evaluation and runs the engine through
// POST /api/cases/{key}/evaluate, which answers the evaluated case's detail. `eligibility` arrives with 05.2-14.
export type CaseDetail = Card & {
  eligibility?: Eligibility;
  lot: Lot;
  adopted: Adopted[];
  adopted_ended: Adopted[];
  requests: Request[];
  result: Result | null;
  fact_checks: FactCheck[];
  summary: string;
  engine_input: Record<string, unknown> | null;
  versions: Versions;
  approvals: Approvals;
};

// Each entry of GET /api/cases/{id}/activity, oldest first.
export type ActivityEntry = {
  at: string;
  day: number | null;
  type: string;
  group: "Decisions" | "Assistant" | "Data";
  payload: Record<string, unknown>;
};

// GET /api/settings: the bounds come from the API (api/settings_bounds.json), never from the client.
export type Settings = {
  version: number;
  values: { waste_price: number; min_sellable_days: number; shortlist_step: number };
  bounds: {
    waste_price: { min: number; max: number; unit: string };
    min_sellable_days: { min: number; max: number; unit: string };
    shortlist_step: { allowed: number[] };
  };
};
export type SettingsSaved = Settings & { reevaluated: string[] };

// POST /api/signals/{id}/confirm: one label per lot, and every case re-evaluated with its new active version.
export type ConfirmResult = { fact_id: string; labels: string[]; cases: { case_id: string; key: string; rec_version: string }[] };

