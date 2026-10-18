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
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { neededText } from "../../copy.ts";
import { Button, Icon, Pill, Table } from "../../ui.tsx";
import {
  ApprovalCancelled, BookingLine, Box, DECLINE_OPEN, REJECT_OPEN, ReasonForm, WriteError, decisionWrite, isOwner, sendWrite,
  useFormOpener, useRefetchOnFocus, useStepWrite,
} from "../approval.tsx";
import type { StepModule, StepProps } from "../types.ts";

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
      <BookingLine d={d} session={session} activity={activity} keep={keep} />
      <p className="m-0 flex items-center gap-2 rounded-lg bg-sunken p-2 text-label text-tx2">
        <Icon name="lock" className="shrink-0" />{ONLY_PEOPLE}
      </p>
    </div>
  );
}

// --- the primary -----------------------------------------------------------------------------------------------------

// Accept transfer (the destination) or Approve plan (the source), whichever the server says this account does next,
// with Decline transfer… or Reject stock decision… beside it.
function Primary({ d, session, reload }: StepProps) {
  const write = useStepWrite(d, reload);
  const form = useFormOpener();
  const key = d.next_action.key;
  if (key !== "accept" && key !== "approve") return null;
  const accept = key === "accept";
  return (
    <div className="flex min-w-0 max-w-full flex-col items-start gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="primary" busy={write.busy} onClick={() => void write.run(decisionWrite(d, accept ? "accept" : "approve"))}>
          {accept ? ACCEPT_TRANSFER : APPROVE_PLAN}
        </Button>
        <Button ref={form.opener} kind="plain" aria-expanded={form.open} disabled={write.busy}
          onClick={() => form.setOpen(true)}>{accept ? DECLINE_OPEN : REJECT_OPEN}</Button>
      </div>
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
