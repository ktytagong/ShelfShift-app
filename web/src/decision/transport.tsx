// The transport details panel (sketch 004 A; 06.1-UI-SPEC "Execute" item 1; 06.1-CONTEXT D-30, D-33, D-55). One
// component, shared by the Branch transfers drawer (06.1-29) and the stock decision's Execute step (06.1-27): the
// Scheduled truck run's header, the Decision and Movement boxes with their own chips (a committed decision is not a
// departure), six planned milestones, the movement record, capacity, the two stops and the planned-times note. It
// reads GET /api/cases/{id}/transport only and records nothing: Dispatch and Receipt stay in Execute (D-55).
// Rules kept here:
// - Planned times are planned: "{12:00} · planned", never "Bangkok time"; a milestone without a time reads
//   "Unscheduled"; a recorded time reads "Recorded {…} Bangkok time" from the session's zone (D-58, D-63).
// - No live position, arrival estimate, delay or countdown anywhere (D-18, D-30): nothing here reads a clock.
// - A value the data lacks reads "not recorded" or "not stored", and the carrier and coordinator say "(assumed)" and
//   "(represented)": never a guess. The run's timetable is reference data of the simulated scenario, so the header
//   carries the "Simulated" chip (D-15).
// - The movement state and its quantities come from what was recorded (the server's `movement`); the decision state
//   is its own chip (D-33). The client derives no step from a state (D-24): the step is the server's.
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { ApiError } from "../api.ts";
import { getTransport, type MilestoneKey, type MovementState, type TransportView } from "../contract.ts";
import { dateLabel, hhmm, money, units } from "../format.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import { useSession } from "../shell.tsx";
import { STEP_KEYS, stepText } from "../steps.ts";
import { Icon, KindChip, LoadError, Loading, Pill, type IconName } from "../ui.tsx";
import {
  DISPATCHED, MOVEMENT_CHIP, NO_TRANSPORT, NOT_YET_RECORDED, PARTIALLY_RECEIVED, PLANNED, PLANNED_NOTE, RECEIPT_DISCREPANCY,
  RECEIVED, TERMS, UNSCHEDULED, decisionLook, recordedLine, shortShipment,
} from "../vocab.ts";

// --- words (UI-SPEC "Execute" item 1, "Dispatch, movement and receipt", sketch 004 A) --------------------------------

export const NOT_A_DEPARTURE = "A committed decision is not a departure."; // (proposed, UI-SPEC)
export const LOADING_TRANSPORT = "Loading the transport details…"; // (proposed)
export const TRANSPORT_DIDNT_LOAD = "The transport details didn't load"; // (proposed)
export const COLD_CHAIN = "Cold chain";
export const AMBIENT = "Ambient";
export const TOTE_USE_NOT_STORED = "Tote use per unit: not stored";
export const MILESTONES_HEADING = "Planned milestones"; // (proposed)
export const MOVEMENT_RECORD = "Movement record"; // sketch 004 A
export const CAPACITY = "Capacity"; // sketch 004 A
export const STOPS = "Stops"; // sketch 004 A
export const ORIGIN_STOP = "Origin · load"; // sketch 004 A
export const DESTINATION_STOP = "Destination · receive"; // sketch 004 A
export const SPARE_TOTES_NOT_RECORDED = "Spare totes: not recorded"; // (proposed)
export const QUOTE_NOT_RECORDED = "Quote: not recorded"; // (proposed)
export const CARRIER_NOT_RECORDED = "Carrier: not recorded"; // (proposed)
export const COORDINATOR_NOT_RECORDED = "Coordinator: not recorded"; // (proposed)
export const PLANNED_WORD = "planned";
export const RECEIVING_UNSCHEDULED = "receiving unscheduled"; // sketch 004 A
export const DESTINATION_NOT_RECORDED = "not recorded"; // a transport view without a destination (proposed)

