import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = (file: string) => readFileSync(new URL(file, import.meta.url), "utf8");
const tokens = new Map([...source("./index.css").matchAll(/--color-([\w-]+):\s*([^;]+);/g)].map(m => [m[1], m[2].trim()]));
function luminance(name: string, value = tokens.get(name)): number {
  assert.match(value ?? "", /^#[0-9a-f]{6}$/i, `invalid colour token ${name}`);
  const channels = [1, 3, 5].map(i => parseInt(value!.slice(i, i + 2), 16) / 255)
    .map(s => s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4);
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}
const contrast = (a: number, b: number) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
const meets = (value: number, threshold: number) => value >= threshold;
for (const [fg, bg, minimum] of [
  ["tx", "page", 4.5], ...["page", "side", "col", "surface"].map(bg => ["tx2", bg, 4.5]),
  ["tx3", "surface", 4.5], ["accent", "surface", 4.5],
  ...["bad", "warn", "good", "gray", "info", "chill"].map(t => [t, `${t}-soft`, 4.5]),
  ["on-navy", "navy-950", 4.5], ["on-navy-2", "navy-950", 4.5],
  ["plan-line", "surface", 3], ["navy-800", "surface", 3], ["gold-500", "navy-950", 3],
] as [string, string, number][]) {
  test(`contrast ${fg} on ${bg} reaches ${minimum}`, () => {
    const value = contrast(luminance(fg), luminance(bg));
    assert.ok(meets(value, minimum), `${fg} on ${bg}: ${value} < ${minimum}`);
  });
}
for (const bg of ["page", "side", "col"]) test(`tx3 is unsuitable on ${bg}`, () => {
  assert.ok(contrast(luminance("tx3"), luminance(bg)) < 4.5);
});
test("contrast threshold includes exactly 4.5 and excludes 4.49 without rounding", () => {
  assert.ok(meets(4.5, 4.5)); assert.equal(meets(4.49, 4.5), false);
  assert.equal(contrast(luminance("black", "#000000"), luminance("white", "#ffffff")), 21);
});
test("invalid sRGB token reports its name", () => {
  assert.throws(() => luminance("broken-token", "#fff"), /broken-token/);
});
test("table heads, dataset mark and first loading line use the off-white-safe text token", () => {
  assert.match(source("./ui.tsx"), /scope="col"[^\n]*text-tx2/);
  assert.match(source("./shell.tsx"), /data-testid="dataset-mark"[^\n]*text-tx2/);
  assert.match(source("./main.tsx"), /text-body text-tx2">Loading…/);
});
