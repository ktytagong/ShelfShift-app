// Presentation only: stored projections and totals always come from the server.
import type { BenchmarkSummary, ReportsWorkspaceView } from "./contract.ts";
import { dateLabel, expected, LOCALE, units as quantity, type Cal } from "./format.ts";

export const SCENARIO_IDS = ["store_rule", "store_rule_checked", "shelfshift", "markdowns_only"] as const;

// UI-SPEC "Chart colours", waste chart: each scenario keeps its own colour and point style, whatever its legend
// position. The line colour of rect and triangle comes from ui.tsx's chart datasets; tone gives the other two.
export const WASTE_STYLE = {
  store_rule: { pointStyle: "triangle", tone: "plan", colour: "#8b93b8", mark: "▲" },
  store_rule_checked: { pointStyle: "rect", tone: "plan", colour: "#5b6487", mark: "■" },
  shelfshift: { pointStyle: "circle", tone: "public", colour: "#13207a", mark: "●" },
  markdowns_only: { pointStyle: "rectRot", tone: "waste", colour: "#b54708", mark: "◆" },
} as const;

// Recorded values in words: never JSON, never a key name ("m20" reads "20% markdown", "PHANTOM_STOCK" "Phantom
// stock"). The value itself is the saved one; only its presentation changes.
const NOT_RECORDED = "Not recorded in this run";
const recordedNumber = (n: number) => new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 3 }).format(n);
function words(key: string): string {
  const markdown = /^m(\d+)$/.exec(key);
  if (markdown) return `${markdown[1]}% markdown`;
  const text = key.replace(/_/g, " ").trim().toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}
export function plainValue(value: unknown): string {
  if (value === null || value === undefined) return NOT_RECORDED;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return recordedNumber(value);
  if (typeof value === "string") return /^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$/.test(value) ? words(value) : value;
  if (Array.isArray(value)) return value.length ? value.map(plainValue).join(", ") : "None recorded";
  const entries = Object.entries(value as Record<string, unknown>);
  return entries.length ? entries.map(([k, v]) => `${words(k)}: ${plainValue(v)}`).join(" · ") : "None recorded";
}
// The evaluation settings' `checks` object (04-API-CONTRACT "Evaluation settings and digest") as one sentence.
export function checksSetting(value: unknown): string {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return plainValue(value);
  const { evidence, cap_per_version: cap, lot_once_per_day: once, answers, ...rest } = value as Record<string, unknown>;
  const parts: string[] = [];
  if (Array.isArray(evidence)) parts.push(`${evidence.map((k) => plainValue(k).toLowerCase()).join(" and ")} checks only`);
  if (typeof cap === "number") parts.push(`at most ${recordedNumber(cap)} per recommendation version`);
  if (once === true) parts.push("each lot counted at most once a day");
  if (typeof answers === "string") parts.push(`answers ${answers}`);
  parts.push(...Object.entries(rest).map(([k, v]) => `${words(k).toLowerCase()}: ${plainValue(v)}`));
  const text = parts.join(" · ");
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : NOT_RECORDED;
}

// Placement only: the summary's totals and changes are still the saved server values.
export function cumulative(byDay: Record<string, unknown>[], scenarioIds: readonly string[]): number[][] {
  return scenarioIds.map((id) => {
    let total = 0;
    return byDay.map((row) => (total += Number(row[id])));
  });
}

export function wasteSentence(totals: Record<string, { units: number }>, change: Record<string, { units: number }>): string {
  return `Automatic markdowns only: ${quantity(totals.markdowns_only.units)} units wasted. ShelfShift decisions with those checks: ${quantity(totals.shelfshift.units)} units (${signedUnits(change.shelfshift.units)} units vs Automatic markdowns only).`;
}

export function signedUnits(value: number): string {
  return `${value > 0 ? "+" : ""}${quantity(value).replace("-", "−")}`;
}

