// Step 8 · Verify (06.1-UI-SPEC "Verify and close"; 06.1-CONTEXT D-34, D-65, D-67): what the engine expected at decision
// time beside what was recorded, the receipt, and Close by the owner with a closing note.
// Rules kept here:
// - Expected and Actual are separate boxes. Expected is the engine's estimate (dashed, "Simulated"); Actual reads "Not
//   yet recorded" until outcomes are recorded, so nothing simulated is ever shown as observed (T-06.1-80, PROD-11).
// - The receipt's quantities are the execution view's recorded ones (QtyBar); an open receipt discrepancy shows its
//   chip and the bad banner "Receipt discrepancy—reconciliation required." in place of Close (D-65, T-06.1-81).
// - Close is offered only while the server's next action is `close` and names this account (the owner); Reconcile is
//   offered (as the way to Execute, where its form lives) only while the next action is `reconcile` for this account.
//   The server checks again: its 403 or 409 reason shows word for word; 409 discrepancy_open closes the form and
//   shows the discrepancy banner. One Idempotency-Key per press, kept for Try again (hooks.useAction).
// - The form state is kept per decision in a small external store, because the summary's Primary opens the form that
//   this panel draws (they are separate components of the page).
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useEffect, useId, useRef, useState, useSyncExternalStore, type FormEvent, type ReactNode } from "react";
import { ApiError } from "../../api.ts";
import { closeDecision, getExecution, type CaseDetail61, type Discrepancy, type ExecutionView, type Session61 } from "../../contract.ts";
import { retryable } from "../../copy.ts";
import { expected, markDate, money, units } from "../../format.ts";
import { useAction } from "../../hooks.ts";
import { useSession } from "../../shell.tsx";
import { Banner, Button, ErrorLine, KindChip, Loading, Pill, QtyBar, TRY_AGAIN } from "../../ui.tsx";
import {
  DISCREPANCY_RECONCILED, NEXT_ACTION_LABEL, NOT_YET_RECORDED, RECEIPT_DISCREPANCY, RECEIPT_DISCREPANCY_BADGE, receiptLine,
} from "../../vocab.ts";
import { decisionDate, named } from "../shared.ts";
import type { StepModule, StepProps } from "../types.ts";

export const EXPECTED_TITLE = "Expected";
export const ACTUAL_TITLE = "Actual";
export const RECEIPT_TITLE = "Receipt";
export const EXPECTED_LINE = (net: string, wasted: string) => `${net} net · ${wasted} units wasted`;
export const EXPECTED_SOURCE = "Engine estimate at decision time";
export const NO_ESTIMATE = "No engine estimate for this stock decision."; // (proposed) no recommendation to compare
export const ACTUAL_WHEN = (date: string) => // (proposed)
  `Appears when sales after ${date} are recorded or a new dataset is loaded.`;
export const NO_DISCREPANCY = "No discrepancy";
export const NO_RECEIPT = "Nothing to receive: this plan moves no stock between branches."; // (proposed)
export const LOADING_RECEIPT = "Loading the receipt…"; // (proposed)
export const CLOSE_LABEL = NEXT_ACTION_LABEL.close; // "Close stock decision"
export const RECONCILE_LABEL = NEXT_ACTION_LABEL.reconcile; // "Reconcile discrepancy" (proposed in vocab.ts)
export const CLOSING_NOTE = "Closing note";
export const KEEP_OPEN = "Keep it open"; // (proposed)
export const NOTE_REQUIRED = "Write a closing note to close this stock decision."; // (proposed)
// The banner's meta line (UI-SPEC "Open discrepancy"): "Dispatched 110 · Received 104 · Damaged 0 · Missing 6".
export const DISCREPANCY_META = (q: { dispatched: number; received: number; damaged: number; missing: number }) =>
  receiptLine(units(q.dispatched), units(q.received), units(q.damaged), units(q.missing));

// --- the close form's state, kept per decision (the summary's Primary opens it, this panel draws it) ----------------

type CloseState = { open: boolean; refusal: ApiError | null; focusPrimary: boolean };
const CLOSED_FORM: CloseState = { open: false, refusal: null, focusPrimary: false };
const forms = new Map<string, CloseState>();
const listeners = new Set<() => void>();
function setForm(caseId: string, next: Partial<CloseState>) {
  forms.set(caseId, { ...(forms.get(caseId) ?? CLOSED_FORM), ...next });
  for (const f of listeners) f();
}
function subscribe(f: () => void) {
  listeners.add(f);
  return () => { listeners.delete(f); };
}
function useCloseForm(caseId: string): CloseState {
  return useSyncExternalStore(subscribe, () => forms.get(caseId) ?? CLOSED_FORM);
}

// --- who may act (the server's next action; the server checks again) -----------------------------------------------

const mayClose = (d: CaseDetail61, s: Session61) => d.next_action.key === "close" && named(d, s);
const mayReconcile = (d: CaseDetail61, s: Session61) => d.next_action.key === "reconcile" && named(d, s);

// --- the execution view (receipt quantities and discrepancies) ------------------------------------------------------

type ExecutionLoad = { view: ExecutionView | null; error: ApiError | null; reload: () => void };

