// The stock decision's two simple writes, built in one place for the stock decision page (decision/approval.tsx
// re-exports them for its step modules) and for the Decision board's drag and Move (boardmove.ts, 06.1-CONTEXT D-69),
// so a move sends exactly the path and body the stock decision page sends. No React here: the node tests import it.
import type { CaseDetail61 } from "../contract.ts";

export type StepWrite = { path: string; body: Record<string, unknown> };

export const casePath = (caseId: string, route: string) => `/api/cases/${encodeURIComponent(caseId)}/${route}`;

// Send for approval and Check availability (the same route: Check availability holds again in a new round). A card
// or a detail fits: only the id and the recommendation version are read.
export const sendWrite = (d: Pick<CaseDetail61, "case_id" | "rec_version">): StepWrite =>
  ({ path: casePath(d.case_id, "send"), body: { rec_version: d.rec_version } });

// Prepare the work order: an empty body (the server reads the approved plan).
export const prepareWrite = (d: Pick<CaseDetail61, "case_id">): StepWrite => ({ path: casePath(d.case_id, "prepare"), body: {} });
