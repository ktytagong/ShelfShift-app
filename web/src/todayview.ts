// Today's pure parts (06.1-UI-SPEC "Today"; sketch 002 B; 06.1-CONTEXT D-11, D-18, D-22, D-33): the row shapes of
// GET /api/today as 06.1-13 built them (04-API-CONTRACT "Product mode (06.1)", the 2026-10-17 Today row), where a
// planned minute sits on the 06:00–18:00 day strip, how markers that would overlap are stacked, which cards a summary
// tile keeps, and the card and tile texts. Planned times only (D-18): nothing here reads a clock, and no helper says
// "now", "today", "ago", overdue or time left. The server orders the cards; a tile only keeps the cards whose `tiles`
// name it, in the server's order (UI-SPEC "Today" item 1), and nothing here sorts them.
// Wording the user has not confirmed is marked "(proposed)", one named constant each.
// Pure, erasable syntax only, so `node --test` runs it.
import type {
  DayMin, Lane, LaneItem, NextAction, TileKey, TodayCard, TodayCounts, TodayHqView, TodayView,
} from "./contract.ts";
import { productLabel } from "./derive.ts";
import { hhmm, shortDate } from "./format.ts";
import { NEXT_ACTION_LABEL, NOT_TAKEN, ROLE_LABEL, type NextActionKey } from "./vocab.ts";

// --- the shapes of GET /api/today (contract.ts; the names this module and the page use) -----------------------------

export type {
  BranchRow, CauseSource, ComingUp, DayMin, Incoming, Lane, LaneItem, Moving, Outcome, OutgoingRun, TileKey, TodayCard,
  TodayCounts, TodayException,
} from "./contract.ts";
export type Who = NextAction["who"][number];
export type StoreToday = TodayView;
export type NetworkToday = TodayHqView;
export type AnyToday = TodayView | TodayHqView;
export const isNetwork = (v: AnyToday): v is NetworkToday => "network" in v;

// --- the day strip's geometry (UI-SPEC "Today" item 2) --------------------------------------------------------------

export const STRIP_FROM = 6 * 60; // 06:00
export const STRIP_TO = 18 * 60; // 18:00

// Where `min` (minutes after the scenario day's midnight; past 1440 for a later day) sits between `from` and `to`, as a
// fraction 0..1. A minute outside the window clamps to its nearer end and is flagged, so the strip can say so in the
// marker's name instead of drawing it off the track.
export function position(min: number, from: number, to: number): { x: number; outside: boolean } {
  if (min < from) return { x: 0, outside: true };
  if (min > to) return { x: 1, outside: true };
  return { x: (min - from) / (to - from), outside: false };
}

// A planned {day, min} as minutes after the scenario day's midnight.
export const minuteOf = (t: DayMin, scenarioDay: number): number => (t.day - scenarioDay) * 1440 + t.min;

export const STACK_GAP = 0.04; // markers closer than 4% of the strip would overlap
export const ROW_MID = 0.5;
export const ROW_UP = 0.28;
export const ROW_DOWN = 0.74;

// The vertical place (a fraction of the lane height) of each marker at fractions `xs`, in the input's order: markers
// closer than STACK_GAP to their neighbour form a cluster that alternates 28% / 74%; a lone marker stays at 50%.
export function stackMarkers(xs: readonly number[]): number[] {
  const order = xs.map((x, i) => ({ x, i })).sort((a, b) => a.x - b.x || a.i - b.i);
  const rows = new Array<number>(xs.length).fill(ROW_MID);
  let start = 0;
  for (let k = 1; k <= order.length; k++) {
    if (k < order.length && order[k].x - order[k - 1].x < STACK_GAP) continue;
    if (k - start > 1) {
      for (let j = start; j < k; j++) rows[order[j].i] = (j - start) % 2 === 0 ? ROW_UP : ROW_DOWN;
    }
    start = k;
  }
  return rows;
}

// --- the summary tiles (UI-SPEC "Today" item 1) ---------------------------------------------------------------------

export const TILES: readonly TileKey[] = ["decisions_due", "approvals_waiting", "stock_checks", "transfers_at_risk"];
export const TILE_LABEL: Readonly<Record<TileKey, string>> = {
  decisions_due: "Decisions due",
  approvals_waiting: "Approvals waiting",
  stock_checks: "Stock checks",
  transfers_at_risk: "Transfers at risk",
};

