// The 06.1 client contract: the TypeScript shapes, client calls and query builders of 04-API-CONTRACT.md
// "## Product mode (06.1)" (06.1-01), so page plans import shapes and calls instead of guessing them (D-44: every
// route and key lands on both sides together). It extends api.ts (the Phase 4 / 05.2 adapter) and never edits it.
// Rules kept here:
// - Every write goes through api.ts `post` with the caller's Idempotency-Key (one key per user action, resent on Try
//   again); sign-in sends its key too, which the server keeps when the request carries a session cookie.
// - The passcode travels only in the sign-in POST body: never in a URL, a query string, storage or a log (T-06.1-05).
// - Query builders only name what the page asks for, in a fixed key order; the server scopes, filters, orders and
//   pages (05.2 D-26, 06.1 D-17). Nothing here sorts or filters a page on the client.
// - Steps, stages, owners, next actions and blocks are the server's (D-24); no type here derives one.
// - Where the contract row names a list but not its row keys (work orders, movements, discrepancies, Today's lanes;
//   lot rows have theirs since 06.1-14's row), the type carries the keys the contract does name plus
//   `[key: string]: unknown`, so a page reads only what the contract states; the API plan that builds the route
//   states the rest in a dated contract row.
// Erasable syntax only (no enums, namespaces or parameter properties), so `node --test` runs this file as it is.
import {
  get, post, type ActivityEntry, type Board, type Card, type CaseDetail, type CaseState, type Eligibility, type ErrorCode,
  type Hold, type OptionRow, type Session, type Triple,
} from "./api.ts";
import type { BoardRisk } from "./derive.ts";
import type { StageKey, StepKey } from "./steps.ts";
import type { HistoryActor, NextActionKey } from "./vocab.ts";

// --- error codes (contract "Tables and error codes") ---------------------------------------------------------------
export type ErrorCode61 = ErrorCode | "unknown_account" | "already_owned" | "not_owned" | "dispatch_blocked"
  | "discrepancy_open";

// --- identity (contract "Identity and permissions") -----------------------------------------------------------------

// A store code of the workspace's reference data (a store manager) or "HQ".
export type Account = string;

// GET /api/session in 06.1: `account` added, `role` and `branch` derived from it, `seats` removed; `nav_counts` for the
// signed-in account (06.1-13), optional until that plan lands.
export type Session61 = Omit<Session, "seats"> & {
  account: Account;
  nav_counts?: { today: number; board: number; tasks: number };
};

export type SignInResult = { ok: true; reset_ms: number; account: Account; role: Session["role"]; branch: string | null };

// POST /api/session: {account, passcode} in the body only. The server answers a rotated session cookie.
export function signIn(account: Account, passcode: string, key: string): Promise<SignInResult> {
  return post<SignInResult>("/api/session", { account, passcode }, key);
}

// POST /api/session/sign-out: clears the account and keeps the workspace in this browser.
export function signOut(key: string): Promise<{ signed_out: true }> {
  return post<{ signed_out: true }>("/api/session/sign-out", {}, key);
}

// --- cards and detail (contract "Ownership and the steps", "Card and detail keys") ---------------------------------

export type FirstStage = "needs_evaluation" | "needs_action" | null;
export type NextAction = { key: NextActionKey | null; who: { role: Session["role"]; branch: string | null }[] };
export type Owner61 = { account: Account; role: Session["role"]; label: string };
export type BlockedReason = "missing_data" | "hold_expired" | "dispatch_blocked";
export type ClosedAs = "COMPLETED" | "CANCELLED" | null; // CANCELLED arrives with Phase 7

// The keys 06.1 adds to every card and detail.
export type Card61Keys = {
  step: StepKey;
  step_number: number;
  stage: StageKey;
  first_stage: FirstStage;
  owner: Owner61 | null;
  next_action: NextAction;
  blocked: boolean;
  blocked_reasons: BlockedReason[];
  closed_as: ClosedAs;
};
export type Card61 = Card & Card61Keys;

export type StageCounts = { decide: number; authorise: number; do: number; check: number; closed: number };

