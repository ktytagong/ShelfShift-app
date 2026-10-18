// The stock decision page's banners (07-UI-SPEC "Banner", "Banners on the case", "Holds, flags and reports"; 07-CONTEXT
// D-13, D-17 as replaced by D-36 for prepared work, D-24). They sit under the summary, above the step panel, in the
// order lot hold, review flag, new report, each with at most one primary action. They are not dismissible: each stays
// while its condition holds, and every condition is the server's (the detail's `holds`, `review_flags`, `reports`).
// Rules kept here:
// - Only the lot's branch manager resolves a hold (the server refuses anyone else, 403); everyone else reads who can.
// - On PREPARED work a hold or an open review flag says "Dispatch blocked" with its reason and action (D-36): the
//   server refuses the dispatch (api/execution.py), never only a disabled button. Clearing a flag never clears a hold.
// - Every write sends an Idempotency-Key (hooks.useAction: Try again resends the same key); a refusal shows the
//   server's message word for word; a button keeps its label while busy.
// - Times are real action times in the session's zone ("… Bangkok time", 06.1 D-58).
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useEffect, useState, type ReactNode } from "react";
import { post } from "../api.ts";
import type { CaseBannerKeys, CaseDetail61, CaseHold, CaseReport, CaseReviewFlag, Session61 } from "../contract.ts";
import { recordedTime } from "../copy.ts";
import { useAction } from "../hooks.ts";
import { useSession } from "../shell.tsx";
import { Banner, Button, ErrorLine, LinkButton } from "../ui.tsx";
import { NEW_CONFIRMED_INFO, REVIEW_BEFORE_DISPATCH, cityOf } from "../vocab.ts";

// --- strings ---------------------------------------------------------------------------------------------------------

export const LOT_HOLD_TITLE = (lot: string, what: string) => `Lot hold · lot ${lot} · ${what}`;
export const HELD_FOR_REVIEW = (condition: string) => `Held for review: ${condition}`;
export const PLACED_META = (placer: string, at: string, owner: string) =>
  `Placed by ${placer} manager${at === "" ? "" : ` · ${at}`} · Owner: ${owner} manager`;
export const ONLY_OWNER = (store: string) => `Only the ${store} manager can resolve this hold.`;
export const CONFIRM_PRINTED_DATE = "Confirm printed date";
export const INSPECT_AND_RELEASE = "Inspect and release";
// D-36: prepared work names the block and its action.
export const DISPATCH_BLOCKED_HOLD = "Dispatch blocked · Resolve the lot hold.";
export const REVIEW_HOLD = "Review hold";
// The release step (07-UI-SPEC "Consequential actions", "cases" → "stock decisions", 06.1 D-61).
export const RELEASE_SENTENCE = (lot: string) =>
  `Release the hold on lot ${lot}? Release it only after inspecting the stock. Stock decisions involving this lot can then be committed again.`;
export const RELEASE_NOTE = "Inspection note"; // (proposed) the server requires a note on every release
export const ADD_A_NOTE = "Add a note"; // (proposed)
export const RELEASE_HOLD = "Release hold";
export const KEEP_HOLD = "Keep hold";
export const TASKS_HREF = "#/tasks";
// Review flags (D-17; prepared work per D-36).
export const DISPATCH_BLOCKED_FLAG = "Dispatch blocked · Review new confirmed information.";
export const REVIEW_CHANGES = "Review changes";
export const FLAG_META = (label: string, at: string) => `${label}${at === "" ? "" : ` · confirmed ${at}`}`;
export const KEEP_PLAN = "Reviewed · keep plan";
export const KEEP_SENTENCE = (label: string) =>
  `Keep this plan after reviewing ${label}? Your note and the fact revisions you reviewed are logged. Lot holds and Required checks still apply, and newer confirmed information raises a new flag.`;
