// The Decision board's pure helpers (06.1-UI-SPEC "Decision board"; sketch 005 B; D-23, D-24, D-54). The server
// scopes, filters, orders and pages the board (D-17) and states every card's `stage`, `step` and `next_action`
// (D-24); these helpers only place the server's page in its columns, in the order it came, and put the server's keys
// into words. Nothing here sorts, filters out or derives a step, a stage or an owner.
// Pure, erasable syntax only, so `node --test` runs it.
import type { Badge, Session } from "./api.ts";
import type { Card61 } from "./contract.ts";
import { dateLabel, hhmm, money, type Cal } from "./format.ts";
import type { IconName, Tone } from "./ui.tsx";
import {
  CAPACITY_HOLD_EXPIRED, DISPATCH_BLOCKED, IN_STORE, LOGISTICS, MISSING_DATA, NEXT_ACTION_LABEL, RECEIPT_DISCREPANCY_BADGE,
  ROLE_LABEL, STOCK_CHECK_REQUESTED,
} from "./vocab.ts";
import { NEW_CONFIRMED_INFO } from "./vocab.ts"; // Phase 7 badge words (07-22)

// --- the columns -----------------------------------------------------------------------------------------------------

// Decide's sub-groups in their fixed order (UI-SPEC: "1 · Trigger · new", "2 · Context", "4 · Compare"). Options never
// holds a card (the morning pass runs it), so its group is listed only when the server puts a card there.
const DECIDE_STEPS = ["TRIGGER", "CONTEXT", "OPTIONS", "COMPARE"] as const;
const ALWAYS_SHOWN: readonly string[] = ["TRIGGER", "CONTEXT", "COMPARE"];

export type DecideGroup<C> = { step: string; cards: C[] };
export type StageGroups<C> = { decide: DecideGroup<C>[]; authorise: C[]; do: C[]; check: C[]; closed: C[] };
type Placed = Pick<Card61, "stage" | "step" | "closed_as">;

// The server's page in the four columns: each card in its server `stage`; Decide's cards under their server `step`;
// a closed card (Archive, or closed_as set) at the end of Check and close. One pass in the page's order, so no group
// is ever reordered. A Decide step this version does not know gets its own group after the known ones.
export function stageGroups<C extends Placed>(cards: readonly C[]): StageGroups<C> {
  const decide = new Map<string, C[]>(DECIDE_STEPS.map((s) => [s, []]));
  const out: StageGroups<C> = { decide: [], authorise: [], do: [], check: [], closed: [] };
  for (const c of cards) {
    if (c.step === "ARCHIVE" || (c.closed_as !== null && c.closed_as !== undefined)) out.closed.push(c);
    else if (c.stage === "decide") {
      const group = decide.get(c.step) ?? [];
      if (!decide.has(c.step)) decide.set(c.step, group);
      group.push(c);
    } else out[c.stage].push(c);
  }
  for (const [step, list] of decide) {
    if (ALWAYS_SHOWN.includes(step) || list.length > 0) out.decide.push({ step, cards: list });
  }
  return out;
}

// --- badges ---------------------------------------------------------------------------------------------------------

// At most two badges, then "+{N}" whose accessible name lists the rest (UI-SPEC "Badge"; zero-one-many S4).
export function badgeOverflow(list: readonly string[], max = 2): { shown: string[]; more: number; moreLabel: string } {
  const rest = list.slice(max);
  return { shown: list.slice(0, max), more: rest.length, moreLabel: rest.join(", ") };
}

// The card's exception badges in the server's badge order, then the blocks the badges do not already say (UI-SPEC
// "Card" row 5). Words from the status vocabulary; the hold's static time is the server's own text ("Held until
// 14:35", D-57); an unverified amount is the decision exposure, never cash. The badge words are vocab.ts's.
export const UNVERIFIED_NOT_ALLOWED = "Unverified · plan may not be allowed"; // 05 A-10's form, kept
export const unverifiedExposure = (amount: string) => `Unverified · estimated decision exposure ${amount}`; // C-8
// 05 A-9's "on other facts" form with the C-8 noun (proposed).
export const unverifiedExposureOther = (amount: string) =>
  `Unverified · plan may not be allowed · estimated decision exposure ${amount} on other facts`;

export type BadgeChip = { kind: string; tone: Tone; icon: IconName | null; text: string };

