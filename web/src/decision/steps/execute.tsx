// Step 7 · Execute (06.1-UI-SPEC "Execute"; 06.1-CONTEXT D-30, D-33..D-35, D-48, D-49, D-55, D-65): people record
// what happened to the stock. The source manager records the dispatch, the destination manager the receipt; the
// record panel shows the server's checks and refusals, the work list shows each work order, and a transfer carries
// 06.1-29's transport details panel (imported, never redrawn here).
// Rules kept here:
// - Who acts is the server's: a record form is offered only while the server's next action names it and the signed-in
//   account among the people who act (next_action.who); the server checks the account again and its 403 or 409 is
//   shown word for word. The current step is the server's (D-24): nothing here moves the decision.
// - Every record starts empty: no field is pre-filled with the committed, dispatched or expected quantity (D-48;
//   BUSINESS-FLOW-BRIEF §5). One Idempotency-Key per press, kept for Try again (hooks.useAction).
// - Dispatched and received units stay separate (D-48, D-65): the movement record and the work list read what was
//   recorded, and nothing claims a receipt before one exists.
// - The form opens from the summary's primary button (the deliberate step); the panel holds the form inline.
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useEffect, useId, useRef, useState, useSyncExternalStore, type FormEvent, type ReactNode } from "react";
import { ApiError } from "../../api.ts";
import {
  getExecution, reconcile, recordDispatch, recordPrice, recordReceipt, type Discrepancy, type DispatchBlock,
  type DispatchBlockKind, type ExecutionChoice, type ExecutionView, type MovementState, type Session61, type WorkOrder,
} from "../../contract.ts";
import { retryable } from "../../copy.ts";
import { dateLabel, units } from "../../format.ts";
import { useAction, useRefetchOnVisible } from "../../hooks.ts";
import { useSession } from "../../shell.tsx";
import { Banner, Button, ErrorLine, Icon, Loading, LoadError, Pill, QtyBar, TRY_AGAIN, type IconName } from "../../ui.tsx";
import {
  AFTER_DISPATCH_REVIEW, DISCREPANCY_RECONCILED, DISPATCH_BLOCKED, NEXT_ACTION_LABEL, NO_TRANSPORT, READY_TO_DISPATCH,
  DISCREPANCY, RECEIPT_DISCREPANCY, RELOAD_DECISION, UNSCHEDULED, receiptLine, recordedLine, type NextActionKey,
} from "../../vocab.ts";
import { MovementChip, TransportPanel } from "../transport.tsx";
import { named } from "../shared.ts";
import type { StepModule, StepProps } from "../types.ts";

// --- words -----------------------------------------------------------------------------------------------------------

export const RECORD_DISPATCH = NEXT_ACTION_LABEL.record_dispatch; // "Record dispatch"
export const UNITS_DISPATCHED = "Units dispatched";
export const UP_TO_COMMITTED = (n: number) => `Up to ${units(n)} committed units.`;
export const LOT = "Lot";
export const CONDITION = "Condition";
export const REQUIRED = "(required)"; // UI-SPEC "Accessibility contract": record forms mark required fields in words
export const CHOOSE = "Choose…"; // (proposed) the empty first option of a select: nothing is chosen for the person
// The submit's accessible description (proposed, UI-SPEC "Destructive and consequential actions").
export const DISPATCH_DESCRIPTION = (n: string | null, store: string) =>
  n === null ? `Records the units leaving ${store}. This updates the stock record.`
    : `Records ${n} units leaving ${store}. This updates the stock record.`;
export const CLOSE_FORM = "Close without recording"; // (proposed, UI-SPEC Record dispatch)
export const TRANSPORT_DETAILS = "Transport details";
export const WORK_LIST = "Work orders"; // (proposed)
export const NO_WORK_ORDERS = "No work orders yet. They appear when the plan is prepared."; // (proposed)
export const LOADING_EXECUTION = "Loading the work orders…"; // (proposed)
export const EXECUTION_DIDNT_LOAD = "The work orders didn't load"; // (proposed)
// The line under the step (sketch 006; UI-SPEC "Execute" item 4).
export const EXECUTE_LINE = "The work order moves on when someone records the dispatch and the receipt.";
export const DONE = "Done";
export const RECORD_RECEIPT = NEXT_ACTION_LABEL.record_receipt; // "Record receipt"
export const UNITS_RECEIVED = "Units received in good condition";
export const UNITS_DAMAGED = "Units damaged";
export const RECEIPT_REASON = "Reason (required for a short or damaged receipt)";
export const OPENS_DISCREPANCY = // (proposed, UI-SPEC)
  "This receipt opens a discrepancy. The stock decision can't close until it is reconciled.";