export const REVIEW_NOTE = "Review note";
export const ADD_A_REVIEW_NOTE = "Add a review note";
export const KEEP_FLAG_OPEN = "Keep flag open";
export const CANCEL_OPEN = "Cancel approved plan…"; // D-36's button
export const CANCEL_SUBMIT = "Cancel approved plan";
export const CANCEL_SENTENCE =
  "Cancel this approved plan? Its reservations are released and its work order can't be prepared or executed. The decision and work order stay in history, marked cancelled.";
export const REASON = "Reason";
export const ADD_A_REASON = "Add a reason";
export const KEEP_APPROVED = "Keep approved plan";
export const STAYS_ON_BOARD = "The lot stays on the Decision board while it's at risk."; // D-36's helper, G3's on-screen term
// New reports (D-16; spec §9 signal banner).
export const NEW_REPORT_TITLE = (store: string) => `New report from ${store} may change this plan`;
export const REVIEW_IMPACT = "Review impact";
export const REPORT_KIND: Readonly<Record<string, string>> = { // (proposed) a report kind in words
  CAPACITY: "Capacity", DEMAND_EVENT: "Demand event", TRANSPORT: "Transport", STOCK_COUNT: "Stock count",
  EXPIRY: "Printed expiry date", CONDITION: "Condition",
};
export const reportHref = (signalId: string) => `#/events/${encodeURIComponent(signalId)}`;
// The successor link and Reassess (D-24, FLOW-14).
export const FOLLOWS = (key: string, how: "REJECTED" | "CANCELLED", at: string) =>
  `Follows ${key} · ${how === "REJECTED" ? "Rejected" : "Cancelled"}${at === "" ? "" : ` ${at}`}`;
export const FOLLOWED_BY = (key: string) => `Followed by ${key}`;
export const REASSESS = "Reassess";

// "16 Oct 2026, 20:30 Bangkok time", or "" when the instant or the zone can't be read.
export function realAt(iso: string | null, tz: string): string {
  if (iso === null) return "";
  const at = recordedTime(iso, tz);
  return at === "" ? "" : `${at} ${cityOf(tz)} time`;
}

const isLotManager = (s: Session61, branch: string) => s.role === "manager" && s.branch === branch;

// --- the inline second step (07-UI-SPEC ConfirmStep; inline, never a modal) ------------------------------------------

export function NoteStep({ testId, sentence, noteLabel, missing, confirm, confirmKind, dismiss, onDismiss, onConfirm,
  busy, error }: {
  testId: string; sentence: ReactNode; noteLabel: string; missing: string; confirm: string;
  confirmKind: "primary" | "danger"; dismiss: string; onDismiss: () => void; onConfirm: (note: string) => void;
  busy: boolean; error: Parameters<typeof ErrorLine>[0]["error"];
}) {
  const [note, setNote] = useState("");
  const id = `${testId}-note`;
  const empty = note.trim() === "";
  return (
    <div data-testid={testId} className="flex w-full min-w-0 flex-col items-start gap-2 rounded-lg border border-line bg-white p-4">
      <p className="m-0 text-body break-words text-tx">{sentence}</p>
      <div className="flex w-full min-w-0 flex-col gap-1">
        <label htmlFor={id} className="text-label text-tx3">{noteLabel}</label>
        <textarea id={id} value={note} readOnly={busy} onChange={(e) => setNote(e.target.value)} rows={2}
          className="w-96 max-w-full rounded-md border border-line-2 bg-white p-2 text-body read-only:bg-side" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button kind={confirmKind} busy={busy} disabled={empty} onClick={() => onConfirm(note.trim())}>{confirm}</Button>
        <Button kind="ghost" disabled={busy} onClick={onDismiss}>{dismiss}</Button>
        {empty && <span className="text-label text-tx3">{missing}</span>}
      </div>
      <ErrorLine error={error} />
    </div>
  );
}

// --- lot holds -------------------------------------------------------------------------------------------------------