const compact = (n: number) => new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
const counted = (n: number, word: string) => `${compact(n)} ${word}${n === 1 ? "" : "s"}`;
export const percent = (n: number) => `${quantity(n * 100)}%`;
export function checksLine(checks: BenchmarkSummary["checks"]["shelfshift"]): string {
  return checks ? `${counted(checks.requests, "request")} · ${counted(checks.lots_counted, "lot")} counted · ${compact(checks.corrected_up)} up / ${compact(checks.corrected_down)} down` : "No stock checks in this scenario";
}
export function qualityLine(q: BenchmarkSummary["decision_quality"]): string {
  return `${counted(q.limit_breaks.shelfshift, "limit break")} · measured · ${compact(q.approved)} approved · ${compact(q.rejected)} rejected · ${compact(q.waiting)} waiting`;
}
export function forecastLine(q: BenchmarkSummary["forecast_quality"]): string {
  const ratio = (n: number | null) => n === null ? "—" : `${compact(n * 100)}%`;
  return `WAPE ${ratio(q.wape)} / ${ratio(q.baseline_wape)} baseline · ${ratio(q.coverage_80)} range coverage · ${counted(q.scored_days, "day")} scored`;
}
// The saved backtest report's provider mix: per-provider series counts (UI-SPEC "{TimesFM 2.5} {49,880} series ·
// {seasonal} {120}"), named by the pipeline's names; an unknown code is shown as sent (08-21: the real report is a map).
export function providerMix(providers: Readonly<Record<string, number>>, names: Readonly<Record<string, string>>): string {
  return Object.entries(providers).map(([code, n]) => `${names[code] ?? code} ${new Intl.NumberFormat("en-US").format(n)} series`)
    .join(" · ");
}
export function branchLine(stores: number): string {
  return `${counted(stores, "store")} · recorded branch amounts`;
}

// Calendar placement for the saved pre-window backtest, anchored to a session date.
export function scenarioDate(cal: Cal, day: number): string {
  const exact = dateLabel(cal, day);
  if (exact) return exact;
  const anchor = cal[0];
  if (!anchor) return `Scenario day ${day}`;
  const date = new Date(`${anchor.date}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + day - anchor.day);
  return date.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
}

export type WaterfallStep = { kind: "total" | "change"; value: number };

// Placement arithmetic only. The final total is a saved value, never derived from the changes.
export function runningTotals(steps: WaterfallStep[]): number[] {
  let running = 0;
  return steps.map((step) => (running = step.kind === "total" ? step.value : running + step.value));
}

export function waterfallBars(steps: WaterfallStep[], axisMin: number): [number, number][] {
  const totals = runningTotals(steps);
  return steps.map((step, i) => [step.kind === "total" ? axisMin : totals[i - 1] ?? 0, totals[i]]);
}

export function axisFor(steps: WaterfallStep[]): { min: number; note: boolean } {
  const edges = runningTotals(steps);
  if (!edges.length) return { min: 0, note: false };
  const low = Math.min(...edges), high = Math.max(...edges), span = high - low;
  if (low <= 0 || span >= high * 0.5) return { min: 0, note: false };
  const padding = span > 0 ? span * 0.1 : Math.max(1, Math.abs(high) * 0.1);
  const round = 10 ** Math.floor(Math.log10(padding));
  const min = Math.max(0, Math.floor((low - padding) / round) * round);
  return { min, note: min !== 0 };
}

export function horizonLabel(cal: Cal, horizon: { first_day: number; last_day: number }): string {
  const first = dateLabel(cal, horizon.first_day), last = dateLabel(cal, horizon.last_day);
  return first && last ? `${first} – ${last}` : "—";
}

export function transfersText(n: number): string {
  return `${quantity(n)} ${n === 1 ? "transfer" : "transfers"}`;
}

export function combinedLine(combined: ReportsWorkspaceView["expected"]["combined"]):
  "total" | "not_additive" | "not_computed" | null {
  return combined === null ? null : combined.additive ? "total"
    : combined.reason === "overlap" ? "not_additive" : "not_computed";
}

export function wasteChange(units: number | null, kg: number | null): string {
  const sign = (n: number | null, format: (n: number | null) => string) => {
    const text = format(n).replace("-", "−").replace(/^−0$/, "0");
    return n !== null && n > 0 && text !== "0" ? `+${text}` : text;
  };
  return `${sign(units, expected)} units · ${sign(kg, quantity)} kg`;
}
