// Stock & lots' pure parts (06.1-UI-SPEC "Stock & lots"; D-15, D-31, D-56): the cover bar of the recorded stock by the
// forecast to expiry, the row's decision action and the cell texts. The action is the server's `action` key (06.1-14:
// "view" with the live decision's key, "open" with this morning's flag key, null otherwise), so the client never
// derives whether a decision exists and never offers a second one for a lot. Wording the user has not confirmed is
// marked "(proposed)", one constant each. Pure, erasable syntax only, so `node --test` runs it.
import type { LotHistoryEntry } from "./contract.ts";
import { expected, shortDate, units } from "./format.ts";
import { TERMS, historyLine, recordedLine, scenarioDateLine, type HistoryLine } from "./vocab.ts";

export const NO_STOCK = "No stock on record";
export const NO_FORECAST = "No forecast through expiry"; // (proposed) a day to expiry has no forecast point
export const NOT_COUNTED = "Not counted"; // (proposed) the record holds no count day for the lot
export const NONE = "—";

// --- the cover bar -------------------------------------------------------------------------------------------------

export type CoverParts = { fill: number; surplus: number; text: string };

// The recorded quantity is the track: the base forecast through expiry fills it (never past the stock), the rest is
// the surplus that will not sell by expiry. The forecast is an expectation, in whole units; the record keeps one
// decimal. No stock draws no bar; a missing forecast is said, never drawn as zero demand.
export function coverParts(row: { qty: number; forecast_to_expiry: number | null }): CoverParts {
  const qty = row.qty;
  if (!(qty > 0)) return { fill: 0, surplus: 0, text: NO_STOCK };
  if (row.forecast_to_expiry === null) return { fill: 0, surplus: 0, text: NO_FORECAST };
  const sold = Math.min(Math.max(0, row.forecast_to_expiry), qty);
  return { fill: sold / qty, surplus: (qty - sold) / qty, text: `${expected(sold)} of ${units(qty)} sell by expiry` };
}

// --- the row's decision action (D-31, D-56) -----------------------------------------------------------------------

export type LotActionKey = "view" | "open" | null;
export type RowAction = { label: string; href: string; primary: boolean };

// "View stock decision" for a live decision, "Open stock decision" for a flagged lot (the decision page evaluates it
// once through the evaluate route, which reuses the lot's decision), nothing otherwise or without the key to link to.
export function rowAction(row: { case_key: string | null; flag_key: string | null; action: LotActionKey }): RowAction | null {
  const to = (key: string) => `#/cases/${encodeURIComponent(key)}`;
  if (row.action === "view" && row.case_key !== null) {
    return { label: TERMS.VIEW_STOCK_DECISION, href: to(row.case_key), primary: false };
  }
  if (row.action === "open" && row.flag_key !== null) {
    return { label: TERMS.OPEN_STOCK_DECISION, href: to(row.flag_key), primary: true };
  }
  return null;
}

// --- cell texts --------------------------------------------------------------------------------------------------

// "Sun 18 Oct · printed" or "· estimated" (the expiry source is simulated data: printed on the label or estimated).
export function expiryText(row: { expiry_date: string | null; expiry_source: string }): string {
  if (row.expiry_date === null) return NONE;
  return `${shortDate(row.expiry_date)} · ${row.expiry_source.toLowerCase()}`;
}

// "Counted Sat 17 Oct" on the scenario date; "Not counted for 3 days" when the last count is older (a count of scenario
// days, never a clock); NOT_COUNTED without a count.
export function countText(row: { last_verified_day: number | null; last_verified_date: string | null }, day: number): string {
  if (row.last_verified_day === null || row.last_verified_date === null) return NOT_COUNTED;
  const n = day - row.last_verified_day;
  if (n <= 0) return `Counted ${shortDate(row.last_verified_date)}`;
  return `Not counted for ${n} ${n === 1 ? "day" : "days"}`;
}

// "20% on Sun 18 Oct": the schedule's next step deeper than the one in force, or "—".
export function markdownText(next: { day: number; date: string | null; pct: number } | null): string {
  if (next === null) return NONE;
  return next.date === null ? `${units(next.pct)}%` : `${units(next.pct)}% on ${shortDate(next.date)}`;
}

// The discrepancy chip's text (proposed): "1 open"; null for none (the cell shows "—").
export function discrepancyText(n: number): string | null {
  return n > 0 ? `${units(n)} open` : null;
}

// --- the lot's history (D-56 "View history"; D-58, D-63: both clocks on every line) -------------------------------

// A decision's line (proposed): "Stock decision SUS-19 opened"; it carries no "by", because the record does not say
// whether the screen or a person opened it.
export function decisionOpened(key: string): string {
  return `${TERMS.STOCK_DECISION} ${key} opened`;
}
export const CLOSED_AS: Readonly<Record<string, string>> = { COMPLETED: "Completed", CANCELLED: "Cancelled" }; // (proposed)
// The movement kinds as the history's event types (vocab.EVENT_TITLES: "Dispatch recorded", "Receipt recorded", ...).
const MOVEMENT_EVENT: Readonly<Record<string, string>> = {
  DISPATCH: "dispatched", RECEIPT: "received", PRICE_APPLIED: "price_applied",
};

// The drawer's history in the server's order (recorded time): decisions in the Decisions group, stock movements in
// Data. Movements read through vocab.historyLine, so their titles and details match the stock decision's history.
export function lotHistory(entries: LotHistoryEntry[], tz: string): HistoryLine[] {
  return entries.map((e) => {
    if (e.type === "stock_decision") {
      const meta = [e.recorded_at === null ? "" : recordedLine(e.recorded_at, tz),
        e.scenario_date === null ? "" : scenarioDateLine(e.scenario_date)].filter((x) => x !== "").join(" · ");
      return { title: decisionOpened(e.key), meta, detail: e.closed_as === null ? "" : CLOSED_AS[e.closed_as] ?? "",
        group: "Decisions" };
    }
    const payload: Record<string, unknown> = e.kind === "RECEIPT"
      ? { received_qty: e.qty, damaged_qty: e.damaged_qty, reason: e.reason }
      : { qty: e.qty, lot_id: e.lot_id, reason: e.reason };
    const line = historyLine({
      type: MOVEMENT_EVENT[e.kind] ?? e.kind.toLowerCase(), at: e.recorded_at ?? "", day: e.scenario_day,
      scenario_date: e.scenario_date, actor: e.actor, payload, group: "Data",
    }, tz);
    return e.key === null ? line : { ...line, detail: [line.detail, e.key].filter((x) => x !== "").join(" · ") };
  });
}
