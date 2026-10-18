// The writes and lines Compare, Approve and Commit share (06.1-26; 06.1-UI-SPEC "Interaction states and feedback",
// "Booking holds", "Destructive and consequential actions"; 06.1-CONTEXT D-54, D-57, D-61). This module sits beside
// the step modules, not under steps/: the page loads every steps/*.tsx as a step module, and this one is none.
// Rules kept here:
// - One Idempotency-Key per user action, kept while pending and for Try again (hooks.useAction); afterwards the decision
//   and the session refetch, and the page shows the decision's current step (the server's, D-24).
// - A refusal maps to its action: stale version → Reload the stock decision; hold expired → the bad booking line and
//   Check availability; not owned → the server's message and Take ownership when this account may take it;
//   conflict → the reasons word for word; forbidden and cap → the message alone, never resent; busy or timeout →
//   Try again with the same key and body.
// - Booking holds (D-57): a static end time from the server, never minutes left; an expired hold never reads as
//   booked. The decision re-reads on open, on focus and visibility, and once at the hold's end (useRefreshAt).
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, post } from "../api.ts";
import { takeOwnership, type CaseDetail61, type Session61 } from "../contract.ts";
import { errorAction } from "../copy.ts";
import { useAction } from "../hooks.ts";
import { useSession } from "../shell.tsx";
import { Button, ErrorLine, Icon, TRY_AGAIN } from "../ui.tsx";
import { BOOKING_CONFIRMED, CHECK_AVAILABILITY, HOLD_EXPIRED, RELOAD_DECISION, holdHeld } from "../vocab.ts";
import { TAKE_OWNERSHIP } from "./steps/trigger.tsx";
import type { StepProps } from "./types.ts";
import { KeepBoardLocation } from "./location.ts";
import { casePath, decisionBody, prepareWrite, sendWrite, type Decision, type StepWrite } from "./writes.ts";

// --- strings ---------------------------------------------------------------------------------------------------------

export const SEND_FOR_APPROVAL = "Send for approval";
export { CHECK_AVAILABILITY };
export const REJECT_OPEN = "Reject stock decision…";
export const REJECT_SUBMIT = "Reject stock decision";
export const KEEP_DECISION = "Keep the stock decision";
// 07-21 (07-CONTEXT D-24, D-36): closing a plan never hides the lot; the 06.1 line said it was not reopened.
export const REJECT_HELPER = "The lot stays on the Decision board while it's at risk."; // D-36, with the D-61 term
export const DECLINE_OPEN = "Decline transfer…";
export const DECLINE_SUBMIT = "Decline transfer";
export const KEEP_TRANSFER = "Keep the transfer";
export const REASON = "Reason";
export const REJECT_FORM_NAME = "Reject stock decision";
export const DECLINE_FORM_NAME = "Decline transfer";
// 07-21: Cancel approved plan… (07-UI-SPEC "Consequential actions"; 07-CONTEXT D-23, D-25, D-36) and the required
// reason's hint while the box is empty (the confirm button stays disabled with it, never a bare "Cancel").
export const CANCEL_OPEN = "Cancel approved plan…";
export const CANCEL_SUBMIT = "Cancel approved plan";
export const KEEP_APPROVED = "Keep approved plan";
export const CANCEL_SENTENCE = "Cancel this approved plan? Its reservations are released and its work order can't be "
  + "prepared or executed. The decision and work order stay in history, marked cancelled.";
export const ADD_A_REASON = "Add a reason";
// A refused acknowledgement and a changed plan (07-CONTEXT D-26, D-32; the orchestrator's 07-17 wording): the step
// unticks its box and reads the stock decision again, so no button goes with these lines.
export const ACK_AGAIN = "These facts changed · acknowledge again";
export const PLAN_CHANGED_AGAIN = "This plan changed · review it again";
export const APPROVAL_CANCELLED = // (proposed; 05's line with the D-61 term)
  "Approval cancelled: the stock decision got a new version. Send for approval again.";