// GET /api/cases in 06.1: the 06.1 filters (boardQuery61), `stage_counts` and `blocked_total` under the current
// filters, and 05.2-15's `evaluated` and `at_risk` counts under the same scope.
export type Board61 = Omit<Board, "cases"> & {
  cases: Card61[];
  stage_counts: StageCounts;
  blocked_total: number;
  evaluated?: number;
  at_risk?: number;
};

// The detail's hold adds the database's now() (ISO UTC); `minutes_left` stays a number the client never shows (D-57).
export type Hold61 = Hold & { server_now: string };

// Comparison (contract "Comparison"): each column is the engine's option row plus transport and moved triples.
export type ComparisonColumnKey = "recommended" | "simple_rule" | "keep";
export type ComparisonColumn = OptionRow & { transport: Triple; moved: Triple; column: ComparisonColumnKey };
export type Comparison = {
  status: "ok" | "missing_data" | "not_computed";
  reason: string | null;
  columns: ComparisonColumn[];
};

export type CaseDetail61 = Omit<CaseDetail, "hold"> & Card61Keys & { hold: Hold61 | null; comparison: Comparison };

// Each activity entry adds the actor (null for the screen and the engine) and the ISO scenario date of `day`; the list
// is ordered by `at`, the recorded time. vocab.historyLine reads it.
export type ActivityEntry61 = ActivityEntry & { actor: HistoryActor | null; scenario_date: string | null };

const casePath = (caseId: string, route: string) => `/api/cases/${encodeURIComponent(caseId)}/${route}`;

// POST /api/cases/{id}/take-ownership: the source manager or HQ (D-67); ownership grants no approval or policy right.
export type OwnershipResult = { owner: Owner61; taken_at: string };
export function takeOwnership(caseId: string, key: string): Promise<OwnershipResult> {
  return post<OwnershipResult>(casePath(caseId, "take-ownership"), {}, key);
}

// --- execution (contract "Execution") -------------------------------------------------------------------------------

export type DispatchBlockKind = "wrong_state" | "cancelled" | "lot_hold" | "review_flag" | "recheck" | "checks_unavailable";
// One reason the dispatch check refused; `reason` and `resolution` are the server's words, shown word for word.
export type DispatchBlock = {
  kind: DispatchBlockKind;
  id: string | null;
  lot_ids: string[];
  owner_branch: string | null;
  reason: string;
  resolution: string;
};
// RECONCILE is the stock adjustment a reconciliation writes (06.1-11).
export type MovementKind = "DISPATCH" | "RECEIPT" | "PRICE_APPLIED" | "RECONCILE";
export type Movement = { kind: MovementKind; action_id: string; [key: string]: unknown };
export type DiscrepancyKind = "SHORT" | "DAMAGED";

// The execution view's rows as 06.1-09's contract row states them (2026-10-17). `kind` and `state` are the server's
// codes; a quantity not recorded yet is null, never 0.
export type WorkOrder = {
  action_id: string; kind: string; state: string; lot_id: string; new_lot_id: string | null; sku: string;
  origin: string | null; dest: string | null; qty: number | null; run_code: string | null; run_day: number | null;
  pct: number | null; effective_day: number | null;
  dispatched_qty: number | null; received_qty: number | null; damaged_qty: number | null;
};
export type MovementRow = {
  movement_id: string; action_id: string; kind: MovementKind; lot_id: string | null; qty: number; damaged_qty: number;
  condition: string | null; reason: string | null; recorded_at: string | null;
};
export type Discrepancy = {
  discrepancy_id: string; action_id: string; kind: DiscrepancyKind; units: number; open_units: number;
  state: string; opened_at: string | null; resolution: string | null;
};
// A condition or resolution the record forms offer: a key of reference data and its label.
export type ExecutionChoice = { key: string; label: string };

// GET /api/cases/{id}/execution.
export type ExecutionView = {
  dispatch: { allowed: boolean; blocks: DispatchBlock[] };
  work_orders: WorkOrder[];
  movements: MovementRow[];
  discrepancies: Discrepancy[];
  after_dispatch: Record<string, unknown>[];
  options: { conditions: ExecutionChoice[]; resolutions: ExecutionChoice[] };
};

// A write's answer where the contract states only the state it leaves.
export type RecordResult = { state: CaseState; [key: string]: unknown };

