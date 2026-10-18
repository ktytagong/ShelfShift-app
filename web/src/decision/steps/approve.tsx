// Step 5 · Approve (06.1-UI-SPEC "Step panel" row 5, "Booking holds", "Primary action by state and role"; 06.1-CONTEXT
// D-54, D-57, D-67): who approves (the source store's manager) and who accepts (the destination's, for a transfer),
// the resources the round holds, the booking line, and the callout that only people approve.
// Rules kept here:
// - The primary is the server's next action for this account (next_action.who): the destination's Accept transfer with
//   Decline transfer…, then the source's Approve plan with Reject stock decision… (D-54). The server checks the role
//   again; a 403 shows its message word for word (T-06.1-76).
// - Booking holds (D-57): "Capacity held until 14:35 Bangkok time." from the server's `until`, never minutes left;
//   the decision re-reads on open, on focus and visibility, and once at the hold's end from the server's clock (the
//   page's useRefreshAt), so an expired hold reads "Capacity hold expired. …" and never as booked or silently renewed.
// - A plan's resources and their availability are the API's (available excludes this decision's own bookings).
// - The acknowledgement (07-CONTEXT D-12, D-26; 07-UI-SPEC "Approve step: acknowledging advisory checks", E13): while
//   the plan in force has Advisory facts, the panel lists them under "Acknowledge unverified facts ({N})" and the
//   primary carries one box; Accept transfer and Approve plan stay disabled, with the hint beside them, until it is
//   ticked, and the decision then sends the detail's `commitment.ack` and the plan shown (writes.decisionBody). The
//   tick belongs to that exact set (writes.ackIdentity): a refusal (ack_required, stale_plan) unticks it and reloads
//   the stock decision. With no Advisory fact there is no panel, no box and no hint. The list sits in the panel and
//   only the box and the button in the summary's action slot, so a long list never pushes Approve out of view
//   (07-SCREENS-MAP 07-17 T9).
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useEffect, useRef, useState } from "react";
import { factTitle, neededText } from "../../copy.ts";
import { unverifiedExposure } from "../../boardview.ts";
import { money } from "../../format.ts";
import { Button, Icon, Pill, Table } from "../../ui.tsx";
import {
  ApprovalCancelled, BookingLine, Box, DECLINE_OPEN, REJECT_OPEN, ReasonForm, WriteError, decisionWrite, isOwner, sendWrite,
  useFormOpener, useRefetchOnFocus, useStepWrite,
} from "../approval.tsx";
import type { StepModule, StepProps } from "../types.ts";
import { ackIdentity } from "../writes.ts";
// 07-21: the final-approval rule (07-UI-SPEC "Approve step"; 07-CONTEXT D-12).
import { cantApproveYet, commitmentWaits } from "../../copy.ts";
import { isFinalDecision } from "../../derive.ts";
import { contextHref, firstBlocker } from "../commitment.ts";

// --- strings ---------------------------------------------------------------------------------------------------------

export const APPROVE_PLAN = "Approve plan";
export const ACCEPT_TRANSFER = "Accept transfer";
export const SOURCE_APPROVAL = "Source approval";
export const DESTINATION_ACCEPTANCE = "Destination acceptance";
export const MANAGER = (store: string) => `${store} manager`;
export const MAY_DECLINE = "May decline, with a reason.";
export const CHIP = { // (proposed: Waiting, Not sent yet)
  waiting: "Waiting",
  approved: "Approved",
  accepted: "Accepted",
  notSent: "Not sent yet",
} as const;
export const HELD_NOW = "Held now";
export const HELD_WHEN_SENT = "Held when you send";
export const BOOKED = "Booked"; // (proposed) the resources once the round committed
export const AVAILABLE_NOTE = "Available excludes this decision's own bookings";
export const RESOURCE_HEAD = ["Resource", "Needed", "Available"];
export const KEEP_HOLDS_NOTHING = "Keep holds nothing.";
export const NOTHING_TO_HOLD = "Nothing to hold for this plan."; // (proposed; drops Phase 5's "today")
export const ONLY_PEOPLE = "Only people can approve.";
// The acknowledgement (07-UI-SPEC "Approve step"; the singular form for one fact is Claude's, so the sentence reads).
export const ACK_TITLE = (n: number) => `Acknowledge unverified facts (${n})`;
export const ACK_TICK = (n: number) => (n === 1
  ? "I've seen this unverified fact and approve with it unresolved"
  : `I've seen these ${n} unverified facts and approve with them unresolved`);