export const REASON_NOW_REQUIRED = "A reason is now required."; // (proposed) announced when the reason becomes required
export const RECONCILE_DISCREPANCY = NEXT_ACTION_LABEL.reconcile; // "Reconcile discrepancy" (proposed in vocab)
export const RECORD_RECONCILIATION = "Record reconciliation"; // (proposed, UI-SPEC)
export const RESOLUTION = "Resolution";
export const UNITS = "Units";
export const REASON = "Reason";
export const RECORD_PRICE = NEXT_ACTION_LABEL.record_price; // "Record price change"
export const markdownLine = (lot: string, pct: number, store: string) => `Mark lot ${lot} down ${pct}% at ${store}.`;
export const PRICE_TICK = "I changed the shelf price for this lot"; // (proposed, UI-SPEC)
export const TICK_TO_RECORD = "Tick the box to record."; // (proposed, UI-SPEC)
export const KEEP_LINE = "Nothing to record. The store's markdown schedule prices this lot."; // (proposed, UI-SPEC)
export const AFTER_DISPATCH_LINE = "The commitment stands. Stock already moved is not recalled."; // (proposed, UI-SPEC)
export const resolveLine = (resolution: string) => `Resolve: ${resolution}`;
export const LOT_HOLD = "Lot hold"; // UI-SPEC: a lot hold's row reads "Lot hold" and the server's reason
export const blockOwner = (branch: string) => `Owner: ${branch} manager`; // (proposed)
export const unitWord = (n: number) => (n === 1 ? "unit" : "units");
export const UP_TO_OPEN = (n: number) => `Up to ${units(n)} ${unitWord(n)} still open.`; // (proposed)
// The reconcile form's chosen discrepancy was reconciled elsewhere (proposed; quick 261018-4fi).
export const DISCREPANCY_GONE = "This discrepancy was already reconciled. Choose another one.";
// One open discrepancy in words (proposed): "Missing · 4 of 4 units open".
export const DISCREPANCY_KIND: Readonly<Record<string, string>> = { SHORT: "Missing", DAMAGED: "Damaged" };
export const discrepancyOpen = (kind: string, open: number, all: number) =>
  `${DISCREPANCY_KIND[kind] ?? kind} · ${units(open)} of ${units(all)} ${unitWord(all)} open`;
export const transferTask = (qty: string, lot: string, from: string, to: string) => // (proposed)
  `Transfer ${qty} units of lot ${lot} · ${from} → ${to}`;
export const markdownTask = (lot: string, pct: number, store: string) => `Mark lot ${lot} down ${pct}% · ${store}`; // (proposed)
export const plannedOn = (date: string) => `${date} · planned`;

// --- the execution view (GET /api/cases/{id}/execution) --------------------------------------------------------------

// The rows as 06.1-09's contract row states them (04-API-CONTRACT 2026-10-17), typed in contract.ts.
type ExecView = ExecutionView;

// One view per stock decision, shared by the summary's button and the panel (a small external store): the panel loads
// it; the button only reads it, to stay away while the panel shows a block. An answer that arrives after a newer load
// started is dropped; a failed refetch keeps the last view and shows its error.
type ExecLoad = { view: ExecView | null; error: ApiError | null };
const EMPTY: ExecLoad = { view: null, error: null };
const loads = new Map<string, ExecLoad>();
const seqs = new Map<string, number>();
const listeners = new Set<() => void>();
const emit = () => { for (const f of listeners) f(); };
function subscribe(f: () => void) {
  listeners.add(f);
  return () => { listeners.delete(f); };
}
function loadExecution(caseId: string) {
  const mine = (seqs.get(caseId) ?? 0) + 1;
  seqs.set(caseId, mine);
  getExecution(caseId).then(
    (v) => {
      if (seqs.get(caseId) !== mine) return;
      loads.set(caseId, { view: v, error: null });
      emit();
    },
    (e: unknown) => {
      if (seqs.get(caseId) !== mine) return;
      const error = e instanceof ApiError ? e : new ApiError(0, "error", String(e));
      loads.set(caseId, { view: loads.get(caseId)?.view ?? null, error });
      emit();
    },
  );
}
function useExecutionStore(caseId: string): ExecLoad {
  return useSyncExternalStore(subscribe, () => loads.get(caseId) ?? EMPTY);
}

// Which record form is open per decision (the summary's button opens it; a success or Close without recording shuts it).
const open = new Map<string, NextActionKey>();
function setOpen(caseId: string, key: NextActionKey | null) {
  if (key === null) open.delete(caseId);
  else open.set(caseId, key);
  emit();
}
function useOpen(caseId: string): NextActionKey | null {
  return useSyncExternalStore(subscribe, () => open.get(caseId) ?? null);
}

// --- who acts --------------------------------------------------------------------------------------------------------

const RECORD_KEYS: readonly NextActionKey[] = ["record_dispatch", "record_receipt", "record_price", "reconcile"];


// The record form this account may open now, or null. HQ may also reconcile (the server's rule: the owner or HQ,
// UI-SPEC open question 2), though the next action names the owner only.
function formFor(d: StepProps["d"], s: Session61): NextActionKey | null {
  const key = d.next_action.key;
  if (key === null || !RECORD_KEYS.includes(key)) return null;
  return named(d, s) || (key === "reconcile" && s.role === "hq") ? key : null;
}

