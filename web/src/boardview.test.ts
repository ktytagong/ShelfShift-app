// node --test (no dependency): the Decision board's pure helpers (06.1-UI-SPEC "Decision board"; D-23, D-24). The
// board groups the server's page by the server's `stage` and `step` keys and never reorders a card inside a group;
// badges show at most two plus "+{N}"; the Next line names the action and who does it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { badgeChip, badgeOverflow, blockedToggleText, showingText, stageGroups } from "./boardview.ts";

type C = { key: string; stage: "decide" | "authorise" | "do" | "check"; step: string; closed_as: string | null };
const c = (key: string, stage: C["stage"], step: string, closed_as: string | null = null): C =>
  ({ key, stage, step, closed_as });

test("stageGroups puts each card in its server stage, Decide by step, closed cards last, order kept", () => {
  // The server's page order, mixed across stages on purpose: the groups must keep this order inside each group.
  const page = [
    c("SUS-7", "decide", "COMPARE"), c("SUS-3", "check", "ARCHIVE", "COMPLETED"), c("SUS-19", "decide", "TRIGGER"),
    c("SUS-2", "authorise", "APPROVE"), c("SUS-5", "check", "VERIFY"), c("SUS-1", "decide", "TRIGGER"),
    c("SUS-4", "do", "EXECUTE"), c("SUS-8", "decide", "CONTEXT"), c("SUS-6", "authorise", "COMMIT"),
    c("SUS-9", "check", "ARCHIVE"),
  ];
  const g = stageGroups(page);
  const keys = (xs: C[]) => xs.map((x) => x.key);
  assert.deepEqual(g.decide.map((d) => [d.step, keys(d.cards)]), [
    ["TRIGGER", ["SUS-19", "SUS-1"]], ["CONTEXT", ["SUS-8"]], ["COMPARE", ["SUS-7"]],
  ]);
  assert.deepEqual(keys(g.authorise), ["SUS-2", "SUS-6"]);
  assert.deepEqual(keys(g.do), ["SUS-4"]);
  assert.deepEqual(keys(g.check), ["SUS-5"]);
  assert.deepEqual(keys(g.closed), ["SUS-3", "SUS-9"]);
});

test("stageGroups keeps the three Decide groups when they are empty, and lists Options only if a card stands on it", () => {
  const empty = stageGroups<C>([]);
  assert.deepEqual(empty.decide.map((d) => [d.step, d.cards.length]), [["TRIGGER", 0], ["CONTEXT", 0], ["COMPARE", 0]]);
  assert.deepEqual([empty.authorise, empty.do, empty.check, empty.closed], [[], [], [], []]);
  const odd = stageGroups([c("SUS-1", "decide", "OPTIONS")]);
  assert.deepEqual(odd.decide.map((d) => [d.step, d.cards.map((x) => x.key)]),
    [["TRIGGER", []], ["CONTEXT", []], ["OPTIONS", ["SUS-1"]], ["COMPARE", []]]);
});

test("badgeOverflow shows two badges and names the rest behind '+{N}'", () => {
  assert.deepEqual(badgeOverflow(["a", "b", "c", "d"]), { shown: ["a", "b"], more: 2, moreLabel: "c, d" });
  assert.deepEqual(badgeOverflow(["a", "b"]), { shown: ["a", "b"], more: 0, moreLabel: "" });
  assert.deepEqual(badgeOverflow(["a"]), { shown: ["a"], more: 0, moreLabel: "" });
  assert.deepEqual(badgeOverflow([]), { shown: [], more: 0, moreLabel: "" });
});

test("showingText and blockedToggleText read the server's counts", () => {
  assert.equal(showingText(5, 37), "Showing 5 of 37");
  assert.equal(showingText(0, 0), "Showing 0 of 0");
  assert.equal(blockedToggleText(3), "Blocked only (3)");
  assert.equal(blockedToggleText(0), "Blocked only (0)");
});

