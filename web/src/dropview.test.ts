import assert from "node:assert/strict";
import test from "node:test";
import { checkDrop } from "./dropview.ts";

const bound = 1024;
const file = (type: string, size = 12) => ({ type, size, name: "report-evidence" });

test("text drops fall through even without a loaded file policy", () => {
  assert.deepEqual(checkDrop([], bound), { kind: "ignored" });
  assert.deepEqual(checkDrop([], Number.NaN), { kind: "ignored" });
});

test("PNG JPEG and PDF preserve the original file for the report sheet", () => {
  for (const type of ["image/png", "image/jpeg", "application/pdf"]) {
    const original = Object.freeze(file(type));
    const result = checkDrop([original], bound);
    assert.equal(result.kind, "accepted");
    if (result.kind === "accepted") assert.equal(result.file, original);
  }
});

test("multiple files are refused without inspecting one selected file", () => {
  assert.deepEqual(checkDrop([file("text/plain", bound + 1), file("image/png")], bound), {
    kind: "refused", error: "Drop one file at a time.",
  });
});

test("unsupported MIME is refused regardless of the file extension", () => {
  for (const type of ["text/plain", "", "application/octet-stream", "image/gif", "image/svg+xml"]) {
    assert.deepEqual(checkDrop([{ ...file(type), name: "evidence.jpg" }], bound), {
      kind: "refused", error: "Only PNG, JPEG or PDF files can be attached.",
    });
  }
});

test("the caller supplied size bound is inclusive and controls the refusal", () => {
  for (const size of [bound - 1, bound]) assert.equal(checkDrop([file("image/jpeg", size)], bound).kind, "accepted");
  assert.deepEqual(checkDrop([file("image/jpeg", bound + 1)], bound), {
    kind: "refused", error: "This file is larger than 5 MB. Choose a smaller file.",
  });
  assert.equal(checkDrop([file("image/jpeg", bound + 1)], bound + 1).kind, "accepted");
});

test("FileList-shaped inputs work without copying or mutating the selected file", () => {
  const original = Object.freeze(file("application/pdf"));
  const files = Object.freeze({ 0: original, length: 1 });
  assert.deepEqual(checkDrop(files, bound), { kind: "accepted", file: original });
});

test("an invalid upload policy refuses files without a fallback or exception", () => {
  for (const invalid of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, undefined as unknown as number]) {
    assert.deepEqual(checkDrop([file("image/png")], invalid), {
      kind: "refused", error: "File uploads are unavailable. Reload this page and try again.",
    });
  }
});