// The server's tile key a summary tile filters the cards by (each card lists the tiles it falls under, 06.1-13).
export function filterKey(tile: TileKey): TileKey {
  return tile;
}

// The cards under a tile, in the server's order (a filter only; never a sort). No tile keeps every card.
export function byTile<C extends Pick<TodayCard, "tiles">>(cards: readonly C[], tile: TileKey | null): C[] {
  if (tile === null) return [...cards];
  const k = filterKey(tile);
  return cards.filter((c) => c.tiles.includes(k));
}

// "10:00", or "10:00 · Sun 18 Oct" when the planned time falls on another day than the scenario date.
export function plannedAt(t: DayMin & { date?: string | null }, scenarioDay: number): string {
  return t.day === scenarioDay || !t.date ? hhmm(t.min) : `${hhmm(t.min)} · ${shortDate(t.date)}`;
}

// The tiles' notes (UI-SPEC "Today" item 1; the 0 forms say "None…", never "today").
export const firstDecideBy = (at: string) => `First: decide by ${at}`;
export const noneOn = (date: string) => `None on ${date}`;
export const waitingForYou = (n: number) => `${n} waiting for you`;
export const WAITING_NETWORK = (n: number) => `${n} waiting`; // (proposed) HQ's note: the network's approvals
export const NONE_WAITING = "None waiting";
export const openChecks = (n: number) => `${n} open`;
export const NONE_OPEN = "None open";
export const runNeedsDecision = (run: string, at: string) => `${run} needs a decision by ${at}`;
export const atRiskCount = (n: number) => `${n} at risk`; // (proposed) no transfer at risk names a run's deadline
export const NONE = "None";

export function tileNote(tile: TileKey, counts: TodayCounts, cards: readonly TodayCard[], scenario: { day: number; date: string },
  network: boolean): string {
  const n = counts[tile];
  switch (tile) {
    case "decisions_due":
      return n === 0 || counts.first_deadline === null
        ? noneOn(shortDate(scenario.date)) : firstDecideBy(plannedAt(counts.first_deadline, scenario.day));
    case "approvals_waiting":
      return n === 0 ? NONE_WAITING : network ? WAITING_NETWORK(n) : waitingForYou(n);
    case "stock_checks":
      return n === 0 ? NONE_OPEN : openChecks(n);
    case "transfers_at_risk": {
      if (n === 0) return NONE;
      const first = cards.find((c) => c.tiles.includes("transfers_at_risk") && c.deadline?.cause.run_code);
      return first?.deadline && first.deadline.cause.run_code
        ? runNeedsDecision(first.deadline.cause.run_code, plannedAt(first.deadline, scenario.day)) : atRiskCount(n);
    }
  }
}

// The filter line and the filtered empty state (proposed, UI-SPEC "UI Considerations" empty S3).
export const showingTile = (tile: TileKey) => `Showing: ${TILE_LABEL[tile]}`;
export const CLEAR_FILTER = "Clear filter"; // (proposed)
export const noTileHeading = (tile: TileKey) => `No ${TILE_LABEL[tile].toLowerCase()}`; // (proposed)
export const noTileBody = (tile: TileKey, store: string) => `No ${TILE_LABEL[tile].toLowerCase()} for ${store}.`; // (proposed)

// --- the action cards (UI-SPEC "Today" item 3) ----------------------------------------------------------------------

// The next action in words: vocab.NEXT_ACTION_LABEL, which holds the two stock-check keys 06.1-13 added; an unknown
// key gives no label.
export function nextLabel(key: string | null): string | null {
  if (key === null) return null;
  return NEXT_ACTION_LABEL[key as NextActionKey] ?? null;
}

// Whether the signed-in account is one of the next action's actors (the 4px navy rule and the primary button).
export function actsOn(next: TodayCard["next_action"], s: { role: Who["role"]; branch: string | null }): boolean {
  return next.key !== null && next.who.some((w) => w.role === s.role && (w.role === "hq" || w.branch === s.branch));
}

// "S042 manager" or "HQ planner".
export const whoName = (w: Who) => (w.role === "hq" || w.branch === null ? ROLE_LABEL.hq : `${w.branch} manager`);