export const runTitle = (source: string, dest: string) => `${TERMS.SCHEDULED_TRUCK_RUN} · ${source} → ${dest}`;
export const quoteText = (amount: string) => `Quote ${amount}`;
export const spareTotes = (n: number, run: string) => `${units(n)} spare totes on ${run}`; // (proposed)
export const travelText = (min: number) => `${units(min)} min planned`; // sketch 004 A
export const qtyUnits = (n: number) => `${units(n)} ${n === 1 ? "unit" : "units"}`;
export const packTask = (qty: string, store: string) => `Pick and pack ${qty} · ${store}`; // UI-SPEC work list
export const receiveTask = (qty: string, store: string) => `Receive ${qty} · ${store}`; // sketch 004 A

// The carrier and the coordinator the data model does not hold (sketch 004 "Values not in the data model").
export const CARRIER: Readonly<Record<string, string>> = { own_fleet: "Own fleet (assumed)" };
export const COORDINATOR: Readonly<Record<string, string>> = { represented: "Logistics coordinator (represented)" };
const CARRIER_SOURCE = "The data holds no carrier: the run is assumed to use the retailer's own fleet."; // (proposed)

// The six milestones, in order, by their server keys (D-30).
export const MILESTONE_NAMES: Readonly<Record<MilestoneKey, string>> = {
  pack_from: "Pack from",
  ready: "Ready",
  loading_cutoff: "Loading cutoff",
  departure: "Departure",
  arrival: "Arrival",
  received: "Received",
};
const MILESTONE_ICON: Partial<Record<MilestoneKey, IconName>> = { loading_cutoff: "lock" };

// --- the two chips (D-33: never merged) -------------------------------------------------------------------------------

// The words live in vocab.ts (DECISION_CHIP, MOVEMENT_CHIP), so every page reads a state the same way (R-2).

export function DecisionChip({ state }: { state: string }) {
  const look = decisionLook(state);
  return <span data-testid="decision-chip"><Pill tone={look.tone} icon={look.icon}>{look.text}</Pill></span>;
}

// The movement chip, plus "Short shipment · {104} of {110}" when fewer units were dispatched than committed. Without
// a movement (no work order, or one carried out with nothing recorded) it reads "Not yet recorded", never a state.
export function MovementChip({ state, committed, dispatched }:
  { state: MovementState | null; committed: number | null; dispatched: number | null }) {
  const short = committed !== null && dispatched !== null && dispatched < committed;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {state === null
        ? <span data-testid="movement-chip" className="text-label text-tx2">{NOT_YET_RECORDED}</span>
        : <span data-testid="movement-chip"><Pill tone={MOVEMENT_CHIP[state].tone} icon={MOVEMENT_CHIP[state].icon}>
          {MOVEMENT_CHIP[state].text}
        </Pill></span>}
      {short && <Pill tone="warn" icon="alert">{shortShipment(dispatched, committed)}</Pill>}
    </span>
  );
}

// --- the panel ----------------------------------------------------------------------------------------------------------

type Load = { view: TransportView | null; error: ApiError | null; reload: () => void };

// GET /api/cases/{id}/transport on mount, on a new case or workspace version, on Try again and when the tab becomes
// visible again. An answer that arrives after a newer load started is dropped; a failed refetch keeps the last view.
function useTransport(caseId: string, version: number): Load {
  const [view, setView] = useState<TransportView | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const seq = useRef(0);
  const load = useCallback(() => {
    const mine = ++seq.current;
    getTransport(caseId).then(
      (v) => { if (mine === seq.current) { setView(v); setError(null); } },
      (e: ApiError) => { if (mine === seq.current) setError(e); },
    );
  }, [caseId]);
  useEffect(() => { setView(null); setError(null); }, [caseId]);
  useEffect(() => load(), [load, version]);
  useRefetchOnVisible(load);
  return { view, error, reload: load };
}

