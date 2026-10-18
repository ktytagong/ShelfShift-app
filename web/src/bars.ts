// The arithmetic every 06.1 bar draws (06.1-UI-SPEC "New 06.1 primitives"): step-bar segments. Pure and erasable
// TypeScript only (no enums, namespaces or parameter properties), so `node --test` runs this file; no React here.
// ui.tsx draws what these return; the numbers and labels a reader needs always sit in text beside the bar.

// --- Step bar: nine segments, done / current / later --------------------------------------------------------------

export type SegmentState = "done" | "current" | "later";

const OPTIONS_STEP = 3; // Options: the engine prepares it, so the server never sends it as the current step (D-25)

// `current` is the server's step_number (1..total), or 0 for a flag before evaluation (no step yet: every segment is
// later). With `optionsDone` (a recommendation exists, 06.1-01 contract "OPTIONS is always done once a recommendation
// exists") the Options segment is done at any step and is never drawn as the current one. A number outside 0..total,
// or not whole, is a caller error: RangeError, never a guessed bar.
export function segmentStates(current: number, total: number, optionsDone = false): SegmentState[] {
  if (!Number.isInteger(total) || total < 1) throw new RangeError(`total must be a whole number of steps, got ${total}`);
  if (!Number.isInteger(current) || current < 0 || current > total) {
    throw new RangeError(`current must be a whole step from 0 to ${total}, got ${current}`);
  }
  return Array.from({ length: total }, (_, i): SegmentState => {
    const step = i + 1;
    if (optionsDone && current > 0 && step === OPTIONS_STEP) return "done";
    return step < current ? "done" : step === current ? "current" : "later";
  });
}

// --- Quantity bar: committed track, dispatched and received fills, damaged and missing parts --------------------------

// A transfer's quantities as the server records them (06.1-01 contract, receipt row): received and damaged are separate
// and received + damaged <= dispatched. null means "not recorded yet", never zero.
export type QtyInput = { committed: number; dispatched: number | null; received: number | null; damaged?: number | null };
export type QtyParts = {
  // Whole units for the adjacent text; null where the record does not exist yet.
  units: {
    committed: number; dispatched: number | null; received: number | null; damaged: number | null;
    missing: number | null; // dispatched but neither received nor damaged (known only once a receipt is recorded)
    short: number | null; // committed but never dispatched (known once a dispatch is recorded)
  };
  // Fractions of the track (the committed quantity, or the dispatched one if a record ever exceeds it), 0 when absent.
  widths: { dispatched: number; received: number; damaged: number; missing: number; short: number };
};

const nonNegative = (n: number) => Math.max(0, n);

export function qtyParts(q: QtyInput): QtyParts {
  const dispatched = q.dispatched;
  const received = dispatched === null ? null : q.received;
  const damaged = received === null ? null : q.damaged ?? 0;
  const missing = received === null || damaged === null || dispatched === null
    ? null : nonNegative(dispatched - received - damaged);
  const short = dispatched === null ? null : nonNegative(q.committed - dispatched);
  const track = Math.max(q.committed, dispatched ?? 0);
  const part = (n: number | null) => (n === null || track <= 0 ? 0 : n / track);
  return {
    units: { committed: q.committed, dispatched, received, damaged, missing, short },
    widths: { dispatched: part(dispatched), received: part(received), damaged: part(damaged), missing: part(missing), short: part(short) },
  };
}

// --- Scaled bars: each value as a fraction of the largest ---------------------------------------------------------

// `max` defaults to the largest value. A zero (or negative) maximum gives zeros, never NaN; a value above the maximum
// is capped at 1 and a negative one at 0, so no bar overflows its track.
export function scaled(values: number[], max: number = Math.max(0, ...values)): number[] {
  return values.map((v) => (max > 0 ? Math.min(1, nonNegative(v) / max) : 0));
}

// --- History filter: All · Decisions · Data --------------------------------------------------------------------------

// The filter names are the API's group names (GET /api/cases/{id}/activity sends "Decisions", "Data" and
// "Assistant"), matched exactly: the client never re-maps a server group. "Assistant" entries show under All only.
export type HistoryFilter = "All" | "Decisions" | "Data";

// Keeps the caller's order (the caller passes entries sorted by recorded time, D-58).
export function historyFilter<T extends { group: string }>(entries: T[], filter: HistoryFilter): T[] {
  return filter === "All" ? entries : entries.filter((e) => e.group === filter);
}

// --- Chart tables: what "Show as table" draws (06.1-UI-SPEC "Accessibility contract": every chart has a table) -----------

// A line or bar series, or a band (the forecast's 80% range) with a low and a high value per label.
export type ChartSeries =
  | { name: string; values: (number | null)[] }
  | { name: string; low: (number | null)[]; high: (number | null)[] };
export type ChartData = { labels: string[]; series: ChartSeries[] };
export type ChartTable = { head: string[]; rows: (string | number | null)[][] };

// One row per label: the label, then one column per series, or two for a band ("{name} low", "{name} high"). A value
// the series lacks stays null (the table shows it as missing), never zero.
export function tableRows(data: ChartData, labelHead = "Date"): ChartTable {
  const at = (xs: (number | null)[], i: number) => xs[i] ?? null;
  const head = [labelHead, ...data.series.flatMap((s) => ("values" in s ? [s.name] : [`${s.name} low`, `${s.name} high`]))];
  const rows = data.labels.map((label, i) => [
    label,
    ...data.series.flatMap((s) => ("values" in s ? [at(s.values, i)] : [at(s.low, i), at(s.high, i)])),
  ]);
  return { head, rows };
}