// The bodies, as the contract rows give them. `condition` and `resolution` are keys of reference data
// (`execution.conditions`, `execution.resolutions`), never typed here.
export type DispatchBody = { action_id: string; qty: number; lot_id: string; condition: string };
export type ReceiptBody = {
  action_id: string; received_qty: number; damaged_qty: number; condition: string; reason: string;
};
export type PriceBody = { action_id: string; confirmed: true };
export type ReconcileBody = { discrepancy_id: string; resolution: string; units: number; reason: string };
export type CloseBody = { reason: string };

export function recordDispatch(caseId: string, body: DispatchBody, key: string): Promise<{ state: "PREPARED"; movement: Movement }> {
  return post(casePath(caseId, "dispatch"), body, key);
}
export function recordReceipt(caseId: string, body: ReceiptBody, key: string): Promise<RecordResult> {
  return post(casePath(caseId, "receipt"), body, key);
}
export function recordPrice(caseId: string, body: PriceBody, key: string): Promise<RecordResult> {
  return post(casePath(caseId, "price-applied"), body, key);
}
export function reconcile(caseId: string, body: ReconcileBody, key: string): Promise<RecordResult> {
  return post(casePath(caseId, "reconcile"), body, key);
}
export function closeDecision(caseId: string, body: CloseBody, key: string): Promise<RecordResult> {
  return post(casePath(caseId, "close"), body, key);
}
export function getExecution(caseId: string): Promise<ExecutionView> {
  return get<ExecutionView>(casePath(caseId, "execution"));
}

// --- read views (contract "Read views"): branch-scoped, paged, in the server's order ---------------------------------

export type Paged<T> = { items: T[]; page: number; page_size: number; total: number };
// A planned time: a scenario day and minutes after its midnight.
export type DayMin = { day: number; min: number };

export type MilestoneKey = "pack_from" | "ready" | "loading_cutoff" | "departure" | "arrival" | "received";
export type MovementState =
  | "planned" | "dispatched" | "partially_received" | "received" | "discrepancy" | "expired" | "cancelled";

// GET /api/cases/{id}/transport; `run` null → "No transport assigned.".
export type TransportView = {
  run: {
    code: string; day: number; loading_cutoff_min: number; departs_min: number; arrives_min: number;
    cold_chain: boolean; spare_totes: number | null; quote: number | null;
  } | null;
  source: string;
  destination: string | null;
  packing_min: number | null;
  milestones: { key: MilestoneKey; day: number | null; min: number | null; recorded_at: string | null }[];
  decision: { state: CaseState; step: StepKey };
  movement: {
    state: MovementState | null;
    committed_qty: number | null;
    dispatched_qty: number | null;
    received_qty: number | null;
    damaged_qty: number | null;
    dispatched_at: string | null;
    received_at: string | null;
  };
  assumed: { carrier: "own_fleet"; coordinator: "represented" };
};
export function getTransport(caseId: string): Promise<TransportView> {
  return get<TransportView>(casePath(caseId, "transport"));
}

// Each item of GET /api/transfers.
export type TransferItem = {
  action_id: string;
  case_id: string;
  key: string;
  sku: string;
  lot_id: string;
  new_lot_id: string | null;
  source: string;
  dest: string;
  committed_qty: number;
  dispatched_qty: number | null;
  received_qty: number | null;
  damaged_qty: number | null;
  run_code: string | null;
  run_day: number | null;
  departs_min: number | null;
  arrives_min: number | null;
  decision_state: CaseState;
  movement_state: MovementState | null;
  next_action: NextAction;
};
export type TransfersView = Paged<TransferItem>;

// GET /api/today as 06.1-13 built it (the 2026-10-17 Today row): a manager's store view, or HQ's network view. A tile
// key names the summary tile a card counts under; times are planned only (D-18).
export type TileKey = "decisions_due" | "approvals_waiting" | "stock_checks" | "transfers_at_risk";
export type CauseSource = "run" | "markdown" | "stock_check_due";
export type TodayCounts = Record<TileKey, number> & { first_deadline: (DayMin & { date: string }) | null };
type TodayWho = NextAction["who"][number];

