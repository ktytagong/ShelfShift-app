// The Planned schedule (06.1-UI-SPEC "Planned schedule"; 06.1-CONTEXT D-17, D-18, D-29; sketch 003 A, the model page
// of the visual-first directive, D-10): a work-order table beside a horizontal time grid. A row says what, a bar says
// when and for how long, and alignment shows sequence and overlap. It is read-only: no drag, no time editing, no
// actual bars, no delay, overdue or conflict flag and no current-time line. It reads GET /api/schedule only; the
// server scopes (a manager's own store), filters, groups, orders and pages (D-17), and the client lays out the rows in
// the order they arrive. A time is drawn only where the server sent one (schedulegrid.ts); a task with no planned time
// reads "Unscheduled" and has no bar. Strings marked (proposed) are Claude's wording, one constant each.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { ApiError } from "../api.ts";
import { STORE_PLACEHOLDER } from "../copy.ts";
import { getSchedule, type ScheduleRow, type ScheduleView } from "../contract.ts";
import { hhmm, units } from "../format.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import {
  DAY_PAD, HEAD_H, ROW_H, barGeometry, columnsFor, connectorPath, dateOfDay, rangeLabel, shortDay, stepDate, type Geometry,
  type GridTime, type GridView, type GridWindow,
} from "../schedulegrid.ts";
import { PageHead, useSession, type Route } from "../shell.tsx";
import {
  Button, Drawer, Icon, Kv, LabelledSelect, LoadError, Loading, Pager, Pill, Segmented, StaleBanner, type IconName,
} from "../ui.tsx";
import { CLEAR_FILTERS, NO_TRANSFERS, PLANNED_NOTE, TERMS, UNSCHEDULED, scheduleStatus } from "../vocab.ts";

export const TITLE = "Planned schedule";
export const NAV_LABEL = "Schedule";
export const LOADING = "Loading the planned schedule…"; // (proposed, UI-SPEC "Interaction states")
export const DIDNT_LOAD = "The planned schedule didn't load"; // (proposed, UI-SPEC "Error (page load)")
export const TABS_LABEL = "Schedule views"; // (proposed) the accessible name of Work orders | Activities
export const TAB_WORK_ORDERS = "Work orders";
export const TAB_ACTIVITIES = "Activities";
export const COL_NAME = "Work order / activity";
export const COL_ACTIVITY = "Activity"; // the first column on the Activities tab (sketch 003)
export const COL_START = "Start";
export const COL_END = "End";
export const COL_STATUS = "Status";
export const AT_OPENING = "at opening"; // a markdown's day-only time (UI-SPEC "{Sun 18 Oct}, at opening")
export const NO_END = "—"; // (proposed) the End cell of a milestone, which has one planned moment
export const PLANNED_WORD = "planned"; // R-7: a planned time reads with "planned"
export const COLLAPSE = "Collapse"; // (proposed) the parent chevron's accessible name, with the row title
export const EXPAND = "Expand"; // (proposed)
// The toolbar (UI-SPEC "Planned schedule" Panel). "Scenario date" replaces D-29's "Today" (UI-SPEC open question 4,
// recommended default; proposed): it returns the stepper to the scenario date.
export const SCENARIO_DATE_CHIP = "Scenario date";
export const DATE_LABEL = "Date"; // (proposed) the accessible name of the stepper group
export const STEP_WORD: Readonly<Record<GridView, string>> = { day: "day", week: "week", month: "month" };
export const PREVIOUS = "Previous"; // (proposed) "Previous day", "Previous week", "Previous month"
export const NEXT = "Next"; // (proposed) followed by the step word, as PREVIOUS is
export const GROUP_BY = "Group by";
export const GROUPS: readonly [View["group"], string][] = [
  ["work_order", "Work order"], ["branch", "Branch"], ["run", TERMS.SCHEDULED_TRUCK_RUN],
];
export const WINDOW_LABEL = "Window"; // (proposed) the accessible name of Day | Week | Month
export const WINDOWS: readonly [GridView, string][] = [["day", "Day"], ["week", "Week"], ["month", "Month"]];
export const BRANCH_FILTER = "Branch";
export const ACTIVITY_FILTER = "Activity";
export const STATUS_FILTER = "Status";
export const ALL_ACTIVITIES = "All activities"; // (proposed, sketch 003)
export const ALL_STATUSES = "All statuses"; // (proposed, sketch 003)
// The server's activity and status keys (04-API-CONTRACT 2026-10-17 row), in its order.
export const ACTIVITY_KEYS = [
  "decision_deadline", "stock_check", "pick_and_pack", "loading_cutoff", "transport", "receive", "markdown", "delivery",
] as const;
export const STATUS_KEYS = [
  "open", "not_requested", "answered", "confirmed", "expired", "superseded", "not_started", "scheduled", "dispatched",
  "partially_received", "received", "discrepancy", "done", "rejected", "closed", "cancelled", "voided",
] as const;
// A run from another store that brings a transfer here (06.1-13 deviation 5): its sub line names the origin plainly
// (proposed); the run's destination is the store in view.
export const incomingRun = (origin: string) => `Incoming from ${origin}`;
// The legend (UI-SPEC "Legend").
export const LEGEND_TITLE = "Planned schedule";
export const LEGEND_DEPENDS = "Depends on";
export const LEGEND_UNSCHEDULED = "Unscheduled = no planned time in the data";
// Empty states (UI-SPEC "Copywriting Contract", Empty: schedule; sketch 003).
export const nothingPlanned = (range: string) => `Nothing planned for ${range}`;
export const NOTHING_PLANNED_BODY =
  "Planned work appears here when a stock decision is sent or a scheduled truck run is booked.";
