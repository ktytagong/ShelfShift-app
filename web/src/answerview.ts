import type { AnswerReading, Request } from "./api.ts";
import { ANSWER_COPY } from "./copy.ts";
import { markDate } from "./format.ts";

export function shouldRetryOnOpen(reading: AnswerReading | null | undefined, manager: boolean, evidence: boolean, attempted: boolean): boolean {
  return manager && evidence && !attempted && (reading === null || (reading?.status === "INTERRUPTED" && reading.attempt < 2));
}

export type ConfirmOption = { source: "typed" | "reading"; label: string };
export function confirmOptions(r: Request, reading: AnswerReading | null): ConfirmOption[] {
  if (!r.answer) return [];
  const expiry = r.part === "expiry";
  const fields = r.answer.fields as typeof r.answer.fields & { dates?: Record<string, string> };
  const typed = expiry ? fields.dates : fields.counts;
  const read = reading?.status === "DONE" && !reading.after_confirmation
    ? (expiry ? reading.printed_dates : reading.pending_counts) : null;
  const complete = (values: typeof typed | typeof read) => r.lot_ids.length > 0 && r.lot_ids.every((lot) => {
    const value = values?.[lot];
    return expiry ? typeof value === "string" && value !== "MIXED"
      : typeof value === "object" && value !== null && value.shelf_units != null && value.backroom_units != null;
  });
  const label = (values: NonNullable<typeof typed | typeof read>) => r.lot_ids.map((lot) => {
    const value = values[lot];
    return `Lot ${lot}: ${typeof value === "string" ? `printed date ${markDate(value)}`
      : `shelf ${value.shelf_units} · backroom ${value.backroom_units}`}`;
  }).join("; ");
  const result: ConfirmOption[] = [];
  if (complete(typed)) result.push({ source: "typed", label: `Typed by ${r.to_branch}: ${label(typed!)}` });
  const equal = complete(typed) && r.lot_ids.every((lot) => {
    const a = typed?.[lot], b = read?.[lot];
    return typeof a === "string" ? a === b : typeof b === "object" && b !== null && a?.shelf_units === b.shelf_units && a?.backroom_units === b.backroom_units;
  });
  if (complete(read) && !equal) result.push({ source: "reading",
    label: `${expiry ? "Read from photo" : "Read from reply"}: ${label(read!)} · ${ANSWER_COPY.pending}` });
  return result;
}

export type ReadingLine = { kind: "observation" | "value" | "not_checked" | "conflict" | "note"; text: string };
export function readingLines(reading: AnswerReading): ReadingLine[] {
  const lines: ReadingLine[] = reading.observations.map((text) => ({ kind: "observation", text: `Read from photo: ${text}` }));
  for (const [lot, count] of Object.entries(reading.pending_counts ?? {})) lines.push({ kind: "value",
    text: `Lot ${lot} · Read from reply: shelf ${count.shelf_units ?? "Not checked"} · backroom ${count.backroom_units ?? "Not checked"}` });
  for (const [lot, date] of Object.entries(reading.printed_dates ?? {})) lines.push({ kind: "value",
    text: `Lot ${lot} · Read from photo: printed date ${markDate(date)}` });
  if (reading.not_checked) lines.push({ kind: "not_checked", text: reading.not_checked });
  for (const conflict of reading.conflicts) {
    const [kind, lot, part] = conflict.field.split(".");
    lines.push({ kind: "conflict", text: `${kind === "dates" ? ANSWER_COPY.dateConflict : ANSWER_COPY.countConflict} · Lot ${lot}`
      + `${part ? ` · ${part === "shelf_units" ? "Shelf" : "Backroom"}` : ""} · Typed: ${conflict.typed} · ${kind === "dates" ? "Photo" : "Reply"}: ${conflict.read}` });
  }
  if (reading.note) lines.push({ kind: "note", text: ANSWER_COPY.instructions });
  return lines;
}