// One server badge as its chip (the board's cards and the Needs evaluation page).
export function badgeChip(b: Badge, currency: string): BadgeChip {
  switch (b.kind) {
    case "hold_expiring": return { kind: b.kind, tone: "info", icon: "clock", text: b.text };
    case "hold_expired": return { kind: b.kind, tone: "bad", icon: "lock", text: CAPACITY_HOLD_EXPIRED };
    case "missing_data": return { kind: b.kind, tone: "bad", icon: "alert", text: MISSING_DATA };
    case "evidence_requested": return { kind: b.kind, tone: "info", icon: "inbox", text: STOCK_CHECK_REQUESTED };
    case "unverified": {
      const notAllowed = b.not_allowed_fact_ids.length > 0;
      const text = b.decision_regret === null
        ? (notAllowed ? UNVERIFIED_NOT_ALLOWED : b.text)
        : (notAllowed ? unverifiedExposureOther : unverifiedExposure)(money(b.decision_regret, currency));
      return { kind: b.kind, tone: "warn", icon: "alert", text };
    }
    default: return phase7Badge(b) ?? { kind: b.kind, tone: "gray", icon: null, text: b.text };
  }
}

// --- Phase 7 badges, their priority and who resolves a block (07-UI-SPEC "Board badges", surface 2; 07-22) -------------

export const LOT_HOLD = "Lot hold";
export const REQUIRED_CHECK = "Required check";
export const NEW_REPORT = "New report";
export const RESOLVE_HOLD = "Resolve hold";
export const RESOLVE_CHECK = "Resolve check"; // (proposed) a Required check's owner line

// The four kinds Phase 7 adds, in words; null for any other kind.
function phase7Badge(b: { kind: string; text: string }): BadgeChip | null {
  switch (b.kind) {
    case "lot_hold": return { kind: b.kind, tone: "bad", icon: "hold", text: LOT_HOLD };
    case "required_check": return { kind: b.kind, tone: "bad", icon: "lock", text: REQUIRED_CHECK };
    case "review_flag": return { kind: b.kind, tone: "warn", icon: "flag", text: NEW_CONFIRMED_INFO };
    case "new_report": return { kind: b.kind, tone: "info", icon: "info", text: NEW_REPORT };
    default: return null;
  }
}

// The UI-SPEC's priority: lot hold, Required check, plan may not be allowed, review flag, exposure, stock check
// requested, new report. 06.1's blocking badges (missing data, dispatch blocked, capacity hold expired, receipt
// discrepancy) keep their place ahead of them, and every other kind follows, each group in the server's order.
const FIRST = ["missing_data", "dispatch_blocked", "hold_expired", "discrepancy"];
function rank(c: BadgeChip): number {
  if (FIRST.includes(c.kind)) return 0;
  if (c.kind === "unverified") return c.text.startsWith(UNVERIFIED_NOT_ALLOWED) ? 3 : 5;
  const at = ["lot_hold", "required_check", "", "review_flag", "", "evidence_requested", "new_report"].indexOf(c.kind);
  return at < 0 ? 8 : at + 1;
}
export function byPriority(chips: readonly BadgeChip[]): BadgeChip[] {
  return chips.map((c, i) => ({ c, i })).sort((a, b) => rank(a.c) - rank(b.c) || a.i - b.i).map((x) => x.c);
}

type Blockable = { badges?: Badge[]; blocked_reasons?: readonly string[]; state?: string };
const ownerOf = (b: Badge | undefined): string[] => {
  const o = (b as { owner_branches?: unknown } | undefined)?.owner_branches;
  return Array.isArray(o) ? o.map(String) : [];
};

// The Next line of a card a lot hold or a Required check blocks: "Resolve hold" with the hold's owners (also on
// prepared work, whose dispatch the hold blocks, D-36), else "Resolve check" with the facts' owners; null otherwise.
export function blockedLine(card: Blockable): { action: string; who: string } | null {
  const reasons = card.blocked_reasons ?? [];
  const find = (k: string) => (card.badges ?? []).find((b) => b.kind === k);
  const hold = find("lot_hold");
  const names = (b: Badge | undefined) => ownerOf(b).map((x) => `${x} manager`).join(" · ");
  if (hold !== undefined && (reasons.includes("lot_hold") || reasons.includes("dispatch_blocked"))) {
    return { action: RESOLVE_HOLD, who: names(hold) };
  }
  const check = find("required_check");
  if (check !== undefined && reasons.includes("required_check")) return { action: RESOLVE_CHECK, who: names(check) };
  return null;
}