const transfers = (v: ExecView) => v.work_orders.filter((w) => w.kind === "TRANSFER");
// The transfer the dispatch check is about: the first still PLANNED, else the latest (the view's own rule).
function target(v: ExecView): WorkOrder | null {
  const t = transfers(v);
  return t.find((w) => w.state === "PLANNED") ?? t[t.length - 1] ?? null;
}

// The server's blocks, shown only before the stock left: a prepared decision whose transfer is still to dispatch.
function preDispatchBlocks(d: StepProps["d"], v: ExecView | null): DispatchBlock[] {
  if (v === null || d.state !== "PREPARED") return [];
  const t = target(v);
  if (t === null || (t.state !== "PLANNED" && t.state !== "CANCELLED")) return [];
  return v.dispatch.blocks;
}

// --- the summary's primary: opens the record form ----------------------------------------------------------------------

function Primary({ d, session, go }: StepProps) {
  const opened = useOpen(d.case_id);
  const { view } = useExecutionStore(d.case_id);
  const key = formFor(d, session);
  if (key === null || opened === key) return null;
  // No Record dispatch while the server reports a block: the panel's banner says why (D-49).
  if (key === "record_dispatch" && (d.blocked_reasons.includes("dispatch_blocked") || preDispatchBlocks(d, view).length > 0)) {
    return null;
  }
  return (
    <Button kind="primary" onClick={() => { setOpen(d.case_id, key); go("EXECUTE"); }}>{NEXT_ACTION_LABEL[key]}</Button>
  );
}

// --- the panel ---------------------------------------------------------------------------------------------------------

function Panel(props: StepProps) {
  const { d, session } = props;
  const caseId = d.case_id;
  const { view, error } = useExecutionStore(caseId);
  const dep = `${d.state}|${d.next_action.key ?? ""}|${session.version}|${d.blocked_reasons.join(",")}`;
  useEffect(() => loadExecution(caseId), [caseId, dep]);
  useRefetchOnVisible(() => loadExecution(caseId));
  if (view === null) {
    return error === null
      ? <Loading text={LOADING_EXECUTION} />
      : <LoadError title={EXECUTION_DIDNT_LOAD} error={error} onRetry={() => loadExecution(caseId)} />;
  }
  const isTransfer = transfers(view).length > 0 || (view.work_orders.length === 0 && recommendedKind(d) === "TRANSFER");
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {error !== null && <ErrorLine error={error} action={{ label: TRY_AGAIN, onClick: () => loadExecution(caseId) }} />}
      <RecordPanel {...props} view={view} />
      <WorkList view={view} />
      <section aria-labelledby="transport-title" className="flex min-w-0 flex-col gap-2">
        <h3 id="transport-title" className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{TRANSPORT_DETAILS}</h3>
        {isTransfer
          ? <TransportPanel key={`${d.state}|${view.movements.length}`} caseId={caseId} />
          : <p className="m-0 text-body text-tx2">{NO_TRANSPORT}</p>}
      </section>
      {isTransfer && <p className="m-0 text-label text-tx2">{EXECUTE_LINE}</p>}
    </div>
  );
}

function recommendedKind(d: StepProps["d"]): string | null {
  const r = d.result;
  return r?.options.find((o) => o.key === r.recommended)?.kind ?? null;
}

// The record panel: the open form for the account that acts.
function RecordPanel(props: StepProps & { view: ExecView }) {
  const { d, session, view } = props;
  const opened = useOpen(d.case_id);
  const key = formFor(d, session);
  const form = key !== null && opened === key ? key : null;
  const blocks = preDispatchBlocks(d, view);
  const t = target(view);
  const sent = transfers(view).find((w) => w.state === "DISPATCHED") ?? null;
  const gaps = view.discrepancies.filter((g) => g.state === "OPEN");
  const settled = view.discrepancies.length > 0 && gaps.length === 0;
  // HQ reconciles from here: the summary's button is the owner's (the server's next action names the owner).
  const opener = key === "reconcile" && form === null && !named(d, session);
  const price = view.work_orders.find((w) => w.kind === "MARKDOWN" && w.state === "PLANNED") ?? null;
  const keep = view.work_orders.length === 0 && recommendedKind(d) === "KEEP";
  const ready = d.state === "PREPARED" && t !== null && t.state === "PLANNED" && blocks.length === 0;
  return (
    <div data-testid="record-panel" className="flex min-w-0 flex-col gap-4 empty:hidden">
      {keep && <p data-testid="keep-line" className="m-0 text-body text-tx">{KEEP_LINE}</p>}
      {blocks.length > 0 && <BlockedBanner blocks={blocks} />}
      {ready && form === null && <span><Pill tone="good" icon="check">{READY_TO_DISPATCH}</Pill></span>}
      {view.after_dispatch.length > 0 && <AfterDispatchBanner rows={view.after_dispatch} />}
      {gaps.length > 0 && (
        <DiscrepancyBanner view={view} gaps={gaps}>
          {opener && (
            <Button kind="primary" onClick={() => setOpen(d.case_id, "reconcile")}>{RECONCILE_DISCREPANCY}</Button>
          )}
        </DiscrepancyBanner>
      )}
      {settled && <span><Pill tone="gray" icon="check">{DISCREPANCY_RECONCILED}</Pill></span>}
      {form === "record_dispatch" && blocks.length === 0 && t !== null && <DispatchForm {...props} order={t} />}
      {form === "record_receipt" && sent !== null && <ReceiptForm {...props} order={sent} />}
      {form === "reconcile" && gaps.length > 0 && <ReconcileForm {...props} gaps={gaps} />}
      {form === "record_price" && price !== null && <PriceForm {...props} order={price} />}
    </div>
  );
}