// Where the card's button goes: the stock decision opens at its current step (the server's), which is where its next
// action is done; a stock check is answered or confirmed on Tasks.
export function actionHref(c: Pick<TodayCard, "kind" | "case_key">): string {
  return c.kind === "stock_check" ? "#/tasks" : caseHref(c.case_key);
}
export const caseHref = (key: string) => `#/cases/${encodeURIComponent(key)}`;

// The card's what line: "Decide: Fresh milk 1 L · MILK-1L lot MLK-042" at the Decide stage, else the server step's name
// ("Verify: …"); a stock check reads "Stock check: … lot …" (proposed beyond the UI-SPEC's "Decide:" example).
export const DECIDE = "Decide";
export function whatLine(c: Pick<TodayCard, "kind" | "what" | "status">, stepName: (k: string) => string): string {
  const lot = c.what.lot_id ? ` lot ${c.what.lot_id}` : "";
  const thing = `${productLabel(c.what.sku, c.what.product)}${lot}`;
  if (c.kind === "stock_check") return `Stock check: ${thing}`;
  const verb = c.status.stage === "decide" || c.status.step === null ? DECIDE : stepName(c.status.step);
  return `${verb}: ${thing}`;
}

// "SUS-19 · S042 → S017" (or "SUS-19 · S042" without a destination).
export function whereLine(c: Pick<TodayCard, "case_key" | "where">): string {
  return [c.case_key, c.where.dest ? `${c.where.source} → ${c.where.dest}` : c.where.source].join(" · ");
}

// The packing time in words: 120 → "2 h", 90 → "1 h 30 min", 45 → "45 min".
export function duration(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  return [h > 0 ? `${h} h` : "", m > 0 || h === 0 ? `${m} min` : ""].filter((x) => x !== "").join(" ");
}

// The deadline's cause in words (UI-SPEC "Today": "R-02 cutoff 12:00 minus 2 h packing"). The run's loading cutoff
// is the deadline plus the packing time (06.1-13: decide-by = cutoff − packing_min). Proposed for the two other causes.
export const CAUSE_MARKDOWN = "before the next markdown step"; // (proposed)
export const CAUSE_STOCK_CHECK = "stock check due"; // (proposed)
export function causeText(d: NonNullable<TodayCard["deadline"]>, packing: number | null): string | null {
  if (d.cause.source === "run" && d.cause.run_code) {
    return packing === null ? `${d.cause.run_code} cutoff`
      : `${d.cause.run_code} cutoff ${hhmm((d.min + packing) % 1440)} minus ${duration(packing)} packing`;
  }
  if (d.cause.source === "markdown") return CAUSE_MARKDOWN;
  if (d.cause.source === "stock_check_due") return CAUSE_STOCK_CHECK;
  return null;
}

// "Decide by 10:00" for a stock decision; "Answer by 10:00" for a stock check (proposed).
export const deadlineWord = (c: Pick<TodayCard, "kind">) => (c.kind === "stock_check" ? "Answer by" : "Decide by");

// The second meta line: "Owner: Bang Na manager · Decide by 10:00 · R-02 cutoff 12:00 minus 2 h packing".
export const NOT_TAKEN_OWNER = `Owner: ${NOT_TAKEN}`; // a stock decision nobody took yet (UI-SPEC "Ownership")
export function ownerDeadlineLine(c: TodayCard, scenarioDay: number, packing: number | null): string {
  const owner = c.owner === null ? NOT_TAKEN_OWNER : `Owner: ${c.owner.label}`;
  if (c.deadline === null) return owner;
  const cause = causeText(c.deadline, packing);
  return [owner, `${deadlineWord(c)} ${plannedAt(c.deadline, scenarioDay)}`, cause ?? ""].filter((x) => x !== "")
    .join(" · ");
}

// --- the strip's markers and bars (UI-SPEC "Today" item 2; D-18) ----------------------------------------------------

// One thing drawn on a lane: a marker (a diamond or a stock-check tick at one planned minute) or a bar (pick and pack,
// travel) from its planned start to its planned end. `left` and `width` are percentages of the 06:00–18:00 track;
// `row` is the fraction of the lane height (stackMarkers); `name` is the button's accessible name ("…, planned").
export type StripElement = {
  id: string;
  shape: "marker" | "bar";
  kind: LaneItem["kind"];
  left: number;
  width: number;
  row: number;
  label: string;
  name: string;
  dashed: boolean;
  outside: boolean;
  caseKey: string | null;
  requestId: string | null;
};

