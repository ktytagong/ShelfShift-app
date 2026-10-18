// Today (06.1-UI-SPEC "Today"; sketch 002 B; 06.1-CONTEXT D-11, D-18, D-22, D-33): the page a signed-in person lands
// on ("#/"). It answers what is happening (the planned day strip), what needs attention (the four summary tiles and
// their first deadline) and what to do next (the first action card's button), from GET /api/today only: planned times
// from the data and recorded states. There is no current-time line, countdown, overdue or "missed" state (D-18): a
// time is a planned time, and an item without one says "Unscheduled".
// The server scopes (a manager's own store, T-06.1-63: no branch is sent), counts, orders and pages; the client never
// re-sorts. A pressed tile only keeps the cards whose server `tiles` name it, in the server's order (UI-SPEC item 1).
// A card or a strip marker opens the drawer; its button goes to the stock decision, which opens at the server's
// current step, where the next action is done. Strings marked (proposed) are Claude's wording, one constant each.
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { ApiError } from "../api.ts";
import { getToday } from "../contract.ts";
import { AMBIENT, COLD_CHAIN, runTitle } from "../decision/transport.tsx";
import { evaluatedLine } from "../boardview.ts";
import { NEEDS_EVALUATION } from "../copy.ts";
import { dateLabel, hhmm, markDate, shortDate, signed, units } from "../format.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { PageHead, useSession } from "../shell.tsx";
import { stepName } from "../steps.ts";
import {
  CLEAR_FILTER, STRIP_FROM, STRIP_TO, TILES, TILE_LABEL, actionHref, actsOn, byTile, caseHref, causeText, isNetwork,
  laneElements, laneLabel, nextLabel, noTileBody, noTileHeading, ownerDeadlineLine, plannedAt, showingTile,
  tileNote, whatLine, whereLine, whoName, type AnyToday, type BranchRow, type Lane, type Moving, type NetworkToday,
  type StoreToday, type StripElement, type TileKey, type TodayCard, type TodayCounts, type TodayException,
} from "../todayview.ts";
import {
  Drawer, Icon, KindChip, Kv, LinkButton, LoadError, Loading, Pager, Pill, StaleBanner, type IconName, type Tone,
} from "../ui.tsx";
import {
  ALL_BRANCHES, CAPACITY_HOLD_EXPIRED, DECISION_CHIP, DISPATCH_BLOCKED, MISSING_DATA, MOVEMENT_CHIP, NEEDS_ACTION,
  NOT_TAKEN, NOT_YET_RECORDED, NO_TRANSFERS, PLANNED, PLANNED_NOTE, RECEIPT_DISCREPANCY_BADGE, RECEIVED,
  STOCK_CHECK_REQUESTED, TERMS, UNSCHEDULED, type ChipLook,
} from "../vocab.ts";

// --- copy (proposed unless noted) -------------------------------------------------------------------------------------
export const TITLE = "Today";
export const LOADING = "Loading today's work…"; // (proposed, UI-SPEC "Interaction states")
export const DIDNT_LOAD = "Today's work didn't load"; // (proposed, UI-SPEC "Error (page load)")
export const TILES_LABEL = "Needs attention";
export const ACTIONS_LABEL = "Action cards"; // (proposed) the card list's accessible name
export const PLANNED_WORD = "planned";
export const ANSWERED = "Answered"; // (proposed) a stock check answered and waiting to be confirmed
export const DRAWER_LABELS = { what: "What", where: "Where", owner: "Owner", deadline: "Deadline", status: "Status",
  next: "Next action" } as const;
export const NO_DEADLINE_SET = "No planned deadline"; // (proposed) the drawer's Deadline for an Unscheduled card
export const PLAN_SOURCE = "plan: engine recommendation"; // (proposed, UI-SPEC drawer source line)
export const deadlineSource = (run: string) => `Deadline: run ${run} timetable minus packing time`; // (proposed)
export const DEADLINE_MARKDOWN = "Deadline: the markdown schedule"; // (proposed)
export const DEADLINE_CHECK = "Deadline: the stock check's due time"; // (proposed)
export const deadlineDrawer = (at: string, date: string) => `${at} (planned, ${date})`;
export const stripTitle = (store: string, iso: string) => `${store} · ${markDate(iso)}`;
export const STRIP_SUB = "06:00–18:00 · planned times";
export const LEGEND = {
  deadline: "Decision deadline", cutoff: "Loading cutoff", work: "Scheduled work", conditional: "Conditional work (if sent)",
  delivery: "Delivery",
} as const;
export const INFO_LABELS = { // (proposed) the drawer's rows for a strip marker no card stands for
  planned: "Planned", units: "Units", arrival: "Planned arrival", status: "Status", cutoff: "Loading cutoff",
  departs: "Departs", arrives: "Arrives", totes: "Spare totes", zone: "Transport",
} as const;
export const SCHEDULED = "Scheduled"; // (proposed) a supplier delivery the day start has not recorded yet
export const deliveryTitle = (sku: string) => `Delivery · ${sku}`;
export const atPlanned = (at: string) => `${at} (planned)`;
// Empty and HQ copy (UI-SPEC "Copywriting Contract" Empty: Today; "Today" HQ variant)
export const nothingDue = (date: string) => `Nothing needs a decision on ${date}`; // (proposed, from sketch 002)
export const NOTHING_DUE_BODY = "The strip shows the planned runs and deliveries only."; // (proposed, from sketch 002)
export const HQ_EMPTY =
  "Nothing waits on HQ. Store managers own their stock decisions; HQ can take ownership from the Decision board."; // (proposed)