// The work order's recorded units as the receipt left them: missing = dispatched − received − damaged (06.1-09).
function receiptOf(w: WorkOrder | undefined) {
  const dispatched = w?.dispatched_qty ?? 0;
  const received = w?.received_qty ?? 0;
  const damaged = w?.damaged_qty ?? 0;
  return { dispatched, received, damaged, missing: Math.max(dispatched - received - damaged, 0) };
}

// "Receipt discrepancy—reconciliation required." with the receipt's line and each open part (D-65). Shown to
// everyone who opens the decision; the form is the owner's or HQ's.
function DiscrepancyBanner({ view, gaps, children }: { view: ExecView; gaps: Discrepancy[]; children?: ReactNode }) {
  const order = view.work_orders.find((w) => w.action_id === gaps[0].action_id);
  const r = receiptOf(order);
  return (
    <div data-testid="discrepancy-banner" className="min-w-0">
      <Banner tone="bad" icon="alert" title={RECEIPT_DISCREPANCY}
        meta={receiptLine(units(r.dispatched), units(r.received), units(r.damaged), units(r.missing))}>
        <ul className="m-0 flex min-w-0 list-none flex-col gap-1 p-0">
          {gaps.map((g) => (
            <li key={g.discrepancy_id} className="num text-label break-words text-ink-2">
              {discrepancyOpen(g.kind, g.open_units, g.units)}
            </li>
          ))}
        </ul>
        {children}
      </Banner>
    </div>
  );
}

// --- form parts ------------------------------------------------------------------------------------------------------

const FIELD = "num h-11 w-full max-w-60 rounded-md border bg-white px-2 text-body md:h-8";
const fieldLook = (bad: boolean) => `${FIELD} ${bad ? "border-bad" : "border-line-2"}`;

// A whole number typed into a field, or null for empty or anything else (the server names the field it refuses).
function whole(text: string): number | null {
  const t = text.trim();
  return /^\d+$/.test(t) ? Number(t) : null;
}

// Which field a 400 names: the server's messages start with the field ("qty must be …", "reason is required …").
function fieldOf(error: ApiError | null, fields: readonly string[]): string | null {
  if (error === null || error.status !== 400) return null;
  const first = error.message.split(/\s/, 1)[0];
  return fields.includes(first) ? first : null;
}

function Label({ htmlFor, text, required = true }: { htmlFor: string; text: string; required?: boolean }) {
  return (
    <label htmlFor={htmlFor} className="text-body font-semibold text-tx">
      {text}{required && <span className="font-normal text-tx2"> {REQUIRED}</span>}
    </label>
  );
}

function ReadOnlyLot({ lot }: { lot: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-body font-semibold text-tx">{LOT}</span>
      <span data-testid="form-lot" className="font-mono text-body text-tx">{lot}</span>
    </div>
  );
}

function ConditionField({ id, value, onChange, choices, error }: {
  id: string; value: string; onChange: (v: string) => void; choices: ExecutionChoice[]; error: ApiError | null;
}) {
  const bad = error !== null;
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <Label htmlFor={id} text={CONDITION} />
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} aria-required="true"
        aria-invalid={bad || undefined} aria-describedby={bad ? `${id}-error` : undefined} className={fieldLook(bad)}>
        <option value="">{CHOOSE}</option>
        {choices.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
      </select>
      {bad && <ErrorLine id={`${id}-error`} error={error} />}
    </div>
  );
}

// The error under the submit: anything the fields don't own, with Try again for the retry group and "Reload the stock
// decision" for a 409 (the work order or the decision moved on since the form opened: the form closes and the
// execution view and the decision reload, so the server's state shows).
function FormError({ error, owned, retry, reload }: {
  error: ApiError | null; owned: boolean; retry: () => void; reload: () => void;
}) {
  if (error === null || owned) return null;
  const action = retryable(error.code) || error.status === 0 ? { label: TRY_AGAIN, onClick: retry }
    : error.status === 409 ? { label: RELOAD_DECISION, onClick: reload } : undefined;
  return <ErrorLine error={error} action={action} />;
}

function useFocusFirst() {
  const ref = useRef<HTMLFormElement>(null);
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>("input:not([type=checkbox]), select, textarea, input")?.focus();
  }, []);
  return ref;
}

function after(props: StepProps) {
  setOpen(props.d.case_id, null);
  loadExecution(props.d.case_id);
  props.reload();
}

// A form's success step, the same after the first press and after a Try again that succeeds (D-24, D-67): nothing
// when the write did not succeed (undefined), else the form closes and the execution view and the decision reload.
const finish = (props: StepProps) => (out: unknown) => {
  if (out !== undefined) after(props);
};

