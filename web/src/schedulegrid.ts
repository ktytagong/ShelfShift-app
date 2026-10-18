// The Planned schedule's pure geometry (06.1-UI-SPEC "Planned schedule", the schedule grid dimensions; 06.1-CONTEXT
// D-18, D-29; sketch 003 A). The page lays out exactly what GET /api/schedule sends: a time is drawn only where the
// server gave one, a null start is "Unscheduled" and gets no bar, and a markdown (a day without a clock time) has no
// bar in Day view and sits on its day in Week and Month. Nothing here sorts, filters or reads a clock: dates are
// computed from the ISO dates and day indexes the server sent, on the UTC calendar.
// Erasable syntax only, so `node --test` runs this file as it is.

export type GridView = "day" | "week" | "month";
export type GridTime = { day: number; min: number | null };
export type Span = { start: GridTime | null; end: GridTime | null };
export type DateRange = { from: string; to: string };
export type GridWindow = DateRange & { from_day: number; to_day: number };

// UI-SPEC "Fixed component dimensions": hour column 64px (Day), day column 128px (Week), 40px (Month); rows 44px,
// header 40px; Day runs 06:00..18:00, so its last column ends at 19:00.
export const HOUR_W = 64;
export const WEEK_W = 128;
export const MONTH_W = 40;
export const ROW_H = 44;
export const HEAD_H = 40;
export const DAY_FIRST_MIN = 6 * 60;
export const DAY_HOURS = 13;
export const DAY_END_MIN = DAY_FIRST_MIN + DAY_HOURS * 60;
// The page leaves this much room before 06:00 and after 19:00 in Day view, so a mark on an edge is drawn whole; it is
// added where the grid is drawn, never to a geometry.
export const DAY_PAD = 12;
const DAY_MIN = 24 * 60;
const WEEK_DIAMOND = 12; // a Week diamond sits 12px into its day cell, its label beside it (sketch 003)
const CELL_GAP = 3; // a Week or Month bar leaves 3px on each side of its cells

export type Column = { key: string; label: string; sub: string; width: number; date: string | null };

const pad = (n: number) => String(n).padStart(2, "0");
const utc = (iso: string) => new Date(`${iso}T00:00:00Z`);
const iso = (d: Date) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
const WEEKDAY = new Intl.DateTimeFormat("en-US", { weekday: "short", timeZone: "UTC" });
const MONTH_SHORT = new Intl.DateTimeFormat("en-US", { month: "short", timeZone: "UTC" });
const MONTH_LONG = new Intl.DateTimeFormat("en-US", { month: "long", timeZone: "UTC" });

// The ISO date `n` days after `date`.
export function addDays(date: string, n: number): string {
  const d = utc(date);
  d.setUTCDate(d.getUTCDate() + n);
  return iso(d);
}

// The days of a window, first to last.
function datesOf(r: DateRange): string[] {
  const out: string[] = [];
  for (let d = r.from; d <= r.to; d = addDays(d, 1)) out.push(d);
  return out;
}

// The time columns of a window: Day the 13 hours from 06:00, Week its seven days ("Mon · 12"), Month one narrow column
// per day (the day number, the weekday's initial under it).
export function columnsFor(view: GridView, r?: DateRange): Column[] {
  if (view === "day") {
    return Array.from({ length: DAY_HOURS }, (_, i) => {
      const label = `${pad(6 + i)}:00`;
      return { key: label, label, sub: "", width: HOUR_W, date: null };
    });
  }
  if (r === undefined) return [];
  return datesOf(r).map((date) => {
    const d = utc(date);
    const wd = WEEKDAY.format(d);
    return view === "week"
      ? { key: date, label: `${wd} · ${d.getUTCDate()}`, sub: "", width: WEEK_W, date }
      : { key: date, label: String(d.getUTCDate()), sub: wd.charAt(0), width: MONTH_W, date };
  });
}

export type Geometry = { left: number; width: number; shape: "bar" | "diamond"; clipStart: boolean; clipEnd: boolean };

