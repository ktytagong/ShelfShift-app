// Pure count formatting. Unscored and invalid results never become passes.
import type { EvalCell, EvalCounts, EvalFailure, EvalSummaryPayload } from "./api.ts";
import { EVAL_COPY as C } from "./copy.ts";

export function cellText(cell: EvalCell): { text: string; result: string } {
  if (cell.result === "INVALID") return { text: C.invalid, result: C.notScored };
  if (cell.a === 0) return { text: C.na, result: C.na };
  const text = `${cell.p} of ${cell.a}`;
  if (cell.f > 0) return { text: `${text} · ${cell.f} failed`, result: C.fail };
  return { text, result: cell.result === "PASS" && cell.p === cell.a ? C.pass : C.notScored };
}
export function groupLine(counts: EvalCounts): string {
  return C.group.replace("{p}", String(counts.p)).replace("{a}", String(counts.a))
    .replace("{f}", String(counts.f)).replace("{x}", String(counts.x));
}
export function evalDate(value: string): string {
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).format(new Date(value));
}
export function noticeFor(data: EvalSummaryPayload): string | null {
  const latest = data.latest_unpublished;
  if (!latest || (data.published && Date.parse(latest.date) <= Date.parse(data.published.date))) return null;
  return (data.published ? C.datedNotice.replace("{date}", evalDate(latest.date)) : C.notice)
    .replace("{reason}", () => latest.reason);
}
export function failuresByGroup(failures: EvalFailure[]): EvalFailure[] {
  return [...failures].sort((a, b) => a.run - b.run || a.scenario - b.scenario || a.item_id.localeCompare(b.item_id));
}
export function usageLine(usage: { calls: number; tokens: number }): string {
  return C.usage.replace("{calls}", String(usage.calls)).replace("{tokens}", String(usage.tokens));
}