export const NO_MATCH = "No activity matches these filters.";
// The read-only drawer (UI-SPEC "Drawer").
export const DRAWER_KEYS = {
  owner: "Owner", branch: "Branch", qty: "Quantity", planned: "Planned", status: "Status", depends: "Depends on",
  run: TERMS.SCHEDULED_TRUCK_RUN, decision: "Linked stock decision",
} as const;
export const UNSCHEDULED_LONG = "Unscheduled · no planned time in the data";
export const NONE = "—"; // (proposed) a drawer value the row does not have
export const ACTIVITIES_TITLE = "Activities"; // (proposed) the work order drawer's list of its activities
export const OPEN_TRANSFERS = "Open Branch transfers"; // (proposed) a transfer activity's linked page
export const OPEN_TASKS = "Open Tasks"; // (proposed) a stock check's linked page
export const unitsText = (n: number) => `${units(n)} ${n === 1 ? "unit" : "units"}`;

// The server's row kinds in words, with the icon that marks the kind (D-29: colour and icon mark the kind only) and,
// for a leaf, its fill on the grid (UI-SPEC "Kind colours"). Transport has one fill: the row carries no cold-chain
// flag, so the grid never claims one.
type KindLook = { label: string; icon: IconName; fill: string };
export const KIND: Readonly<Record<string, KindLook>> = {
  work_order: { label: "Work order", icon: "list", fill: "" },
  run: { label: TERMS.SCHEDULED_TRUCK_RUN, icon: "truck", fill: "" },
  branch: { label: "Branch", icon: "pin", fill: "" },
  decision_deadline: { label: "Decision deadline", icon: "clock", fill: "bg-gold-500" },
  stock_check: { label: TERMS.STOCK_CHECK, icon: "check", fill: "bg-navy-700" },
  pick_and_pack: { label: "Pick and pack", icon: "box", fill: "bg-navy-800" },
  loading_cutoff: { label: "Loading cutoff", icon: "lock", fill: "bg-gold-600" },
  transport: { label: "Transport", icon: "truck", fill: "bg-kind-ambient" },
  receive: { label: "Receive", icon: "inbox", fill: "bg-navy-700" },
  markdown: { label: "Markdown", icon: "tag", fill: "bg-gold-300" },
  delivery: { label: "Supplier delivery", icon: "inbox", fill: "bg-navy-700" },
};
const CONTAINERS = new Set(["work_order", "run", "branch"]);
const kindOf = (k: string): KindLook => KIND[k] ?? { label: k.replace(/_/g, " "), icon: "circle", fill: "bg-navy-700" };

// A diamond's short label on the Day grid, before its time ("Decide by 10:00"; proposed, sketch 003).
export const MILESTONE_WORD: Readonly<Record<string, string>> = {
  decision_deadline: "Decide by", loading_cutoff: "Cutoff", delivery: "Delivery",
};

// The response as built (04-API-CONTRACT 2026-10-17 row, 06.1-13), typed in contract.ts.
export type Row = ScheduleRow;
export type View = ScheduleView;

// --- time words -----------------------------------------------------------------------------------------------------

const sameTime = (a: GridTime, b: GridTime | null) => b === null || (a.day === b.day && a.min === b.min);

// "Sat 17 Oct, 10:00–12:00", "Sat 17 Oct, 10:00", "Sun 18 Oct, at opening", or across midnight "Sat 17 Oct, 23:00 –
// Sun 18 Oct, 01:00"; null when the data has no planned time.
export function plannedText(r: Pick<Row, "start" | "end">, win: GridWindow): string | null {
  const s = r.start;
  if (s === null) return null;
  const day = (t: GridTime) => shortDay(dateOfDay(win, t.day));
  if (s.min === null) return `${day(s)}, ${AT_OPENING}`;
  const e = r.end;
  if (sameTime(s, e) || e === null || e.min === null) return `${day(s)}, ${hhmm(s.min)}`;
  return e.day === s.day ? `${day(s)}, ${hhmm(s.min)}–${hhmm(e.min)}` : `${day(s)}, ${hhmm(s.min)} – ${day(e)}, ${hhmm(e.min)}`;
}

// One Start or End cell: the time, with its short date under it when the window holds more than one day or the time
// falls on another day than the window's.
function TimeCell({ t, v, win }: { t: GridTime; v: GridView; win: GridWindow }) {
  const date = shortDay(dateOfDay(win, t.day));
  const showDate = v !== "day" || t.day !== win.from_day || t.min === null;
  return (
    <span className="num flex flex-col justify-center leading-tight whitespace-nowrap">
      <span className={t.min === null ? "text-label" : ""}>{t.min === null ? AT_OPENING : hhmm(t.min)}</span>
      {showDate && <span className="text-label text-tx2">{date}</span>}
    </span>
  );
}

// --- statuses (R-2: one tone, icon and label per recorded state) -----------------------------------------------------

// The words live in vocab.ts (SCHEDULE_STATUS), beside the Decision and Movement chips (R-2).
const statusLook = scheduleStatus;

// --- toolbar controls ------------------------------------------------------------------------------------------------