// --- Record dispatch (the source manager) ------------------------------------------------------------------------------

function DispatchForm(props: StepProps & { view: ExecView; order: WorkOrder }) {
  const { d, view, order } = props;
  const [qty, setQty] = useState("");
  const [condition, setCondition] = useState("");
  const ids = useId();
  const ref = useFocusFirst();
  const act = useAction(async (body: Parameters<typeof recordDispatch>[1], key: string) => {
    try {
      return await recordDispatch(d.case_id, body, key);
    } catch (e) {
      if (e instanceof ApiError && e.code === "dispatch_blocked") loadExecution(d.case_id);
      throw e;
    }
  });
  const committed = order.qty ?? 0;
  const n = whole(qty);
  const store = order.origin ?? d.source;
  const err = act.error;
  const field = fieldOf(err, ["qty", "condition"]);
  const done = finish(props);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const out = await act.run({ action_id: order.action_id, qty: n ?? Number.NaN, lot_id: order.lot_id, condition });
    done(out);
  };
  const qtyId = `${ids}-qty`;
  return (
    <form ref={ref} aria-label={RECORD_DISPATCH} noValidate onSubmit={(e) => void submit(e)}
      className="flex min-w-0 flex-col gap-4 rounded-xl border border-line p-4">
      <fieldset disabled={act.busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
        <div className="flex min-w-0 flex-col gap-1">
          <Label htmlFor={qtyId} text={UNITS_DISPATCHED} />
          <input id={qtyId} type="number" inputMode="numeric" min={1} max={committed} step={1} value={qty}
            onChange={(e) => setQty(e.target.value)} aria-required="true" aria-invalid={field === "qty" || undefined}
            aria-describedby={field === "qty" ? `${qtyId}-help ${qtyId}-error` : `${qtyId}-help`} className={fieldLook(field === "qty")} />
          <p id={`${qtyId}-help`} className="num m-0 text-label text-tx3">{UP_TO_COMMITTED(committed)}</p>
          {field === "qty" && <ErrorLine id={`${qtyId}-error`} error={err} />}
        </div>
        <ReadOnlyLot lot={order.lot_id} />
        <ConditionField id={`${ids}-condition`} value={condition} onChange={setCondition} choices={view.options.conditions}
          error={field === "condition" ? err : null} />
        <div className="flex flex-wrap items-center gap-2">
          <Button kind="primary" type="submit" busy={act.busy} aria-describedby={`${ids}-desc`}>{RECORD_DISPATCH}</Button>
          <Button kind="ghost" onClick={() => setOpen(d.case_id, null)}>{CLOSE_FORM}</Button>
          <span id={`${ids}-desc`} className="sr-only">{DISPATCH_DESCRIPTION(n === null ? null : units(n), store)}</span>
        </div>
      </fieldset>
      <FormError error={err} owned={field !== null} retry={() => void act.retry().then(done)}
        reload={() => after(props)} />
    </form>
  );
}

// --- the dispatch blocks (D-49) and after-dispatch evidence (D-35) -----------------------------------------------------

const BLOCK_ICON: Readonly<Record<DispatchBlockKind, IconName>> = {
  wrong_state: "alert", cancelled: "x", lot_hold: "lock", review_flag: "flag", recheck: "alert",
  checks_unavailable: "alert",
};

// "Dispatch blocked": one row per block with the server's reason and resolution word for word and its owner. It
// replaces the dispatch form; missing checks are a block like any other, never "clear".
function BlockedBanner({ blocks }: { blocks: DispatchBlock[] }) {
  return (
    <div data-testid="dispatch-blocked" className="min-w-0">
      <Banner tone="bad" icon="lock" title={DISPATCH_BLOCKED}>
        <ul className="m-0 flex min-w-0 list-none flex-col gap-2 p-0">
          {blocks.map((b, i) => (
            <li key={`${b.kind}-${b.id ?? i}`} data-testid="dispatch-block" className="flex min-w-0 items-start gap-2">
              <span className="inline-flex h-5 items-center text-bad"><Icon name={BLOCK_ICON[b.kind] ?? "alert"} size={14} /></span>
              <span className="flex min-w-0 flex-col">
                <span className="text-body break-words text-ink">
                  {b.kind === "lot_hold" && <span className="font-semibold">{LOT_HOLD} · </span>}{b.reason}
                </span>
                <span className="text-label break-words text-ink-2">{resolveLine(b.resolution)}</span>
                {b.owner_branch !== null && <span className="text-label break-words text-ink-2">{blockOwner(b.owner_branch)}</span>}
              </span>
            </li>
          ))}
        </ul>
      </Banner>
    </div>
  );
}

// New confirmed information after the stock left: an operational exception that blocks and recalls nothing (D-35).
function AfterDispatchBanner({ rows }: { rows: ExecutionView["after_dispatch"] }) {
  return (
    <div data-testid="after-dispatch" className="min-w-0">
      <Banner tone="warn" icon="flag" title={AFTER_DISPATCH_REVIEW} meta={AFTER_DISPATCH_LINE}>
        <ul className="m-0 flex min-w-0 list-none flex-col gap-1 p-0">
          {rows.map((r, i) => (
            <li key={String(r.flag_id ?? i)} className="text-label break-words text-ink-2">{String(r.reason ?? "")}</li>
          ))}
        </ul>
      </Banner>
    </div>
  );
}