function useExecution(d: CaseDetail61): ExecutionLoad {
  const [view, setView] = useState<ExecutionView | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    getExecution(d.case_id).then(
      (v) => { if (live) { setView(v); setError(null); } },
      (e) => { if (live) setError(e instanceof ApiError ? e : new ApiError(0, "network", String(e))); },
    );
    return () => { live = false; };
  }, [d, tick]); // a reloaded detail is a new object: the receipt is read again with it
  return { view, error, reload: () => setTick((t) => t + 1) };
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const openOnes = (v: ExecutionView | null): Discrepancy[] => (v?.discrepancies ?? []).filter((x) => x.state === "OPEN");

// The transfer work orders whose stock left the source (dispatched or received), in the server's round order.
function movedTransfers(v: ExecutionView) {
  return v.work_orders
    .filter((w) => w.kind === "TRANSFER" && num(w.dispatched_qty) !== null)
    .map((w) => ({
      id: String(w.action_id),
      committed: num(w.qty) ?? 0,
      dispatched: num(w.dispatched_qty) as number,
      received: num(w.received_qty),
      damaged: num(w.damaged_qty),
    }));
}

// --- the boxes ------------------------------------------------------------------------------------------------------

function Box({ title, chip, dashed = false, testId, children }: {
  title: string; chip?: ReactNode; dashed?: boolean; testId: string; children: ReactNode;
}) {
  const frame = dashed ? "border border-dashed border-plan-line bg-page" : "border border-line bg-white";
  return (
    <div data-testid={testId} className={`flex min-w-0 flex-col gap-2 rounded-xl p-4 ${frame}`}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{title}</h3>
        {chip}
      </div>
      {children}
    </div>
  );
}

function Expected({ d, currency }: { d: CaseDetail61; currency: string }) {
  const r = d.result;
  const rec = r === null ? undefined : r.options.find((o) => o.key === r.recommended);
  return (
    <Box title={EXPECTED_TITLE} chip={<KindChip kind="simulated" />} dashed testId="verify-expected">
      {rec === undefined
        ? <p className="m-0 text-body text-tx2">{NO_ESTIMATE}</p>
        : <p className="num m-0 text-display font-semibold break-words text-tx">
          {EXPECTED_LINE(money(rec.net.base, currency), expected(rec.wasted_units))}
        </p>}
      <p className="m-0 text-label text-tx2">{EXPECTED_SOURCE}</p>
    </Box>
  );
}

function Actual({ date }: { date: string }) {
  return (
    <Box title={ACTUAL_TITLE} testId="verify-actual">
      <p className="m-0 text-display font-semibold text-tx3">{NOT_YET_RECORDED}</p>
      {date !== "" && <p className="num m-0 text-label text-tx2">{ACTUAL_WHEN(markDate(date))}</p>}
    </Box>
  );
}

function Receipt({ load, refused }: { load: ExecutionLoad; refused: boolean }) {
  const { view, error, reload } = load;
  if (view === null) {
    return (
      <Box title={RECEIPT_TITLE} testId="verify-receipt">
        {error === null ? <Loading text={LOADING_RECEIPT} />
          : <ErrorLine error={error} action={retryable(error.code) || error.status === 0 ? { label: TRY_AGAIN, onClick: reload } : undefined} />}
      </Box>
    );
  }
  const moved = movedTransfers(view);
  const open = openOnes(view).length > 0 || refused;
  const settled = view.discrepancies.length > 0 && !open;
  const chip = open ? <Pill tone="bad" icon="alert">{RECEIPT_DISCREPANCY_BADGE}</Pill>
    : settled ? <Pill tone="good" icon="check">{DISCREPANCY_RECONCILED}</Pill>
    : moved.length > 0 ? <Pill tone="good" icon="check">{NO_DISCREPANCY}</Pill>
    : null;
  return (
    <Box title={RECEIPT_TITLE} testId="verify-receipt">
      {moved.length === 0
        ? <p className="m-0 text-body text-tx2">{NO_RECEIPT}</p>
        : moved.map((t) => (
          <div key={t.id} className="min-w-0">
            <QtyBar committed={t.committed} dispatched={t.dispatched} received={t.received} damaged={t.damaged} className="w-full max-w-60" />
          </div>
        ))}
      {chip !== null && <span>{chip}</span>}
    </Box>
  );
}

// The bad banner of an open discrepancy, with the way to Execute's Reconcile form for the people the server names.
function DiscrepancyBanner({ d, session, view }: { d: CaseDetail61; session: Session61; view: ExecutionView | null }) {
  const gaps = openOnes(view);
  const t = view === null ? undefined : movedTransfers(view).find((m) => gaps.some((g) => g.action_id === m.id));
  const meta = t === undefined || t.received === null ? undefined : DISCREPANCY_META({
    dispatched: t.dispatched, received: t.received, damaged: t.damaged ?? 0,
    missing: Math.max(0, t.dispatched - t.received - (t.damaged ?? 0)),
  });
  return (
    <Banner tone="bad" icon="alert" title={RECEIPT_DISCREPANCY} meta={meta}>
      {mayReconcile(d, session) && (
        <a href={`#/cases/${encodeURIComponent(d.case_id)}?step=execute`}
          className="inline-flex min-h-8 items-center rounded-md border border-line-2 bg-white px-4 text-body font-semibold text-tx hover:bg-side max-md:min-h-11">
          {RECONCILE_LABEL}
        </a>
      )}
    </Banner>
  );
}