export type TodayCard = {
  key: string; // "case:{case_id}" | "request:{request_id}"
  kind: "stock_decision" | "stock_check";
  what: { sku: string; product: string | null; lot_id: string | null; plan?: string | null; fact_kind?: string };
  where: { source: string; dest: string | null };
  owner: { account: string; role: TodayWho["role"]; label: string } | null;
  deadline: (DayMin & { date: string; cause: { source: CauseSource | null; run_code?: string | null; request_id?: string } })
    | null;
  date: string;
  status: {
    state: string; step: string | null; stage: string | null; first_stage: string | null; blocked: boolean;
    blocked_reasons: string[];
  };
  next_action: { key: string | null; who: TodayWho[] };
  case_key: string;
  case_id: string;
  tiles: TileKey[];
};

export type LaneItem =
  | { kind: "decision_deadline"; case_key: string; case_id: string; day: number; min: number }
  | { kind: "stock_check_due"; request_id: string; case_key: string; case_id: string; day: number; min: number }
  | {
    kind: "pick_and_pack"; case_key: string; case_id: string; action_id: string | null; run_code: string; qty: number;
    start: DayMin; end: DayMin; conditional: boolean;
  }
  | { kind: "delivery"; inbound_id: string; sku: string; qty: number; day: number; min: number; status: string | null }
  | { kind: "loading_cutoff"; day: number; min: number }
  | { kind: "travel"; start: DayMin; end: DayMin };
export type Lane = {
  key: string; // "decisions" | "stock_checks" | "floor" | "run:{code}"
  kind: "decisions" | "stock_checks" | "floor" | "run";
  branch: string;
  run_code?: string;
  dest?: string;
  cold_chain?: boolean;
  items: LaneItem[];
};

export type Moving = {
  case_key: string; case_id: string; action_id: string | null; sku: string; source: string; dest: string;
  run_code: string; run_day: number; qty: number; arrival: DayMin | null;
  decision: { state: string; step: string }; movement: { state: MovementState | null };
};
export type OutgoingRun = {
  run_code: string; day: number; source: string; dest: string; cold_chain: boolean; spare_totes: number | null;
  loading_cutoff: DayMin; departure: DayMin; arrival: DayMin; transfers: Moving[];
};
export type Incoming =
  | { kind: "delivery"; inbound_id: string; branch: string; sku: string; qty: number; arrival: DayMin; status: string | null }
  | ({ kind: "transfer" } & Moving);
export type Outcome = {
  case_key: string; case_id: string; sku: string; lot_id: string; plan: string | null;
  expected: { net_vs_keep: number | null; kind: "simulated" }; actual: null;
};
export type ComingUp = {
  kind: "markdown_step" | "price_change"; case_key: string; lot_id: string; sku: string; day: number; date: string | null;
  min: null; pct: number; source: "schedule" | "work_order";
};
export type TodayException = {
  kind: "unverified_fact" | "discrepancy" | "dispatch_blocked" | "hold_expired"; id: string; case_key: string;
  case_id: string; owner: TodayCard["owner"]; next_action: TodayCard["next_action"]; action_id?: string;
  discrepancy_kind?: string; units?: number; open_units?: number; fact_ids?: string[];
};

export type TodayView = {
  scenario_date: string; day: number; branch: string; packing_min: number | null;
  summary: TodayCounts; lanes: Lane[]; cards: TodayCard[];
  movements: { outgoing: OutgoingRun[]; incoming: Incoming[] };
  outcomes: Outcome[]; coming_up: ComingUp[]; exceptions: TodayException[];
};
export type BranchRow = Record<TileKey, number> & { branch: string; first_deadline: (DayMin & { date: string }) | null };
export type TodayHqView = {
  scenario_date: string; day: number; branch: null;
  network: TodayCounts & { evaluated: number; at_risk: number };
  branches: Paged<BranchRow>;
  cards: TodayCard[];
};
// HQ's "Branches needing attention" pages through `page`; a manager's view has one page.
export function getToday(branch?: string, page?: number): Promise<TodayView | TodayHqView> {
  return get<TodayView | TodayHqView>(query("/api/today", [["branch", branch], ["page", page]]));
}

