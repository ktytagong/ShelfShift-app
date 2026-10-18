// Step 6 · Commit (06.1-UI-SPEC "Step panel" row 6, "Primary action by state and role"; 06.1-CONTEXT D-26, D-61): what
// happens when both sides approve (the recheck, the bookings, the work order), "Booking confirmed." once the round
// committed, the work order line once it exists, and the source manager's Prepare.
// Rules kept here:
// - Prepare is offered while the server's next action is prepare for this account (the source store's manager); its
//   wording follows the recommended plan's kind. The server rechecks the plan: a conflict shows its reasons word for
//   word and writes nothing.
// - The work order line reads the server's work orders (GET /api/cases/{id}/execution); nothing here invents one.
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useCallback, useEffect, useState } from "react";
import { ApiError, NETWORK } from "../../api.ts";
import { getExecution, type WorkOrder } from "../../contract.ts";
import { retryable } from "../../copy.ts";
import { units } from "../../format.ts";
import { Button, ErrorLine, TRY_AGAIN } from "../../ui.tsx";
import { BOOKING_CONFIRMED, MARKDOWN_WORK_ORDER, TERMS } from "../../vocab.ts";
import { BookingLine, WriteError, isCommitted, prepareWrite, useRefetchOnFocus, useStepWrite } from "../approval.tsx";
import type { StepModule, StepProps } from "../types.ts";
// 07-21: Prepare's reason while something blocks commitment (07-UI-SPEC "Approve step"; 07-CONTEXT D-12).
import { cantPrepareYet } from "../../copy.ts";
import { contextHref, firstBlocker } from "../commitment.ts";
// 07-21: Cancel approved plan… beside Prepare (07-CONTEXT D-23, D-36; the server lists it in commitment.next_actions).
import { isMe } from "../../derive.ts";
import { CANCEL_OPEN, ReasonForm, cancelWrite, useFormOpener } from "../approval.tsx";

// --- strings ---------------------------------------------------------------------------------------------------------

// (proposed, from sketch 006) the re-check paragraph
export const COMMIT_TEXT = "When both sides approve, the plan is checked again against the current records and limits. "
  + "If it still holds, the bookings are confirmed and a work order is created. If the facts changed, a new plan "
  + "version cancels the approvals and they are asked again.";
export const PREPARE_TRANSFER = "Prepare transfer work order";
export const PREPARE_MARKDOWN = "Prepare markdown work order"; // (proposed)
export const TRANSFER_LINE = (qty: number, run: string | null) =>
  ` · ${units(qty)} units${run === null ? "" : ` · ${run}`}`;
export const MARKDOWN_LINE = (pct: number) => ` · ${pct}% off`; // (proposed)
export const LOADING_WORK_ORDERS = "Loading the work order…"; // (proposed)
export { BOOKING_CONFIRMED };

// --- the work orders -------------------------------------------------------------------------------------------------

const HAS_WORK_ORDER = ["PREPARED", "EXECUTED", "OBSERVED", "CLOSED"];
// The lines this step draws: a transfer's or a markdown's work order (contract.ts WorkOrder).
const isLine = (o: WorkOrder) => o.kind === "TRANSFER" || o.kind === "MARKDOWN";

// The decision's work orders, read once its state has one (and again when the decision changes).
function useWorkOrders(d: StepProps["d"]) {
  const [orders, setOrders] = useState<WorkOrder[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const wanted = HAS_WORK_ORDER.includes(d.state);
  const caseId = d.case_id;
  const load = useCallback(async () => {
    setError(null);
    try {
      const view = await getExecution(caseId);
      setOrders(view.work_orders.filter(isLine));
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    }
  }, [caseId]);
  useEffect(() => {
    if (wanted) void load();
  }, [wanted, load, d.state, d.rec_version]);
  return { wanted, orders, error, load };
}

// --- the panel and the primary ---------------------------------------------------------------------------------------

function Panel({ d, session, activity, reload }: StepProps) {
  useRefetchOnFocus(reload);
  const work = useWorkOrders(d);
  const kind = d.result?.options.find((o) => o.key === d.result?.recommended)?.kind;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <p data-testid="commit-text" className="m-0 text-body text-tx2">{COMMIT_TEXT}</p>
      {isCommitted(d, activity) && <BookingLine d={d} session={session} activity={activity} keep={kind === "KEEP"} />}
      {work.wanted && work.orders === null && work.error === null && <p className="m-0 text-label text-tx2">{LOADING_WORK_ORDERS}</p>}
      <ErrorLine error={work.error} action={work.error !== null && retryable(work.error.code)
        ? { label: TRY_AGAIN, onClick: () => void work.load() } : undefined} />
      {work.orders !== null && work.orders.length > 0 && (
        <ul data-testid="work-orders" className="m-0 flex list-none flex-col gap-1 p-0">
          {work.orders.map((o) => (
            <li key={o.action_id} data-testid="work-order-line" className="num text-body text-tx break-words">
              {o.kind === "TRANSFER" ? TERMS.TRANSFER_WORK_ORDER : MARKDOWN_WORK_ORDER}{" "}
              <span className="font-mono break-all">{o.action_id}</span>
              {o.kind === "TRANSFER" ? TRANSFER_LINE(o.qty ?? 0, o.run_code) : o.pct === null ? "" : MARKDOWN_LINE(o.pct)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Prepare transfer work order (or markdown) by the source manager while the server says prepare is next.
function Primary({ d, session, reload }: StepProps) {
  const write = useStepWrite(d, reload);
  const cancel = useFormOpener();
  if (d.next_action.key !== "prepare") return null;
  const kind = d.result?.options.find((o) => o.key === d.result?.recommended)?.kind;
  const blocker = firstBlocker(d, session.calendar);
  const mayCancel = (d.commitment?.next_actions ?? []).includes("cancel_approved")
    && (isMe(session, d.source) || isMe(session, d.dest));
  return (
    <div className="flex min-w-0 max-w-full flex-col items-start gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="primary" busy={write.busy} disabled={blocker !== null} onClick={() => void write.run(prepareWrite(d))}>
          {kind === "MARKDOWN" ? PREPARE_MARKDOWN : PREPARE_TRANSFER}
        </Button>
        {mayCancel && (
          <Button ref={cancel.opener} kind="plain" aria-expanded={cancel.open} disabled={write.busy}
            onClick={() => cancel.setOpen(true)}>{CANCEL_OPEN}</Button>
        )}
      </div>
      {blocker !== null && (
        <a data-testid="prepare-blocked" href={contextHref(d.case_id)}
          className="text-label text-bad underline underline-offset-2 break-words">{cantPrepareYet(blocker)}</a>
      )}
      <WriteError write={write} d={d} session={session} reload={reload} />
      {mayCancel && cancel.open && (
        <ReasonForm kind="cancel" d={d} session={session} reload={reload} onClose={cancel.close}
          write={(reason) => cancelWrite(d, reason)} />
      )}
    </div>
  );
}

export const step: StepModule = { key: "COMMIT", Panel, Primary };