function HoldBanner({ d, hold, session, reload }: {
  d: CaseDetail61; hold: CaseHold; session: Session61; reload: () => void;
}) {
  const { refresh } = useSession();
  const [open, setOpen] = useState(false);
  const release = useAction((note: string, key: string) =>
    post<unknown>(`/api/holds/${encodeURIComponent(hold.hold_id)}/release`, { note }, key));
  const owner = isLotManager(session, hold.owner_branch);
  const prepared = d.state === "PREPARED";
  const what = hold.kind === "MIXED_DATES" ? hold.reason : HELD_FOR_REVIEW(hold.reason);
  const placed = PLACED_META(hold.placed_by, realAt(hold.placed_at, session.time_zone), hold.owner_branch);
  const confirm = async (note: string) => {
    if ((await release.run(note)) !== undefined) {
      setOpen(false);
      reload();
      await refresh().catch(() => {});
    }
  };
  const toggle = () => { release.clear(); setOpen((o) => !o); };
  return (
    <div data-testid={prepared ? "dispatch-blocked-hold" : "lot-hold-banner"} data-hold={hold.hold_id}
      className="flex min-w-0 flex-col gap-2">
      <Banner tone="bad" icon={prepared ? "lock" : "hold"}
        title={prepared ? DISPATCH_BLOCKED_HOLD : LOT_HOLD_TITLE(hold.lot_id, what)}
        meta={<>
          {prepared && <span className="block">{LOT_HOLD_TITLE(hold.lot_id, what)}</span>}
          <span className="block">{placed}</span>
          {!owner && <span className="block">{ONLY_OWNER(hold.owner_branch)}</span>}
        </>}>
        {owner && (prepared
          ? <Button kind="primary" onClick={toggle}>{REVIEW_HOLD}</Button>
          : hold.kind === "MIXED_DATES"
            ? <>
              <LinkButton primary href={TASKS_HREF}>{CONFIRM_PRINTED_DATE}</LinkButton>
              <Button kind="ghost" onClick={toggle}>{INSPECT_AND_RELEASE}</Button>
            </>
            : <Button kind="primary" onClick={toggle}>{INSPECT_AND_RELEASE}</Button>)}
      </Banner>
      {owner && open && (
        <NoteStep testId="release-step" sentence={RELEASE_SENTENCE(hold.lot_id)} noteLabel={RELEASE_NOTE}
          missing={ADD_A_NOTE} confirm={RELEASE_HOLD} confirmKind="primary" dismiss={KEEP_HOLD}
          onDismiss={() => { release.clear(); setOpen(false); }} onConfirm={(n) => void confirm(n)}
          busy={release.busy} error={release.error} />
      )}
    </div>
  );
}

// --- review flags ----------------------------------------------------------------------------------------------------

const COMMITTED = ["APPROVED", "PREPARED", "EXECUTED", "OBSERVED"];

// A manager who approves this plan: the source, or the destination of a transfer (the server checks the same, 403).
function approves(d: CaseDetail61, s: Session61): boolean {
  if (s.role !== "manager" || s.branch === null) return false;
  return s.branch === d.source || (d.approvals.required.includes("destination") && s.branch === d.dest);
}