// GET /api/schedule: times only from planned data; null start and end read "Unscheduled". A markdown's time has a
// day and no minute. The owner is the account that acts (06.1-13, as built), not text.
export type ScheduleTime = { day: number; min: number | null };
export type ScheduleOwner = { account: Account; role: Session["role"]; label: string };
export type ScheduleRow = {
  id: string;
  parent_id: string | null;
  kind: string;
  title: string;
  sub: string | null;
  branch: string | null;
  start: ScheduleTime | null;
  end: ScheduleTime | null;
  status: string | null;
  owner: ScheduleOwner | null;
  qty: number | null;
  run_code: string | null;
  case_key: string | null;
  depends_on: string[];
};
export type ScheduleWindow = { from: string; to: string; from_day: number; to_day: number };
export type ScheduleView = {
  date: string;
  view: "day" | "week" | "month";
  group: "work_order" | "branch" | "run";
  branch: string | null; // the store the rows are for (HQ: the one asked for; null for none)
  scenario_date: string;
  window: ScheduleWindow;
  rows: ScheduleRow[];
  page: number;
  page_size: number;
  total: number;
};

// GET /api/lots and /api/lots/{lot_id}, with the keys the 2026-10-17 contract row states (06.1-14, as built). Stock,
// lot, expiry and delivery values are simulated and the forecast is precomputed (`kinds`, D-15). `action` says what the
// screen may offer: "view" with the live decision's `case_key`, "open" with this morning's `flag_key`, null otherwise;
// never a second decision for a lot (D-31). `expiry_source` and the history's `kind`/`state` are the server's codes.
export type NextMarkdown = { day: number; date: string | null; pct: number };
export type LotAction = "view" | "open" | null;
export type LotRow = {
  lot_id: string;
  branch: string;
  sku: string;
  product: string | null;
  zone: string;
  origin: string | null;
  expiry_day: number;
  expiry_date: string | null;
  expiry_source: string;
  qty: number;
  markdown_pct: number;
  last_verified_day: number | null;
  last_verified_date: string | null;
  reserved: number;
  available: number;
  incoming: number;
  forecast_to_expiry: number | null;
  next_markdown: NextMarkdown | null;
  discrepancies: number;
  case_key: string | null;
  case_state: CaseState | null;
  flag_key: string | null;
  action: LotAction;
  eligibility: Eligibility;
  kinds: { stock: "simulated"; forecast: "forecast" };
};
export type LotsView = Paged<LotRow> & { scope: { branch: string | null; default: boolean } };
export type LotHold = {
  hold_id: string; kind: string; reason: string; placed_by: string; placed_at: string | null; state: string;
};
export type LotReviewFlag = {
  flag_id: string; case_id: string; key: string | null; fact_id: string; reason: string; created_at: string | null;
};
export type LotTransfer = {
  action_id: string; case_id: string | null; key: string | null; source: string; dest: string; qty: number | null;
  lot_id: string; new_lot_id: string | null; run_code: string | null; run_day: number | null; run_date: string | null;
  state: string;
};
type LotHistoryTimes = { recorded_at: string | null; scenario_day: number | null; scenario_date: string | null };
export type LotHistoryEntry =
  | LotHistoryTimes & {
    type: "stock_decision"; id: string; case_id: string; key: string; state: CaseState; closed_as: ClosedAs;
  }
  | LotHistoryTimes & {
    type: "movement"; id: string; movement_id: string; kind: MovementKind; case_id: string; key: string | null;
    action_id: string; lot_id: string | null; qty: number; damaged_qty: number; condition: string | null;
    reason: string | null; actor: HistoryActor;
  };
export type LotDetail = LotRow & {
  holds: LotHold[];
  review_flags: LotReviewFlag[];
  transfers: LotTransfer[];
  history: LotHistoryEntry[];
};

// A read view the dataset cannot fill (the synthetic set has no public sales): the server's reason, shown as sent.
export type Unavailable = { available: false; reason: string };

// GET /api/analytics (06.1-14's row, as built): public sales history, simulated aging, estimated waste, each labelled
// by its kind.
export type AnalyticsSalesRow = { sku: string; product: string; units: number; hours_out_of_stock: number; kind: "public" };
export type AnalyticsSeries = {
  forecast_run_id: string | null;
  forecast_cutoff: string | null;
  sku: string;
  product: string;
  days: { date: string; units: number; hours_out_of_stock: number }[];
  forecast: { date: string; low: number; base: number; high: number; provider: string | null }[];
};
export type AgingRow = {
  bucket: "0–1" | "2–3" | "4–7" | "8+"; min_days: number | null; max_days: number | null; units: number; lots: number;
  kind: "simulated";
};
export type WasteRow = {
  lot_id: string; sku: string; product: string; key: string | null; units: number; kg: number; value: number | null;
  basis: "keep"; source: "engine" | "screen"; kind: "estimated";
};
export type AnalyticsData = {
  available: true;
  branch: string;
  period: { from: string; to: string };
  sales_by_product: AnalyticsSalesRow[];
  series: AnalyticsSeries | null;
  aging: AgingRow[];
  waste: WasteRow[];
};
export type AnalyticsView = AnalyticsData | Unavailable;

