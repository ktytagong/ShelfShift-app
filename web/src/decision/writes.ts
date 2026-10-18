// The stock decision's two simple writes, built in one place for the stock decision page (decision/approval.tsx
// re-exports them for its step modules) and for the Decision board's drag and Move (boardmove.ts, 06.1-CONTEXT D-69),
// so a move sends exactly the path and body the stock decision page sends. No React here: the node tests import it.
import type { Acknowledgement } from "../api.ts";
import type { CaseDetail61 } from "../contract.ts";

export type StepWrite = { path: string; body: Record<string, unknown> };

export const casePath = (caseId: string, route: string) => `/api/cases/${encodeURIComponent(caseId)}/${route}`;

// Send for approval and Check availability (the same route: Check availability holds again in a new round). A card
// or a detail fits: only the id and the recommendation version are read.
export const sendWrite = (d: Pick<CaseDetail61, "case_id" | "rec_version">): StepWrite =>
  ({ path: casePath(d.case_id, "send"), body: { rec_version: d.rec_version } });

// Prepare the work order: an empty body (the server reads the approved plan).
export const prepareWrite = (d: Pick<CaseDetail61, "case_id">): StepWrite => ({ path: casePath(d.case_id, "prepare"), body: {} });

// An approve or accept names the plan the page shows (`plan_key`, the detail's plan in force `suggested_key`; 07 review
// C, D-32) and, while that plan has Advisory facts, carries the exact acknowledgement the detail lists
// (`commitment.ack`; 07-CONTEXT D-12, D-26). The step's own tick gates the button: this only builds the body. A reject
// or decline sends neither (the server checks a plan key there only when one is named).
export type Decision = "approve" | "accept" | "reject" | "decline";
export function decisionBody(
  d: Pick<CaseDetail61, "rec_version" | "suggested_key"> & { commitment?: { ack: Acknowledgement | null } },
  decision: Decision, reason?: string,
): Record<string, unknown> {
  const body: Record<string, unknown> = { rec_version: d.rec_version, decision };
  if (reason !== undefined) body.reason = reason;
  if (decision === "approve" || decision === "accept") {
    body.plan_key = d.suggested_key;
    const ack = d.commitment?.ack ?? null;
    if (ack !== null) body.acknowledged = ack;
  }
  return body;
}

// The acknowledgement's identity: a tick is for this exact set (version, plan and facts), so a reload that brings
// another set shows the box unticked. "" with nothing to acknowledge.
export function ackIdentity(ack: Acknowledgement | null | undefined): string {
  return ack == null ? "" : JSON.stringify([ack.rec_version, ack.plan_key, [...ack.fact_ids].sort()]);
}

// --- 07-21: Select another plan and Cancel approved plan (04-API-CONTRACT "Select another plan", "Cancel approved
// plan"; 07-CONTEXT D-23, D-25). Own block, additive. The server rechecks the option (check_plan on today's data) and
// the seat; its refusal shows word for word.
export const selectWrite = (d: Pick<CaseDetail61, "case_id" | "rec_version">, planKey: string): StepWrite =>
  ({ path: casePath(d.case_id, "select"), body: { rec_version: d.rec_version, plan_key: planKey } });
export const cancelWrite = (d: Pick<CaseDetail61, "case_id">, reason: string): StepWrite =>
  ({ path: casePath(d.case_id, "cancel"), body: { reason } });