export function TransportPanel({ caseId }: { caseId: string }) {
  const { session } = useSession();
  const { view, error, reload } = useTransport(caseId, session.version);
  if (view === null) {
    return error === null
      ? <Loading text={LOADING_TRANSPORT} />
      : <LoadError title={TRANSPORT_DIDNT_LOAD} error={error} onRetry={reload} />;
  }
  return (
    <div data-testid="transport-panel" className="flex min-w-0 flex-col gap-4">
      {view.run === null
        ? <p className="m-0 text-body text-tx2">{NO_TRANSPORT}</p>
        : <RunDetail view={view} run={view.run} caseKey={caseId} />}
    </div>
  );
}

type Run = NonNullable<TransportView["run"]>;

function RunDetail({ view, run, caseKey }: { view: TransportView; run: Run; caseKey: string }) {
  const { session } = useSession();
  const dest = view.destination ?? DESTINATION_NOT_RECORDED;
  const carrier = CARRIER[view.assumed.carrier] ?? CARRIER_NOT_RECORDED;
  const coordinator = COORDINATOR[view.assumed.coordinator] ?? COORDINATOR_NOT_RECORDED;
  return (
    <>
      <div className="flex min-w-0 flex-col gap-2">
        <h3 className="m-0 flex min-w-0 flex-wrap items-center gap-2 text-title font-semibold text-tx">
          <span className="rounded-md bg-navy-50 px-2 font-mono text-body text-navy-800">{run.code}</span>
          <span className="min-w-0 break-words">{runTitle(view.source, dest)}</span>
        </h3>
        <div data-testid="run-chips" className="flex flex-wrap items-center gap-1">
          {run.cold_chain
            ? <Pill tone="chill" icon="snow">{COLD_CHAIN}</Pill>
            : <Pill tone="gray" icon="box">{AMBIENT}</Pill>}
          <span className="inline-flex items-center"><Pill tone="gray" icon="tag">{carrier}</Pill></span>
          <Pill tone="gray" icon="user">{coordinator}</Pill>
          <Pill tone="gray" icon="tag">
            {run.quote === null ? QUOTE_NOT_RECORDED : quoteText(money(run.quote, session.currency))}
          </Pill>
          <KindChip kind="simulated" />
          <KindChip kind="assumption" source={CARRIER_SOURCE} />
        </div>
      </div>
      <DecisionMovement view={view} run={run} caseKey={caseKey} />
      <section aria-label={MILESTONES_HEADING} className="flex min-w-0 flex-col gap-2">
        <h4 className="m-0 text-label font-semibold uppercase tracking-wide text-tx3">{MILESTONES_HEADING}</h4>
        <MilestoneTrack view={view} />
      </section>
      <section aria-label={MOVEMENT_RECORD} className="flex min-w-0 flex-col gap-2">
        <h4 className="m-0 text-label font-semibold uppercase tracking-wide text-tx3">{MOVEMENT_RECORD}</h4>
        <MovementRecord movement={view.movement} />
      </section>
      <Capacity run={run} />
      <Stops view={view} run={run} caseKey={caseKey} />
      <p data-testid="planned-note" className="m-0 flex items-center gap-1 text-label text-tx3">
        <Icon name="clock" size={12} />{PLANNED_NOTE}
      </p>
    </>
  );
}

