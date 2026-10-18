// Explicit review choices; a suggestion is never a submitted fact until a person reviews it.
import { NUMBER_FIELDS } from "./reportcopy.ts";
export type Suggestion = { value: unknown; state: string; source?: string | null; excerpt?: string | null };
export type ReviewRow = { field: string; required?: boolean; suggestion?: Suggestion; typed?: unknown; choice?: "accept" | "keep" | "edit" | "clear"; edited?: unknown };
export type FieldSource = "typed" | "gemini_accepted" | "gemini_edited" | "cleared";
const present = (value: unknown) => value !== undefined && value !== null && value !== "";
const normalized = (value: unknown, field: string) => !present(value) ? value : NUMBER_FIELDS.has(field) ? Number(value) : typeof value === "string" ? value.trim() : value;
const same = (a: unknown, b: unknown, field: string) => normalized(a, field) === normalized(b, field);
export function sheetPath(message: string, file: unknown): "ai" | "manual" {
  return message.trim() !== "" || file != null ? "ai" : "manual";
}
export function rowState(row: ReviewRow) {
  const suggested = row.suggestion?.state === "suggested" && present(row.suggestion.value);
  const typed = present(row.typed);
  const needsReview = !!suggested && (!typed || !same(row.typed, row.suggestion!.value, row.field));
  const state = (name: string, value: unknown, reviewed = true) => ({state: name, value, needsReview, reviewed});
  if (row.choice === "clear") return state("cleared", null);
  if (row.choice === "edit") return state("edited", row.edited ?? null, present(row.edited) || row.required === false);
  if (row.choice === "keep" && typed) return state("typed", row.typed);
  if (row.choice === "accept" && suggested) return state("accepted", row.suggestion!.value);
  if (typed) return state(needsReview ? "conflict" : "typed", row.typed, !needsReview);
  if (suggested) return state("suggested", row.suggestion!.value, false);
  return state("needs_input", null);
}
export function reviewProgress(rows: ReviewRow[]) {
  const states = rows.map(rowState).filter(row => row.needsReview);
  return {reviewed: states.filter(row => row.reviewed).length, total: states.length};
}
export function highlightRange(message: string, excerpt: string): [number, number] | null {
  const at = excerpt === "" ? -1 : message.indexOf(excerpt);
  return at < 0 ? null : [at, at + excerpt.length];
}
export function fieldSources(rows: ReviewRow[]): Record<string, FieldSource> {
  if (rows.some(row => !rowState(row).reviewed)) throw new Error("Review each suggested field: Accept, Edit or Clear.");
  return Object.fromEntries(rows.map(row => {
    const state = rowState(row);
    const source: FieldSource = state.state === "cleared" ? "cleared" : state.state === "accepted" ? "gemini_accepted"
      : state.state === "edited" && row.suggestion?.state === "suggested"
        ? same(state.value, row.suggestion.value, row.field) ? "gemini_accepted" : "gemini_edited" : "typed";
    return [row.field, source];
  }));
}