export const ACK_HINT = "Tick the box to approve with unverified facts";

// The plan in force's Advisory facts while an approve or accept must acknowledge them; [] otherwise.
function advisoryToAck(d: StepProps["d"]) {
  const c = d.commitment;
  return c !== undefined && c.ack !== null ? c.advisory : [];
}
export const READ_BRIEF = "Read decision brief in Compare";

// --- the panel -------------------------------------------------------------------------------------------------------

type Side = "source" | "destination";

function sideChip(d: StepProps["d"], side: Side) {
  const decided = d.approvals.decisions[side];
  if (decided !== undefined) {
    return <Pill tone="good" icon="check">{side === "source" ? CHIP.approved : CHIP.accepted}</Pill>;
  }
  if (d.state === "PENDING_APPROVAL") return <Pill tone="info" icon="clock">{CHIP.waiting}</Pill>;
  return <Pill tone="gray" icon="circle">{CHIP.notSent}</Pill>;
}

function Panel({ d, session, activity, reload }: StepProps) {
  useRefetchOnFocus(reload);
  const r = d.result;
  const best = r?.options.find((o) => o.key === r.recommended) ?? null;
  const keep = best?.kind === "KEEP";
  const dest = best?.dest ?? d.dest;
  const needsDest = d.approvals.required.includes("destination");
  const res = d.approvals.resources;
  const heldTitle = d.state === "PENDING_APPROVAL" ? HELD_NOW
    : d.state === "OPEN" || d.state === "WAITING" ? HELD_WHEN_SENT : BOOKED;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <ApprovalCancelled d={d} activity={activity} />
      <a href={`#/cases/${encodeURIComponent(d.case_id)}?step=compare&focus=decision-brief`}
        className="w-fit max-w-full py-1 text-body text-navy-800 underline underline-offset-2 [overflow-wrap:anywhere] max-md:min-h-11">
        {READ_BRIEF}
      </a>
      <div className="grid min-w-0 gap-4 md:grid-cols-2">
        <Box title={SOURCE_APPROVAL} chip={sideChip(d, "source")} testId="source-approval">
          <p className="m-0 text-body text-tx">{MANAGER(d.source)}</p>
        </Box>
        {needsDest && dest !== null && (
          <Box title={DESTINATION_ACCEPTANCE} chip={sideChip(d, "destination")} testId="destination-acceptance">
            <p className="m-0 text-body text-tx">{MANAGER(dest)}</p>
            <p className="m-0 text-label text-tx2">{MAY_DECLINE}</p>
          </Box>
        )}
      </div>
      <Box title={heldTitle} testId="held-resources" chip={<span className="text-label text-tx3">{AVAILABLE_NOTE}</span>}>
        {keep ? <p className="m-0 text-body text-tx2">{KEEP_HOLDS_NOTHING}</p>
          : res.length === 0 ? <p className="m-0 text-body text-tx2">{NOTHING_TO_HOLD}</p>
          : (
            <div className="relative min-w-0 overflow-x-auto">
              <Table head={RESOURCE_HEAD} numeric={[false, true, true]}
                rows={res.map((x) => [
                  <span className="block min-w-40 break-words">{x.label}</span>, neededText(x.needed, x.unit),
                  neededText(x.available, x.unit),
                ])} />
            </div>
          )}
      </Box>
      <AckFacts d={d} session={session} />
      <BookingLine d={d} session={session} activity={activity} keep={keep} />
      <p className="m-0 flex items-center gap-2 rounded-lg bg-sunken p-2 text-label text-tx2">
        <Icon name="lock" className="shrink-0" />{ONLY_PEOPLE}
      </p>
    </div>
  );
}