export const BRANCHES_TITLE = "Branches needing attention";
export const BRANCH_COLUMN = "Branch";
export const boardForBranch = (b: string) => `#/board?branch=${encodeURIComponent(b)}`;
// Movements (UI-SPEC "Today" item 3)
export const MOVEMENTS_TITLE = "Movements";
export const outgoingLine = (n: number) => `Outgoing · ${n} scheduled truck run${n === 1 ? "" : "s"}`;
export const INCOMING = "Incoming";
export const noRunsTo = (store: string) => `No scheduled truck runs to ${store}.`;
export const plannedRunLine = (cutoff: string, departs: string, arrives: string, totes: number | null) =>
  `Planned: cutoff ${cutoff} · departs ${departs} · arrives ${arrives}${totes === null ? "" : ` · ${units(totes)} spare totes`}`;
export const decisionChipText = (t: string) => `Decision: ${t}`;
export const movementChipText = (t: string) => `Movement: ${t}`;
export const deliveryLine = (sku: string, qty: number, at: string) =>
  `Delivery · ${sku} · ${units(qty)} units · arrives ${at}`; // (proposed)
export const fromLine = (source: string, run: string) => `from ${source} on ${run}`; // (proposed)
// Outcomes and Coming up
export const OUTCOMES_TITLE = "Outcomes";
export const OUTCOMES_SUB = "expected vs actual";
export const EXPECTED = "Expected";
export const ACTUAL = "Actual";
export const EXPECTED_NOTE = "Engine estimate vs keeping it · simulated";
export const actualAppears = (date: string) => `Appears when sales after ${date} are recorded.`; // (proposed)
export const NO_OUTCOMES = "No stock decision is being verified."; // (proposed)
export const COMING_UP = "Coming up";
export const markdownStepLine = (lot: string, pct: number, date: string) =>
  `${lot} goes to ${pct}% off at opening${date === "" ? "" : ` · ${date}`}`;
export const NOTHING_COMING = "No markdown steps planned."; // (proposed)
// Exceptions
export const EXCEPTIONS_TITLE = "Exceptions";
export const NO_EXCEPTIONS = "No exceptions need you.";
export const UNVERIFIED = "Unverified"; // (proposed) the chip of a stock fact not verified before moving
export const UNVERIFIED_LINE = "Stock facts not verified before moving"; // (proposed)
export const openUnits = (open: number, total: number) => `${units(open)} of ${units(total)} units open`; // (proposed)
export const exceptionMeta = (owner: string, next: string) => `Owner: ${owner} · Next: ${next}`;

// --- the load (the requests.tsx latest-ref pattern) -------------------------------------------------------------------

type Load = { view: AnyToday | null; error: ApiError | null; reload: () => void };

// GET /api/today on mount, on a new workspace version (another write in this workspace), on Try again and when the
// tab becomes visible again. A refetch keeps the old content: a failure then shows the stale banner over it.
function useToday(page: number): Load {
  const { session } = useSession();
  const [view, setView] = useState<AnyToday | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const seq = useRef(0);
  const reload = useCallback(() => {
    const mine = ++seq.current;
    // HQ's branch table pages through ?page= (a manager's view has one page).
    getToday(undefined, page).then(
      (v) => { if (mine === seq.current) { setView(v); setError(null); } },
      (e: ApiError) => { if (mine === seq.current) setError(e); },
    );
  }, [page]);
  useEffect(() => reload(), [reload, session.version]);
  useRefetchOnVisible(reload);
  return { view, error, reload };
}

// --- the status chip of a card (one tone, icon and label per state, R-2) ----------------------------------------------

type Look = ChipLook;
const BLOCK_LOOK: Readonly<Record<string, Look>> = {
  dispatch_blocked: { tone: "bad", icon: "lock", text: DISPATCH_BLOCKED },
  hold_expired: { tone: "bad", icon: "lock", text: CAPACITY_HOLD_EXPIRED },
  missing_data: { tone: "bad", icon: "alert", text: MISSING_DATA },
};
function statusLook(c: TodayCard): Look {
  if (c.kind === "stock_check") {
    return c.status.state === "ANSWERED"
      ? { tone: "info", icon: "check", text: ANSWERED } : { tone: "info", icon: "inbox", text: STOCK_CHECK_REQUESTED };
  }
  const block = c.status.blocked_reasons.map((r) => BLOCK_LOOK[r]).find((l) => l !== undefined);
  if (block) return block;
  if (c.status.first_stage === "needs_evaluation") return { tone: "info", icon: "clock", text: NEEDS_EVALUATION };
  if (c.status.first_stage === "needs_action") return { tone: "warn", icon: "alert", text: NEEDS_ACTION };
  const d = DECISION_CHIP[c.status.state as keyof typeof DECISION_CHIP];
  return d ?? { tone: "gray", icon: "circle", text: c.status.step === null ? c.status.state : stepName(c.status.step) };
}
function StatusChip({ card }: { card: TodayCard }) {
  const l = statusLook(card);
  return <span data-testid="today-status"><Pill tone={l.tone} icon={l.icon}>{l.text}</Pill></span>;
}

// --- what the drawer shows ----------------------------------------------------------------------------------------------