test("badgeChip: the status words of 06.1 (D-61), and an unverified amount is an exposure, never cash", () => {
  assert.equal(badgeChip({ kind: "hold_expired", text: "Hold expired" }, "USD").text, "Capacity hold expired");
  assert.equal(badgeChip({ kind: "evidence_requested", text: "Evidence requested" }, "USD").text, "Stock check requested");
  assert.equal(badgeChip({ kind: "missing_data", text: "Missing data" }, "USD").text, "Missing data");
  const unverified = {
    kind: "unverified" as const, text: "Unverified", fact_ids: ["f1"], not_allowed_fact_ids: [], decision_regret: 23.333,
    net_revenue_difference: -5, waste_policy_difference: 28.333,
  };
  assert.equal(badgeChip(unverified, "USD").text, `Unverified · estimated decision exposure ${"$"}23.33`);
  for (const b of [unverified, { ...unverified, not_allowed_fact_ids: ["f1"] }, { ...unverified, decision_regret: null }]) {
    assert.doesNotMatch(badgeChip(b, "USD").text, /cash|saving|loss|at stake/i);
  }
});

// --- Phase 7 badges (07-22; 07-UI-SPEC "Board badges", surface 2) ---------------------------------------------------

test("cardBadges: Phase 7's priority, two shown, the rest behind '+N' with their names", async () => {
  const { cardBadges } = await import("./boardview.ts");
  const card = {
    badges: [
      { kind: "new_report", text: "New report" },
      { kind: "review_flag", text: "New confirmed information · review required" },
      { kind: "evidence_requested", text: "Evidence requested" },
      { kind: "required_check", text: "Required check" },
      { kind: "lot_hold", text: "Lot hold" },
    ],
    blocked_reasons: [], next_action: { key: null, who: [] },
  } as unknown as Parameters<typeof cardBadges>[0];
  const texts = cardBadges(card, "USD").map((c) => c.text);
  assert.deepEqual(texts, ["Lot hold", "Required check", "New confirmed information · review required",
    "Stock check requested", "New report"]);
  assert.deepEqual(badgeOverflow(texts), {
    shown: ["Lot hold", "Required check"], more: 3,
    moreLabel: "New confirmed information · review required, Stock check requested, New report",
  });
});

test("cardBadges: a not-allowed fact ranks above a review flag, an exposure below it", async () => {
  const { cardBadges } = await import("./boardview.ts");
  const unverified = (notAllowed: boolean) => ({
    kind: "unverified", text: "Unverified", fact_ids: ["f"], not_allowed_fact_ids: notAllowed ? ["f"] : [],
    decision_regret: notAllowed ? null : 12, net_revenue_difference: null, waste_policy_difference: null,
  });
  const card = (u: object) => ({
    badges: [{ kind: "review_flag", text: "x" }, u], blocked_reasons: [], next_action: { key: null, who: [] },
  }) as unknown as Parameters<typeof cardBadges>[0];
  assert.deepEqual(cardBadges(card(unverified(true)), "USD").map((c) => c.kind), ["unverified", "review_flag"]);
  assert.deepEqual(cardBadges(card(unverified(false)), "USD").map((c) => c.kind), ["review_flag", "unverified"]);
});

test("nextLine names who resolves a lot hold or a Required check", async () => {
  const { nextLine } = await import("./boardview.ts");
  const next = { key: "send", who: [{ role: "manager", branch: "S042" }] };
  const held = {
    next_action: next, blocked_reasons: ["lot_hold", "required_check"],
    badges: [{ kind: "lot_hold", text: "Lot hold", owner_branches: ["S017"] },
      { kind: "required_check", text: "Required check", owner_branches: ["S042"] }],
  } as unknown as Parameters<typeof nextLine>[0];
  assert.deepEqual(nextLine(held), { action: "Resolve hold", who: "S017 manager" });
  const checked = { ...held, blocked_reasons: ["required_check"] } as unknown as Parameters<typeof nextLine>[0];
  assert.deepEqual(nextLine(checked), { action: "Resolve check", who: "S042 manager" });
  const free = { next_action: next, blocked_reasons: [], badges: [] } as unknown as Parameters<typeof nextLine>[0];
  assert.deepEqual(nextLine(free), { action: "Review and send", who: "S042 manager" });
});