function FlagBanner({ d, flags, session, reload }: {
  d: CaseDetail61; flags: CaseReviewFlag[]; session: Session61; reload: () => void;
}) {
  const { refresh } = useSession();
  const [open, setOpen] = useState<"keep" | "cancel" | null>(null);
  const [cannot, setCannot] = useState(false); // the server said the plan can't continue (409 cannot_continue)
  const keep = useAction((note: string, key: string) =>
    post<unknown>(`/api/cases/${encodeURIComponent(d.case_id)}/review`, { flag_ids: flags.map((f) => f.flag_id), note }, key));
  const cancel = useAction((reason: string, key: string) =>
    post<unknown>(`/api/cases/${encodeURIComponent(d.case_id)}/cancel`, { reason }, key));
  const prepared = d.state === "PREPARED";
  const blocks = d.commitment?.blocks ?? [];
  const canKeep = prepared || !(cannot || (d.state === "APPROVED" && blocks.length > 0));
  const mayCancel = !canKeep && (d.state === "APPROVED" || d.state === "PREPARED");
  const label = flags.map((f) => f.fact_label ?? f.reason).join("; ");
  const done = async () => {
    setOpen(null);
    reload();
    await refresh().catch(() => {});
  };
  const runKeep = async (note: string) => {
    if ((await keep.run(note)) !== undefined) await done();
  };
  const refused = keep.error?.code === "cannot_continue";
  const [shown, setShown] = useState(keep.error);
  useEffect(() => {
    if (refused) { setCannot(true); setShown(keep.error); setOpen(null); } // the keep action goes, the refusal stays
  }, [refused, keep.error]);
  const runCancel = async (reason: string) => {
    if ((await cancel.run(reason)) !== undefined) await done();
  };
  const toggle = (which: "keep" | "cancel") => { keep.clear(); cancel.clear(); setOpen((o) => (o === which ? null : which)); };
  const who = approves(d, session);
  return (
    <div data-testid={prepared ? "dispatch-blocked-flag" : "review-flag-banner"} className="flex min-w-0 flex-col gap-2">
      <Banner tone={prepared ? "bad" : "warn"} icon={prepared ? "lock" : "flag"}
        title={prepared ? DISPATCH_BLOCKED_FLAG : NEW_CONFIRMED_INFO}
        meta={<>
          {prepared && <span className="block">{NEW_CONFIRMED_INFO}</span>}
          {flags.map((f) => (
            <span key={f.flag_id} className="block">{FLAG_META(f.fact_label ?? f.reason, realAt(f.created_at, session.time_zone))}</span>
          ))}
          {prepared && <span className="block text-tx3">{REVIEW_BEFORE_DISPATCH}</span>}
        </>}>
        {who && (canKeep
          ? <Button kind="primary" onClick={() => toggle("keep")}>{prepared ? REVIEW_CHANGES : KEEP_PLAN}</Button>
          : mayCancel && <Button kind="primary" onClick={() => toggle("cancel")}>{CANCEL_OPEN}</Button>)}
      </Banner>
      {cannot && <ErrorLine error={shown} />}
      {who && open === "keep" && canKeep && (
        <NoteStep testId="keep-step" sentence={KEEP_SENTENCE(label)} noteLabel={REVIEW_NOTE} missing={ADD_A_REVIEW_NOTE}
          confirm={KEEP_PLAN} confirmKind="primary" dismiss={KEEP_FLAG_OPEN} onDismiss={() => toggle("keep")}
          onConfirm={(n) => void runKeep(n)}
          busy={keep.busy} error={keep.error} />
      )}
      {who && open === "cancel" && (
        <div className="flex min-w-0 flex-col gap-1">
          <NoteStep testId="cancel-step" sentence={CANCEL_SENTENCE} noteLabel={REASON} missing={ADD_A_REASON}
            confirm={CANCEL_SUBMIT} confirmKind="danger" dismiss={KEEP_APPROVED} onDismiss={() => toggle("cancel")}
            onConfirm={(n) => void runCancel(n)} busy={cancel.busy} error={cancel.error} />
          <p className="m-0 text-label text-tx3">{STAYS_ON_BOARD}</p>
        </div>
      )}
    </div>
  );
}

// --- new reports -----------------------------------------------------------------------------------------------------

function ReportBanner({ report, session }: { report: CaseReport; session: Session61 }) {
  const at = realAt(report.created_at, session.time_zone);
  const kind = REPORT_KIND[report.kind] ?? report.kind;
  return (
    <div data-testid="new-report-banner" className="min-w-0">
      <Banner tone="info" icon="info" title={NEW_REPORT_TITLE(report.branch)} meta={at === "" ? kind : `${kind} · ${at}`}>
        <a href={reportHref(report.signal_id)} className="text-body font-semibold text-navy-800 underline underline-offset-2">
          {REVIEW_IMPACT}
        </a>
      </Banner>
    </div>
  );
}