// GET /api/forecasts (06.1-14's row, as built): the precomputed run and each product's seven-day base. `provider` is
// the pipeline's code (data/forecast.py PROVIDERS); `providers` counts the codes behind the page's items.
export type ForecastProvider = "ai_forecast" | "arima_plus" | "local-seasonal-naive";
export type ForecastRun = {
  id: string; provider: string; history_to: string; period_from: string; period_to: string;
  providers: Record<string, number>; label: string | null; confidence_level: number | null; input_label: string | null;
};
export type ForecastItem = {
  provider_codes: string[];
  sku: string;
  product: string;
  kind: "forecast";
  daily_base: number[];
  base_total: number;
  low_total: number;
  high_total: number;
  simulated_inputs: boolean;
};
export type ForecastsData = {
  available: true;
  branch: string;
  run: ForecastRun | null;
  items: ForecastItem[];
  page: number;
  page_size: number;
  total: number;
};
export type ForecastsView = ForecastsData | Unavailable;

// --- query builders: URLSearchParams in a fixed key order; empty and default values left out -----------------------

type Param = [string, string | number | boolean | null | undefined];

// Reports (08): published evaluation data and workspace records are separate reads.
export type ReportFigure = "open" | "pending_approval" | "unanswered_check" | "committed" | "cancelled";
export type ReportsWorkspaceView = {
  scope: { branch: string | null; role: "manager" | "hq" };
  activity: {
    open: { count: number; need_action: number };
    pending_approval: { count: number; waiting_for_you: number };
    unanswered_check: { count: number; past_due: number; addressed_to_you: number };
    committed: { count: number; dispatched: number; received: number };
    cancelled: { count: number };
  };
  expected: { kind: "SIMULATED"; plans: {
    case_id: string; key: string; sku: string; plan_key: string; plan_label: string; plan_kind: string;
    source_branch: string; dest_branch: string | null; horizon: { first_day: number; last_day: number };
    net_change: number | null; waste_change_units: number | null; waste_change_kg: number | null;
    transport_and_handling: number | null; computed: boolean;
  }[]; page: number; page_size: number; total: number; combined: null | {
    additive: true; plans: number; net_change: number; waste_change_units: number; waste_change_kg: number;
    transport_and_handling: number;
  } | { additive: false; plans: number; reason: "overlap" | "not_computed" } };
  actual: { kind: "RECORDED"; dispatched_units: number; dispatched_transfers: number; received_units: number;
    discrepancies_open: number; sales_after_receipt: null; waste_after_receipt: null };
};
export type BenchmarkVersions = { engine: string; dataset: string; policy: string; forecast_runs: string[];
  evaluator: string; settings: string };