// --- Record price change (the source manager) ---------------------------------------------------------------------------

function PriceForm(props: StepProps & { view: ExecView; order: WorkOrder }) {
  const { d, order } = props;
  const [ticked, setTicked] = useState(false);
  const ids = useId();
  const ref = useFocusFirst();
  const act = useAction((body: Parameters<typeof recordPrice>[1], key: string) => recordPrice(d.case_id, body, key));
  const done = finish(props);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ticked) return;
    const out = await act.run({ action_id: order.action_id, confirmed: true });
    done(out);
  };
  const err = act.error;
  return (
    <form ref={ref} aria-label={RECORD_PRICE} noValidate onSubmit={(e) => void submit(e)}
      className="flex min-w-0 flex-col gap-4 rounded-xl border border-line p-4">
      <fieldset disabled={act.busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
        <p className="m-0 text-body break-words text-tx">{markdownLine(order.lot_id, order.pct ?? 0, order.origin ?? d.source)}</p>
        <label className="flex min-w-0 items-start gap-2 text-body text-tx max-md:min-h-11">
          <input type="checkbox" checked={ticked} onChange={(e) => setTicked(e.target.checked)} className="mt-1 h-4 w-4 shrink-0" />
          <span>{PRICE_TICK}</span>
        </label>
        <div className="flex flex-col items-start gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <Button kind="primary" type="submit" busy={act.busy} disabled={!ticked}
              aria-describedby={ticked ? undefined : `${ids}-tick`}>{RECORD_PRICE}</Button>
            <Button kind="ghost" onClick={() => setOpen(d.case_id, null)}>{CLOSE_FORM}</Button>
          </div>
          {!ticked && <p id={`${ids}-tick`} className="m-0 text-label text-tx2">{TICK_TO_RECORD}</p>}
        </div>
      </fieldset>
      <FormError error={err} owned={false} retry={() => void act.retry().then(done)}
        reload={() => after(props)} />
    </form>
  );
}

// --- Record receipt (the destination manager) ---------------------------------------------------------------------------

function ReceiptForm(props: StepProps & { view: ExecView; order: WorkOrder }) {
  const { d, view, order } = props;
  const [received, setReceived] = useState("");
  const [damaged, setDamaged] = useState("");
  const [condition, setCondition] = useState("");
  const [reason, setReason] = useState("");
  const ids = useId();
  const ref = useFocusFirst();
  const act = useAction((body: Parameters<typeof recordReceipt>[1], key: string) => recordReceipt(d.case_id, body, key));
  const sent = order.dispatched_qty ?? 0;
  const r = whole(received);
  const x = whole(damaged);
  const missing = r === null || x === null ? null : sent - r - x;
  const opens = (x !== null && x > 0) || (missing !== null && missing > 0);
  const err = act.error;
  const field = fieldOf(err, ["received_qty", "damaged_qty", "condition", "reason"]);
  const done = finish(props);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const out = await act.run({
      action_id: order.action_id, received_qty: r ?? Number.NaN, damaged_qty: x ?? Number.NaN, condition, reason,
    });
    done(out);
  };
  const num = (id: string, label: string, value: string, set: (v: string) => void, key: string) => {
    const bad = field === key;
    return (
      <div className="flex min-w-0 flex-col gap-1">
        <Label htmlFor={id} text={label} />
        <input id={id} type="number" inputMode="numeric" min={0} step={1} value={value}
          onChange={(e) => set(e.target.value)} aria-required="true" aria-invalid={bad || undefined}
          aria-describedby={bad ? `${id}-error` : undefined} className={fieldLook(bad)} />
        {bad && <ErrorLine id={`${id}-error`} error={err} />}
      </div>
    );
  };
  const reasonId = `${ids}-reason`;
  const badReason = field === "reason";
  return (
    <form ref={ref} aria-label={RECORD_RECEIPT} noValidate onSubmit={(e) => void submit(e)}
      className="flex min-w-0 flex-col gap-4 rounded-xl border border-line p-4">
      <fieldset disabled={act.busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
        <div className="grid min-w-0 gap-4 md:grid-cols-2">
          {num(`${ids}-received`, UNITS_RECEIVED, received, setReceived, "received_qty")}
          {num(`${ids}-damaged`, UNITS_DAMAGED, damaged, setDamaged, "damaged_qty")}
        </div>
        <div data-testid="receipt-summary" aria-live="polite" className="flex min-w-0 flex-col gap-1">
          <p className="num m-0 text-label break-words text-tx2">
            {r === null || x === null || missing === null || missing < 0
              ? `Dispatched ${units(sent)}`
              : receiptLine(units(sent), units(r), units(x), units(missing))}
          </p>
          {/* The bar only: its numbers are the line above (R-6). */}
          <div aria-hidden="true" className="[&_.num]:hidden">
            <QtyBar committed={order.qty ?? sent} dispatched={sent}
              received={r !== null && x !== null && missing !== null && missing >= 0 ? r : null}
              damaged={r !== null && x !== null && missing !== null && missing >= 0 ? x : null} className="w-full max-w-60" />
          </div>
          {opens && <p className="m-0 rounded-md bg-warn-soft px-2 py-1 text-label break-words text-tx">{OPENS_DISCREPANCY}</p>}
          {opens && <span className="sr-only">{REASON_NOW_REQUIRED}</span>}
        </div>
        <ReadOnlyLot lot={order.new_lot_id ?? order.lot_id} />
        <ConditionField id={`${ids}-condition`} value={condition} onChange={setCondition} choices={view.options.conditions}
          error={field === "condition" ? err : null} />
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor={reasonId} className="text-body font-semibold text-tx">{RECEIPT_REASON}</label>
          <textarea id={reasonId} rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
            aria-required={opens ? "true" : "false"} aria-invalid={badReason || undefined}
            aria-describedby={badReason ? `${reasonId}-error` : undefined}
            className={`w-full max-w-xl rounded-md border bg-white px-2 py-1 text-body ${badReason ? "border-bad" : "border-line-2"}`} />
          {badReason && <ErrorLine id={`${reasonId}-error`} error={err} />}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button kind="primary" type="submit" busy={act.busy}>{RECORD_RECEIPT}</Button>
          <Button kind="ghost" onClick={() => setOpen(d.case_id, null)}>{CLOSE_FORM}</Button>
        </div>
      </fieldset>
      <FormError error={err} owned={field !== null} retry={() => void act.retry().then(done)}
        reload={() => after(props)} />
    </form>
  );
}