// "Acknowledge unverified facts ({N})": each Advisory fact's title (copy.factTitle, from the fact check of the same id)
// with its exposure chip, in the server's order. The fact's own row in Context says more.
function AckFacts({ d, session }: Pick<StepProps, "d" | "session">) {
  const facts = advisoryToAck(d);
  if (facts.length === 0) return null;
  return (
    <div data-testid="ack-facts" className="flex min-w-0 flex-col gap-2 rounded-xl border border-warn bg-warn-soft p-4">
      <h3 className="m-0 text-body font-semibold text-tx">{ACK_TITLE(facts.length)}</h3>
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {facts.map((a) => {
          const f = d.fact_checks.find((x) => x.fact_id === a.fact_id);
          return (
            <li key={a.fact_id} data-fact-id={a.fact_id} className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="num min-w-0 text-body break-all">
                {f !== undefined ? factTitle(f, session.calendar) : `${a.branch} · lot ${a.lot_ids.join(", ")}`}
              </span>
              {f !== undefined && (
                <Pill tone="warn" icon="alert">{unverifiedExposure(money(f.decision_regret, session.currency))}</Pill>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// --- the primary -----------------------------------------------------------------------------------------------------

// Accept transfer (the destination) or Approve plan (the source), whichever the server says this account does next,
// with Decline transfer… or Reject stock decision… beside it.
function Primary({ d, session, reload }: StepProps) {
  const write = useStepWrite(d, reload);
  const form = useFormOpener();
  const ack = d.commitment?.ack ?? null;
  const n = advisoryToAck(d).length;
  const [tickedFor, setTickedFor] = useState("");
  const latestReload = useRef(reload);
  latestReload.current = reload;
  // A refused acknowledgement or a changed plan: untick, and read the stock decision again (D-26, D-32).
  const err = write.error;
  useEffect(() => {
    if (err !== null && (err.code === "ack_required" || err.code === "stale_plan")) {
      setTickedFor("");
      latestReload.current();
    }
  }, [err]);
  const key = d.next_action.key;
  if (key !== "accept" && key !== "approve") return null;
  const accept = key === "accept";
  const needsTick = ack !== null && n > 0;
  const ticked = needsTick && tickedFor === ackIdentity(ack);
  const boxId = `ack-${d.case_id}`;
  // While something blocks commitment, the approving decision that would commit is disabled with the first blocker;
  // one that would not commit stays enabled with the note (D-12). The server refuses a blocked commit in any case.
  const blocker = firstBlocker(d, session.calendar);
  const final = isFinalDecision(d, accept ? "destination" : "source");
  const blockedFinal = blocker !== null && final;
  return (
    <div className="flex min-w-0 max-w-full flex-col items-start gap-2">
      {needsTick && (
        <label htmlFor={boxId} className="flex min-h-11 max-w-full items-start gap-2 text-body md:min-h-0">
          <input id={boxId} type="checkbox" checked={ticked} disabled={write.busy} className="mt-1 shrink-0"
            onChange={(e) => {
              write.clear();
              setTickedFor(e.target.checked ? ackIdentity(ack) : "");
            }} />
          <span className="min-w-0 break-words">{ACK_TICK(n)}</span>
        </label>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="primary" busy={write.busy} disabled={(needsTick && !ticked) || blockedFinal}
          onClick={() => void write.run(decisionWrite(d, accept ? "accept" : "approve"))}>
          {accept ? ACCEPT_TRANSFER : APPROVE_PLAN}
        </Button>
        <Button ref={form.opener} kind="plain" aria-expanded={form.open} disabled={write.busy}
          onClick={() => form.setOpen(true)}>{accept ? DECLINE_OPEN : REJECT_OPEN}</Button>
        {needsTick && !ticked && !write.busy && !blockedFinal && <span className="text-label text-tx3">{ACK_HINT}</span>}
      </div>
      {blockedFinal && (
        <a data-testid="approve-blocked" href={contextHref(d.case_id)}
          className="text-label text-bad underline underline-offset-2 break-words">{cantApproveYet(blocker)}</a>
      )}
      {!final && blocker !== null && (
        <p data-testid="commit-waits" className="m-0 text-label text-tx2 break-words">{commitmentWaits(blocker)}</p>
      )}
      <WriteError write={write} d={d} session={session} reload={reload}
        check={!accept && isOwner(d, session) ? () => void write.run(sendWrite(d)) : undefined} />
      {form.open && (
        <ReasonForm kind={accept ? "decline" : "reject"} d={d} session={session} reload={reload} onClose={form.close}
          write={(reason) => decisionWrite(d, accept ? "decline" : "reject", reason)} />
      )}
    </div>
  );
}

export const step: StepModule = { key: "APPROVE", Panel, Primary };