export type BenchmarkScenario = "store_rule" | "store_rule_checked" | "shelfshift" | "markdowns_only";
type Scenarios<T> = Record<BenchmarkScenario, T>;
type MetricRows = Record<string, unknown>[];
export type BenchmarkSummary = {
  kind: "SIMULATED";
  scenarios: { key: BenchmarkScenario; world: string; label: string }[];
  network: { stores: number; at_risk_lots_by_day: MetricRows };
  money: { by_scenario: Scenarios<number>; steps: MetricRows; by_day: MetricRows };
  footnote_x: number;
  waste: { by_day: MetricRows; totals: Scenarios<{ units: number; kg: number }>;
    change_vs_markdowns_only: Scenarios<{ units: number; kg: number }> };
  headline: Scenarios<{ net: number; full_price_coverage: number | null; shortfall_filled: number | null;
    action_adjusted_fulfillment: number | null; incremental_units_sold_vs_markdowns_only: number;
    units_wasted: number; kg_wasted: number; sell_through_moved: number | null; reasons: Record<string, string> }>;
  checks: Scenarios<null | { requests: number; lots_counted: number; corrected_up: number;
    corrected_down: number; nothing_to_correct: number }>;
  trips: Scenarios<{ short: number; failed: number; transport_unshipped: number }>;
  closing: Scenarios<{ on_hand_units: number; in_transit_units: number; open_transfers: number; costs_charged: number }>;
  decision_quality: { limit_breaks: Scenarios<number>; opened: number; approved: number; rejected: number;
    waiting: number; left_open: Record<string, number>; retry_limit: number; time_to_decision: number | null };
  forecast_quality: { forecast_run_id: string; window: { first_day: number; last_day: number }; cutoff_day: number;
    wape: number | null; baseline_wape: number | null; bias: number | null; baseline_bias: number | null;
    coverage_80: number | null; scored_days: number; excluded_stockout_days: number; providers: string[];
    input_counts: Record<string, number>; series_without_in_stock_days: number };
  settings: Record<string, unknown>; assumptions: { markdown_response: unknown; stock_record_errors: unknown };
  categories_without_schedule: string[]; policy: Record<string, unknown>;
};
export type BenchmarkRun = {
  kind: "SIMULATED"; benchmark_id: string; published_at: string; git_commit: string;
  window: { first_day: number; last_day: number }; versions: BenchmarkVersions;
  differs: (keyof BenchmarkVersions)[]; summary: BenchmarkSummary;
  breakdown: null | { benchmark_id: string; kind: "SIMULATED"; chart: "money" | "waste"; by: "day" | "branch";
    rows: MetricRows; page: number; page_size: number; total: number };
};
export type BenchmarkView = {
  status: "none" | "current" | "historical"; app: BenchmarkVersions; current: BenchmarkRun | null;
  historical: BenchmarkRun | null; settings_note: { field: string; workspace: unknown; benchmark: unknown }[];
};
export type ReportsWorkspaceQuery = { branch?: string; page?: number; page_size?: number };
export type ReportsBenchmarkQuery = ReportsWorkspaceQuery & (
  { benchmark_id?: string; breakdown?: undefined; chart?: undefined } |
  { benchmark_id: string; breakdown: "day" | "branch"; chart: "money" | "waste" }
);
export function reportsWorkspaceQuery(q: ReportsWorkspaceQuery = {}): string {
  return query("/api/reports/workspace", [["branch", q.branch], ["page", q.page], ["page_size", q.page_size]]);
}
export function getReportsWorkspace(q: ReportsWorkspaceQuery = {}): Promise<ReportsWorkspaceView> {
  return get<ReportsWorkspaceView>(reportsWorkspaceQuery(q));
}
export function reportsBenchmarkQuery(q: ReportsBenchmarkQuery = {}): string {
  if ((q.breakdown || q.chart) && !q.benchmark_id?.trim()) throw new Error("A breakdown requires benchmark_id");
  return query("/api/reports/benchmark", [["branch", q.branch], ["benchmark_id", q.benchmark_id],
    ["breakdown", q.breakdown], ["chart", q.chart], ["page", q.page], ["page_size", q.page_size]]);
}
export function getReportsBenchmark(q: ReportsBenchmarkQuery = {}): Promise<BenchmarkView> {
  return get<BenchmarkView>(reportsBenchmarkQuery(q));
}

// `path?k=v&…` in the order given. A value is left out when it is null, undefined, false, a blank string or the first
// page; true is sent as "1". Never sorts or filters anything but the parameters themselves.
function query(path: string, params: Param[]): string {
  const p = new URLSearchParams();
  for (const [k, v] of params) {
    if (v === null || v === undefined || v === false) continue;
    if (typeof v === "string" && v.trim() === "") continue;
    if (k === "page" && v === 1) continue;
    p.set(k, v === true ? "1" : typeof v === "string" ? v.trim() : String(v));
  }
  const text = p.toString();
  return text === "" ? path : `${path}?${text}`;
}