// --- Reconcile discrepancy (the owner or HQ) ----------------------------------------------------------------------------

function ReconcileForm(props: StepProps & { view: ExecView; gaps: Discrepancy[] }) {
  const { d, view, gaps } = props;
  const [gapId, setGapId] = useState(gaps[0].discrepancy_id);
  const [resolution, setResolution] = useState("");
  const [count, setCount] = useState("");
  const [reason, setReason] = useState("");
  const ids = useId();
  const ref = useFocusFirst();
  const act = useAction((body: Parameters<typeof reconcile>[1], key: string) => reconcile(d.case_id, body, key));
  // The draft is bound to the chosen discrepancy by id (quick 261018-4fi): when it is no longer open (reconciled in
  // another window), the form never falls back to another one; it clears the draft and waits for a new choice.
  const gap = gaps.find((g) => g.discrepancy_id === gapId) ?? null;
  const gone = gap === null;
  const { busy, clear } = act;
  useEffect(() => {
    if (!gone || busy) return;
    setResolution("");
    setCount("");
    setReason("");
    clear(); // neither Record nor Try again can resend the old draft
  }, [gone, busy, clear]);
  // A new pick starts that discrepancy's own draft (Codex check 4fi): the stored attempt goes, so Try again never
  // resends the previous discrepancy's press, and a draft typed for a discrepancy still open is not carried over.
  // After the line above, the draft is already empty; what was typed since is kept for the new pick.
  const pick = (id: string) => {
    if (id === gapId) return;
    clear();
    if (!gone) {
      setResolution("");
      setCount("");
      setReason("");
    }
    setGapId(id);
  };
  const n = whole(count);
  const err = act.error;
  const field = fieldOf(err, ["resolution", "units", "reason"]);
  const done = finish(props);
  const gapField = `${ids}-gap`;
  const goneId = `${ids}-gone`;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (gap === null) {
      document.getElementById(gapField)?.focus();
      return;
    }
    const out = await act.run({ discrepancy_id: gap.discrepancy_id, resolution, units: n ?? Number.NaN, reason });
    done(out);
  };
  const select = (id: string, label: string, value: string, set: (v: string) => void, choices: ExecutionChoice[], key: string,
    empty: boolean, note?: string) => {
    const bad = field === key;
    const described = [note, bad ? `${id}-error` : undefined].filter(Boolean).join(" ") || undefined;
    return (
      <div className="flex min-w-0 flex-col gap-1">
        <Label htmlFor={id} text={label} />
        <select id={id} value={value} onChange={(e) => set(e.target.value)} aria-required="true"
          aria-invalid={bad || undefined} aria-describedby={described} className={fieldLook(bad)}>
          {empty && <option value="">{CHOOSE}</option>}
          {choices.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
        </select>
        {bad && <ErrorLine id={`${id}-error`} error={err} />}
      </div>
    );
  };
  const unitsId = `${ids}-units`;
  const reasonId = `${ids}-reason`;
  const unitsHelp = gap === null ? undefined : `${unitsId}-help`;
  const unitsDescribed = [unitsHelp, field === "units" ? `${unitsId}-error` : undefined].filter(Boolean).join(" ") || undefined;
  return (
    <form ref={ref} aria-label={RECONCILE_DISCREPANCY} noValidate onSubmit={(e) => void submit(e)}
      className="flex min-w-0 flex-col gap-4 rounded-xl border border-line p-4">
      <fieldset disabled={act.busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
        {gone && <ErrorLine id={goneId} error={DISCREPANCY_GONE} />}
        {(gaps.length > 1 || gone) && select(gapField, DISCREPANCY, gap?.discrepancy_id ?? "", pick,
          gaps.map((g) => ({ key: g.discrepancy_id, label: discrepancyOpen(g.kind, g.open_units, g.units) })), "discrepancy_id",
          gone, gone ? goneId : undefined)}
        {select(`${ids}-resolution`, RESOLUTION, resolution, setResolution, view.options.resolutions, "resolution", true)}
        <div className="flex min-w-0 flex-col gap-1">
          <Label htmlFor={unitsId} text={UNITS} />
          <input id={unitsId} type="number" inputMode="numeric" min={1} max={gap?.open_units} step={1} value={count}
            onChange={(e) => setCount(e.target.value)} aria-required="true" aria-invalid={field === "units" || undefined}
            aria-describedby={unitsDescribed} className={fieldLook(field === "units")} />
          {gap !== null && <p id={unitsHelp} className="num m-0 text-label text-tx3">{UP_TO_OPEN(gap.open_units)}</p>}
          {field === "units" && <ErrorLine id={`${unitsId}-error`} error={err} />}
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <Label htmlFor={reasonId} text={REASON} />
          <textarea id={reasonId} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} aria-required="true"
            aria-invalid={field === "reason" || undefined} aria-describedby={field === "reason" ? `${reasonId}-error` : undefined}
            className={`w-full max-w-xl rounded-md border bg-white px-2 py-1 text-body ${field === "reason" ? "border-bad" : "border-line-2"}`} />
          {field === "reason" && <ErrorLine id={`${reasonId}-error`} error={err} />}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button kind="primary" type="submit" busy={act.busy}>{RECORD_RECONCILIATION}</Button>
          <Button kind="ghost" onClick={() => setOpen(d.case_id, null)}>{CLOSE_FORM}</Button>
        </div>
      </fieldset>
      <FormError error={err} owned={field !== null} retry={() => void act.retry().then(done)}
        reload={() => after(props)} />
    </form>
  );
}

