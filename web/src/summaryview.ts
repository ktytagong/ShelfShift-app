// Checked summary projections only. These helpers neither calculate impact nor start generation.
export type SummaryRef = { result_id: string; path: string; kind: string; value: number; text?: string };
export type SummaryRow = { case_id: string; key: string; rec_version?: string | null; whatif_version?: string | null;
  benefit_now?: number | null; benefit_if?: number | null; plan_now?: string | null; plan_if?: string | null };
export type SummaryRows = { cases: SummaryRow[]; affected?: number; previewed?: number; [key: string]: unknown };
export type ImpactSummaryData = {
  output_id: string; signal_id: string; revision: number; status: string; text: string; refs: SummaryRef[];
  rows_result_id: string | null; preview_key: string; rows: SummaryRows;
  checks: { numbers_checked?: number | null; claims?: number | null; removed?: number | null };
  usage?: { model_calls?: number | null; tool_calls?: number | null; total_ms?: number | null;
    tool_durations?: { result_id?: string; tool?: string; duration_ms?: number }[]; [key: string]: unknown } | null;
  provenance: string; reason?: string | null; written_for_unconfirmed?: boolean;
  model_id?: string | null; assistant_version?: string | null; created_at?: string | null;
  capped_text?: string | null;
};
export type SummaryEvent = { event: string; data: Record<string, unknown> };
export type SummaryProgress = "being_written" | "checking" | null;

export function summaryState(summary: ImpactSummaryData | null, progress: SummaryProgress = null) {
  if (summary?.status === "HISTORICAL" || summary?.written_for_unconfirmed) return "historical";
  if (summary?.status === "OUT_OF_DATE") return "out_of_date";
  if (summary?.status === "FALLBACK" || summary?.provenance === "FALLBACK") return "unavailable";
  if (progress) return progress;
  if (summary?.status === "GENERATING") return "being_written";
  return summary ? "current" : "absent";
}
const measured = (n: unknown): number | null => typeof n === "number" && Number.isFinite(n) && n >= 0 ? n : null;
const count = (n: unknown, one: string, many = `${one}s`) => `${measured(n) ?? "Missing"} ${n === 1 ? one : many}`;
export function checkLine(summary: ImpactSummaryData, { stored = false } = {}) {
  const checks = summary.checks;
  const n = checks.numbers_checked ?? checks.claims;
  const checked = `${count(n, "number")} checked${(measured(checks.removed) ?? 0) > 0 ? ` · ${checks.removed} removed` : ""}`;
  if (summary.provenance === "PRECOMPUTED" && !stored) return `${checked} · 0 Gemini calls on page load`;
  const ms = measured(summary.usage?.total_ms);
  const seconds = ms === null ? "Missing" : new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(ms / 1000);
  return `${checked} · ${count(summary.usage?.model_calls, "Gemini call")} · ${count(summary.usage?.tool_calls, "tool call")} · ${seconds} s`;
}
function lookup(value: unknown, path: string): unknown {
  for (const part of path.split(".")) {
    if (["__proto__", "prototype", "constructor"].includes(part) || value === null || typeof value !== "object"
      || !Object.prototype.hasOwnProperty.call(value, part)) return undefined;
    value = (value as Record<string, unknown>)[part];
  }
  return value;
}
export function refTarget(summary: ImpactSummaryData, ref: SummaryRef,
  preview?: { preview_key?: string; cases: SummaryRow[] } | null) {
  if (!ref.result_id || ref.result_id !== summary.rows_result_id || typeof ref.path !== "string"
    || typeof ref.value !== "number" || !Number.isFinite(ref.value) || lookup(summary.rows, ref.path) !== ref.value) return null;
  const match = /^cases\.(\d+)\.(.+)$/.exec(ref.path);
  const row = match ? summary.rows.cases[Number(match[1])] : null;
  const current = row ? preview?.cases.find(c => c.case_id === row.case_id) : null;
  const field = match?.[2].replace(/^benefits\.now\.net$/, "benefit_now").replace(/^benefits\.if_confirmed\.net$/, "benefit_if");
  const panel = summary.status === "CURRENT" && !summary.written_for_unconfirmed
    && preview?.preview_key === summary.preview_key && row && current && field
    && row.rec_version === current.rec_version && row.whatif_version === current.whatif_version
    && lookup(current, field) === ref.value;
  return { scope: panel ? "panel" as const : "stored" as const, caseId: row?.case_id ?? null, path: ref.path, value: ref.value };
}

// Keep raw unfinished input so callers can append decoded byte chunks. A split
// CRLF cannot be mistaken for a blank line. EOF never publishes a partial frame.
export function parseSse(input: string): { events: SummaryEvent[]; rest: string } {
  const events: SummaryEvent[] = [];
  let lineStart = 0, consumed = 0, event = "message", data: string[] = [];
  for (let i = 0; i < input.length; i++) {
    if (input[i] !== "\r" && input[i] !== "\n") continue;
    if (input[i] === "\r" && i === input.length - 1) break;
    const line = input.slice(lineStart, i);
    if (input[i] === "\r" && input[i + 1] === "\n") i++;
    lineStart = i + 1;
    if (line === "") {
      if (data.length && ["status", "tool", "tool_result", "summary", "error"].includes(event)) {
        const parsed: unknown = JSON.parse(data.join("\n"));
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Invalid summary stream object");
        events.push({ event, data: parsed as Record<string, unknown> });
      }
      event = "message"; data = []; consumed = lineStart;
    } else if (!line.startsWith(":")) {
      const at = line.indexOf(":");
      const name = at < 0 ? line : line.slice(0, at);
      const value = at < 0 ? "" : line.slice(at + 1).replace(/^ /, "");
      if (name === "event") event = value;
      if (name === "data") data.push(value);
    }
  }
  return { events, rest: input.slice(consumed) };
}