export function cardBadges(
  card: Pick<Card61, "badges" | "blocked_reasons" | "next_action">, currency: string,
): BadgeChip[] {
  const out = (card.badges ?? []).map((b) => badgeChip(b, currency));
  const has = (kind: string) => out.some((x) => x.kind === kind);
  for (const r of card.blocked_reasons ?? []) {
    if (r === "dispatch_blocked" && !has(r)) out.push({ kind: r, tone: "bad", icon: "lock", text: DISPATCH_BLOCKED });
    if (r === "missing_data" && !has(r)) out.push({ kind: r, tone: "bad", icon: "alert", text: MISSING_DATA });
    if (r === "hold_expired" && !has(r)) out.push({ kind: r, tone: "bad", icon: "lock", text: CAPACITY_HOLD_EXPIRED });
  }
  // The server's next action is Reconcile only while a receipt discrepancy is open (D-65).
  if (card.next_action?.key === "reconcile") {
    out.push({ kind: "discrepancy", tone: "bad", icon: "alert", text: RECEIPT_DISCREPANCY_BADGE });
  }
  return byPriority(out); // the UI-SPEC's priority (07-22)
}

// --- the Next line and who ------------------------------------------------------------------------------------------

// "S042 manager" or "HQ planner" for one actor of next_action.who.
export function actorName(w: { role: Session["role"]; branch: string | null }): string {
  return w.role === "hq" || w.branch === null ? ROLE_LABEL.hq : `${w.branch} manager`;
}

// The Next line (UI-SPEC "Card" row 6): the action in words and who does it, several joined with " · "; null when
// the server names no next action (an archived decision, or one waiting on the morning's re-evaluation).
export function nextLine(card: Pick<Card61, "next_action"> & Blockable): { action: string; who: string } | null {
  const blocked = blockedLine(card); // a lot hold or Required check names who resolves it (07-22)
  if (blocked !== null) return blocked;
  const key = card.next_action?.key ?? null;
  if (key === null) return null;
  return { action: NEXT_ACTION_LABEL[key] ?? key, who: card.next_action.who.map(actorName).join(" · ") };
}

// --- the deadline chip ----------------------------------------------------------------------------------------------

export const NO_DEADLINE = "No deadline";

// "Decide by 10:00" (warn) when the planned deadline falls on the scenario date; "Decide by 10:00 · Sun 18 Oct" (gray)
// on another day; "No deadline" (gray) without one. Planned times only: no countdown, never "overdue" (D-18).
export function deadlineChip(
  card: Pick<Card61, "due">, today: number, cal: Cal,
): { text: string; tone: "warn" | "gray" } {
  if (card.due === null) return { text: NO_DEADLINE, tone: "gray" };
  const at = `Decide by ${hhmm(card.due.min)}`;
  if (card.due.day === today) return { text: at, tone: "warn" };
  const day = dateLabel(cal, card.due.day);
  return { text: day === "" ? at : `${at} · ${day}`, tone: "gray" };
}

// --- what a card touches --------------------------------------------------------------------------------------------

// The run of a transfer plan, read from the plan key the engine writes ("tr:{dest}:{run}:{qty}:{rest}"); null for
// keep and markdown plans.
export function runOf(planKey: string | null): string | null {
  if (planKey === null || !planKey.startsWith("tr:")) return null;
  const run = planKey.split(":")[2];
  return run === undefined || run === "" ? null : run;
}

export const CHILLED = "Chilled";

// Affects (UI-SPEC "Card" row 4): "S042 · S017 · Logistics (R-02) · Chilled": the source, the destination, the run's
// logistics and the chilled zone, each only when it applies.
export function affectsText(card: Pick<Card61, "source" | "dest" | "suggested_key" | "zone">): string {
  const run = runOf(card.suggested_key);
  return [card.source, card.dest, run === null ? null : LOGISTICS(run), card.zone === "chilled" ? CHILLED : null]
    .filter((x): x is string => x !== null && x !== "").join(" · ");
}

// "Lot L-0042 · S042 → S017", or "Lot L-0042 · S042 · in store" without a destination.
export function lotRoute(card: Pick<Card61, "lot_id" | "source" | "dest">): string {
  return `Lot ${card.lot_id} · ${card.dest === null ? `${card.source} · ${IN_STORE}` : `${card.source} → ${card.dest}`}`;
}

// --- the toolbar's counts (UI-SPEC "Toolbar"): the server's numbers, never counted here -------------------------------

// "Showing 5 of 37": the items on this page against the server's total under the current filters.
export function showingText(n: number, total: number): string {
  return `Showing ${n} of ${total}`;
}

// "Blocked only (3)": the toggle with the server's blocked total under the other filters (C-7).
export function blockedToggleText(n: number): string {
  return `Blocked only (${n})`;
}

// 05.2 D-38's line (05.2-22's wording; defined here until build/product holds 05.2-22's constant, 06.1-30 dedupes):
// "Evaluated 12 of 40 at-risk items" from the server's `evaluated` and `at_risk` under the current scope and filters.
export function evaluatedLine(evaluated: number, atRisk: number): string {
  return `Evaluated ${evaluated} of ${atRisk} at-risk items`;
}