// Decision | Movement: two boxes, each with its own chip (D-33), two columns at 768px and wider.
function DecisionMovement({ view, run, caseKey }: { view: TransportView; run: Run; caseKey: string }) {
  const { session } = useSession();
  const m = view.movement;
  const step = stepText({ step: view.decision.step, step_number: STEP_KEYS.indexOf(view.decision.step) + 1 });
  const last = m.received_at ?? m.dispatched_at;
  const recorded = last === null ? NOT_YET_RECORDED : recordedLine(last, session.time_zone);
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="grid min-w-0 grid-cols-1 gap-2 md:grid-cols-2">
        <div data-testid="decision-box" className="min-w-0 rounded-xl border border-line p-3">
          <p className="m-0 flex items-center gap-1 text-label font-semibold uppercase tracking-wide text-tx3">
            <Icon name="board" size={12} />{TERMS.STOCK_DECISION} · {caseKey}
          </p>
          <div className="mt-1"><DecisionChip state={view.decision.state} /></div>
          {step !== null && <p className="m-0 mt-1 text-label text-tx2">{step}</p>}
        </div>
        <div data-testid="movement-box" className="min-w-0 rounded-xl border border-line p-3">
          <p className="m-0 flex items-center gap-1 text-label font-semibold uppercase tracking-wide text-tx3">
            <Icon name="truck" size={12} />Movement · {run.code}
          </p>
          <div className="mt-1">
            <MovementChip state={m.state} committed={m.committed_qty} dispatched={m.dispatched_qty} />
          </div>
          <p className="num m-0 mt-1 text-label break-words text-tx2">
            {m.state === "discrepancy" ? RECEIPT_DISCREPANCY : recorded}
          </p>
        </div>
      </div>
      <p className="m-0 text-label text-tx3">{NOT_A_DEPARTURE}</p>
    </div>
  );
}

// "12:00 · planned", with the day when it is not the scenario date ("12:00 · Sun 18 Oct · planned").
function plannedText(day: number | null, min: number | null, today: number, cal: TransportCal): string | null {
  if (min === null) return null;
  const other = day !== null && day !== today ? dateLabel(cal, day) : "";
  return [hhmm(min), other, PLANNED_WORD].filter((x) => x !== "").join(" · ");
}
type TransportCal = Parameters<typeof dateLabel>[0];

