// The Decision board's moves (06.1-CONTEXT D-69, which keeps C-21 and the D-23 board bullet): dragging a card to another
// column, or choosing a column in its Move control, requests the stock decision's next business action. A move is one
// of four things:
// - direct: one existing server-checked write, built by decision/writes.ts exactly as the stock decision page sends it
//   (Send for approval with the card's rec_version, or Prepare with an empty body);
// - guided: the stock decision's own form in a drawer (approval, stock records, the closing note, or a prerequisite);
// - blocked: a reason on screen, and no request;
// - none: the card's own column.
// No move writes a stage or a step, and nothing here maps a state to a step: the card's stage, step and next action
// are the server's, the server checks every write, and a card changes column only when the server's board says so.
// Who may act is the stock decision page's own rule (decision/shared.ts mayAct). No React here: node imports it.
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import type { Card61 } from "./contract.ts";
import type { Session } from "./api.ts";
import { mayAct } from "./decision/shared.ts";
import { prepareWrite, sendWrite, type StepWrite } from "./decision/writes.ts";
import { STAGES, stageName, type StageKey } from "./steps.ts";
import { NEXT_ACTION_LABEL } from "./vocab.ts";

// --- strings ---------------------------------------------------------------------------------------------------------

export const CLOSED_HEADING = "Closed"; // the Check and close column's Closed group (pages/board.tsx re-exports it)
export const MOVE = "Move"; // (approved by the user 2026-10-18) the button that requests the chosen move
export const MOVE_TO = (key: string) => `Move ${key} to`; // (approved by the user 2026-10-18) the column select's name
export const MOVE_KEY = (key: string) => `Move ${key}`; // (approved by the user 2026-10-18) the Move button's name
export const MOVE_PICK = "Choose a column…"; // (approved by the user 2026-10-18) the select's first option, which requests nothing
export const MOVE_CLOSED = "This stock decision is closed. Open it to review its history."; // (approved by the user 2026-10-18)
export const MOVE_NO_UNDO = // (approved by the user 2026-10-18)
  "Recorded approvals and stock movements cannot be undone from the Decision board. Open the stock decision to review the next action.";
export const MOVE_NOT_YET = (key: string, target: string, next: string | null) => // (approved by the user 2026-10-18)
  next === null
    ? `${key} can't move to ${target} yet. Open the stock decision for details.`
    : `${key} can't move to ${target} yet. Next action: ${next}.`;
export const MOVE_STALE = "This stock decision changed. Review its current status and move it again."; // (approved by the user 2026-10-18)
export const MOVE_SAVING = "Saving the move…"; // (approved by the user 2026-10-18)
export const MOVE_SAVED = (key: string) => `Move saved for ${key}.`; // (approved by the user 2026-10-18)
export const MOVE_DRAWER_TITLE = (key: string, target: string) => `${key} → ${target}`; // (approved by the user 2026-10-18)
export const MOVE_LOADING = "Loading the next action…"; // (approved by the user 2026-10-18)
export const MOVE_LOAD_FAILED = "The stock decision didn't load"; // (approved by the user 2026-10-18)
export const MOVE_COMPLETE = (target: string) => // (approved by the user 2026-10-18)
  `Complete the next action to move this stock decision to ${target}.`;
export const MOVE_WAITING = "Waiting for the outstanding work to finish."; // (approved by the user 2026-10-18)
export const MOVE_NEXT = (action: string, who: string) => // (approved by the user 2026-10-18) the drawer's line when another account acts next
  `Next: ${action}${who === "" ? "" : ` · ${who}`}.`;
export const MOVE_NOW_IN = (key: string, where: string) => `${key} is now in ${where}.`; // (approved by the user 2026-10-18)
export const MOVE_DONE = "Done"; // (approved by the user 2026-10-18) closes the guided drawer
export const DROP_TO_CLOSE = "Drop here to add a closing note."; // (approved by the user 2026-10-18) the Closed zone during a drag

// --- the moves -------------------------------------------------------------------------------------------------------

export type MoveTarget = StageKey | "closed";
export type MoveCard = Pick<Card61, "case_id" | "key" | "stage" | "step" | "state" | "closed_as" | "rec_version"
  | "next_action" | "blocked" | "needs_evaluation">;
export type MoveChoice =
  | { kind: "direct"; write: StepWrite }
  | { kind: "guided" }
  | { kind: "blocked"; reason: string }
  | { kind: "none" };

export const targetName = (target: MoveTarget): string => (target === "closed" ? CLOSED_HEADING : stageName(target));
export const MOVE_TARGETS: readonly MoveTarget[] = [...STAGES.map((s) => s.key), "closed"];

// Send for approval and Prepare save on a move when the server names this account for them; approvals, stock records
// and closing use the stock decision's forms. A forward move whose next action is a prerequisite (ownership, an
// evaluation, a stock check, another account's turn) opens that form too, and never claims the card already moved.
export function moveChoice(card: MoveCard, target: MoveTarget, session: Pick<Session, "role" | "branch">): MoveChoice {
  if (card.step === "ARCHIVE" || card.closed_as !== null) return { kind: "blocked", reason: MOVE_CLOSED };
  if (target === card.stage) return { kind: "none" };
  const key = card.next_action.key;
  const notYet = (): MoveChoice => ({
    kind: "blocked", reason: MOVE_NOT_YET(card.key, targetName(target), key === null ? null : NEXT_ACTION_LABEL[key]),
  });
  if (target === "closed") return card.stage === "check" && key === "close" ? { kind: "guided" } : notYet();
  const from = STAGES.findIndex((s) => s.key === card.stage);
  const to = STAGES.findIndex((s) => s.key === target);
  if (to < from) return { kind: "blocked", reason: MOVE_NO_UNDO };
  if (to !== from + 1) return notYet();
  if (card.stage === "decide") {
    return key === "send" && card.rec_version !== null && !card.blocked && mayAct(card, session)
      ? { kind: "direct", write: sendWrite(card) } : { kind: "guided" };
  }
  if (card.stage === "authorise") {
    return key === "prepare" && !card.blocked && mayAct(card, session)
      ? { kind: "direct", write: prepareWrite(card) } : { kind: "guided" };
  }
  return card.stage === "do" ? { kind: "guided" } : notYet();
}

// True once the server has the stock decision at the move's column or past it (another account may have moved it on
// while the drawer was open), or closed: the guided drawer then says where it is and offers Done.
export function moveDone(d: Pick<MoveCard, "stage" | "step">, target: MoveTarget): boolean {
  if (d.step === "ARCHIVE") return true;
  if (target === "closed") return false;
  return STAGES.findIndex((s) => s.key === d.stage) >= STAGES.findIndex((s) => s.key === target);
}

// What the guided drawer watches on the board's card: a change to any of these means another account acted (or a count
// made the plan infeasible: blocked can change with nothing else changing), so the drawer reads the decision again.
export function moveRefreshKey(live: Pick<Card61, "state" | "step" | "rec_version" | "next_action" | "blocked"
  | "blocked_reasons">): string {
  return [live.state, live.step, live.rec_version, live.next_action.key, live.blocked, live.blocked_reasons.join(",")].join("|");
}

// A move must never submit a different plan or action from the card the person picked up: the fresh read must match.
export function sameMove(card: MoveCard, current: MoveCard): boolean {
  return card.case_id === current.case_id && card.rec_version === current.rec_version
    && card.state === current.state && card.step === current.step && card.stage === current.stage
    && card.next_action.key === current.next_action.key;
}
