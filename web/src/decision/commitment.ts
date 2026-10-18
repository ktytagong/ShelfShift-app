// The commitment rule as the stock decision page shows it (07-21; 07-UI-SPEC "CommitState", "Approve step";
// 07-CONTEXT D-12). It sits beside the step modules, not under steps/ (the page loads every steps/*.tsx as a step).
// Everything is derived from the case read: the server's commitment.blocks in its order (lot holds, then Required
// checks), the fact checks' titles and Blocked labels, the advisory facts to acknowledge. No fetch and no rule of its
// own: the server refuses a blocked commit or Prepare (409 commitment_blocked) and its message shows word for word.
import type { VerifiedFact } from "../api.ts";
import type { CaseDetail61 } from "../contract.ts";
import {
  BLOCKS_COMMITMENT, NO_BLOCKING_CHECKS, advisoryToAcknowledge, blockerText, factTitle, moreBlockers,
} from "../copy.ts";
import { blockersOf } from "../derive.ts";
import type { Cal } from "../format.ts";
import type { CommitStateView } from "../ui.tsx";

export const SHOWN_BLOCKERS = 2; // the tile's lines before "+N more" (07-UI-SPEC E2 overflow)

// "#/cases/{id}?step=context": where every blocker line leads (the fact checks and their ways forward).
export const contextHref = (caseId: string) => `#/cases/${encodeURIComponent(caseId)}?step=context`;

const facts = (d: CaseDetail61) => d.fact_checks as VerifiedFact[];

// Every blocker's line, in the server's order.
export function blockerLines(d: CaseDetail61, cal: Cal): string[] {
  return blockersOf({ commitment: d.commitment, fact_checks: facts(d) }, (f) => factTitle(f, cal)).map(blockerText);
}

// The first blocker's line, or null when nothing blocks (the approve and prepare reasons).
export function firstBlocker(d: CaseDetail61, cal: Cal): string | null {
  return blockerLines(d, cal)[0] ?? null;
}

// The Commitment tile while a commit or Prepare is still ahead (the server sends a commitment then); null otherwise,
// so a finished decision shows no tile.
export function commitView(d: CaseDetail61, cal: Cal): CommitStateView | null {
  const c = d.commitment;
  if (c === undefined || !["OPEN", "WAITING", "PENDING_APPROVAL", "APPROVED"].includes(d.state)) return null;
  const lines = blockerLines(d, cal);
  const href = contextHref(d.case_id);
  if (lines.length > 0) {
    const extra = lines.length - SHOWN_BLOCKERS;
    return {
      blocked: true, chip: BLOCKS_COMMITMENT,
      lines: lines.slice(0, SHOWN_BLOCKERS).map((text) => ({ text, href })),
      more: extra > 0 ? { text: moreBlockers(extra), href } : null, note: null,
    };
  }
  const n = c.advisory.length;
  return { blocked: false, chip: NO_BLOCKING_CHECKS, lines: [], more: null, note: n > 0 ? advisoryToAcknowledge(n) : null };
}