// --- the decision's routes -------------------------------------------------------------------------------------------

// casePath, sendWrite and prepareWrite live in writes.ts, so the Decision board's moves build the same writes (D-69).
export { casePath, prepareWrite, sendWrite, type StepWrite };
export { cancelWrite, selectWrite } from "./writes.ts"; // 07-21

// The body is writes.decisionBody: an approve or accept names the plan shown and carries the acknowledgement (D-26).
export const decisionWrite = (d: CaseDetail61, decision: Decision, reason?: string): StepWrite => ({
  path: casePath(d.case_id, "decision"), body: decisionBody(d, decision, reason),
});
export const rejectWrite = (d: CaseDetail61, reason: string): StepWrite => ({ path: casePath(d.case_id, "reject"), body: { reason } });

// "#/cases/{id}": the decision at its current step (the server's), as "Back to the current step" leads.
export function showCurrentStep(caseId: string): void {
  window.location.hash = `#/cases/${encodeURIComponent(caseId)}`;
}

// One keyed decision write. Afterwards the decision and session refetch; guided moves keep the Decision board open.
export function useStepWrite(d: CaseDetail61, reload: () => void, done?: () => void) {
  const { refresh } = useSession();
  const keepBoard = useContext(KeepBoardLocation);
  const caseId = d.case_id;
  return useAction(async (w: StepWrite, key: string) => {
    const out = await post<{ state: string }>(w.path, w.body, key);
    done?.();
    if (!keepBoard) showCurrentStep(caseId);
    reload();
    await refresh().catch(() => {}); // the next page load or write shows a session failure
    return out;
  });
}

export type StepWriteAction = ReturnType<typeof useStepWrite>;

// --- who is who ------------------------------------------------------------------------------------------------------

// True when the signed-in account owns the decision (D-54: only the owner sends).
export function isOwner(d: CaseDetail61, s: Session61): boolean {
  const o = d.owner;
  if (o === null) return false;
  return o.role === "hq" ? s.role === "hq" : s.role === "manager" && s.branch === o.account;
}

// True when the signed-in account may take ownership (the source store's manager or HQ, D-67); the server decides.
export function mayTake(d: CaseDetail61, s: Session61): boolean {
  return s.role === "hq" || (s.role === "manager" && s.branch === d.source);
}

// --- a write's refusal and its action --------------------------------------------------------------------------------

// The error line under a write: the server's message word for word (since quick 261017-l5q the server words
// stale_version and hold_expired in the UI-SPEC's sentences) and the one action its code maps to; a conflict lists its
// reasons word for word.
export function WriteError({ write, d, session, reload, check }: {
  write: StepWriteAction; d: CaseDetail61; session: Session61; reload: () => void; check?: () => void;
}) {
  const raw = write.error;
  if (raw === null) return null;
  const line = raw.code === "ack_required" ? ACK_AGAIN : raw.code === "stale_plan" ? PLAN_CHANGED_AGAIN : null;
  const err = line === null ? raw : new ApiError(raw.status, raw.code, line, raw.extra);
  const next = errorAction(err.code);
  const reloadAction = { label: RELOAD_DECISION, onClick: () => { write.clear(); reload(); } };
  let action: { label: string; onClick: () => void } | undefined;
  if (err.code === "stale_version") {
    action = reloadAction;
  } else if (err.code === "hold_expired") {
    action = check !== undefined ? { label: CHECK_AVAILABILITY, onClick: check } : reloadAction;
  } else if (next === "reload_case") {
    action = reloadAction;
  } else if (next === "retry") {
    action = { label: TRY_AGAIN, onClick: () => void write.retry() };
  }
  const reasons = err.code === "conflict" && Array.isArray(err.extra.reasons) ? err.extra.reasons.map(String) : [];
  return (
    <div data-testid="write-error" className="flex min-w-0 max-w-full flex-col items-start gap-1">
      <ErrorLine error={err} action={action} />
      {reasons.length > 0 && (
        <ul data-testid="conflict-reasons" className="m-0 list-disc pl-6 text-body text-tx">
          {reasons.map((x, i) => <li key={i} className="break-words">{x}</li>)}
        </ul>
      )}
      {err.code === "not_owned" && mayTake(d, session) && <TakeOwnershipAgain d={d} reload={() => { write.clear(); reload(); }} />}
    </div>
  );
}