// --- the work list -------------------------------------------------------------------------------------------------------

// The movement state a work order's recorded movements give (the chip from transport.tsx, D-33).
function movementState(w: WorkOrder): MovementState | null {
  switch (w.state) {
    case "PLANNED": return "planned";
    case "DISPATCHED": return "dispatched";
    case "RECEIVED": {
      const short = (w.received_qty ?? 0) + (w.damaged_qty ?? 0) < (w.dispatched_qty ?? 0) || (w.damaged_qty ?? 0) > 0;
      return short ? "partially_received" : "received";
    }
    case "CANCELLED": return "cancelled";
    case "EXPIRED": return "expired";
    default: return null;
  }
}

function WorkList({ view }: { view: ExecView }) {
  const { session } = useSession();
  const cal = session.calendar;
  const last = (actionId: string) => {
    const mine = view.movements.filter((m) => m.action_id === actionId && m.recorded_at !== null);
    return mine.length === 0 ? null : mine[mine.length - 1];
  };
  return (
    <section aria-labelledby="work-list-title" className="flex min-w-0 flex-col gap-2">
      <h3 id="work-list-title" className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{WORK_LIST}</h3>
      {view.work_orders.length === 0
        ? <p className="m-0 text-body text-tx2">{NO_WORK_ORDERS}</p>
        : (
          <ul className="m-0 flex min-w-0 list-none flex-col gap-2 p-0">
            {view.work_orders.map((w) => {
              const day = w.kind === "TRANSFER" ? w.run_day : w.effective_day;
              const when = day === null ? UNSCHEDULED : plannedOn(dateLabel(cal, day));
              const task = w.kind === "TRANSFER"
                ? transferTask(units(w.qty ?? 0), w.lot_id, w.origin ?? "", w.dest ?? "")
                : markdownTask(w.lot_id, w.pct ?? 0, w.origin ?? "");
              const m = last(w.action_id);
              const state = movementState(w);
              return (
                <li key={w.action_id} data-testid={`work-${w.action_id}`}
                  className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-1 rounded-lg border border-line px-3 py-2 md:grid-cols-[112px_1fr_auto] md:items-center">
                  <span className="num text-label text-tx2">{when}</span>
                  <span className="min-w-0 text-body break-words text-tx">
                    <span className="mr-1 font-mono text-label text-tx3">{w.action_id}</span>{task}
                    {m !== null && m.recorded_at !== null && (
                      <span className="num block text-label text-tx2">{recordedLine(m.recorded_at, session.time_zone)}</span>
                    )}
                  </span>
                  <span className="min-w-0">
                    {w.state === "DONE"
                      ? <Pill tone="good" icon="check">{DONE}</Pill>
                      : <MovementChip state={state} committed={w.qty} dispatched={w.dispatched_qty} />}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
    </section>
  );
}

export const step: StepModule = { key: "EXECUTE", Panel, Primary };