// A card, or a strip marker that no card stands for (a delivery, a run, another account's decision): a title, its
// facts and, when it belongs to a stock decision, the link to it.
export type Subject =
  | { kind: "card"; card: TodayCard }
  | { kind: "info"; title: string; ref?: string; items: [string, string][]; caseKey?: string; source?: string };

// --- the page ---------------------------------------------------------------------------------------------------------

function TodayPage() {
  const { session } = useSession();
  const [branchPage, setBranchPage] = useState(1);
  const { view, error, reload } = useToday(branchPage);
  const [tile, setTile] = useState<TileKey | null>(null);
  const [subject, setSubject] = useState<Subject | null>(null);

  let body: ReactNode;
  if (view === null) {
    body = error === null ? <Loading text={LOADING} /> : <LoadError title={DIDNT_LOAD} error={error} onRetry={reload} />;
  } else {
    const network = isNetwork(view);
    const counts: TodayCounts = network ? view.network : view.summary;
    const scenario = { day: view.day, date: view.scenario_date };
    const packing = network ? null : view.packing_min;
    const store = network ? ALL_BRANCHES : view.branch;
    body = (
      <>
        {error !== null && <StaleBanner kind="refresh_failed" onAct={reload} />}
        <Tiles counts={counts} cards={view.cards} scenario={scenario} network={network} pressed={tile}
          onPress={(t) => setTile((cur) => (cur === t ? null : t))} />
        {network && <p data-testid="evaluated-line" className="m-0 -mt-2 text-label text-tx2">
          {evaluatedLine(view.network.evaluated, view.network.at_risk)}
        </p>}
        {network ? <Branches view={view} onPage={setBranchPage} /> : <DayStrip view={view} onOpen={setSubject} />}
        <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <div className="flex min-w-0 flex-col gap-4">
            {!network && view.summary.decisions_due === 0 && (
              <div data-testid="today-empty" className="rounded-2xl border border-line bg-white p-4">
                <h2 className="m-0 text-title font-semibold text-tx">{nothingDue(shortDate(view.scenario_date))}</h2>
                <p className="m-0 mt-1 text-body text-tx2">{NOTHING_DUE_BODY}</p>
              </div>
            )}
            {network && view.cards.length === 0 && tile === null && (
              <p data-testid="today-empty" className="m-0 rounded-2xl border border-line bg-white p-4 text-body text-tx2">
                {HQ_EMPTY}
              </p>
            )}
            <ActionCards cards={view.cards} tile={tile} onClear={() => setTile(null)} scenarioDay={view.day}
              packing={packing} store={store} onOpen={(card) => setSubject({ kind: "card", card })}
              session={session} />
          </div>
          {!network && (
            <div className="flex min-w-0 flex-col gap-4">
              <Movements view={view} />
              <Outcomes view={view} />
              <Exceptions view={view} />
            </div>
          )}
        </div>
      </>
    );
  }

  return (
    <>
      <PageHead title={TITLE} />
      <div className="flex min-w-0 flex-col gap-6 p-4 md:p-6">{body}</div>
      {subject !== null && view !== null && (
        <TodayDrawer subject={subject} onClose={() => setSubject(null)}
          packing={isNetwork(view) ? null : view.packing_min} />
      )}
    </>
  );
}

// --- the four summary tiles (UI-SPEC "Today" item 1) ------------------------------------------------------------------

const TILE_ICON: Readonly<Record<TileKey, IconName>> = {
  decisions_due: "clock", approvals_waiting: "check", stock_checks: "inbox", transfers_at_risk: "swap",
};

function Tiles({ counts, cards, scenario, network, pressed, onPress }: {
  counts: TodayCounts; cards: TodayCard[]; scenario: { day: number; date: string }; network: boolean;
  pressed: TileKey | null; onPress: (t: TileKey) => void;
}) {
  return (
    <div role="group" aria-label={TILES_LABEL} className="grid min-w-0 grid-cols-2 gap-4 lg:grid-cols-4">
      {TILES.map((t) => {
        const n = counts[t];
        const on = pressed === t;
        return (
          <button key={t} type="button" aria-pressed={on} onClick={() => onPress(t)} data-testid={`tile-${t}`}
            className={`flex min-w-0 flex-col items-start gap-1 rounded-2xl border bg-white p-4 text-left shadow-card hover:shadow-float ${on ? "border-navy-800 ring-2 ring-navy-100" : "border-line"}`}>
            <span className="flex items-center gap-1 text-label font-semibold text-tx2">
              <Icon name={TILE_ICON[t]} />{TILE_LABEL[t]}
            </span>
            <span className={`num text-display font-semibold ${n === 0 ? "text-tx3" : "text-tx"}`}>{n}</span>
            <span className="text-label break-words text-tx3">{tileNote(t, counts, cards, scenario, network)}</span>
          </button>
        );
      })}
    </div>
  );
}

// --- the planned day strip (UI-SPEC "Today" item 2; D-18: no now line) --------------------------------------------------

const HOURS = Array.from({ length: (STRIP_TO - STRIP_FROM) / 60 + 1 }, (_, i) => STRIP_FROM + i * 60);