function StepButton({ icon, label, disabled, onClick }: {
  icon: IconName; label: string; disabled: boolean; onClick: () => void;
}) {
  return (
    <button type="button" aria-label={label} disabled={disabled} onClick={onClick}
      className="inline-flex h-7 w-7 items-center justify-center rounded-full text-tx2 hover:bg-white hover:text-tx disabled:opacity-50 max-md:h-11 max-md:w-11">
      <Icon name={icon} />
    </button>
  );
}

// The legend (UI-SPEC "Legend"): the kinds the grid draws, the connector and the Unscheduled rule.
const LEGEND_DIAMONDS = ["decision_deadline", "loading_cutoff", "markdown", "delivery"] as const;
const LEGEND_PILLS = ["pick_and_pack", "transport"] as const;
function Legend() {
  return (
    <div data-testid="schedule-legend"
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-line px-4 py-3 text-label text-tx2">
      <span className="font-semibold text-tx">{LEGEND_TITLE}</span>
      {LEGEND_DIAMONDS.map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <i className={`inline-block h-2.5 w-2.5 rotate-45 rounded-[2px] ring-1 ring-navy-950 ${KIND[k].fill}`} />
          {KIND[k].label}
        </span>
      ))}
      {LEGEND_PILLS.map((k) => (
        <span key={k} className="inline-flex items-center gap-1.5">
          <i className={`inline-block h-2.5 w-6 rounded-full ${KIND[k].fill}`} />{KIND[k].label}
        </span>
      ))}
      <span className="inline-flex items-center gap-1.5">
        <svg aria-hidden="true" width="24" height="8" viewBox="0 0 24 8">
          <path d="M0 4H18" stroke={LINK} strokeWidth="1.4" /><path d="M18 1L23 4L18 7Z" fill={LINK} />
        </svg>
        {LEGEND_DEPENDS}
      </span>
      <span>{LEGEND_UNSCHEDULED}</span>
    </div>
  );
}

// --- the page -------------------------------------------------------------------------------------------------------

type Tab = "work_orders" | "activities";

const ISO_DATE = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;
const oneOf = <T extends string>(v: string | undefined, all: readonly (readonly [T, string])[]): T | undefined =>
  all.find(([k]) => k === v)?.[0];
const oneOfKeys = (v: string | undefined, keys: readonly string[]): string => (keys.includes(v ?? "") ? v ?? "" : "");

// A deep link may name the window: "#/schedule?date=2026-10-17&view=week&group=run" (UI-SPEC "Routes"), and the
// activity, the status and, for HQ, the branch; an unknown value falls back to the default.
// Every hash change (the Schedule link, Back to an earlier address) brings a new route object and mounts the page
// afresh from the address, even when it names the address the page mounted with; the page's own replaceState fires
// no hash change, so its choices never remount it.
function SchedulePage({ route }: { route: Route }) {
  const seen = useRef({ route, n: 0 });
  if (seen.current.route !== route) seen.current = { route, n: seen.current.n + 1 };
  return <ScheduleView key={seen.current.n} route={route} />;
}

