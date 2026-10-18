// The Decision board's moves in the page (06.1-CONTEXT D-69; the rules are in boardmove.ts):
// - useBoardMoves: a move first reads the stock decision again (GET /api/cases/{id}) and goes ahead only when it still
//   matches the card picked up (sameMove); a direct write then goes through useAction, so it carries one
//   Idempotency-Key that Try again resends with the same body. After a save the board reloads from the server, and
//   focus goes to the card's Move select wherever the server put it (or to the move's status line).
// - BoardMoveDrawer: a guided move opens the stock decision's own step module (decision/modules.ts) in a drawer, with
//   KeepBoardLocation so a write refreshes in place instead of leaving the Decision board. The drawer reads the stock
//   decision again whenever the board's poll shows its card changed, so it never offers a form the server moved past.
// A refusal shows the server's message word for word (ErrorLine), with the action its code maps to (copy.errorAction).
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, NETWORK, get, post, type Session } from "./api.ts";
import { nextLine } from "./boardview.ts";
import {
  CLOSED_HEADING, MOVE_COMPLETE, moveDone, moveRefreshKey, MOVE_DONE, MOVE_DRAWER_TITLE, MOVE_LOAD_FAILED, MOVE_LOADING, MOVE_NEXT, MOVE_NOW_IN,
  MOVE_SAVED, MOVE_STALE, MOVE_WAITING, moveChoice, sameMove, targetName, type MoveTarget,
} from "./boardmove.ts";
import type { CaseDetail61, Card61, Session61 } from "./contract.ts";
import { errorAction } from "./copy.ts";
import { KeepBoardLocation } from "./decision/location.ts";
import { STEP_MODULES } from "./decision/modules.ts";
import { mayAct } from "./decision/shared.ts";
import type { StepProps } from "./decision/types.ts";
import type { StepWrite } from "./decision/writes.ts";
import { useAction, useActivity, useCase, useRefreshAt, type ActivityLoad } from "./hooks.ts";
import { useSession } from "./shell.tsx";
import { stageName, stepName, type StepKey } from "./steps.ts";
import { Button, Drawer, ErrorLine, Loading, NeedsEvaluation, Pill, StepBar, TRY_AGAIN } from "./ui.tsx";
import { RELOAD_DECISION, TERMS } from "./vocab.ts";

export type GuidedMove = { card: Card61; target: MoveTarget };

export type BoardMovement = {
  dragging: Card61 | null; busy: boolean;
  onDrag: (card: Card61 | null) => void;
  onMove: (card: Card61, target: MoveTarget) => void;
};

// The select a card's Move control names, found after the board re-renders the card in its new column.
const moveSelect = (key: string) => document.querySelector<HTMLSelectElement>(`select[data-move-for="${CSS.escape(key)}"]`);