// What a strip element opens: the card that stands for it, else the facts the lane holds.
function subjectFor(e: StripElement, lane: Lane, view: StoreToday): Subject {
  const item = lane.items[Number(e.id.split(":").pop())];
  if (e.requestId !== null) {
    const card = view.cards.find((c) => c.key === `request:${e.requestId}`);
    if (card) return { kind: "card", card };
  } else if (e.caseKey !== null) {
    const card = view.cards.find((c) => c.kind === "stock_decision" && c.case_key === e.caseKey);
    if (card) return { kind: "card", card };
  }
  if (item.kind === "delivery") {
    return { kind: "info", title: deliveryTitle(item.sku), items: [
      [INFO_LABELS.units, units(item.qty)],
      [INFO_LABELS.arrival, atPlanned(hhmm(item.min))],
      [INFO_LABELS.status, item.status === "received" ? RECEIVED : item.status === "scheduled" ? SCHEDULED : PLANNED],
    ] };
  }
  if (item.kind === "loading_cutoff" || item.kind === "travel") {
    const run = view.movements.outgoing.find((r) => r.run_code === lane.run_code && r.dest === lane.dest);
    const rows: [string, string][] = run === undefined ? [[INFO_LABELS.planned, e.label]] : [
      [INFO_LABELS.cutoff, atPlanned(hhmm(run.loading_cutoff.min))],
      [INFO_LABELS.departs, atPlanned(hhmm(run.departure.min))],
      [INFO_LABELS.arrives, atPlanned(hhmm(run.arrival.min))],
      [INFO_LABELS.zone, run.cold_chain ? COLD_CHAIN : AMBIENT],
      ...(run.spare_totes === null ? [] : [[INFO_LABELS.totes, units(run.spare_totes)] as [string, string]]),
    ];
    return { kind: "info", title: runTitle(lane.branch, lane.dest ?? ""), ref: lane.run_code, items: rows };
  }
  return { kind: "info", title: e.caseKey ?? e.label, items: [[INFO_LABELS.planned, e.label]],
    caseKey: e.caseKey ?? undefined };
}

// Kind colours only (UI-SPEC "Kind colours"): colour marks the kind of activity, never its status.
const DIAMOND: Partial<Record<StripElement["kind"], string>> = {
  decision_deadline: "bg-gold-500", loading_cutoff: "bg-gold-600", delivery: "bg-navy-700",
};
function barLook(e: StripElement, lane: Lane): string {
  if (e.kind === "travel") return lane.cold_chain ? "bg-chill text-white" : "bg-kind-ambient text-white";
  return e.dashed ? "border-[1.5px] border-dashed border-plan-line bg-sunken text-tx2" : "bg-navy-800 text-white";
}

function Marker({ e, onOpen }: { e: StripElement; onOpen: () => void }) {
  // The label sits left of the diamond near the strip's end, and always for a loading cutoff, whose run's travel bar
  // starts right after it (sketch 002 B), so labels stay on the track and never cover a bar.
  const flip = e.left > 70 || e.kind === "loading_cutoff";
  const where = flip ? { right: `${100 - e.left}%`, top: `${e.row * 100}%` } : { left: `${e.left}%`, top: `${e.row * 100}%` };
  return (
    <button type="button" aria-label={e.name} onClick={onOpen} style={where}
      className={`absolute z-[1] flex -translate-y-1/2 items-center gap-1 rounded-md ${flip ? "-mr-1.5 flex-row-reverse" : "-ml-1.5"}`}>
      {e.kind === "stock_check_due"
        ? <span aria-hidden="true" className="h-3 w-2 shrink-0 rounded-sm bg-navy-700" />
        : <span aria-hidden="true" className={`h-3 w-3 shrink-0 rotate-45 ring-1 ring-navy-950 ${DIAMOND[e.kind] ?? "bg-navy-700"}`} />}
      <span className="flex items-center gap-0.5 rounded bg-white px-1 text-label whitespace-nowrap text-tx">
        {e.kind === "loading_cutoff" && <Icon name="lock" size={12} />}
        {e.label}
      </span>
    </button>
  );
}

// A bar is drawn to its planned length; its label sits beside it (after it, or before it near the strip's end), so a
// short bar never cuts its words. The whole thing is one button with the full accessible name.
function Bar({ e, lane, onOpen }: { e: StripElement; lane: Lane; onOpen: () => void }) {
  const end = e.left + Math.max(e.width, 1);
  const flip = end > 75;
  const where = flip ? { right: `${100 - end}%` } : { left: `${e.left}%` };
  return (
    <button type="button" aria-label={e.name} onClick={onOpen} style={where}
      className={`absolute top-1/2 z-[1] flex -translate-y-1/2 items-center gap-1 ${flip ? "flex-row-reverse" : ""}`}>
      <span aria-hidden="true" style={{ width: `calc(${Math.max(e.width, 1)} * var(--track) / 100)` }}
        className={`block h-6 shrink-0 rounded-full ${barLook(e, lane)}`} />
      <span className="rounded bg-white px-1 text-label whitespace-nowrap text-tx2">{e.label}</span>
    </button>
  );
}