// The Decision board's filters (contract "Card and detail keys" → GET /api/cases). `store` is HQ's only: a manager's
// query never carries `branch` (the server scopes a manager to the signed-in store). `risk` is 05.2's HQ filter.
export type BoardFilters61 = {
  report?: ReportFigure;
  store?: string;
  risk?: BoardRisk;
  owner?: "me" | "";
  product?: string;
  deadline?: "scenario_date" | "none" | "";
  blocked?: boolean;
  page?: number;
};
export function boardQuery61(session: Pick<Session, "role">, f: BoardFilters61): string {
  return query("/api/cases", [
    ["branch", session.role === "hq" ? f.store : undefined],
    ["risk", f.risk === "all" ? undefined : f.risk],
    ["owner", f.owner], ["product", f.product], ["deadline", f.deadline], ["blocked", f.blocked], ["page", f.page],
    ["report", f.report],
  ]);
}

export type TransfersQuery = {
  direction?: "outgoing" | "incoming" | "all"; branch?: string; state?: MovementState; page?: number; pageSize?: number;
};
export function transfersQuery(q: TransfersQuery): string {
  return query("/api/transfers", [["direction", q.direction], ["branch", q.branch], ["state", q.state],
    ["page", q.page], ["page_size", q.pageSize]]);
}
export function getTransfers(q: TransfersQuery): Promise<TransfersView> {
  return get<TransfersView>(transfersQuery(q));
}

export type ScheduleQuery = {
  date?: string; view?: ScheduleView["view"]; group?: ScheduleView["group"]; branch?: string; activity?: string;
  status?: string; page?: number;
};
export function scheduleQuery(q: ScheduleQuery): string {
  return query("/api/schedule", [["date", q.date], ["view", q.view], ["group", q.group], ["branch", q.branch],
    ["activity", q.activity], ["status", q.status], ["page", q.page]]);
}
export function getSchedule(q: ScheduleQuery): Promise<ScheduleView> {
  return get<ScheduleView>(scheduleQuery(q));
}

export type LotsQuery = { branch?: string; product?: string; atRisk?: boolean; withCase?: boolean; page?: number };
export function lotsQuery(q: LotsQuery): string {
  return query("/api/lots", [["branch", q.branch], ["product", q.product], ["at_risk", q.atRisk],
    ["with_case", q.withCase], ["page", q.page]]);
}
export function getLots(q: LotsQuery): Promise<LotsView> {
  return get<LotsView>(lotsQuery(q));
}
export function getLot(lotId: string): Promise<LotDetail> {
  return get<LotDetail>(`/api/lots/${encodeURIComponent(lotId)}`);
}

export type AnalyticsQuery = { branch?: string; product?: string };
export function analyticsQuery(q: AnalyticsQuery): string {
  return query("/api/analytics", [["branch", q.branch], ["product", q.product]]);
}
export function getAnalytics(q: AnalyticsQuery): Promise<AnalyticsView> {
  return get<AnalyticsView>(analyticsQuery(q));
}

export type ForecastsQuery = { branch?: string; page?: number };
export function forecastsQuery(q: ForecastsQuery): string {
  return query("/api/forecasts", [["branch", q.branch], ["page", q.page]]);
}
export function getForecasts(q: ForecastsQuery): Promise<ForecastsView> {
  return get<ForecastsView>(forecastsQuery(q));
}

// Public release metadata (11-CONTEXT D-05, D-06, D-08; judge-ready contract). No session or write key.
export type AboutPayload = {
  git_commit: string;
  engine_version: string;
  dataset_version: string;
  assistant_version: string | null;
  gemini_model: string;
  vertex_location: string;
  label: string | null;
  data_as_of: string | null;
  forecast: { headline: string; providers: Record<string, number>; run_id: string; cutoff_date: string } | null;
  error_model: { share: number; range: number[] } | null;
  contact: string;
  google_cloud: boolean;
  weekcheck_status: "in_code" | "not_in_release";
  benchmark_status: "published" | "not_published";
  benchmark: { benchmark_id: string; dataset_version: string; published_at: string; git_commit: string; first_day: string; last_day: string;
    stores: number | null; series: number | null; version_match: boolean; differs: string[] } | null;
  ai_eval_status: "published" | "not_published";
  ai_eval: { eval_id: string; published_at: string; model_id: string; assistant_version: string; dataset_version: string;
    engine_version: string; build_commit: string; reference_fingerprint: string; version_match: boolean; differing_fields: string[] } | null;
  latest_unpublished: { run_date: string; reason: string } | null;
};
export const getAbout = () => get<AboutPayload>("/api/about");