// --- Close: the summary's button and the panel's inline form ---------------------------------------------------------

function Primary({ d, session, go }: StepProps) {
  const form = useCloseForm(d.case_id);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!form.focusPrimary) return;
    button.current?.focus();
    setForm(d.case_id, { focusPrimary: false });
  }, [form.focusPrimary, d.case_id]);
  if (!mayClose(d, session) || form.open || form.refusal?.code === "discrepancy_open") return null;
  return (
    <Button ref={button} kind="primary" onClick={() => { setForm(d.case_id, { open: true, refusal: null }); go("VERIFY"); }}>
      {CLOSE_LABEL}
    </Button>
  );
}

function CloseForm({ d, reload, go }: Pick<StepProps, "d" | "reload" | "go">) {
  const { refresh } = useSession();
  const noteId = useId();
  const helpId = useId();
  const [note, setNote] = useState("");
  const [empty, setEmpty] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { field.current?.focus(); }, []);
  const act = useAction((body: { reason: string }, key: string) => closeDecision(d.case_id, body, key));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const reason = note.trim();
    if (reason === "") {
      setEmpty(true);
      field.current?.focus();
      return;
    }
    await done(await act.run({ reason }));
  };
  // The success step, the same after the first press and after a Try again that succeeds (D-24, D-67).
  async function done(out: unknown) {
    if (out === undefined) return;
    setForm(d.case_id, { open: false, refusal: null });
    go("ARCHIVE"); // the closing line and the full history
    reload();
    await refresh().catch(() => {}); // the next page load or write shows a session failure
  }
  const err = act.error;
  useEffect(() => {
    if (err === null || retryable(err.code) || err.status === 0) return;
    // A refusal the page could not foresee: the server's reason stays on the panel, the form closes, the decision
    // reloads (409 discrepancy_open then shows the banner, D-65).
    setForm(d.case_id, { open: false, refusal: err });
    reload();
  }, [err, d.case_id, reload]);
  return (
    <form data-testid="close-form" noValidate onSubmit={(e) => void submit(e)}
      className="flex min-w-0 flex-col gap-2 rounded-xl border border-line bg-white p-4">
      <label htmlFor={noteId} className="text-label font-semibold text-tx">{CLOSING_NOTE}</label>
      <textarea id={noteId} ref={field} required value={note} rows={3} aria-invalid={empty || undefined}
        aria-describedby={empty ? helpId : undefined}
        onChange={(e) => { setNote(e.target.value); if (e.target.value.trim() !== "") setEmpty(false); }}
        className="min-w-0 rounded-md border border-line-2 bg-white p-2 text-body text-tx" />
      {empty && <p id={helpId} className="m-0 text-label text-bad">{NOTE_REQUIRED}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="submit" kind="primary" busy={act.busy}>{CLOSE_LABEL}</Button>
        <Button kind="ghost" onClick={() => { act.clear(); setForm(d.case_id, { open: false, focusPrimary: true }); }}>{KEEP_OPEN}</Button>
      </div>
      {err !== null && (retryable(err.code) || err.status === 0) && (
        <ErrorLine error={err} action={{ label: TRY_AGAIN, onClick: () => void act.retry().then(done) }} />
      )}
    </form>
  );
}

function Panel({ d, session, reload, go }: StepProps) {
  const { session: plain } = useSession();
  const form = useCloseForm(d.case_id);
  const load = useExecution(d);
  const refusal = form.refusal;
  const refusedOpen = refusal?.code === "discrepancy_open";
  const open = openOnes(load.view).length > 0 || refusedOpen;
  // A refusal belongs to the state it answered. Another reason clears once the server no longer offers Close here; a
  // discrepancy refusal clears once the receipt's discrepancies read reconciled (the server's own record then).
  const view = load.view;
  useEffect(() => {
    if (refusal === null) return;
    const reconciled = view !== null && view.discrepancies.length > 0 && openOnes(view).length === 0;
    if (refusedOpen ? reconciled : !mayClose(d, session)) setForm(d.case_id, { refusal: null });
  }, [refusal, refusedOpen, view, d, session]);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {open && <DiscrepancyBanner d={d} session={session} view={load.view} />}
      <div className="grid min-w-0 grid-cols-[repeat(auto-fit,minmax(min(256px,100%),1fr))] gap-4">
        <Expected d={d} currency={plain.currency} />
        <Actual date={decisionDate(d, session)} />
        <Receipt load={load} refused={refusedOpen} />
      </div>
      {refusal !== null && !refusedOpen && <ErrorLine error={refusal} />}
      {form.open && !open && mayClose(d, session) && <CloseForm d={d} reload={reload} go={go} />}
    </div>
  );
}

export const step: StepModule = { key: "VERIFY", Panel, Primary };