function DayStrip({ view, onOpen }: { view: StoreToday; onOpen: (s: Subject) => void }) {
  const titleId = useId();
  const pct = (m: number) => `${((m - STRIP_FROM) / (STRIP_TO - STRIP_FROM)) * 100}%`;
  return (
    <section data-testid="day-strip" aria-labelledby={titleId}
      className="flex min-w-0 flex-col gap-2 rounded-2xl border border-line bg-white py-4 shadow-card">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4">
        <h2 id={titleId} className="m-0 text-title font-semibold text-tx">{stripTitle(view.branch, view.scenario_date)}</h2>
        <span className="text-label text-tx3">{STRIP_SUB}</span>
      </div>
      <div data-testid="strip-scroll" className="min-w-0 overflow-x-auto px-4">
        <div className="min-w-[680px]">
          <div aria-hidden="true" className="grid grid-cols-[112px_minmax(0,1fr)]">
            <span />
            <div className="relative h-5">
              {HOURS.filter((_, i) => i % 2 === 0).map((m) => (
                <span key={m} style={{ left: pct(m) }}
                  className={`num absolute text-label text-tx3 ${m === STRIP_FROM ? "" : m === STRIP_TO ? "-translate-x-full" : "-translate-x-1/2"}`}>
                  {hhmm(m)}
                </span>
              ))}
            </div>
          </div>
          {view.lanes.map((lane) => (
            <div key={lane.key} data-testid="lane" className="grid grid-cols-[112px_minmax(0,1fr)] items-center border-t border-line">
              <span data-testid="lane-label" className="pr-2 text-label font-semibold break-words text-tx2">{laneLabel(lane)}</span>
              <div className="relative h-12 [container-type:inline-size] [--track:100cqw]">
                {HOURS.map((m) => (
                  <span key={m} aria-hidden="true" style={{ left: pct(m) }} className="absolute inset-y-0 w-px bg-line" />
                ))}
                {laneElements(lane, view.day).map((e) => {
                  const open = () => onOpen(subjectFor(e, lane, view));
                  return e.shape === "bar"
                    ? <Bar key={e.id} e={e} lane={lane} onOpen={open} />
                    : <Marker key={e.id} e={e} onOpen={open} />;
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
      <p data-testid="strip-legend" className="m-0 flex flex-wrap items-center gap-x-4 gap-y-1 px-4 text-label text-tx3">
        <span className="inline-flex items-center gap-1"><span aria-hidden="true" className="h-2 w-2 rotate-45 bg-gold-500 ring-1 ring-navy-950" />{LEGEND.deadline}</span>
        <span className="inline-flex items-center gap-1"><span aria-hidden="true" className="h-2 w-2 rotate-45 bg-gold-600 ring-1 ring-navy-950" />{LEGEND.cutoff}</span>
        <span className="inline-flex items-center gap-1"><span aria-hidden="true" className="h-2 w-3 rounded-full bg-navy-800" />{LEGEND.work}</span>
        <span className="inline-flex items-center gap-1"><span aria-hidden="true" className="h-2 w-3 rounded-full border border-dashed border-plan-line bg-sunken" />{LEGEND.conditional}</span>
        <span className="inline-flex items-center gap-1"><span aria-hidden="true" className="h-2 w-2 rotate-45 bg-navy-700 ring-1 ring-navy-950" />{LEGEND.delivery}</span>
      </p>
      <div className="px-4"><PlannedNote /></div>
    </section>
  );
}

// --- HQ: the branches needing attention (UI-SPEC "Today" HQ variant; Claude's discretion, D-22) -------------------------

const BRANCH_COUNTS: readonly TileKey[] = TILES;

function CountBar({ n, max }: { n: number; max: number }) {
  return (
    <span className="flex min-w-0 items-center justify-end gap-2">
      <span aria-hidden="true" className="h-2 w-16 overflow-hidden rounded-full bg-sunken">
        <span className="block h-full rounded-full bg-navy-700" style={{ width: `${max === 0 ? 0 : (n / max) * 100}%` }} />
      </span>
      <span className={`num ${n === 0 ? "text-tx3" : "text-tx"}`}>{n}</span>
    </span>
  );
}

function Branches({ view, onPage }: { view: NetworkToday; onPage: (p: number) => void }) {
  const titleId = useId();
  const rows = view.branches.items;
  const max = Math.max(0, ...rows.flatMap((r: BranchRow) => BRANCH_COUNTS.map((k) => r[k])));
  return (
    <section aria-labelledby={titleId} className="flex min-w-0 flex-col gap-2 rounded-2xl border border-line bg-white py-4 shadow-card">
      <h2 id={titleId} className="m-0 px-4 text-title font-semibold text-tx">{BRANCHES_TITLE}</h2>
      <div className="min-w-0 overflow-x-auto">
        <table className="w-full min-w-[640px] border-collapse text-body">
          <thead className="bg-side">
            <tr>
              <th scope="col" className="border-b border-line px-4 py-2 text-left text-label font-semibold text-tx2">{BRANCH_COLUMN}</th>
              {BRANCH_COUNTS.map((k) => (
                <th key={k} scope="col" className="border-b border-line px-4 py-2 text-right text-label font-semibold text-tx2">
                  {TILE_LABEL[k]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.branch} data-testid={`branch-row-${r.branch}`} onClick={() => { window.location.hash = boardForBranch(r.branch); }}
                className="cursor-pointer hover:bg-side">
                <td className="border-b border-line px-4 py-2">
                  <a href={boardForBranch(r.branch)} onClick={(e) => e.stopPropagation()}
                    className="font-mono text-navy-800 underline underline-offset-2">{r.branch}</a>
                </td>
                {BRANCH_COUNTS.map((k) => (
                  <td key={k} className="border-b border-line px-4 py-2"><CountBar n={r[k]} max={max} /></td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager page={view.branches.page} pageSize={view.branches.page_size} total={view.branches.total} onPage={onPage} />
    </section>
  );
}

// --- a store's movements, outcomes and exceptions (UI-SPEC "Today" item 3; D-33: decision and movement apart) -----------

function Panel({ testId, title, right, children }: { testId: string; title: string; right?: ReactNode; children: ReactNode }) {
  const titleId = useId();
  return (
    <section data-testid={testId} aria-labelledby={titleId}
      className="flex min-w-0 flex-col gap-2 rounded-2xl border border-line bg-white p-4 shadow-card">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={titleId} className="m-0 text-title font-semibold text-tx">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

function TwoChips({ t }: { t: Moving }) {
  const d = DECISION_CHIP[t.decision.state as keyof typeof DECISION_CHIP]
    ?? { tone: "gray" as Tone, icon: "circle" as IconName, text: t.decision.state };
  const m = t.movement.state === null ? null : MOVEMENT_CHIP[t.movement.state];
  return (
    <span className="flex flex-wrap items-center gap-1">
      <span data-testid="today-decision-chip"><Pill tone={d.tone} icon={d.icon}>{decisionChipText(d.text)}</Pill></span>
      <span data-testid="today-movement-chip">
        {m === null || m === undefined
          ? <Pill tone="gray" icon="clock">{movementChipText(NOT_YET_RECORDED)}</Pill>
          : <Pill tone={m.tone} icon={m.icon}>{movementChipText(m.text)}</Pill>}
      </span>
    </span>
  );
}

function TransferRow({ t, from }: { t: Moving; from?: boolean }) {
  return (
    <li data-testid={`transfer-${t.case_key}`} className="flex min-w-0 flex-col gap-1">
      <span className="text-label break-words text-tx2">
        <a href={caseHref(t.case_key)} className="font-mono text-navy-800 underline underline-offset-2">{t.case_key}</a>
        {` · ${t.sku} · ${units(t.qty)} units`}{from ? ` · ${fromLine(t.source, t.run_code)}` : ""}
      </span>
      <TwoChips t={t} />
    </li>
  );
}

function Movements({ view }: { view: StoreToday }) {
  const out = view.movements.outgoing;
  const incoming = view.movements.incoming;
  return (
    <Panel testId="movements" title={MOVEMENTS_TITLE} right={<span className="text-label text-tx3">{shortDate(view.scenario_date)}</span>}>
      <p className="m-0 text-label font-semibold text-tx2">{outgoingLine(out.length)}</p>
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {out.map((r) => (
          <li key={`${r.run_code}:${r.day}:${r.dest}`} data-testid={`run-row-${r.run_code}`}
            className="flex min-w-0 flex-col gap-1 border-t border-line pt-2">
            <span className="flex flex-wrap items-center gap-1">
              <span className="rounded bg-sunken px-1 font-mono text-label text-tx">{r.run_code}</span>
              <span className="text-body text-tx">{r.source} → {r.dest}</span>
              {r.cold_chain && <Pill tone="chill" icon="snow">{COLD_CHAIN}</Pill>}
            </span>
            <span className="num text-label break-words text-tx2">
              {plannedRunLine(hhmm(r.loading_cutoff.min), hhmm(r.departure.min), hhmm(r.arrival.min), r.spare_totes)}
            </span>
            {r.transfers.length === 0
              ? <span className="text-label text-tx2">{NO_TRANSFERS}</span>
              : <ul className="m-0 flex list-none flex-col gap-2 p-0">{r.transfers.map((t) => <TransferRow key={t.case_key} t={t} />)}</ul>}
          </li>
        ))}
      </ul>
      <p className="m-0 border-t border-line pt-2 text-label font-semibold text-tx2">{INCOMING}</p>
      {incoming.length === 0
        ? <p className="m-0 text-label text-tx2">{noRunsTo(view.branch)}</p>
        : (
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {incoming.map((i) => (i.kind === "delivery"
              ? (
                <li key={i.inbound_id} data-testid={`delivery-${i.inbound_id}`} className="flex min-w-0 flex-wrap items-center gap-1">
                  <span className="num text-label break-words text-tx2">{deliveryLine(i.sku, i.qty, hhmm(i.arrival.min))}</span>
                  {i.status === "received"
                    ? <Pill tone="good" icon="check">{RECEIVED}</Pill>
                    : <Pill tone="plan" icon="clock">{PLANNED}</Pill>}
                </li>
              )
              : <TransferRow key={`in:${i.case_key}`} t={i} from />))}
          </ul>
        )}
      <PlannedNote />
    </Panel>
  );
}

function Outcomes({ view }: { view: StoreToday }) {
  const { session } = useSession();
  return (
    <Panel testId="outcomes" title={OUTCOMES_TITLE} right={(
      <span className="flex flex-wrap items-center gap-1">
        <span className="text-label text-tx3">{OUTCOMES_SUB}</span>
        {view.outcomes.length > 0 && <KindChip kind="simulated" />}
      </span>
    )}>
      {view.outcomes.length === 0 && <p className="m-0 text-label text-tx2">{NO_OUTCOMES}</p>}
      {view.outcomes.map((o) => (
        <div key={o.case_key} data-testid={`outcome-${o.case_key}`} className="flex min-w-0 flex-col gap-1">
          <span className="text-label break-words text-tx2">
            <a href={caseHref(o.case_key)} className="font-mono text-navy-800 underline underline-offset-2">{o.case_key}</a>
            {` · ${o.sku} lot ${o.lot_id}${o.plan ? ` · ${o.plan}` : ""}`}
          </span>
          <div className="grid min-w-0 grid-cols-2 gap-2">
            <div data-testid="expected" className="flex min-w-0 flex-col gap-1 rounded-lg border-[1.5px] border-dashed border-plan-line bg-page p-2">
              <span className="text-label text-tx2">{EXPECTED}</span>
              <span className="num text-display font-semibold text-tx">{signed(o.expected.net_vs_keep, session.currency)}</span>
              <span className="text-label break-words text-tx2">{EXPECTED_NOTE}</span>
            </div>
            <div data-testid="actual" className="flex min-w-0 flex-col gap-1 rounded-lg border border-line bg-white p-2">
              <span className="text-label text-tx3">{ACTUAL}</span>
              <span className="text-title font-semibold text-tx3">{NOT_YET_RECORDED}</span>
              <span className="text-label break-words text-tx3">{actualAppears(markDate(view.scenario_date))}</span>
            </div>
          </div>
        </div>
      ))}
      <div data-testid="coming-up" className="flex min-w-0 flex-col gap-1 border-t border-line pt-2">
        <span className="text-label font-semibold text-tx2">{COMING_UP}</span>
        {view.coming_up.length === 0
          ? <span className="text-label text-tx2">{NOTHING_COMING}</span>
          : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0">
              {view.coming_up.map((c) => (
                <li key={`${c.kind}:${c.case_key}:${c.lot_id}:${c.day}`} className="flex min-w-0 flex-wrap items-center gap-1">
                  <span className="num text-label break-words text-tx">
                    {markdownStepLine(c.lot_id, c.pct, c.date ? shortDate(c.date) : dateLabel(session.calendar, c.day))}
                  </span>
                  <Pill tone="plan" icon="clock">{PLANNED}</Pill>
                </li>
              ))}
            </ul>
          )}
      </div>
    </Panel>
  );
}

const EXCEPTION_LOOK: Readonly<Record<TodayException["kind"], { tone: Tone; icon: IconName; text: string }>> = {
  unverified_fact: { tone: "warn", icon: "alert", text: UNVERIFIED },
  discrepancy: { tone: "bad", icon: "alert", text: RECEIPT_DISCREPANCY_BADGE },
  dispatch_blocked: { tone: "bad", icon: "lock", text: DISPATCH_BLOCKED },
  hold_expired: { tone: "bad", icon: "lock", text: CAPACITY_HOLD_EXPIRED },
};

function Exceptions({ view }: { view: StoreToday }) {
  return (
    <Panel testId="exceptions" title={EXCEPTIONS_TITLE}>
      {view.exceptions.length === 0
        ? <p className="m-0 text-label text-tx2">{NO_EXCEPTIONS}</p>
        : (
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {view.exceptions.map((x) => {
              const look = EXCEPTION_LOOK[x.kind] ?? { tone: "gray" as Tone, icon: "alert" as IconName, text: x.kind };
              const line = x.kind === "unverified_fact" ? UNVERIFIED_LINE
                : x.kind === "discrepancy" && x.open_units !== undefined && x.units !== undefined
                  ? openUnits(x.open_units, x.units) : null;
              const next = nextLabel(x.next_action.key);
              return (
                <li key={`${x.kind}:${x.id}`} data-testid="exception-row" className="flex min-w-0 flex-col gap-1 border-t border-line pt-2 first:border-t-0 first:pt-0">
                  <span className="flex flex-wrap items-center gap-1">
                    <Pill tone={look.tone} icon={look.icon}>{look.text}</Pill>
                    <a href={caseHref(x.case_key)} className="font-mono text-label text-navy-800 underline underline-offset-2">{x.case_key}</a>
                  </span>
                  {line !== null && <span className="text-label break-words text-tx">{line}</span>}
                  <span className="text-label break-words text-tx2">
                    {exceptionMeta(x.owner === null ? NOT_TAKEN : x.owner.label,
                      next === null ? "—" : `${next} · ${x.next_action.who.map(whoName).join(" · ")}`)}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
    </Panel>
  );
}

// --- the action cards (UI-SPEC "Today" item 3) ------------------------------------------------------------------------

function ActionCards({ cards, tile, onClear, scenarioDay, packing, store, onOpen, session }: {
  cards: TodayCard[]; tile: TileKey | null; onClear: () => void; scenarioDay: number; packing: number | null;
  store: string; onOpen: (c: TodayCard) => void; session: { role: "manager" | "hq"; branch: string | null };
}) {
  const shown = byTile(cards, tile);
  return (
    <section aria-label={ACTIONS_LABEL} className="flex min-w-0 flex-col gap-2">
      {tile !== null && (
        <p className="m-0 flex flex-wrap items-center gap-2 text-label text-tx2">
          <span data-testid="today-filter">{showingTile(tile)}</span>
          <button type="button" onClick={onClear}
            className="inline-flex min-h-7 items-center rounded-md px-2 text-label font-semibold text-tx2 hover:bg-gray-soft hover:text-tx max-md:min-h-11">
            {CLEAR_FILTER}
          </button>
        </p>
      )}
      {tile !== null && shown.length === 0 && (
        <div data-testid="today-filter-empty" className="rounded-2xl border border-line bg-white p-4">
          <h2 className="m-0 text-title font-semibold text-tx">{noTileHeading(tile)}</h2>
          <p className="m-0 mt-1 text-body text-tx2">{noTileBody(tile, store)}</p>
        </div>
      )}
      {shown.length > 0 && (
        <div className="grid min-w-0 grid-cols-[repeat(auto-fill,minmax(min(256px,100%),1fr))] gap-4">
          {shown.map((c) => (
            <ActionCard key={c.key} card={c} scenarioDay={scenarioDay} packing={packing} onOpen={() => onOpen(c)}
              acts={actsOn(c.next_action, session)} />
          ))}
        </div>
      )}
    </section>
  );
}


function ActionCard({ card, scenarioDay, packing, onOpen, acts }: {
  card: TodayCard; scenarioDay: number; packing: number | null; onOpen: () => void; acts: boolean;
}) {
  const label = nextLabel(card.next_action.key);
  return (
    <article data-testid={`today-card-${card.key}`} onClick={onOpen}
      className={`flex min-w-0 cursor-pointer flex-col gap-2 rounded-2xl border border-line bg-white p-4 shadow-card hover:shadow-float ${acts ? "border-l-4 border-l-navy-800" : ""}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        {card.deadline === null
          ? (
            <span className="flex flex-wrap items-baseline gap-1">
              <span className="text-body text-tx3 italic">{UNSCHEDULED}</span>
              <span className="num text-label text-tx3">{shortDate(card.date)}</span>
            </span>
          )
          : (
            <span className="flex flex-wrap items-baseline gap-1">
              <span className="num text-title font-semibold text-tx">{plannedAt(card.deadline, scenarioDay)}</span>
              <span className="text-label text-tx3">{PLANNED_WORD}</span>
            </span>
          )}
        <StatusChip card={card} />
      </div>
      <button type="button" onClick={(e) => { e.stopPropagation(); onOpen(); }}
        className="m-0 text-left text-body font-semibold break-words text-tx hover:underline">
        {whatLine(card, stepName)}
      </button>
      <p className="m-0 text-label break-words text-tx2">{whereLine(card)}</p>
      <p className="m-0 text-label break-words text-tx2">{ownerDeadlineLine(card, scenarioDay, packing)}</p>
      {label !== null && (
        <LinkButton compact primary={acts} className="w-fit" href={actionHref(card)} onClick={(e) => e.stopPropagation()}>
          {label}
        </LinkButton>
      )}
    </article>
  );
}

// --- the drawer (a card or a marker) ----------------------------------------------------------------------------------

function TodayDrawer({ subject, onClose, packing }: {
  subject: Subject; onClose: () => void; packing: number | null;
}) {
  const { session } = useSession();
  if (subject.kind === "info") {
    return (
      <Drawer title={subject.title} onClose={onClose}>
        {subject.ref && <p className="m-0 font-mono text-label text-tx3">{subject.ref}</p>}
        <Kv stacked items={subject.items} />
        {subject.caseKey && (
          <LinkButton compact className="w-fit" href={caseHref(subject.caseKey)}>{TERMS.OPEN_STOCK_DECISION}</LinkButton>
        )}
        {subject.source && (
          <p className="m-0 border-t border-dashed border-line pt-2 text-label text-tx3">{subject.source}</p>
        )}
        <PlannedNote />
      </Drawer>
    );
  }
  const c = subject.card;
  const acts = actsOn(c.next_action, session);
  const label = nextLabel(c.next_action.key);
  const cause = c.deadline === null ? null : causeText(c.deadline, packing);
  const deadline = c.deadline === null ? NO_DEADLINE_SET
    : [deadlineDrawer(hhmm(c.deadline.min), shortDate(c.deadline.date)), cause ?? ""]
      .filter((x) => x !== "").join(" · ");
  const source = c.deadline === null ? null
    : c.deadline.cause.source === "run" && c.deadline.cause.run_code ? deadlineSource(c.deadline.cause.run_code)
      : c.deadline.cause.source === "markdown" ? DEADLINE_MARKDOWN
        : c.deadline.cause.source === "stock_check_due" ? DEADLINE_CHECK : null;
  const sources = [source ?? "", c.kind === "stock_decision" && c.what.plan ? PLAN_SOURCE : ""].filter((x) => x !== "");
  return (
    <Drawer title={whatLine(c, stepName)} onClose={onClose}>
      <p className="m-0 font-mono text-label text-tx3">{c.case_key}</p>
      <StatusChip card={c} />
      <Kv stacked items={[
        [DRAWER_LABELS.what, whatLine(c, stepName)],
        [DRAWER_LABELS.where, whereLine(c)],
        [DRAWER_LABELS.owner, c.owner === null ? NOT_TAKEN : c.owner.label],
        [DRAWER_LABELS.deadline, deadline],
        [DRAWER_LABELS.status, statusLook(c).text],
        [DRAWER_LABELS.next, label === null ? "—" : `${label} · ${c.next_action.who.map(whoName).join(" · ")}`],
      ]} />
      <div className="flex flex-wrap items-center gap-2">
        {label !== null && (
          <LinkButton compact primary={acts} className="w-fit" href={actionHref(c)}>{label}</LinkButton>
        )}
        <LinkButton compact className="w-fit" href={caseHref(c.case_key)}>{TERMS.OPEN_STOCK_DECISION}</LinkButton>
      </div>
      {sources.length > 0 && (
        <p className="m-0 border-t border-dashed border-line pt-2 text-label text-tx3">{sources.join(" · ")}</p>
      )}
      <PlannedNote />
    </Drawer>
  );
}

function PlannedNote() {
  return (
    <p data-testid="planned-note" className="m-0 flex items-center gap-1 text-label text-tx3">
      <Icon name="clock" size={12} />{PLANNED_NOTE}
    </p>
  );
}

export const page: PageDef = {
  path: "/",
  title: TITLE,
  nav: { label: TITLE, icon: "today", section: "", order: 1, countKey: "today" },
  Component: TodayPage,
};