export function useBoardMoves(session: Session, onChanged: () => Promise<void>) {
  const { refresh } = useSession();
  const [dragging, onDrag] = useState<Card61 | null>(null);
  const [guided, setGuided] = useState<GuidedMove | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState<ApiError | null>(null);
  const [message, setMessage] = useState("");
  const [focusFor, setFocusFor] = useState<{ key: string; onlyIfLost: boolean } | null>(null);
  const status = useRef<HTMLParagraphElement>(null);
  const moving = useRef(false);
  const requested = useRef<GuidedMove | null>(null);
  const write = useAction(async (request: { write: StepWrite; cardKey: string }, key: string) => {
    const out = await post(request.write.path, request.write.body, key);
    setMessage(MOVE_SAVED(request.cardKey));
    await onChanged();
    await refresh().catch(() => {}); // the next page load or write shows a session failure
    setFocusFor({ key: request.cardKey, onlyIfLost: false });
    return out;
  });

  // After the board shows the server's answer: focus the card's Move select, or the status line when the card is no
  // longer on this page. After the guided drawer closes, only when focus fell to the page itself.
  useEffect(() => {
    if (focusFor === null) return;
    setFocusFor(null);
    const active = document.activeElement;
    if (focusFor.onlyIfLost && active !== null && active !== document.body) return;
    (moveSelect(focusFor.key) ?? status.current)?.focus();
  }, [focusFor]);

  const onMove = async (card: Card61, target: MoveTarget) => {
    onDrag(null);
    if (moving.current || write.busy) return;
    requested.current = { card, target };
    const choice = moveChoice(card, target, session);
    if (choice.kind === "none") return;
    setMessage("");
    setCheckError(null);
    write.clear();
    if (choice.kind === "blocked") {
      setMessage(choice.reason);
      return;
    }
    moving.current = true;
    setChecking(true);
    try {
      const id = card.needs_evaluation === true ? card.key : card.case_id;
      const current = await get<CaseDetail61>(`/api/cases/${encodeURIComponent(id)}`);
      if (!sameMove(card, current)) {
        await onChanged();
        throw new ApiError(409, "stale_version", MOVE_STALE);
      }
      const fresh = moveChoice(current, target, session);
      if (fresh.kind === "direct") await write.run({ write: fresh.write, cardKey: card.key });
      else if (fresh.kind === "guided") setGuided({ card, target });
      else if (fresh.kind === "blocked") setMessage(fresh.reason);
    } catch (e) {
      setCheckError(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    } finally {
      moving.current = false;
      setChecking(false);
    }
  };
  const movement: BoardMovement = { dragging, busy: checking || write.busy, onDrag, onMove: (c, t) => void onMove(c, t) };
  const error = checkError ?? write.error;
  const retry = () => {
    if (checkError !== null && requested.current !== null) void onMove(requested.current.card, requested.current.target);
    else void write.retry();
  };
  const reload = () => {
    setCheckError(null);
    setMessage("");
    write.clear();
    void onChanged();
  };
  // The action a refusal maps to: Try again resends the same key and body; a stale or wrong-state answer reloads; a
  // refusal such as forbidden is the message alone, never resent.
  const next = error === null ? "none" : errorAction(error.code);
  // A conflict lists its reasons word for word, as the stock decision page's WriteError does.
  const reasons = error !== null && error.code === "conflict" && Array.isArray(error.extra.reasons)
    ? error.extra.reasons.map(String) : [];
  const action = next === "retry" ? { label: TRY_AGAIN, onClick: retry }
    : next === "reload_case" || next === "recheck" ? { label: RELOAD_DECISION, onClick: reload } : undefined;
  const close = () => {
    const key = guided?.card.key ?? null;
    setGuided(null);
    if (key !== null) setFocusFor({ key, onlyIfLost: true });
  };
  return { movement, guided, close, message, status, error, action, reasons };
}

export function BoardMoveDrawer({ move, live, poll, onClose, onChanged }: {
  move: GuidedMove; live: Card61 | null; poll: unknown; onClose: () => void; onChanged: () => void;
}) {
  const { session } = useSession();
  const id = move.card.needs_evaluation === true ? move.card.key : move.card.case_id;
  const load = useCase(id, session);
  const activity = useActivity(id, session);
  const reloadCase = load.reload;
  const reloadActivity = activity.reload;
  const reload = useCallback(() => {
    reloadCase();
    reloadActivity();
    onChanged();
  }, [reloadCase, reloadActivity, onChanged]);
  // The board's poll is the drawer's clock: when the card's state, step, version, next action or blocks change there
  // (moveRefreshKey), the stock decision is read again. While the card is off the board's current page or filters, a new
  // board answer (`poll`) reads it again unless a read is still in flight: that poll is skipped, so a read slower than
  // the poll lands instead of being superseded, and the next poll after it lands reads again. The drawer's reads never
  // reload the board.
  const changed: unknown = live === null ? poll : moveRefreshKey(live);
  const seen = useRef<unknown>(changed);
  const reading = load.loading;
  useEffect(() => {
    if (changed === seen.current) return;
    seen.current = changed;
    if (live === null && reading) return;
    reloadCase();
    reloadActivity();
  }, [changed, live, reading, reloadCase, reloadActivity]);
  const d = load.detail as CaseDetail61 | null;
  useRefreshAt(d?.hold?.until ?? null, d?.hold?.server_now ?? null, reload);
  return (
    <Drawer title={MOVE_DRAWER_TITLE(move.card.key, targetName(move.target))} onClose={onClose}>
      {load.error !== null ? <>
        {d === null && <p className="m-0 text-body font-semibold text-ink">{MOVE_LOAD_FAILED}</p>}
        <ErrorLine error={load.error} action={{ label: RELOAD_DECISION, onClick: reload }} />
      </>
        : d === null ? <Loading text={MOVE_LOADING} />
        : d.needs_evaluation === true ? <NeedsEvaluation d={d} session={session} onEvaluated={reload} />
        : <KeepBoardLocation value={true}>
          <MoveWorkspace key={`${d.step}:${d.state}:${d.next_action.key}`} d={d} target={move.target}
            session={session as unknown as Session61} reload={reload} activity={activity} onClose={onClose} />
        </KeepBoardLocation>}
      <a href={`#/cases/${encodeURIComponent(id)}`} className="text-body font-semibold text-navy-800 underline">
        {TERMS.OPEN_STOCK_DECISION}
      </a>
    </Drawer>
  );
}

// The form for the server's next action when the current step has none of its own: Send for approval (or Check
// availability) is next at Context while a stock check is open (C-7), and Compare holds that form. This names the
// action's form; it never derives a step from a state.
function formFor(key: CaseDetail61["next_action"]["key"]) {
  return key === "send" || key === "check_availability" ? STEP_MODULES.COMPARE?.Primary : undefined;
}

function MoveWorkspace({ d, target, session, reload, activity, onClose }: {
  d: CaseDetail61; target: MoveTarget; session: Session61; reload: () => void; activity: ActivityLoad; onClose: () => void;
}) {
  const [viewed, setViewed] = useState<StepKey>(d.step);
  const go = useCallback((step: StepKey) => setViewed(step), []);
  const props: StepProps = { d, session, reload, activity, go };
  const finished = moveDone(d, target);
  const Panel = STEP_MODULES[viewed]?.Panel;
  const Primary = mayAct(d, session) ? STEP_MODULES[d.step]?.Primary ?? formFor(d.next_action.key) : undefined;
  const next = nextLine(d);
  return (
    <div data-testid="guided-move" className="flex min-w-0 flex-col gap-4">
      <StepBar current={d.step_number} optionsDone={d.rec_version !== null} label={stepName(d.step)} />
      {finished ? <>
        <p role="status" className="m-0 text-body">{MOVE_NOW_IN(d.key, d.step === "ARCHIVE" ? CLOSED_HEADING : stageName(d.stage))}</p>
        <Button kind="primary" onClick={onClose}>{MOVE_DONE}</Button>
      </> : <>
        <p className="m-0 text-body text-ink-2">{MOVE_COMPLETE(targetName(target))}</p>
        <Pill tone="info">{stepName(d.step)}</Pill>
        {Primary !== undefined && <Primary {...props} />}
        {Primary === undefined && <p role="status" className="m-0 text-body text-ink-2">
          {next === null ? MOVE_WAITING : MOVE_NEXT(next.action, next.who)}
        </p>}
        {Panel !== undefined && <Panel {...props} />}
      </>}
    </div>
  );
}