// --- the successor link and Reassess ---------------------------------------------------------------------------------

const caseHref = (key: string) => `#/cases/${encodeURIComponent(key)}`;
const LINK = "font-mono font-semibold text-navy-800 underline underline-offset-2";

function Links({ k, session }: { k: CaseBannerKeys; session: Session61 }) {
  const closed = k.predecessor_closed ?? null;
  const pred = k.predecessor ?? null;
  const succ = k.successor ?? null;
  if (pred === null && succ === null) return null;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-body text-tx2">
      {pred !== null && (
        <p data-testid="follows-line" className="m-0 break-words">
          {closed === null ? <>Follows <a href={caseHref(pred)} className={LINK}>{pred}</a></> : <>
            Follows <a href={caseHref(pred)} className={LINK}>{closed.key}</a>
            {FOLLOWS(closed.key, closed.closed_as, realAt(closed.at, session.time_zone)).slice(`Follows ${closed.key}`.length)}
          </>}
        </p>
      )}
      {succ !== null && (
        <p data-testid="followed-by-line" className="m-0 break-words">
          Followed by <a href={caseHref(succ)} className={LINK}>{succ}</a>
        </p>
      )}
    </div>
  );
}

function LotStatusBanner({ d, status, session }: { d: CaseDetail61; status: string; session: Session61 }) {
  const { refresh } = useSession();
  const reassess = useAction((_: Record<string, never>, key: string) =>
    post<{ case_id: string; key: string }>(`/api/cases/${encodeURIComponent(d.case_id)}/reassess`, {}, key));
  const owner = session.role === "manager" && session.branch === d.source
    && (d.commitment?.next_actions ?? []).includes("reassess");
  const press = async () => {
    const out = await reassess.run({});
    if (out !== undefined) {
      window.location.hash = caseHref(out.key);
      await refresh().catch(() => {});
    }
  };
  const awaiting = status === "Awaiting verification";
  return (
    <div data-testid="lot-status-banner" className="flex min-w-0 flex-col gap-1">
      <Banner tone={awaiting ? "info" : "warn"} icon={awaiting ? "clock" : "alert"} title={status}
        meta={`Lot ${d.lot.lot_id} · ${STAYS_ON_BOARD}`}>
        {owner && <Button kind="primary" busy={reassess.busy} onClick={() => void press()}>{REASSESS}</Button>}
      </Banner>
      <ErrorLine error={reassess.error} />
    </div>
  );
}

// --- the stack -------------------------------------------------------------------------------------------------------

// Every banner the stock decision carries now, in the stated order (the links and the lot's status first, then lot
// hold, review flag, new report); nothing when it carries none.
export function DecisionBanners({ d, session, reload }: { d: CaseDetail61; session: Session61; reload: () => void }) {
  const k = d as CaseDetail61 & CaseBannerKeys;
  const holds = k.holds ?? [];
  const flags = COMMITTED.includes(d.state) ? (k.review_flags ?? []).filter((f) => f.cleared_at === null) : [];
  const reports = k.reports ?? [];
  const status = k.lot_status ?? null;
  const links = (k.predecessor ?? null) !== null || (k.successor ?? null) !== null;
  if (holds.length === 0 && flags.length === 0 && reports.length === 0 && status === null && !links) return null;
  return (
    <div data-testid="decision-banners" className="flex min-w-0 flex-col gap-2">
      <Links k={k} session={session} />
      {status !== null && <LotStatusBanner d={d} status={status} session={session} />}
      {holds.map((h) => <HoldBanner key={h.hold_id} d={d} hold={h} session={session} reload={reload} />)}
      {flags.length > 0 && <FlagBanner d={d} flags={flags} session={session} reload={reload} />}
      {reports.map((r) => <ReportBanner key={r.signal_id} report={r} session={session} />)}
    </div>
  );
}
