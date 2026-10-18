// Step 1 · Trigger (06.1-UI-SPEC "Step panel" row 1; 06.1-CONTEXT D-16, D-25, D-54, D-67): why the screen opened this
// stock decision, the screen rule (an assumption of the proposed operating model, D-05), the owner, and Take
// ownership. A decision the screen opens waits here until its owner takes it; ownership approves nothing and grants no
// policy right (D-67), and Send is offered only after it (D-54).
// Rules kept here:
// - Take ownership is offered to the source store's manager or HQ while the server's next action is take_ownership;
//   the server checks the account again and refuses anyone else (T-06.1-58). One Idempotency-Key per press, kept for
//   Try again (hooks.useAction).
// - Already owned (409): the server's sentence word for word, then "Owned by the {owner}." here (the summary's
//   button goes away once the reloaded decision names its owner, so the refusal is kept per decision below).
// - An unmapped source store reads "Transfers unavailable: location or route data missing", never "No beneficial
//   transfer found" (D-16). A flagged item's evaluation runs on the page before this panel exists (NeedsEvaluation).
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useId, useSyncExternalStore, type ReactNode } from "react";
import { ApiError } from "../../api.ts";
import { takeOwnership } from "../../contract.ts";
import { TRANSFERS_UNAVAILABLE, retryable, triggerReason } from "../../copy.ts";
import { dateLabel, money } from "../../format.ts";
import { useAction } from "../../hooks.ts";
import { useSession } from "../../shell.tsx";
import { Button, ErrorLine, KindChip, Pill, TRY_AGAIN } from "../../ui.tsx";
import { NOT_TAKEN, OWNED_BY, TAKE_OWNERSHIP_HELP, recordedLine } from "../../vocab.ts";
import type { StepModule, StepProps } from "../types.ts";

export const TAKE_OWNERSHIP = "Take ownership";
export const OWNER_TITLE = "Owner";
export const OWNER_LINE = (source: string) => `The source branch's manager: ${source} manager.`;
export const TAKEN_BY = (account: string, recorded: string) => (recorded === "" ? `Taken by ${account}` : `Taken by ${account} · ${recorded}`);
export const SCREEN_RULE_TITLE = "Screen rule";
// The screen rule in words (proposed; the operating model is a prototype assumption, D-05). It names no retailer value:
// the markdown schedule and the forecast it reads are data.
export const SCREEN_RULE = "A lot opens a stock decision when the engine expects units of it to expire unsold, even "
  + "with the store's markdown schedule.";
export const SCREEN_RULE_SOURCE = "Proposed operating model for this prototype; not the retailer's stated practice"; // (proposed)
// The trigger sentence is copy.ts triggerReason (quick 261018-h9t).

// --- the already-owned refusal, kept per decision (the button that got it is gone after the reload) -----------------

const refusals = new Map<string, string>();
const listeners = new Set<() => void>();
function setRefusal(caseId: string, message: string) {
  refusals.set(caseId, message);
  for (const f of listeners) f();
}
function subscribe(f: () => void) {
  listeners.add(f);
  return () => { listeners.delete(f); };
}
function useRefusal(caseId: string): string | null {
  return useSyncExternalStore(subscribe, () => refusals.get(caseId) ?? null);
}

// --- who may take it -------------------------------------------------------------------------------------------------

function mayTake({ d, session }: Pick<StepProps, "d" | "session">): boolean {
  if (d.next_action.key !== "take_ownership" || d.owner !== null) return false;
  return session.role === "hq" || (session.role === "manager" && session.branch === d.source);
}

function Primary({ d, session, reload, go }: StepProps) {
  const { refresh } = useSession();
  const helper = useId();
  const act = useAction(async (_: Record<string, never>, key: string) => {
    try {
      return await takeOwnership(d.case_id, key);
    } catch (e) {
      if (e instanceof ApiError && e.code === "already_owned") {
        setRefusal(d.case_id, e.message);
        go("TRIGGER");
        reload();
      }
      throw e;
    }
  });
  if (!mayTake({ d, session })) return null;
  // The success step, the same after the first press and after a Try again that succeeds (D-24, D-67).
  const done = async (out: unknown) => {
    if (out === undefined) return;
    go("TRIGGER"); // the panel shows who took it, with the time; Back to the current step leads on to Compare
    reload();
    await refresh().catch(() => {}); // the next page load or write shows a session failure
  };
  const press = async () => done(await act.run({}));
  const err = act.error;
  return (
    <div className="flex min-w-0 max-w-full flex-col items-start gap-1">
      <Button kind="primary" busy={act.busy} aria-describedby={helper} onClick={() => void press()}>{TAKE_OWNERSHIP}</Button>
      <p id={helper} className="m-0 text-label text-tx2">{TAKE_OWNERSHIP_HELP}</p>
      <ErrorLine error={err} action={err !== null && retryable(err.code) ? { label: TRY_AGAIN, onClick: () => void act.retry().then(done) } : undefined} />
    </div>
  );
}

function Box({ title, chip, testId, children }: { title: string; chip?: ReactNode; testId?: string; children: ReactNode }) {
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

function Panel({ d, session, activity }: StepProps) {
  const { session: plain } = useSession();
  const refusal = useRefusal(d.case_id);
  const reason = triggerReason({ // the case lot's own figures only, never the Keep option's network total
    units: d.at_risk_units,
    kg: d.at_risk_kg,
    value: d.at_risk_value === null ? null : money(d.at_risk_value, plain.currency),
    lot: d.lot.lot_id,
    by: d.lot.expiry_day === null ? null : dateLabel(plain.calendar, d.lot.expiry_day),
  });
  const owner = d.owner;
  const taken = activity.entries?.findLast((e) => e.type === "ownership_taken") ?? null;
  const recorded = taken === null ? "" : recordedLine(taken.at, plain.time_zone);
  const mine = owner !== null && (owner.role === "hq" ? session.role === "hq" : session.role === "manager" && session.branch === owner.account);
  const unmapped = d.eligibility?.applies === true && d.eligibility.status === "unavailable_unmapped";
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {reason !== null && (
        <p data-testid="trigger-reason" className="num m-0 flex flex-wrap items-center gap-x-2 gap-y-1 text-body text-tx break-words">
          <span>{reason}</span><KindChip kind="estimated" />
        </p>
      )}
      {unmapped && <p data-testid="transfers-unavailable" className="m-0 text-body text-tx2">{TRANSFERS_UNAVAILABLE}</p>}
      <div className="grid min-w-0 gap-4 md:grid-cols-2">
        <Box title={SCREEN_RULE_TITLE} chip={<KindChip kind="assumption" source={SCREEN_RULE_SOURCE} />}>
          <p className="m-0 text-body text-tx2">{SCREEN_RULE}</p>
        </Box>
        <Box title={OWNER_TITLE} testId="owner-box">
          <p className="m-0 text-body text-tx">{OWNER_LINE(d.source)}</p>
          {owner === null
            ? <span><Pill tone="gray" icon="user">{NOT_TAKEN}</Pill></span>
            : <p className="num m-0 text-body text-tx2">{TAKEN_BY(owner.account, recorded)}</p>}
          {owner !== null && !mine && <p className="m-0 text-body font-semibold text-tx">{OWNED_BY(owner.label)}</p>}
          {refusal !== null && <ErrorLine error={refusal} />}
        </Box>
      </div>
    </div>
  );
}

export const step: StepModule = { key: "TRIGGER", Panel, Primary };