function ScheduleView({ route }: { route: Route }) {
  const { session } = useSession();
  const hq = session.role === "hq";
  const [tab, setTab] = useState<Tab>("work_orders");
  const [date, setDate] = useState<string | undefined>(
    ISO_DATE.test(route.query.date ?? "") ? route.query.date : undefined);
  const [win, setWin] = useState<GridView>(oneOf(route.query.view, WINDOWS) ?? "day");
  const [group, setGroup] = useState<View["group"]>(oneOf(route.query.group, GROUPS) ?? "work_order");
  const [branch, setBranch] = useState(hq ? route.query.branch?.trim() ?? "" : "");
  const [branchText, setBranchText] = useState(branch);
  const [activity, setActivity] = useState<string>(oneOfKeys(route.query.activity, ACTIVITY_KEYS));
  const [status, setStatus] = useState<string>(oneOfKeys(route.query.status, STATUS_KEYS));
  const [page, setPage] = useState(1);
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null);
  const seq = useRef(0);

  // Every choice is a request: the server windows, groups, filters and pages (D-17). The Activities tab reads the
  // work-order grouping and lists its leaves flat.
  const load = useCallback(() => {
    const mine = ++seq.current;
    const version = session.version;
    getSchedule({
      date, view: win, group: tab === "activities" ? "work_order" : group, branch: hq ? branch : undefined,
      activity: activity || undefined, status: status || undefined, page,
    }).then(
      (v) => { if (mine === seq.current) { setView(v); setError(null); setLoadedVersion(version); } },
      (e: ApiError) => { if (mine === seq.current) setError(e); },
    );
  }, [date, win, group, tab, hq, branch, activity, status, page, session.version]);
  useEffect(() => load(), [load]);
  useRefetchOnVisible(load);
  // The address follows the choices (QA F2): replaceState fires no hashchange, so the page is not routed again, and
  // Back leaves the schedule in one step and returns to it as it was. Defaults and empty values are left out.
  useEffect(() => {
    const q = new URLSearchParams();
    if (date !== undefined) q.set("date", date);
    if (win !== "day") q.set("view", win);
    if (group !== "work_order") q.set("group", group);
    if (activity !== "") q.set("activity", activity);
    if (status !== "") q.set("status", status);
    if (hq && branch !== "") q.set("branch", branch);
    const s = q.toString();
    const to = `#${route.path}${s === "" ? "" : `?${s}`}`;
    if (window.location.hash !== to) window.history.replaceState(window.history.state, "", to);
  }, [date, win, group, activity, status, hq, branch, route]);

  const pick = (f: () => void) => { f(); setPage(1); setSelected(null); };
  const toggle = (id: string) => setCollapsed((c) => {
    const next = new Set(c);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const submitBranch = (e?: FormEvent) => {
    e?.preventDefault();
    const code = branchText.trim();
    if (code !== branch) pick(() => setBranch(code));
  };

  const anchor = view?.date ?? date ?? view?.scenario_date ?? session.data_as_of ?? undefined;
  const scenario = view?.scenario_date ?? session.data_as_of ?? undefined;
  const toolbar = (
    <div className="flex flex-wrap items-center gap-2 border-b border-line p-4">
      <div role="group" aria-label={DATE_LABEL} className="inline-flex items-center gap-0.5 rounded-full bg-sunken p-1">
        <StepButton icon="chevl" label={`${PREVIOUS} ${STEP_WORD[win]}`} disabled={anchor === undefined}
          onClick={() => anchor !== undefined && pick(() => setDate(stepDate(anchor, win, -1)))} />
        <span data-testid="schedule-range" className="num px-1.5 text-body font-semibold whitespace-nowrap text-tx">
          {view !== null ? rangeLabel(win, view.window) : ""}
        </span>
        <StepButton icon="chevr" label={`${NEXT} ${STEP_WORD[win]}`} disabled={anchor === undefined}
          onClick={() => anchor !== undefined && pick(() => setDate(stepDate(anchor, win, 1)))} />
      </div>
      <button type="button" disabled={scenario === undefined} aria-pressed={view !== null && view.date === scenario}
        onClick={() => scenario !== undefined && pick(() => setDate(scenario))}
        className="h-6 rounded-full bg-navy-800 px-2.5 text-label font-semibold text-white hover:bg-navy-950 max-md:h-11">
        {SCENARIO_DATE_CHIP}
      </button>
      {tab === "work_orders" && (
        <>
          <span className="hidden items-center gap-1.5 text-label text-tx2 md:inline-flex">
            {GROUP_BY}
            <Segmented label={GROUP_BY} value={group} options={GROUPS} onPick={(g) => pick(() => setGroup(g))} />
          </span>
          <LabelledSelect className="md:hidden" label={GROUP_BY} value={group} options={GROUPS}
            onPick={(g) => pick(() => setGroup(g as View["group"]))} />
        </>
      )}
      {hq
        ? (
          <form onSubmit={submitBranch} className="flex items-center">
            <label className="flex items-center gap-1.5 text-label text-tx2">
              {BRANCH_FILTER}
              <input type="search" value={branchText} placeholder={STORE_PLACEHOLDER} autoComplete="off"
                onChange={(e) => setBranchText(e.target.value)} onBlur={() => submitBranch()}
                className="h-8 w-36 rounded-md border border-line-2 bg-white px-2 text-body text-tx placeholder:text-tx3 max-md:h-11" />
            </label>
          </form>
        )
        : <LabelledSelect label={BRANCH_FILTER} value={session.branch ?? ""} options={[[session.branch ?? "", session.branch ?? ""]]}
          disabled onPick={() => undefined} />}
      <LabelledSelect label={ACTIVITY_FILTER} value={activity}
        options={[["", ALL_ACTIVITIES], ...ACTIVITY_KEYS.map((k) => [k, kindOf(k).label] as [string, string])]}
        onPick={(a) => pick(() => setActivity(a))} />
      <LabelledSelect label={STATUS_FILTER} value={status}
        options={[["", ALL_STATUSES], ...STATUS_KEYS.map((k) => [k, statusLook(k).text] as [string, string])]}
        onPick={(s) => pick(() => setStatus(s))} />
      <span className="flex-1" />
      <Segmented label={WINDOW_LABEL} value={win} options={WINDOWS} onPick={(w) => pick(() => setWin(w))} />
    </div>
  );

  let body: ReactNode;
  if (view === null) {
    body = error === null ? <Loading text={LOADING} /> : <LoadError title={DIDNT_LOAD} error={error} onRetry={load} />;
  } else {
    const filtered = activity !== "" || status !== "";
    body = (
      <>
        {error !== null && <StaleBanner kind="refresh_failed" onAct={load} />}
        {error === null && loadedVersion !== null && loadedVersion !== session.version && (
          <StaleBanner kind="version_changed" onAct={load} />
        )}
        <section aria-label={TITLE} className="min-w-0 overflow-hidden rounded-2xl border border-line bg-white shadow-card">
          {toolbar}
          {view.rows.length === 0
            ? (
              <div data-testid="schedule-empty" className="flex flex-col items-center gap-1.5 px-6 py-14 text-center">
                <span className="text-tx3"><Icon name="today" size={28} /></span>
                <h2 className="m-0 text-title font-semibold text-tx">
                  {filtered ? NO_MATCH : nothingPlanned(rangeLabel(win, view.window))}
                </h2>
                {filtered
                  ? <Button kind="plain" onClick={() => pick(() => { setActivity(""); setStatus(""); })}>{CLEAR_FILTERS}</Button>
                  : <p className="m-0 text-body text-tx2">{NOTHING_PLANNED_BODY}</p>}
              </div>
            )
            : (
              <ScheduleGrid view={view} flat={tab === "activities"} collapsed={collapsed} onToggle={toggle}
                selected={selected} onSelect={setSelected} />
            )}
          <Legend />
          <Pager page={view.page} pageSize={view.page_size} total={view.total}
            onPage={(p) => { setPage(p); setSelected(null); }} />
        </section>
      </>
    );
  }
  const chosen = view?.rows.find((r) => r.id === selected) ?? null;

  return (
    <>
      <PageHead title={TITLE} />
      <nav aria-label={TABS_LABEL} className="flex gap-6 border-b border-line bg-white px-4 md:px-6">
        {([["work_orders", TAB_WORK_ORDERS], ["activities", TAB_ACTIVITIES]] as [Tab, string][]).map(([t, label]) => (
          <button key={t} type="button" aria-current={tab === t ? "page" : undefined} onClick={() => pick(() => setTab(t))}
            className={`-mb-px h-10 border-b-2 text-body font-semibold max-md:h-11 ${tab === t ? "border-navy-800 text-tx" : "border-transparent text-tx2 hover:text-tx"}`}>
            {label}
          </button>
        ))}
      </nav>
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        <p data-testid="planned-note" className="m-0 flex items-center gap-1 text-label text-tx2">
          <Icon name="clock" size={12} />{PLANNED_NOTE}
        </p>
        {body}
      </div>
      {chosen !== null && view !== null && (
        <ScheduleDrawer row={chosen} view={view} onSelect={setSelected} onClose={() => setSelected(null)} />
      )}
    </>
  );
}

// --- ScheduleGrid: frozen left columns, the time grid, pills and diamonds -------------------------------------------

// The rows to draw, in the server's order: the Work orders tab nests children under their parents (a collapsed
// parent hides its own); the Activities tab lists the same leaf rows flat. Nothing is sorted or filtered here.
function visibleRows(rows: Row[], flat: boolean, collapsed: ReadonlySet<string>): Row[] {
  if (flat) return rows.filter((r) => !CONTAINERS.has(r.kind));
  const hidden = new Set<string>();
  return rows.filter((r) => {
    if (r.parent_id !== null && (hidden.has(r.parent_id) || collapsed.has(r.parent_id))) {
      hidden.add(r.id);
      return false;
    }
    return true;
  });
}

const LEFT = "w-40 md:w-[592px]"; // 160px below 768px; 280 + 88 + 88 + 136 at 768px and wider

// One drawn line of the grid: a row, or the placeholder under a scheduled truck run that carries no transfer.
type Line = { kind: "row"; row: Row } | { kind: "none"; parent: Row };

function linesOf(all: Row[], rows: Row[], flat: boolean, collapsed: ReadonlySet<string>): Line[] {
  return rows.flatMap((r): Line[] => {
    const empty = !flat && r.kind === "run" && !collapsed.has(r.id) && !all.some((c) => c.parent_id === r.id);
    return empty ? [{ kind: "row", row: r }, { kind: "none", parent: r }] : [{ kind: "row", row: r }];
  });
}

// The connector's colours (UI-SPEC "Planned schedule": 1.3px #98a0c2; the selected row's 1.8px navy-800).
const LINK = "#98a0c2";
const LINK_ON = "var(--color-navy-800)";

function ScheduleGrid({ view, flat, collapsed, onToggle, selected, onSelect }: {
  view: View; flat: boolean; collapsed: ReadonlySet<string>; onToggle: (id: string) => void; selected: string | null;
  onSelect: (id: string) => void;
}) {
  const v = view.view as GridView;
  const win = view.window;
  const cols = columnsFor(v, win);
  const days = win.to_day - win.from_day + 1;
  // Day view leaves 12px before 06:00 and after the last hour, so a mark on the grid's edge is drawn whole.
  const pad = v === "day" ? DAY_PAD : 0;
  const gw = cols.reduce((s, c) => s + c.width, 0) + 2 * pad;
  const cw = cols[0]?.width ?? 0;
  const lines = linesOf(view.rows, visibleRows(view.rows, flat, collapsed), flat, collapsed);
  const levelOf = (r: Row) => (flat ? 1 : depth(view.rows, r));
  const grid = {
    backgroundImage: `repeating-linear-gradient(90deg, var(--color-line) 0 1px, transparent 1px ${cw}px)`,
    backgroundPosition: `${pad}px 0`,
  };

  // Dependency connectors (Day only): from the end of each predecessor's mark to the start of the row's own, both on
  // screen; the rows' fixed 44px height gives each mark's centre line.
  const placed = new Map<string, { i: number; g: Geometry }>();
  lines.forEach((l, i) => {
    if (l.kind !== "row" || CONTAINERS.has(l.row.kind)) return;
    const g = barGeometry(l.row, v, win.from_day, days);
    if (g !== null) placed.set(l.row.id, { i, g });
  });
  const mid = (i: number) => i * ROW_H + ROW_H / 2;
  const links = lines.flatMap((l) => (l.kind !== "row" ? [] : l.row.depends_on.flatMap((dep) => {
    const a = placed.get(dep);
    const b = placed.get(l.row.id);
    if (a === undefined || b === undefined) return [];
    const from = { x: a.g.shape === "bar" ? a.g.left + a.g.width : a.g.left + 6, y: mid(a.i) };
    const to = { x: b.g.shape === "bar" ? b.g.left : b.g.left - 6, y: mid(b.i) };
    const c = connectorPath(from, to, v);
    return c === null ? [] : [{ key: `${dep}>${l.row.id}`, c, on: selected === dep || selected === l.row.id }];
  })));

  // A new window, grouping or page opens with its planned work in view: Week and Month at the scenario date's column,
  // Day at the earliest mark. Only the box's scroll position moves; the rows keep the server's order.
  const box = useRef<HTMLDivElement>(null);
  const firstMark = Math.min(...[...placed.values()].map((p) => p.g.left));
  const scenarioCol = cols.findIndex((c) => c.date !== null && c.date === view.scenario_date);
  const target = v === "day" ? (Number.isFinite(firstMark) ? firstMark + pad - 24 : 0) : Math.max(scenarioCol, 0) * cw - cw;
  useLayoutEffect(() => {
    if (box.current !== null) box.current.scrollLeft = Math.max(target, 0);
    // Only when another window, grouping or page arrives, never on a refetch of the same one.
  }, [view.date, view.view, view.group, view.page]);

  return (
    <div ref={box} data-testid="schedule-scroll" className="min-w-0 overflow-x-auto">
      <div role="treegrid" aria-label={TITLE} aria-readonly="true" className="relative min-w-max">
        <div role="row" className="flex border-b border-line" style={{ height: HEAD_H }}>
          <div className={`sticky left-0 z-[4] flex shrink-0 border-r border-line bg-white ${LEFT}`}>
            <HeadCell className="w-40 md:w-[280px]">{flat ? COL_ACTIVITY : COL_NAME}</HeadCell>
            <HeadCell className="hidden w-[88px] md:flex">{COL_START}</HeadCell>
            <HeadCell className="hidden w-[88px] md:flex">{COL_END}</HeadCell>
            <HeadCell className="hidden w-[136px] md:flex">{COL_STATUS}</HeadCell>
          </div>
          <div className="relative flex shrink-0" style={{ width: gw, paddingLeft: pad }}>
            {cols.map((c) => {
              const scenario = c.date !== null && c.date === view.scenario_date;
              return (
                <div key={c.key} role="columnheader" style={{ width: c.width }} aria-current={scenario ? "date" : undefined}
                  className={`flex shrink-0 flex-col justify-center border-l border-line text-label leading-tight whitespace-nowrap ${v === "day" ? "items-start pl-1" : "items-center"} ${scenario ? "bg-navy-50 font-semibold text-navy-800 shadow-[inset_0_-2px_0_var(--color-navy-800)]" : "text-tx2"}`}>
                  {v === "month" && <span>{c.sub}</span>}
                  <span>{c.label}</span>
                </div>
              );
            })}
          </div>
        </div>
        {lines.map((l) => (l.kind === "row"
          ? (
            <GridRow key={l.row.id} row={l.row} level={levelOf(l.row)} flat={flat} v={v} win={win} days={days} gw={gw}
              grid={grid} expanded={!collapsed.has(l.row.id)} onToggle={onToggle} scope={view.branch ?? null}
              selected={selected === l.row.id} onSelect={onSelect} pad={pad} />
          )
          : (
            <div key={`${l.parent.id}:none`} role="row" aria-level={2} data-testid="schedule-none"
              data-parent-id={l.parent.id} className="flex border-b border-line" style={{ height: ROW_H }}>
              <div role="gridcell"
                className={`sticky left-0 z-[4] flex shrink-0 items-center border-r border-line bg-white px-3 text-label text-tx3 italic md:pl-8 ${LEFT}`}>
                {NO_TRANSFERS}
              </div>
              <div className="shrink-0" style={{ width: gw, ...grid }} />
            </div>
          )))}
        {links.length > 0 && (
          <svg aria-hidden="true" className="pointer-events-none absolute left-40 z-[1] overflow-visible md:left-[592px]"
            style={{ top: HEAD_H, width: gw, height: lines.length * ROW_H }}>
            {links.map(({ key, c, on }) => (
              <g key={key} data-testid="schedule-connector" data-on={on ? "true" : undefined}
                transform={`translate(${pad} 0)`}>
                <path d={c.line} fill="none" stroke={on ? LINK_ON : LINK} strokeWidth={on ? 1.8 : 1.3} />
                <path d={c.head} fill={on ? LINK_ON : LINK} />
              </g>
            ))}
          </svg>
        )}
      </div>
    </div>
  );
}

function depth(all: Row[], r: Row): number {
  let n = 1;
  let p = r.parent_id;
  while (p !== null) {
    n += 1;
    p = all.find((x) => x.id === p)?.parent_id ?? null;
  }
  return n;
}

function HeadCell({ className, children }: { className: string; children: ReactNode }) {
  return (
    <div role="columnheader" className={`flex shrink-0 items-center px-3 text-label font-semibold text-tx2 ${className}`}>
      {children}
    </div>
  );
}

// The accessible name of a row: the kind first (the grid's colour and icon mark only the kind, D-29), then the
// server's title, the planned time and the status.
function rowName(r: Row, win: GridWindow): string {
  const kind = kindOf(r.kind).label;
  const title = r.title.startsWith(kind) ? r.title : `${kind}: ${r.title}`;
  const when = plannedText(r, win);
  return [title, when === null ? UNSCHEDULED : `${when} ${PLANNED_WORD}`].join(", ");
}

// The status chip: icon and text from the recorded state (R-6: never colour alone).
function StatusChip({ status, testId }: { status: string; testId?: string }) {
  const look = statusLook(status);
  return <span data-testid={testId} className="inline-flex"><Pill tone={look.tone} icon={look.icon}>{look.text}</Pill></span>;
}

// One row: a click anywhere on it (or Enter on its name button) selects it and opens the read-only drawer; the
// whole row turns navy-50 and its mark navy-800 (UI-SPEC "Selection").
function GridRow({ row: r, level, flat, v, win, days, gw, grid, expanded, onToggle, scope, selected, onSelect, pad }: {
  row: Row; level: number; flat: boolean; v: GridView; win: GridWindow; days: number; gw: number;
  grid: { backgroundImage: string; backgroundPosition: string }; expanded: boolean; onToggle: (id: string) => void;
  scope: string | null; selected: boolean; onSelect: (id: string) => void; pad: number;
}) {
  const container = CONTAINERS.has(r.kind);
  const look = kindOf(r.kind);
  const geo = container ? null : barGeometry(r, v, win.from_day, days);
  // A run from another store that brings a transfer here names its origin plainly.
  const incoming = r.kind === "run" && scope !== null && r.branch !== null && r.branch !== scope;
  const indent = flat ? "" : level === 1 ? "" : level === 2 ? "md:pl-8" : "md:pl-14";
  return (
    <div role="row" data-testid="schedule-row" data-row-id={r.id} data-kind={r.kind} aria-level={level}
      aria-expanded={container && !flat ? expanded : undefined} aria-selected={selected} onClick={() => onSelect(r.id)}
      className="group flex cursor-pointer border-b border-line" style={{ height: ROW_H }}>
      <div data-testid="schedule-left"
        className={`sticky left-0 z-[4] flex shrink-0 border-r border-line ${selected ? "bg-navy-50" : "bg-white group-hover:bg-side"} ${LEFT}`}>
        <div role="gridcell" className={`flex w-40 min-w-0 shrink-0 items-center gap-2 px-3 md:w-[280px] ${indent}`}>
          {container && !flat && (
            <button type="button" aria-expanded={expanded} aria-label={`${expanded ? COLLAPSE : EXPAND} ${r.title}`}
              onClick={(e) => { e.stopPropagation(); onToggle(r.id); }}
              className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-tx2 hover:bg-sunken">
              <span className={expanded ? "" : "-rotate-90"}><Icon name="chev" size={14} /></span>
            </button>
          )}
          {!container && <span className="text-tx2"><Icon name={look.icon} size={14} /></span>}
          <button type="button" aria-label={rowName(r, win)} className="flex min-w-0 flex-1 flex-col text-left">
            <span className={`truncate text-body ${container ? "font-semibold text-tx" : "text-tx2"}`}>{r.title}</span>
            {r.sub !== null && (
              <span className="hidden truncate text-label text-tx2 md:block">
                {incoming ? incomingRun(r.branch!) : r.sub}
              </span>
            )}
            {r.status !== null && <span className="md:hidden"><StatusChip status={r.status} /></span>}
          </button>
        </div>
        {r.start === null
          ? (
            <div role="gridcell" data-testid="schedule-when"
              className="hidden w-[176px] shrink-0 items-center px-3 text-label text-tx2 italic md:flex">
              {UNSCHEDULED}
            </div>
          )
          : (
            <>
              <div role="gridcell" data-testid="schedule-when"
                className={`hidden w-[88px] shrink-0 items-center px-2 text-body md:flex ${container ? "text-tx" : "text-tx2"}`}>
                <TimeCell t={r.start} v={v} win={win} />
              </div>
              <div role="gridcell" className="hidden w-[88px] shrink-0 items-center px-2 text-body text-tx2 md:flex">
                {sameTime(r.start, r.end) || r.end === null ? NO_END : <TimeCell t={r.end} v={v} win={win} />}
              </div>
            </>
          )}
        <div role="gridcell" className="hidden w-[136px] shrink-0 items-center px-3 md:flex">
          {r.status !== null && <StatusChip status={r.status} testId="schedule-status" />}
        </div>
      </div>
      <div role="gridcell" className={`relative shrink-0 ${selected ? "bg-navy-50" : ""}`} style={{ width: gw, ...grid }}>
        {geo !== null && <Mark row={r} geo={{ ...geo, left: geo.left + pad }} v={v} look={look} selected={selected} />}
      </div>
    </div>
  );
}

// A pill (work with a start and an end) or a diamond (a milestone), in the kind's colour, with its label inside the
// pill when it fits and beside it otherwise.
function Mark({ row: r, geo, v, look, selected }: {
  row: Row; geo: Geometry; v: GridView; look: KindLook; selected: boolean;
}) {
  const fill = selected ? "bg-navy-800" : look.fill;
  const s = r.start!;
  if (geo.shape === "diamond") {
    const word = MILESTONE_WORD[r.kind];
    const label = v === "month" ? null
      : s.min === null ? (v === "week" ? AT_OPENING : null)
      : v === "day" ? `${word ?? r.title} ${hhmm(s.min)}` : hhmm(s.min);
    return (
      <span data-testid="schedule-bar" data-shape="diamond" aria-hidden="true"
        className="absolute top-1/2 z-[2] flex -translate-y-1/2 items-center gap-1.5 whitespace-nowrap"
        style={{ left: geo.left - 6 }}>
        <i className={`block h-3 w-3 shrink-0 rotate-45 rounded-[2px] ring-1 ring-navy-950 ${fill}`} />
        {label !== null && <span className="rounded bg-white/90 px-1 text-label font-semibold text-tx">{label}</span>}
      </span>
    );
  }
  const e = r.end!;
  const label = v === "day" ? r.title : v === "week" && s.min !== null && e.min !== null ? `${hhmm(s.min)}–${hhmm(e.min)}` : "";
  const fits = label.length * 7 + 28 <= geo.width;
  return (
    <span data-testid="schedule-bar" data-shape="bar" aria-hidden="true"
      className={`absolute top-1/2 z-[2] flex h-6 -translate-y-1/2 items-center text-label font-semibold whitespace-nowrap text-white ${selected ? "shadow-[0_0_0_2px_#fff,0_0_0_3.5px_var(--color-navy-800)]" : "shadow-card"} ${fill} ${geo.clipStart ? "rounded-l-none" : "rounded-l-full"} ${geo.clipEnd ? "rounded-r-none" : "rounded-r-full"}`}
      style={{ left: geo.left, width: Math.max(geo.width, 6) }}>
      <span className="absolute top-1.5 bottom-1.5 left-1.5 w-0.5 rounded bg-white/60" />
      {label !== "" && (fits
        ? <span className="truncate pr-2.5 pl-3.5">{label}</span>
        : <span className="absolute left-[calc(100%+6px)] text-tx">{label}</span>)}
    </span>
  );
}

// --- the read-only drawer (UI-SPEC "Drawer") -------------------------------------------------------------------------

const ownerLabel = (o: Row["owner"]) => (o === null ? NONE : o.label);
const TRANSFER_KINDS = new Set(["pick_and_pack", "loading_cutoff", "transport", "receive"]);

// Everything the row says, nothing to edit: owner, branch, quantity, planned time, status, dependencies, the
// scheduled truck run and the linked stock decision; a work order or run lists its activities (each selects its row);
// a transfer activity links to Branch transfers and a stock check to Tasks.
function ScheduleDrawer({ row: r, view, onSelect, onClose }: {
  row: Row; view: View; onSelect: (id: string) => void; onClose: () => void;
}) {
  const look = kindOf(r.kind);
  const byId = new Map(view.rows.map((x) => [x.id, x]));
  const kids = view.rows.filter((x) => x.parent_id === r.id);
  const when = plannedText(r, view.window);
  const value = (key: string, v: ReactNode) => <span data-testid={`kv-${key}`}>{v}</span>;
  const deps = r.depends_on.map((id) => byId.get(id)?.title ?? id);
  return (
    <Drawer title={r.title} onClose={onClose}>
      <p className="m-0 flex items-center gap-1.5 text-label text-tx2"><Icon name={look.icon} size={14} />{look.label}</p>
      {r.sub !== null && <p className="m-0 text-label text-tx2">{r.sub}</p>}
      <Kv items={[
        [DRAWER_KEYS.owner, value(DRAWER_KEYS.owner, ownerLabel(r.owner))],
        [DRAWER_KEYS.branch, value(DRAWER_KEYS.branch, r.branch ?? NONE)],
        [DRAWER_KEYS.qty, value(DRAWER_KEYS.qty, r.qty === null ? NONE : unitsText(r.qty))],
        [DRAWER_KEYS.planned, value(DRAWER_KEYS.planned, when ?? UNSCHEDULED_LONG)],
        [DRAWER_KEYS.status, value(DRAWER_KEYS.status, r.status === null ? NONE : <StatusChip status={r.status} />)],
        [DRAWER_KEYS.depends, value(DRAWER_KEYS.depends, deps.length === 0 ? NONE : deps.join("; "))],
        [DRAWER_KEYS.run, value(DRAWER_KEYS.run, r.run_code ?? NONE)],
        [DRAWER_KEYS.decision, value(DRAWER_KEYS.decision, r.case_key === null ? NONE : (
          <a href={`#/cases/${encodeURIComponent(r.case_key)}`} className="font-semibold text-navy-800 underline underline-offset-2">
            {TERMS.VIEW_STOCK_DECISION}
          </a>
        ))],
      ]} />
      {CONTAINERS.has(r.kind) && (
        <section className="flex flex-col gap-1.5 border-t border-line pt-3">
          <h3 className="m-0 text-label font-semibold text-tx2">{ACTIVITIES_TITLE}</h3>
          {kids.length === 0
            ? <p className="m-0 text-label text-tx3 italic">{r.kind === "run" ? NO_TRANSFERS : NONE}</p>
            : (
              <ul data-testid="schedule-activities" className="m-0 flex list-none flex-col gap-1 p-0">
                {kids.map((k) => (
                  <li key={k.id}>
                    <button type="button" onClick={() => onSelect(k.id)}
                      className="flex w-full min-w-0 items-center gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-side max-md:min-h-11">
                      <span className="text-tx2"><Icon name={kindOf(k.kind).icon} size={14} /></span>
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-body text-tx">{k.title}</span>
                        <span className="text-label text-tx2">{plannedText(k, view.window) ?? UNSCHEDULED}</span>
                      </span>
                      {k.status !== null && <StatusChip status={k.status} />}
                    </button>
                  </li>
                ))}
              </ul>
            )}
        </section>
      )}
      {(TRANSFER_KINDS.has(r.kind) || r.kind === "stock_check") && (
        <a href={r.kind === "stock_check" ? "#/tasks" : "#/transfers"}
          className="text-body font-semibold text-navy-800 underline underline-offset-2">
          {r.kind === "stock_check" ? OPEN_TASKS : OPEN_TRANSFERS}
        </a>
      )}
      <p className="m-0 flex items-center gap-1 text-label text-tx2"><Icon name="clock" size={12} />{PLANNED_NOTE}</p>
    </Drawer>
  );
}

export const page: PageDef = {
  path: "/schedule",
  title: TITLE,
  nav: { label: NAV_LABEL, icon: "gantt", section: "", order: 3 },
  Component: SchedulePage,
};