// Where a planned time sits on the grid, in px from the grid's left edge (a diamond's `left` is its centre), or null
// when it is not drawn: no planned start (Unscheduled), a markdown's day-only time in Day view, or a time wholly
// outside the window. A bar that runs past an edge of the grid is cut there, and `clipStart`/`clipEnd` say so; a
// diamond outside the Day grid's hours is not drawn (its time still reads in the Start column).
// `first` is the window's first day index; `days` the number of day columns (Week 7, Month the month's days).
export function barGeometry(t: Span, view: GridView, first: number, days = view === "week" ? 7 : 1): Geometry | null {
  const s = t.start;
  if (s === null) return null;
  const e = t.end ?? s;
  const diamond = s.min === null || (e.day === s.day && e.min === s.min);
  if (view === "day") {
    if (s.min === null || e.min === null) return null;
    const a = (s.day - first) * DAY_MIN + s.min;
    const b = (e.day - first) * DAY_MIN + e.min;
    const x = (m: number) => ((m - DAY_FIRST_MIN) * HOUR_W) / 60;
    if (diamond) {
      return a < DAY_FIRST_MIN || a > DAY_END_MIN ? null
        : { left: x(a), width: 0, shape: "diamond", clipStart: false, clipEnd: false };
    }
    if (b <= DAY_FIRST_MIN || a >= DAY_END_MIN) return null;
    const lo = Math.max(a, DAY_FIRST_MIN);
    const hi = Math.min(b, DAY_END_MIN);
    return { left: x(lo), width: x(hi) - x(lo), shape: "bar", clipStart: lo > a, clipEnd: hi < b };
  }
  const cw = view === "week" ? WEEK_W : MONTH_W;
  const i = s.day - first;
  const j = e.day - first;
  if (j < 0 || i > days - 1) return null;
  if (diamond) {
    return { left: i * cw + (view === "week" ? WEEK_DIAMOND : cw / 2), width: 0, shape: "diamond", clipStart: false,
      clipEnd: false };
  }
  const lo = Math.max(i, 0);
  const hi = Math.min(j, days - 1);
  return { left: lo * cw + CELL_GAP, width: (hi - lo + 1) * cw - 2 * CELL_GAP, shape: "bar", clipStart: lo > i,
    clipEnd: hi < j };
}

export type Point = { x: number; y: number };
export type Connector = { line: string; head: string };

// A dependency connector (Day view only): an elbow from the end of one bar (`from`) to the start of the next (`to`),
// and its arrowhead. When the next bar starts too close to (or before) the end of the first, the line steps out 8px,
// turns halfway between the rows and comes back round into the next bar from its left.
export function connectorPath(from: Point, to: Point, view: GridView): Connector | null {
  if (view !== "day") return null;
  const { x: x1, y: y1 } = from;
  const { x: x2, y: y2 } = to;
  const line = x2 - x1 >= 16
    ? `M${x1},${y1} H${x2 - 8} V${y2} H${x2}`
    : `M${x1},${y1} h8 V${y1 + (y2 >= y1 ? 1 : -1) * (ROW_H / 2)} H${x2 - 10} V${y2} H${x2}`;
  return { line, head: `M${x2 - 6},${y2 - 3} L${x2},${y2} L${x2 - 6},${y2 + 3} Z` };
}

// The date stepper: one day, one week, or the first of the next or previous month.
export function stepDate(date: string, view: GridView, dir: 1 | -1): string {
  if (view === "day") return addDays(date, dir);
  if (view === "week") return addDays(date, 7 * dir);
  const d = utc(date);
  return iso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + dir, 1)));
}

// The ISO date of a day index, from the window the server sent (its first date and first day index).
export function dateOfDay(win: GridWindow, day: number): string {
  return addDays(win.from, day - win.from_day);
}

// "Sat 17 Oct".
export function shortDay(date: string): string {
  const d = utc(date);
  return `${WEEKDAY.format(d)} ${d.getUTCDate()} ${MONTH_SHORT.format(d)}`;
}

// The stepper's label: "Sat 17 Oct 2026", "Mon 12 – Sun 18 Oct 2026" (the month on both ends when they differ), or
// "October 2026".
export function rangeLabel(view: GridView, r: DateRange): string {
  const a = utc(r.from);
  const b = utc(r.to);
  if (view === "day") return `${shortDay(r.from)} ${a.getUTCFullYear()}`;
  if (view === "month") return `${MONTH_LONG.format(a)} ${a.getUTCFullYear()}`;
  const head = a.getUTCMonth() === b.getUTCMonth()
    ? `${WEEKDAY.format(a)} ${a.getUTCDate()}`
    : a.getUTCFullYear() === b.getUTCFullYear() ? shortDay(r.from) : `${shortDay(r.from)} ${a.getUTCFullYear()}`;
  return `${head} – ${shortDay(r.to)} ${b.getUTCFullYear()}`;
}
