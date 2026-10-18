// The names of the nine steps and four stages (06.1-UI-SPEC "The nine steps and four stages"; sketch 006 A). The
// server sends `step`, `step_number` and `stage` on every card and detail (04-API-CONTRACT "Product mode (06.1)",
// "Card and detail keys"); this file only names those keys. It holds no mapping from a case's state to a step: the
// server derives them (D-24), so the client can never show a step the server did not state.
// Pure, erasable syntax only, so `node --test` runs it.

export const STEP_KEYS = [
  "TRIGGER", "CONTEXT", "OPTIONS", "COMPARE", "APPROVE", "COMMIT", "EXECUTE", "VERIFY", "ARCHIVE",
] as const;
export type StepKey = (typeof STEP_KEYS)[number];

const STEP_NAMES: Readonly<Record<StepKey, string>> = {
  TRIGGER: "Trigger", CONTEXT: "Context", OPTIONS: "Options", COMPARE: "Compare", APPROVE: "Approve",
  COMMIT: "Commit", EXECUTE: "Execute", VERIFY: "Verify", ARCHIVE: "Archive",
};

// "COMPARE" → "Compare". A key this version does not know reads in words, never as the raw key.
export function stepName(key: string): string {
  if (key in STEP_NAMES) return STEP_NAMES[key as StepKey];
  const w = key.replace(/_/g, " ").toLowerCase();
  return w.charAt(0).toUpperCase() + w.slice(1);
}

export type StageKey = "decide" | "authorise" | "do" | "check";
export type Stage = { key: StageKey; name: string; steps: readonly StepKey[]; hint: string };

const stage = (key: StageKey, name: string, steps: readonly StepKey[]): Stage =>
  ({ key, name, steps, hint: steps.map(stepName).join(" · ") });

// The board's four columns, in order, each with the steps it holds; the hint is the step names (the column header's
// second line), composed from the one list above.
export const STAGES: readonly Stage[] = [
  stage("decide", "Decide", ["TRIGGER", "CONTEXT", "OPTIONS", "COMPARE"]),
  stage("authorise", "Authorise", ["APPROVE", "COMMIT"]),
  stage("do", "Do", ["EXECUTE"]),
  stage("check", "Check and close", ["VERIFY", "ARCHIVE"]),
];

// "check" → "Check and close"; a key this version does not know reads in words.
export function stageName(key: string): string {
  const s = STAGES.find((x) => x.key === key);
  return s === undefined ? stepName(key) : s.name;
}

// "Step 4 of 9 · Compare" from the server's `step` and `step_number` only; null when the server gave no step (0,
// null or a missing key), so nothing is shown rather than a guess.
export function stepText(card: { step: string | null; step_number: number | null }): string | null {
  if (card.step === null || card.step_number === null || card.step_number < 1) return null;
  return `Step ${card.step_number} of ${STEP_KEYS.length} · ${stepName(card.step)}`;
}