// Take ownership after a not_owned refusal (06.1-UI-SPEC "not_owned (409 on send)"): its own keyed write.
function TakeOwnershipAgain({ d, reload }: { d: CaseDetail61; reload: () => void }) {
  const act = useAction((_: Record<string, never>, key: string) => takeOwnership(d.case_id, key));
  const press = async () => {
    if ((await act.run({})) !== undefined) reload();
  };
  return (
    <div className="flex flex-col items-start gap-1">
      <Button kind="plain" busy={act.busy} onClick={() => void press()}>{TAKE_OWNERSHIP}</Button>
      <ErrorLine error={act.error} />
    </div>
  );
}

// --- the inline Reject and Decline forms -----------------------------------------------------------------------------

// Reject stock decision… and Decline transfer… (06.1-UI-SPEC "Destructive and consequential actions"): a required
// Reason focused on open, the danger submit and the keep button that closes the form and returns focus to its opener.
export function ReasonForm({ kind, d, session, reload, onClose, write: makeWrite }: {
  kind: "reject" | "decline" | "cancel"; d: CaseDetail61; session: Session61; reload: () => void; onClose: () => void;
  write: (reason: string) => StepWrite;
}) {
  const [reason, setReason] = useState("");
  const write = useStepWrite(d, reload);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => input.current?.focus(), []);
  const id = `${kind}-reason`;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void write.run(makeWrite(reason));
  };
  const reject = kind === "reject";
  const cancel = kind === "cancel";
  const blank = reason.trim() === "";
  return (
    <form data-testid={`${kind}-form`} aria-label={cancel ? CANCEL_SUBMIT : reject ? REJECT_FORM_NAME : DECLINE_FORM_NAME}
      onSubmit={submit}
      className="flex w-full min-w-0 max-w-full flex-col items-start gap-2 rounded-lg border border-line bg-white p-4">
      {cancel && <p className="m-0 text-label text-tx break-words">{CANCEL_SENTENCE}</p>}
      <div className="flex w-full min-w-0 flex-col gap-1">
        <label htmlFor={id} className="text-label text-tx3">{REASON}</label>
        <input ref={input} id={id} type="text" required value={reason} readOnly={write.busy}
          onChange={(e) => setReason(e.target.value)}
          className="h-8 w-96 max-w-full rounded-md border border-line-2 bg-white px-2 text-body read-only:bg-side max-md:h-11" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="danger" type="submit" busy={write.busy} disabled={blank}>
          {cancel ? CANCEL_SUBMIT : reject ? REJECT_SUBMIT : DECLINE_SUBMIT}
        </Button>
        <Button kind="ghost" onClick={onClose} disabled={write.busy}>
          {cancel ? KEEP_APPROVED : reject ? KEEP_DECISION : KEEP_TRANSFER}
        </Button>
        {blank && <span className="text-label text-tx3">{ADD_A_REASON}</span>}
      </div>
      {(reject || cancel) && <p className="m-0 text-label text-tx3">{REJECT_HELPER}</p>}  {/* D-36: both closes */}
      <WriteError write={write} d={d} session={session} reload={reload} />
    </form>
  );
}

// The opener of one inline form beside a primary action; the form opens under the row.
export function useFormOpener() {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const close = () => {
    setOpen(false);
    opener.current?.focus();
  };
  return { open, setOpen, opener, close };
}

// --- booking holds (D-57) --------------------------------------------------------------------------------------------