export const PLANNED_SUFFIX = ", planned";
export const decideBy = (key: string, at: string) => `${key} decide by ${at}`;
export const checkDue = (key: string, at: string) => `${key} stock check due ${at}`; // (proposed)
export const packLabel = (qty: number, conditional: boolean) =>
  `Pick & pack ${Math.round(qty)}${conditional ? " · if sent" : ""}`;
export const deliveryLabel = (at: string) => `Delivery ${at}`;
export const cutoffLabel = (at: string) => `cutoff ${at}`;
export const travelLabel = (from: string, to: string) => `departs ${from} · arrives ${to}`;

// The elements of one lane, in the lane's own order (the server's), each placed by `position` on the scenario day's
// 06:00–18:00 window; markers closer than 4% apart are stacked, bars keep the middle of the lane.
export function laneElements(lane: Lane, scenarioDay: number): StripElement[] {
  const at = (t: DayMin) => position(minuteOf(t, scenarioDay), STRIP_FROM, STRIP_TO);
  const run = lane.run_code ?? "";
  const out = lane.items.map((item, i): StripElement => {
    const base = { id: `${lane.key}:${i}`, kind: item.kind, row: ROW_MID, caseKey: null as string | null,
      requestId: null as string | null, dashed: false };
    if (item.kind === "pick_and_pack" || item.kind === "travel") {
      const a = at(item.start);
      const b = at(item.end);
      const label = item.kind === "pick_and_pack" ? packLabel(item.qty, item.conditional)
        : travelLabel(hhmm(item.start.min), hhmm(item.end.min));
      const name = item.kind === "pick_and_pack"
        ? `${label} · ${item.case_key} · ${hhmm(item.start.min)}–${hhmm(item.end.min)}${PLANNED_SUFFIX}`
        : `${run} ${label}${PLANNED_SUFFIX}`;
      return { ...base, shape: "bar", left: a.x * 100, width: Math.max(0, b.x - a.x) * 100, label, name,
        dashed: item.kind === "pick_and_pack" && item.conditional, outside: a.outside && b.outside,
        caseKey: item.kind === "pick_and_pack" ? item.case_key : null };
    }
    const p = at(item);
    const t = hhmm(item.min);
    let label: string;
    let name: string;
    let caseKey: string | null = null;
    let requestId: string | null = null;
    switch (item.kind) {
      case "decision_deadline":
        label = decideBy(item.case_key, t);
        name = label + PLANNED_SUFFIX;
        caseKey = item.case_key;
        break;
      case "stock_check_due":
        label = checkDue(item.case_key, t);
        name = label + PLANNED_SUFFIX;
        caseKey = item.case_key;
        requestId = item.request_id;
        break;
      case "delivery":
        label = deliveryLabel(t);
        name = `${deliveryLabel(t)} · ${item.sku}${PLANNED_SUFFIX}`;
        break;
      case "loading_cutoff":
        label = cutoffLabel(t);
        name = `${run} loading cutoff ${t}${PLANNED_SUFFIX}`;
        break;
    }
    return { ...base, shape: "marker", left: p.x * 100, width: 0, label, name, outside: p.outside, caseKey, requestId };
  });
  const markers = out.filter((e) => e.shape === "marker");
  const rows = stackMarkers(markers.map((m) => m.left / 100));
  markers.forEach((m, i) => { m.row = rows[i]; });
  return out;
}

// A lane's label: "Decisions", "Stock checks", "{S042} floor", "{R-02} → {S017}".
export const LANE_DECISIONS = "Decisions";
export const LANE_CHECKS = "Stock checks";
export const floorLabel = (store: string) => `${store} floor`;
export function laneLabel(lane: Lane): string {
  if (lane.kind === "decisions") return LANE_DECISIONS;
  if (lane.kind === "stock_checks") return LANE_CHECKS;
  if (lane.kind === "floor") return floorLabel(lane.branch);
  return `${lane.run_code ?? ""} → ${lane.dest ?? ""}`;
}
