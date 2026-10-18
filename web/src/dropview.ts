import { ASK_DROP } from "./copy.ts";

type DropFile = { readonly type: string; readonly size: number };
export type DropResult<T> = { kind: "ignored" } | { kind: "accepted"; file: T } | { kind: "refused"; error: string };
const TYPES = new Set(["image/png", "image/jpeg", "application/pdf"]);

// A courtesy precheck only. The server still sniffs file content and enforces its upload policy.
export function checkDrop<T extends DropFile>(files: ArrayLike<T>, maxBytes: number): DropResult<T> {
  if (files.length === 0) return { kind: "ignored" };
  if (files.length !== 1) return { kind: "refused", error: ASK_DROP.count };
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) return { kind: "refused", error: ASK_DROP.unavailable };
  const file = files[0];
  if (!TYPES.has(file.type)) return { kind: "refused", error: ASK_DROP.type };
  if (file.size > maxBytes) return { kind: "refused", error: ASK_DROP.size };
  return { kind: "accepted", file };
}