// The six milestones as an ordered list: horizontal at 768px and wider, vertical below. Planned nodes white with a
// dashed ring; a recorded one navy-800 with a white check; the Received node takes the warn ring with an alert when the
// receipt fell short or opened a discrepancy.
export function MilestoneTrack({ view }: { view: TransportView }) {
  const { session } = useSession();
  const problem = view.movement.state === "partially_received" || view.movement.state === "discrepancy";
  return (
    <ol data-testid="milestones" className="m-0 grid min-w-0 list-none grid-cols-1 gap-2 p-0 md:grid-cols-6">
      {view.milestones.map((m) => {
        const recorded = m.recorded_at !== null;
        const bad = m.key === "received" && recorded && problem;
        const when = recorded
          ? recordedLine(m.recorded_at as string, session.time_zone)
          : plannedText(m.day, m.min, session.day, session.calendar);
        const node = bad
          ? "border-2 border-warn bg-white text-warn"
          : recorded ? "border-2 border-navy-800 bg-navy-800 text-white" : "border-2 border-dashed border-plan-line bg-white text-tx3";
        return (
          <li key={m.key} data-testid={`milestone-${m.key}`}
            className="flex min-w-0 items-start gap-2 md:flex-col md:items-center md:text-center">
            <span aria-hidden="true" className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full ${node}`}>
              {bad ? <Icon name="alert" size={12} /> : recorded ? <Icon name="check" size={12} />
                : MILESTONE_ICON[m.key] ? <Icon name={MILESTONE_ICON[m.key] as IconName} size={12} /> : null}
            </span>
            <span className="flex min-w-0 flex-col">
              <span className="text-body font-semibold text-tx">{MILESTONE_NAMES[m.key]}</span>
              {when === null
                ? <span className="text-label italic text-tx3">{UNSCHEDULED}</span>
                : <span className="num text-label break-words text-tx2">{when}</span>}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

type Box = { name: string; lines: ReactNode[]; look: "plan" | "done" | "good" | "bad" | "gray" };
const BOX_LOOK: Record<Box["look"], string> = {
  plan: "border border-dashed border-plan-line bg-white",
  done: "border border-navy-800 bg-navy-50",
  good: "border border-good bg-good-soft",
  bad: "border border-bad bg-bad-soft",
  gray: "border border-line bg-sunken",
};

// Planned → Dispatched → Received or Partially received (or Expired, Cancelled): dashed until recorded; a recorded box
// shows "Recorded {…} Bangkok time" and its quantity. Dispatched and received stay separate (D-48, D-65).
export function MovementRecord({ movement: m }: { movement: TransportView["movement"] }) {
  const { session } = useSession();
  const at = (iso: string | null) => (iso === null ? NOT_YET_RECORDED : recordedLine(iso, session.time_zone));
  const planned: Box = m.committed_qty === null
    ? { name: PLANNED, lines: [NOT_YET_RECORDED], look: "plan" }
    : { name: PLANNED, lines: [`${qtyUnits(m.committed_qty)} committed`], look: m.state === null ? "plan" : "done" };
  const dispatched: Box = m.dispatched_at === null
    ? { name: DISPATCHED, lines: [NOT_YET_RECORDED], look: "plan" }
    : {
      name: DISPATCHED,
      lines: [at(m.dispatched_at), m.dispatched_qty === null ? "" : `${qtyUnits(m.dispatched_qty)} dispatched`],
      look: "done",
    };
  const short = m.state === "partially_received" || m.state === "discrepancy";
  let outcome: Box;
  if (m.state === "expired" || m.state === "cancelled") {
    outcome = { name: MOVEMENT_CHIP[m.state].text, lines: [], look: "gray" };
  } else if (m.received_at === null) {
    outcome = { name: RECEIVED, lines: [NOT_YET_RECORDED], look: "plan" };
  } else {
    const lines: ReactNode[] = [at(m.received_at)];
    if (m.received_qty !== null) lines.push(`${qtyUnits(m.received_qty)} received`);
    if (m.damaged_qty !== null && m.damaged_qty > 0) lines.push(`${qtyUnits(m.damaged_qty)} damaged`);
    outcome = { name: short ? PARTIALLY_RECEIVED : RECEIVED, lines, look: short ? "bad" : "good" };
  }
  const boxes = [planned, dispatched, outcome];
  return (
    <ol data-testid="movement-record" className="m-0 flex min-w-0 list-none flex-col gap-2 p-0 md:flex-row">
      {boxes.map((b, i) => (
        <li key={i} className={`flex min-w-0 flex-1 flex-col rounded-lg px-3 py-2 ${BOX_LOOK[b.look]}`}>
          <span className="text-body font-semibold text-tx">{b.name}</span>
          {b.lines.filter((l) => l !== "").map((l, j) => (
            <span key={j} className="num text-label break-words text-tx2">{l}</span>
          ))}
        </li>
      ))}
    </ol>
  );
}

// Spare totes from the timetable, and a hatched dashed bar: tote use per unit is not stored, so the share this
// transfer takes is never drawn as a guess.
function Capacity({ run }: { run: Run }) {
  return (
    <section aria-label={CAPACITY} className="flex min-w-0 flex-col gap-1">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="m-0 text-label font-semibold uppercase tracking-wide text-tx3">{CAPACITY}</h4>
        <span className="num text-label text-tx2">
          {run.spare_totes === null ? SPARE_TOTES_NOT_RECORDED : spareTotes(run.spare_totes, run.code)}
        </span>
      </div>
      <div aria-hidden="true"
        className="h-2 w-full rounded-full border border-dashed border-plan-line bg-[repeating-linear-gradient(135deg,var(--color-sunken)_0_6px,#fff_6px_12px)]" />
      <p className="m-0 text-label text-tx2">{TOTE_USE_NOT_STORED}</p>
    </section>
  );
}

// The origin and destination stops (`1fr auto 1fr`, stacked below 768px); selecting one (aria-pressed) shows its
// transfer and its task under the cards.
function Stops({ view, run, caseKey }: { view: TransportView; run: Run; caseKey: string }) {
  const { session } = useSession();
  const [stop, setStop] = useState<"origin" | "dest">("origin");
  const dest = view.destination ?? DESTINATION_NOT_RECORDED;
  const m = view.movement;
  const at = (key: MilestoneKey) => view.milestones.find((x) => x.key === key);
  const time = (key: MilestoneKey) => {
    const x = at(key);
    return x === undefined || x.min === null ? UNSCHEDULED : hhmm(x.min);
  };
  const qty = m.committed_qty === null ? NOT_YET_RECORDED : qtyUnits(m.committed_qty);
  const card = (which: "origin" | "dest", kind: string, store: string, line: string) => (
    <button type="button" aria-pressed={stop === which} onClick={() => setStop(which)}
      className={`flex min-w-0 flex-col gap-1 rounded-xl border bg-white p-3 text-left max-md:min-h-11 ${stop === which ? "border-navy-800 shadow-[0_0_0_1px_var(--color-navy-800)]" : "border-line hover:border-line-2"}`}>
      <span className="text-label uppercase tracking-wide text-tx3">{kind}</span>
      <span className="flex items-center gap-1 text-body font-semibold text-tx"><Icon name="pin" />{store}</span>
      <span className="num text-label break-words text-tx2">{line}</span>
    </button>
  );
  const travel = run.arrives_min - run.departs_min;
  const recordedAt = stop === "origin" ? m.dispatched_at : m.received_at;
  const task = stop === "origin" ? packTask(qty, view.source) : receiveTask(qty, dest);
  return (
    <section aria-label={STOPS} className="flex min-w-0 flex-col gap-2">
      <h4 className="m-0 text-label font-semibold uppercase tracking-wide text-tx3">{STOPS}</h4>
      <div className="grid min-w-0 grid-cols-1 items-stretch gap-2 md:grid-cols-[1fr_auto_1fr]">
        {card("origin", ORIGIN_STOP, view.source,
          `${MILESTONE_NAMES.pack_from} ${time("pack_from")} · cutoff ${time("loading_cutoff")} · departs ${time("departure")} · ${PLANNED_WORD}`)}
        <div aria-hidden="true" className="flex flex-col items-center justify-center gap-1 text-label text-tx3 md:min-w-24">
          {travel >= 0 && <span className="num">{travelText(travel)}</span>}
          <span className="h-4 w-0 border-l-2 border-dashed border-plan-line md:h-0 md:w-full md:border-t-2 md:border-l-0" />
        </div>
        {card("dest", DESTINATION_STOP, dest, `Arrives ${time("arrival")} · ${PLANNED_WORD} · ${RECEIVING_UNSCHEDULED}`)}
      </div>
      <div data-testid="stop-panel" className="flex min-w-0 flex-col gap-2 border-t border-line pt-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2 rounded-lg border border-line px-3 py-2">
          <span className="min-w-0 flex-1 text-body break-words text-tx">
            <span className="font-mono text-label text-tx3">{caseKey}</span> · {TERMS.BRANCH_TRANSFER} · {qty} ·{" "}
            {view.source} → {dest}
          </span>
          <MovementChip state={m.state} committed={m.committed_qty} dispatched={m.dispatched_qty} />
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-2 rounded-lg border border-line px-3 py-2">
          <span className="inline-flex h-7 w-7 items-center justify-center rounded-lg bg-navy-50 text-navy-800">
            <Icon name={stop === "origin" ? "box" : "check"} />
          </span>
          <span className="min-w-0 flex-1 text-body break-words text-tx">{task}</span>
          {recordedAt === null
            ? <Pill tone="plan" icon="clock">{PLANNED}</Pill>
            : <span className="num text-label text-tx2">{recordedLine(recordedAt, session.time_zone)}</span>}
        </div>
      </div>
    </section>
  );
}
