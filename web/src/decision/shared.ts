// What the stock decision page and its step modules share (06.1-30 hoist; 06.1-18, -27, -28 had a copy each). It lives
// beside the step modules, not in steps/, because the page treats every file in steps/ as a step (decision/types.ts).
// - decisionDate: the scenario date a decision is for (D-20)
// - named: whether the signed-in account is among the people the server says act next (D-24, D-67)
// - mayAct: named, plus HQ's Take ownership; the stock decision page's and the Decision board's one act rule (D-69)
// - the history card's loading and empty lines (the page's Latest history and Archive's Full history)
// - useExport: Export stock decision, the API's bytes saved unchanged (moved from the Phase 5 case page, 06.1-28)
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useCallback, useState } from "react";
import { ApiError, NETWORK, getBlob, type Session } from "../api.ts";
import type { HistoryFilter } from "../bars.ts";
import type { CaseDetail61, Session61 } from "../contract.ts";
import { exportFilename } from "../copy.ts";

// The scenario date the decision is for (D-20): the snapshot day of its recommendation in the dataset calendar, else the
// session's scenario date, else the calendar date of the session day. Never the browser's clock.
export function decisionDate(d: Pick<CaseDetail61, "versions">, s: Pick<Session, "calendar" | "data_as_of" | "day">): string {
  const day = d.versions.day;
  const snap = day === null ? undefined : s.calendar.find((c) => c.day === day)?.date;
  return snap ?? s.data_as_of ?? s.calendar.find((c) => c.day === s.day)?.date ?? "";
}

// True when the signed-in account is among the people the server says act next (next_action.who). The server checks
// again on every write; this only decides whether a button shows.
export function named(d: Pick<CaseDetail61, "next_action">, s: Pick<Session61, "role" | "branch">): boolean {
  return d.next_action.who.some((w) => (w.role === "hq" ? s.role === "hq" : s.role === "manager" && w.branch === s.branch));
}

// True when the signed-in account is among the people the server says act next; HQ may also take ownership across
// stores (D-67). The server still refuses anyone else. The one act rule of the stock decision page and the Decision
// board's moves (moved from pages/decision.tsx for 06.1-CONTEXT D-69).
export function mayAct(d: Pick<CaseDetail61, "next_action">, s: Pick<Session61, "role" | "branch">): boolean {
  const key = d.next_action.key;
  if (key === null) return false;
  if (key === "take_ownership" && s.role === "hq") return true;
  return named(d, s);
}

export const LOADING_HISTORY = "Loading the history…"; // (proposed)
export const HISTORY_EMPTY = (f: HistoryFilter) => // 05 copy with the D-61 term
  f === "All" ? "No entries for this stock decision yet." : `No ${f} entries for this stock decision yet.`;

// Export stock decision: GET /api/cases/{id}/export saved byte for byte as "{key}.json" (a read: no Idempotency-Key). A
// failure keeps the API's error for the line under the button.
export function useExport(d: Pick<CaseDetail61, "case_id" | "key">) {
  const [error, setError] = useState<ApiError | null>(null);
  const run = useCallback(async () => {
    setError(null);
    try {
      const blob = await getBlob(`/api/cases/${encodeURIComponent(d.case_id)}/export`);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = exportFilename(d.key);
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    }
  }, [d.case_id, d.key]);
  return { run, error };
}