// True once the round's decisions committed: the bookings exist (a "committed" entry and a state after it).
const COMMITTED_STATES = ["APPROVED", "PREPARED", "EXECUTED", "OBSERVED", "CLOSED"];
export function isCommitted(d: CaseDetail61, activity: StepProps["activity"]): boolean {
  return COMMITTED_STATES.includes(d.state) && (activity.entries?.some((e) => e.type === "committed") ?? false);
}

// The booking line (06.1-UI-SPEC "Booking holds"): "Capacity held until 14:35 Bangkok time." (info) while the round
// holds capacity; "Capacity hold expired. Check availability and book again." (bad) once the server applied the
// expiry; "Booking confirmed." (good) once committed. Nothing for a plan that holds nothing. Never minutes left; the
// action after expiry is the summary's Check availability (one keyed write for the decision).
export function BookingLine({ d, session, activity, keep }: {
  d: CaseDetail61; session: Session61; activity: StepProps["activity"]; keep: boolean;
}) {
  const expired = d.blocked_reasons.includes("hold_expired");
  if (expired) return <Line tone="bad" icon="lock" text={HOLD_EXPIRED} />;
  if (keep) return null;
  const until = d.state === "PENDING_APPROVAL" ? d.hold?.until ?? null : null;
  const held = until === null ? null : holdHeld(until, session.time_zone);
  if (held !== null) return <Line tone="info" icon="clock" text={held} />;
  if (isCommitted(d, activity)) return <Line tone="good" icon="check" text={BOOKING_CONFIRMED} />;
  return null;
}

const LINE_TONE = { info: "bg-info-soft text-info", bad: "bg-bad-soft text-bad", good: "bg-good-soft text-good" } as const;

function Line({ tone, icon, text }: { tone: keyof typeof LINE_TONE; icon: "clock" | "lock" | "check"; text: string }) {
  return (
    <p className={`m-0 flex items-start gap-2 rounded-lg px-4 py-2 text-body font-semibold ${LINE_TONE[tone]}`}>
      <Icon name={icon} className="mt-0.5 shrink-0" />
      <span data-testid="booking-line" className="num min-w-0 break-words">{text}</span>
    </p>
  );
}

// The round a new version cancelled (the server's approval_cancelled entry is the latest round event, D-19).
const ROUND_EVENTS = ["sent", "decision", "committed", "approval_cancelled", "hold_expired"];
export function ApprovalCancelled({ d, activity }: { d: CaseDetail61; activity: StepProps["activity"] }) {
  if (d.state !== "OPEN" || activity.entries === null) return null;
  const last = activity.entries.filter((e) => ROUND_EVENTS.includes(e.type)).at(-1);
  if (last?.type !== "approval_cancelled") return null;
  return (
    <p data-testid="approval-cancelled"
      className="m-0 flex items-start gap-2 rounded-lg bg-gray-soft p-4 text-body text-tx2 break-words">
      <Icon name="redo" className="mt-0.5 shrink-0" /><span className="min-w-0">{APPROVAL_CANCELLED}</span>
    </p>
  );
}

// Re-reads the decision when the window gets focus (D-57; the page itself re-reads on open, on visibility and at the
// hold's end). One GET per focus, nothing on an interval.
export function useRefetchOnFocus(reload: () => void): void {
  const latest = useRef(reload);
  latest.current = reload;
  useEffect(() => {
    const on = () => latest.current();
    window.addEventListener("focus", on);
    return () => window.removeEventListener("focus", on);
  }, []);
}

// A small titled box (the step panels' boxes; trigger.tsx draws the same).
export function Box({ title, chip, testId, children }: { title: string; chip?: ReactNode; testId?: string; children: ReactNode }) {
  return (
    <div data-testid={testId} className="flex min-w-0 flex-col gap-2 rounded-xl border border-line p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{title}</h3>
        {chip}
      </div>
      {children}
    </div>
  );
}
