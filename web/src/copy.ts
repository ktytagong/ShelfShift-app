// The words the web app composes, and the one plain action each contract error code gets (05-UI-SPEC "Interaction
// States and Feedback"; 05-CONTEXT Claude's Discretion "Errors"). Pure, erasable syntax only, so `node --test` runs it.
// Server messages are never rewritten here: an error shows the API `message` word for word, and this file only picks
// the button that goes with it.
export { NETWORK } from "./api.ts";
// The 06.1 vocabulary (vocab.ts) through the same import path: a page imports any word from copy.ts (R-2).
export * from "./vocab.ts";
import type {
  Card, Eligibility, FactCheck, LotCount, MarkdownCrossover, OptionRow, Request, RequestState, Resource, Result,
  Session, Settings, TransportCrossover, WasteCrossover,
} from "./api.ts";
import { COUNT_KINDS, type BoardRisk } from "./derive.ts";
import {
  LOCALE, centsDown, centsUp, dateLabel, expected, factor, markDate, money, signed, units, weekday, zonedToUtc, type Cal,
} from "./format.ts";
import type { IconName, Tone } from "./ui.tsx";
import { cityOf } from "./vocab.ts";

// What the control next to an error offers:
// - reload_case: the stock decision moved on (a newer version, another state, the other side already decided):
//   Reload the stock decision
// - recheck: the booking expired: Recheck and resend
// - retry: a busy, timed-out or unreachable server: Try again, resending the same key and the same body
// - reload_app: the sandbox was reset: the app refreshes the session and returns to the board
// - gate: the session is locked: back to the gate
// - none: the message alone. A cap is not cleared by retrying, a refusal (forbidden, conflict, a bad value) is not
//   cleared by resending, so these are never resent.
export type ErrorAction = "reload_case" | "recheck" | "retry" | "reload_app" | "gate" | "none";

const ACTION: Record<string, ErrorAction> = {
  stale_version: "reload_case",
  wrong_state: "reload_case",
  already_decided: "reload_case",
  hold_expired: "recheck",
  sandbox_reset: "reload_app",
  locked: "gate",
  engine_busy: "retry",
  engine_timeout: "retry",
  engine_unavailable: "retry",
  db_busy: "retry",
  preparing: "retry",
  network: "retry", // no answer at all (the adapter's code)
  error: "retry", // an answer that was not the API's envelope (for example a proxy page): shown as NETWORK
};

export function errorAction(code: string): ErrorAction {
  return Object.hasOwn(ACTION, code) ? ACTION[code] : "none";
}

// True only for the Try again group: a retry resends the same Idempotency-Key and body, never a new submission.
export function retryable(code: string): boolean {
  return errorAction(code) === "retry";
}

// --- the dataset mark (05.2 D-03, D-04 as amended 2026-10-17; 05-UI-SPEC amendments of 2026-10-17) -------------------

// The gate's line and the mark where no session exists yet (the gate, the first load, About without a session).
export const PUBLIC_DATASET = "Public sales dataset";

// The mark every page shows: the session's own label (reference data, never a constant here) with "{date}" filled from
// data_as_of by markDate, so the date is the server's scenario date and never the browser's clock. A label without
// "{date}" (the synthetic test set's) is shown unchanged. With no data_as_of, the " · " part that holds "{date}" is
// left out rather than shown as a placeholder.
export function datasetMark(s: Pick<Session, "label" | "data_as_of">): string {
  if (!s.label.includes("{date}")) return s.label;
  if (s.data_as_of === null) return s.label.split(" · ").filter((part) => !part.includes("{date}")).join(" · ");
  return s.label.replaceAll("{date}", markDate(s.data_as_of));
}

// --- About: the dataset's disclosure (05.2 DATA-16; D-03, D-04; CC BY 4.0 §3(a), §2(a)(6)) ------------------------
// Plain sentences, the same word for word as DATASET.md's paragraphs (tests/data/test_dataset_notice.py reads them from
// this file). They type no figure: the date shift, the units factor and the scenario date are reference data, and
// DATASET.md gives them with the report figures. The retailer the scenario came from is never named.

export const ABOUT_SOURCE =
  "The sales history is FreshRetailNet-50K by Dingdong-Inc (Wang, Gu, Long, Li, Shen, Fu, Zhou, Jiang, 2025), " +
  "https://huggingface.co/datasets/Dingdong-Inc/FreshRetailNet-50K, described in arXiv:2505.16319. © Dingdong-Inc, " +
  "licensed under CC BY 4.0: https://creativecommons.org/licenses/by/4.0/. The dataset is provided as is, without " +
  "warranties. ShelfShift changed it as listed under Modifications. ShelfShift is not affiliated with or endorsed by " +
  "Dingdong-Inc.";

export const ABOUT_KINDS =
  "Public: the daily sales, hours out of stock, discounts, holiday and activity flags, weather, and the city, store, " +
  "product and category codes, all from FreshRetailNet-50K. Assumption: prices, costs, shelf life per category, truck " +
  "runs within a city, storage, the waste price and the units factor, each labelled Assumption with its source. " +
  "Simulated: stock on hand, lots, expiry dates and deliveries, built from the public sales for this scenario and " +
  "never shown as observed.";

export const ABOUT_MODIFICATIONS =
  "Dates are shifted forward by a whole number of weeks, so every date keeps its weekday; the scenario date falls " +
  "inside the shifted held-out week. Units are the public sale_amount times an assumed units factor. Only the " +
  "day-level fields are used, not the hourly arrays. Codes carry a short display prefix. Rows whose discount is " +
  "outside the known range are tagged unknown price and counted. Stock, lots, expiry and deliveries are simulated; " +
  "they are not part of the dataset.";

export const ABOUT_LIMITS =
  "The sales come from an online grocer's front warehouses in China in 2024, so the holiday flags follow that " +
  "calendar. On a day with hours out of stock, the recorded sales understate demand. The stock is simulated, so a " +
  "stock-out in this app is a simulated one, and it seldom falls on a day the public data records as out of stock. " +
  "Store locations and routes are assumed, with travel times derived from them: the dataset has no locations. Prices " +
  "and costs are assumptions, not the grocer's.";

// A paragraph split into text and links: every http(s) address becomes a link, and punctuation that ends the sentence
// or closes a bracket after it ("…/4.0/." or "…/y),") stays text.
export function textLinks(text: string): { text: string; href: string | null }[] {
  const parts: { text: string; href: string | null }[] = [];
  let rest = 0;
  for (const match of text.matchAll(/https?:\/\/\S+/g)) {
    const href = match[0].replace(/[.,;:)]+$/, "");
    const start = match.index ?? 0;
    if (start > rest) parts.push({ text: text.slice(rest, start), href: null });
    parts.push({ text: href, href });
    rest = start + href.length;
  }
  if (rest < text.length) parts.push({ text: text.slice(rest), href: null });
  return parts;
}

// --- the flagged item's rows (05.2-21) --------------------------------------------------------------------------

// One row of a flagged item's body, in display order.
export type BodyRow =
  | { kind: "kv"; label: string; value: string }
  | { kind: "approvals"; cells: { done: boolean; text: string }[] }
  | { kind: "line"; text: string }
  | { kind: "pill"; tone: "good"; text: string };

// --- flagged items and evaluation on open (05.2 D-24, D-25, D-27, D-31; UI-SPEC A-33..A-35, A-40) --------------------

// A lot the screen flagged that the engine has not evaluated: never a best action before the engine has run (D-24).
export const NEEDS_EVALUATION = "Needs evaluation";
// The progress line while the engine runs on open (D-25). No elapsed time and no countdown: planned times only.
export const EVALUATING = "Evaluating this item…";
// The Options footnote's last sentence when stage 1 excluded a destination or the shortlist is on (D-27).
export const restrictedFootnote = "Best among the plans evaluated.";
// Where transfer options would appear for a source store with no location or route data (D-31). It is a data gap,
// never "no beneficial transfer"; keep and markdown work as usual.
export const TRANSFERS_UNAVAILABLE = "Transfers unavailable: location or route data missing";
// The label over the excluded destinations (D-29 stage 1).
export const EXCLUDED_HEADING = "Excluded before the search";

// Each destination stage 1 excluded, as "S031: too late to sell", in the order the API gives (the reasons are the
// server's words, never rewritten).
export function eligibilityLine(e: Pick<Eligibility, "excluded">): string[] {
  return e.excluded.map((x) => `${x.dest}: ${x.reason}`);
}

// A flagged item's body on the board and its page: the screen's figures only, in expected whole units, with the
// markdown step's date from the session calendar. No plan, amount or deadline (D-24); "—" where the screen had none.
export function flagBody(card: Pick<Card, "risk">, cal: Cal): BodyRow[] {
  const r = card.risk;
  const step = r?.step_day == null ? "" : dateLabel(cal, r.step_day);
  const stock = r?.stock_at_step == null ? "—" : `${expected(r.stock_at_step)} units${step === "" ? "" : ` · ${step}`}`;
  return [
    { kind: "kv", label: "At risk of waste", value: r?.keep_waste == null ? "—" : `${expected(r.keep_waste)} units` },
    { kind: "kv", label: "Stock at markdown step", value: stock },
  ];
}

// A lot placed on purpose by the reference data's designed-example rule (05.2 D-18, D-38, D-40; UI-SPEC A-39): its
// label, the tooltip that says what it is, and the tooltip button's name. Shown next to the lot id on the public
// dataset only, so a designed situation is never mistaken for one the simulation produced.
export const DESIGNED_EXAMPLE = "Designed example";
export const DESIGNED_TIP = "A record error placed on purpose to show verify before you move; see About";
export const DESIGNED_TIP_LABEL = "More about the designed example";

// The label of a product's code wherever a product is a field (05.2 D-02; UI-SPEC A-38).
export const PRODUCT_CODE = "Product code";

// The public board's pager line (05.2 D-26; UI-SPEC A-36): "Page 2 of 4 · 37 items", from the server's page,
// page_size and total. Null when everything fits on one page (or there is nothing), which hides the pager.
export function pagerText(page: number, pageSize: number, total: number): string | null {
  const pages = pageSize > 0 ? Math.ceil(total / pageSize) : 1;
  if (pages <= 1) return null;
  return `Page ${units(page)} of ${units(pages)} · ${units(total)} ${total === 1 ? "item" : "items"}`;
}

// The HQ risk filter's options, in order (05.2 D-26; UI-SPEC A-37): every item, the lot's own waste risk, or the
// stock left at its markdown step.
export const RISK_OPTIONS: readonly [BoardRisk, string][] = [["all", "All"], ["waste", "Waste risk"], ["stock", "Stock at step"]];
// The HQ store box's placeholder: the 898 stores are found by typing a code.
export const STORE_PLACEHOLDER = "Type a store code";

// --- the board toolbar, views and List layout (UI-SPEC "Board (UI-03)", "App shell" Views) -------------------------

// The seat as the empty My actions line names it: "Bang Na manager", or "HQ planner".
export function seatName(session: Pick<Session, "role" | "branch">): string {
  return session.role === "hq" || session.branch === null ? "HQ planner" : `${session.branch} manager`;
}

// --- fact checks (UI-SPEC Copywriting "Fact check: …"; spec §6; research Pattern 7) ------------------------------

export type FactOutcome = "not_allowed" | "changes" | "below_guard" | "no_change";

// The engine's flags pick one of four sentences. A not-allowed fact always changes the plan (02 D-29), so feasibility
// is tested first: it never shows a regret figure, whatever its regret.
export function factOutcome(f: FactCheck): FactOutcome {
  if (f.pick_feasible_if_alt === false) return "not_allowed";
  if (f.changes_plan) return "changes";
  if (f.pick_changed) return "below_guard";
  return "no_change";
}

// "PHANTOM_STOCK" → "Phantom stock": a kind this version has no sentence for, in words.
function kindWords(kind: string): string {
  const w = kind.toLowerCase().replace(/_/g, " ").trim();
  return w.charAt(0).toUpperCase() + w.slice(1);
}

const whole = (n: number) => new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 }).format(n);
const kg = (n: number) => new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 2 }).format(n);

type RecordedLot = { lot_id: string; last_verified_day: number | null };

// The stored record's lots (engine_input.lots); a fact may concern another branch's lot, so never the case lot.
function recordedLots(engineInput: Record<string, unknown> | null): RecordedLot[] {
  const lots = engineInput?.lots;
  return Array.isArray(lots) ? (lots as RecordedLot[]) : [];
}

// Why the engine flagged the fact, by kind. A stale count's age is the session day minus the lot's last verified day in
// the stored record, never a constant. The phantom and stale-count lines count from the day the check ran, so for an
// input computed on another day than `today` (a case the API no longer re-evaluates) they give null and no line shows
// (review CR-01).
export function factWhy(f: FactCheck, engineInput: Record<string, unknown> | null, today: number, cal: Cal): string | null {
  void cal;
  const ranToday = typeof engineInput?.today !== "number" || engineInput.today === today;
  switch (f.kind) {
    case "PHANTOM_STOCK":
      if (!ranToday) return null;
      return "On record, but no sales were recorded here yesterday although the forecast expects sales.";
    case "ESTIMATED_EXPIRY":
      return "The expiry date is estimated, not read from the label.";
    case "STALE_COUNT": {
      if (!ranToday) return null;
      const seen = recordedLots(engineInput).find((l) => l.lot_id === f.lot_id)?.last_verified_day;
      return seen == null ? "No count is on record." : `Not counted for ${dayCount(today - seen)}.`;
    }
    default:
      return kindWords(f.kind);
  }
}

// "Lat Phrao · lot MLK-038: recorded 60 units", or "… recorded expiry Sat 17 Oct" for an expiry fact.
export function factTitle(f: FactCheck, cal: Cal): string {
  const where = `${f.branch} · lot ${f.lot_ids.join(", ")}`;
  return f.kind === "ESTIMATED_EXPIRY"
    ? `${where}: recorded expiry ${dateLabel(cal, f.recorded_value)}`
    : `${where}: recorded ${units(f.recorded_value)} units`;
}

// The lead of a plan-changing fact: what acting on the record leaves if the fact is wrong, and the best plan then.
function changeLead(f: FactCheck, cal: Cal): string {
  const best = f.best_plan_if_alt_label;
  if (COUNT_KINDS.includes(f.kind)) {
    return `If ${f.branch}'s ${whole(f.recorded_value - f.alt_value)} units aren't there, acting on the record leaves `
      + `${expected(f.unserved_units)} units of demand unserved that the best plan would serve. The best plan would be: ${best}.`;
  }
  if (f.kind === "ESTIMATED_EXPIRY") {
    return `If lot ${f.lot_id} expires ${weekday(cal, f.alt_value)} instead of ${weekday(cal, f.recorded_value)}, `
      + `the best plan would be: ${best}.`;
  }
  return `The best plan would be: ${best}.`;
}

// The outcome sentence of a fact-check row. A decision regret is a policy-scored amount, never cash: its net-revenue
// part is money and its waste-policy part a policy value, kept apart. The policy clause follows the stored input's
// waste price (engine_input.policy.waste_price), never the amount: absent at 0, kept above 0 even when it is 0.
export function factSentence(f: FactCheck, currency: string, wastePrice: number, cal: Cal): string {
  const best = f.best_plan_if_alt_label;
  switch (factOutcome(f)) {
    case "not_allowed":
      return `If this fact is true, the recommended plan is not allowed. Best plan then: ${best}.`;
    case "changes": {
      const head = `The count changes the plan. Decision regret ${money(f.decision_regret, currency)}: `
        + `net revenue ${signed(f.net_revenue_difference, currency)}`;
      const k = f.waste_kg_difference;
      const policy = wastePrice > 0
        ? `, waste policy ${signed(f.waste_policy_difference, currency)} (${kg(Math.abs(k))} kg `
          + `${k < 0 ? "more" : "less"} waste; a policy value, not cash)`
        : "";
      return `${changeLead(f, cal)} ${head}${policy}.`;
    }
    case "below_guard": // the user's wording, word for word, with no amount and no plan label (G-05-12 (c), 2026-10-16)
      return "A stock count could change the recommended plan, but the estimated impact is below the threshold for prioritizing a check.";
    default:
      return "Checked, wouldn't change the plan.";
  }
}

// --- the Options tab (UI-SPEC "Options (UI-04, ENG-04, D-08)"; Amendments A-1 to A-4) -----------------------------

// The "Best if low" row: the first option, in the engine's order, with the highest score.low (a tie keeps the first);
// null when that row is the suggested one. Nothing is sorted: the rows stay in the engine's order.
export function bestIfLow(options: OptionRow[], recommended: string): string | null {
  let best: OptionRow | null = null;
  for (const o of options) if (best === null || o.score.low > best.score.low) best = o;
  return best === null || best.key === recommended ? null : best.key;
}

// The transport crossover (A-1; spec §6): the run's quote at which the plan changes. A plan that uses the run stays
// best until the quote rises to it; one that does not loses to a transfer when the quote falls below it. The engine
// returns the threshold rounded to the cent (engine/crossover.py), and that cent is shown as it is: a coarser step
// can overstate it, making "until $X" or "below $X" false for quotes in between (G-05-12 (a)). With no change, the
// range is the crossover's own, never a constant. No transfer run: no line.
export function transportLine(c: TransportCrossover, currency: string): string | null {
  if (c.run_code === null) return null;
  if (c.result === "no_change_in_range") {
    return `No ${c.run_code} quote from ${money(c.range[0], currency)} to ${money(c.range[1], currency)} changes the plan.`;
  }
  const x = money(c.value, currency);
  return c.direction === "rises"
    ? `This plan stays best until ${c.run_code} costs ${x}.`
    : `A transfer would win if ${c.run_code} cost below ${x}.`;
}

// The markdown response crossover (A-2): approximate, on a grid, so every sentence ends with "(approx.)".
export function markdownLine(c: MarkdownCrossover): string {
  switch (c.result) {
    case "threshold":
      return `The plan changes if the 20% markdown lifts sales by ×${factor(c.value.m20)} (approx.).`;
    case "multiple_changes":
      return "The plan changes more than once across the tested markdown responses (approx.).";
    default:
      return "No markdown response in the tested range changes the plan (approx.).";
  }
}

// One waste-flip line and its tooltip (null for a line with none): the tooltip says the recommendation can change again.
export type FlipLine = { text: string; tip: string | null };

const RISE_TIP = "Higher waste prices may change the recommendation again."; // user wording, 2026-10-16
const FALL_TIP = "Lower waste prices may change the recommendation again.";

// The number a rising (ceil) or falling (floor) flip amount shows: the same rounding the display uses, at the
// currency's own fraction digits, read back as a number.
function shownCents(v: number, currency: string, mode: "ceil" | "floor"): number {
  const digits = new Intl.NumberFormat(LOCALE, { style: "currency", currency }).resolvedOptions().maximumFractionDigits;
  return Number(new Intl.NumberFormat(LOCALE, {
    minimumFractionDigits: digits, maximumFractionDigits: digits, roundingMode: mode, useGrouping: false,
  }).format(v));
}

// The tie test (user decision 2, 2026-10-16). engine/crossover.py reports `value` as the smallest float at or above the
// exact crossing (one float step more when the ranking breaks a tie there), and its winner ranks first on the half-open
// interval [falls_at, rises_at). So a shown cent within float precision of `value` is the crossing itself, where the
// two plans' scores tie and no plan "becomes preferred". The engine output cannot tell a tie rank_key breaks from a
// strict win at that point, so the client names a boundary there. No engine change.
function isCrossing(shown: number, value: number): boolean {
  return Math.abs(shown - value) <= 4 * Number.EPSILON * Math.abs(value);
}

// The flip-price lines (D-08; A-3, A-4; G-05-12 (d)), shown at every waste price, a price of 0 included. The engine
// reports only the nearest change each way, and the winner can change again further on, so a line names that next
// change and never a half-line of prices ("or more", "below"); its tooltip says so. A rising flip rounds up to the cent
// and a falling one down, so the stated price is always on the far side of the exact crossing, or is the crossing
// itself, which then reads as a boundary; each names the crossover's own label. A falling flip that floors to zero
// keeps A-3's line, checked first. The range is the crossover's own (`range[1]`), never a constant, and the client
// never recomputes a crossing.
export function flipLines(w: WasteCrossover, currency: string): FlipLine[] {
  const zero = money(0, currency);
  const price = money(w.price, currency);
  const max = money(w.range[1], currency);
  const up = w.rises_at;
  const down = w.falls_at;
  if (up.result === "no_change_in_range" && down.result === "no_change_in_range" && w.price > 0) {
    return [{ text: `No waste policy from ${zero}/kg to ${max}/kg changes the plan.`, tip: null }];
  }
  const lines: FlipLine[] = [];
  if (up.result === "threshold") {
    const x = centsUp(up.value, currency);
    lines.push({
      text: isCrossing(shownCents(up.value, currency, "ceil"), up.value)
        ? `The next recommendation boundary above the current price is ${x}/kg.`
        : `The next recommendation change occurs at ${x}/kg: ${up.label} becomes preferred.`,
      tip: RISE_TIP,
    });
  } else if (w.price < w.range[1]) {
    lines.push({ text: `No waste policy from ${price}/kg to ${max}/kg changes the plan.`, tip: null });
  }
  if (down.result === "threshold") {
    const y = centsDown(down.value, currency);
    if (y === zero) {
      lines.push({ text: `At a waste policy of ${zero}/kg the engine would choose ${down.label}.`, tip: null });
    } else {
      lines.push({
        text: isCrossing(shownCents(down.value, currency, "floor"), down.value)
          ? `The next recommendation boundary below the current price is ${y}/kg.`
          : `The next recommendation change below the current price occurs at ${y}/kg: ${down.label} becomes preferred.`,
        tip: FALL_TIP,
      });
    }
  } else if (w.price > 0) {
    lines.push({ text: `No waste policy from ${zero}/kg to ${price}/kg changes the plan.`, tip: null });
  }
  return lines;
}

// The Options footnote. The score sentence joins only when the stored waste price is above 0 (D-08), where the score
// column shows; the shortlist label only when the engine searched a shortlist. A restricted search (stage 1 excluded a
// destination, or the shortlist is on: 05.2 D-27) ends with "Best among the plans evaluated.".
export function optionsFootnote(
  r: Pick<Result, "candidates_checked" | "shortlist">, wastePrice: number, restricted = false,
): string {
  const shortlist = r.shortlist === undefined ? "" : `; ${r.shortlist.label}`;
  const text = "Scenarios are not probabilities: low and high are the forecast's 80% interval. "
    + `${units(r.candidates_checked)} plans searched${shortlist}. One destination per plan. `
    + "Figures are simulated or estimated.";
  const scored = wastePrice > 0
    ? `${text} Score = net − waste price × kg wasted. The waste price is a retailer policy, not a cash cost.`
    : text;
  return restricted ? `${scored} ${restrictedFootnote}` : scored;
}

// --- count requests (UI-SPEC "Verification line", "Requests", Copywriting "Request status"; Amendments A-8 to A-10) --

// A SUPERSEDED request's pill and status, by kind (user decision 6, 2026-10-16): a count request (a kind a count
// settles) was replaced by a recount; a request of any other kind (expiry or combined checks, Phase 7) by a new request.
export function replacedText(r: Pick<Request, "fact_kind">): string {
  return COUNT_KINDS.includes(r.fact_kind) ? "Replaced by a recount" : "Replaced by a new request";
}

const STATE_PILL: Record<Exclude<RequestState, "SUPERSEDED">, { tone: Tone; text: string; icon: IconName }> = {
  OPEN: { tone: "gray", text: "Open", icon: "inbox" }, // a legacy row without the server's sentence
  ANSWERED: { tone: "warn", text: "Answered · confirm the count", icon: "alert" },
  CONFIRMED: { tone: "good", text: "Confirmed", icon: "check" },
  EXPIRED: { tone: "gray", text: "Expired", icon: "clock" }, // legacy rows only: nothing sets EXPIRED since Phase 7
  CANT_CHECK: { tone: "gray", text: "Can't check", icon: "x" },
  HELD: { tone: "bad", text: "Mixed dates · lot held", icon: "lock" },
};

// The server's sentence for an open request part (07-CONTEXT D-06; 04-API-CONTRACT Request `status_kind`), shown word
// for word in its tone: "Awaiting response · respond by {t}", "Unanswered · Unverified", "Photo received · count
// missing" and the partial note, each " · overdue" once due.
const SENTENCE: Record<string, { tone: Tone; icon: IconName }> = {
  AWAITING: { tone: "info", icon: "clock" },
  UNANSWERED: { tone: "warn", icon: "clock" },
  PHOTO_PARTIAL: { tone: "warn", icon: "alert" },
  TEXT_PARTIAL: { tone: "warn", icon: "alert" },
};

type PillInput = Pick<Request, "state" | "fact_kind"> & Partial<Pick<Request, "status" | "status_kind" | "part">>;

function pillOf(r: PillInput): { tone: Tone; text: string; icon: IconName } {
  const said = r.status_kind === undefined ? undefined : SENTENCE[r.status_kind];
  if (said !== undefined && typeof r.status === "string") return { ...said, text: r.status };
  if (r.state === "SUPERSEDED") return { tone: "gray", text: replacedText(r), icon: "redo" };
  if (r.state === "ANSWERED" && r.part === "expiry") return { tone: "warn", text: "Answered · confirm the printed date", icon: "alert" };
  const known = Object.hasOwn(STATE_PILL, r.state) ? STATE_PILL[r.state as Exclude<RequestState, "SUPERSEDED">] : undefined;
  return known ?? { tone: "gray", text: typeof r.status === "string" && r.status !== "" ? r.status : String(r.state), icon: "info" };
}

// A request's state pill on Tasks: the server's sentence while a part is open (D-06), the stored states' labels
// (SUPERSEDED grey and by kind, user decision 6), and a grey pill with the server's text for a state the page does not
// know, never undefined.
export function statePill(r: PillInput): { tone: Tone; text: string } {
  const { tone, text } = pillOf(r);
  return { tone, text };
}

// The pill's icon, so no chip is colour alone (R-6).
export function stateIcon(r: PillInput): IconName {
  return pillOf(r).icon;
}

// --- "When did you check?" (07-CONTEXT D-22; 07-UI-SPEC CheckTime and its errors) ------------------------------------

export const CHECK_TIME = {
  legend: "When did you check?",
  now: "Checked just now",
  at: "At a date and time",
  missing: "Choose when you checked: Checked just now, or a date and time.",
  later: "The check time can't be later than now.",
} as const;

// "Date and time checked (Bangkok time)": the city of the session's zone (caps.time_zone).
export const checkTimeLabel = (timeZone: string) => `Date and time checked (${cityOf(timeZone)} time)`;

// The draft's choice: none on load (no default, D-22), "now", or a typed `datetime-local` value.
export type CheckTimeValue = { mode: "" | "now" | "at"; at: string };
export const NO_CHECK_TIME: CheckTimeValue = { mode: "", at: "" };

// The `observed` field for an answer, or the reason it can't be sent: no choice (or a time it can't read) is
// CHECK_TIME.missing; a typed time later than `nowMs` is CHECK_TIME.later. A typed time is wall time in `timeZone`,
// sent as UTC; "Checked just now" sends "now", and the server records its own time (D-22).
export function checkTimeObserved(v: CheckTimeValue, timeZone: string, nowMs: number): { observed: string } | { error: string } {
  if (v.mode === "now") return { observed: "now" };
  if (v.mode !== "at") return { error: CHECK_TIME.missing };
  const iso = zonedToUtc(v.at, timeZone);
  if (iso === null) return { error: CHECK_TIME.missing };
  return Date.parse(iso) > nowMs ? { error: CHECK_TIME.later } : { observed: iso };
}

// --- the response summary (user wording 2026-10-16; the user's decisions 3 to 6 of the 05 plan revision) ------------

// A person's recorded action time, stored as UTC and shown in the session's `time_zone` (Asia/Bangkok, settings
// caps.time_zone) as "16 Oct 2026, 20:30": day, short month name, year, 24-hour time (user decision 3, 2026-10-16;
// spec rev 6.7 §5 "Timestamps are stored as timezone-aware UTC and displayed in Asia/Bangkok" and §9 "actual times
// appear only when someone records the work"; 07-CONTEXT D-09). The zone is always the caller's session.time_zone,
// never a constant here and never Date's local getters, so the browser's own zone never changes the result. Composed
// from the formatter's parts, so no locale punctuation leaks in. Planned and dataset times never go through it (they
// stay calendar dates). "" for a string Date cannot read or a zone Intl does not know. 05-21 reuses it for history
// and Activity lines.
export function recordedTime(iso: string, timeZone: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  let parts: Intl.DateTimeFormatPart[];
  try {
    parts = new Intl.DateTimeFormat(LOCALE, {
      timeZone, day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(at);
  } catch {
    return "";
  }
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("day")} ${part("month")} ${part("year")}, ${part("hour")}:${part("minute")}`;
}

const countPart = (name: string, v: number | null) => (v === null ? `${name}: not checked` : `${name} ${units(v)}`);

// One lot's count as the Requests page shows it: "Shelf 4 · Backroom 2 · Total 6 units"; a blank part reads
// "Shelf: not checked" (or "Backroom: not checked") and then there is no total and no unit. The reference data names no
// per-product selling unit (stock is counted in units everywhere, e.g. `units_per_tote` "units per tote"), so the
// word is "units", the user's fallback (user decision 4, 2026-10-16).
export function lotCountText(c: LotCount): string {
  const text = `${countPart("Shelf", c.shelf_units)} · ${countPart("Backroom", c.backroom_units)}`;
  return c.shelf_units === null || c.backroom_units === null
    ? text : `${text} · Total ${units(c.shelf_units + c.backroom_units)} units`;
}

// The response summary's Status (user decision 5, 2026-10-16): ANSWERED "Awaiting confirmation", CONFIRMED
// "Confirmed", SUPERSEDED "Not confirmed · replaced by a recount" for a count request and "Not confirmed · replaced by a
// new request" for another kind (Claude's mirror of decision 6, for the user to confirm). Other states show no summary
// and read their pill.
export function responseStatus(r: Pick<Request, "state" | "fact_kind">): string {
  switch (r.state) {
    case "ANSWERED":
      return "Awaiting confirmation";
    case "CONFIRMED":
      return "Confirmed";
    case "SUPERSEDED":
      return COUNT_KINDS.includes(r.fact_kind) ? "Not confirmed · replaced by a recount" : "Not confirmed · replaced by a new request";
    default:
      return statePill(r).text;
  }
}

// "Answered after deadline" when the answer's recorded sandbox day is after the request's due day, in any state (it
// stays after the confirm: 07-CONTEXT D-08), else null. Shown as its own line, never joined to the Status. The due is a
// planned dataset time and no demo minute is recorded for the answer, so Phase 5 compares sandbox days, the one
// comparison on one clock it holds; Phase 7 D-05's real 60-minute deadline replaces this.
// From Phase 7 a request with a real deadline (`due_at`) reads the server's `late` (07-CONTEXT D-05, D-08).
export function responseNote(r: Pick<Request, "due_day" | "answer"> & Partial<Pick<Request, "due_at" | "late">>): string | null {
  if (r.answer === null) return null;
  if (r.due_at != null) return r.late === true ? "Answered after deadline" : null;
  return r.answer.day > r.due_day ? "Answered after deadline" : null;
}

// The read-only response summary of an answered request, as label/value rows: one row per lot ("Lot {id}" with
// lotCountText), "Reply" when the reply is not empty, "Submitted by" the answer's RECORDED seat (reporter_branch and
// reporter_role, never a branch or role the client assumes; user decision 4), "Submitted" the answer revision's
// recorded server time in `timeZone` (left out when it cannot be read; user decision 3), then "Status". The caller
// passes the session's time_zone and a request with an answer; with none there are no rows.
export function responseRows(r: Request, timeZone: string): [string, string][] {
  const a = r.answer;
  if (a === null) return [];
  const rows: [string, string][] = r.lot_ids.map((id) => [
    `Lot ${id}`, lotCountText(a.fields.counts[id] ?? { shelf_units: null, backroom_units: null }),
  ]);
  if (a.text !== "") rows.push(["Reply", a.text]);
  rows.push(["Submitted by", seatName({ role: a.reporter_role, branch: a.reporter_branch })]);
  const at = recordedTime(a.created_at, timeZone);
  if (at !== "") rows.push(["Submitted", at]);
  rows.push(["Status", responseStatus(r)]);
  return rows;
}

// One green line per lot of a confirmed request, from its stored answer: "Count confirmed by Lat Phrao manager ·
// shelf 4 + backroom 2 = 6 · the plan was recomputed". A lot without both counts adds no line.
export function confirmedLines(r: Pick<Request, "to_branch" | "lot_ids" | "answer">): string[] {
  const counts = r.answer?.fields.counts ?? {};
  return r.lot_ids.flatMap((id) => {
    const c = counts[id];
    if (c === undefined || c.shelf_units === null || c.backroom_units === null) return [];
    return [`Count confirmed by ${r.to_branch} manager · shelf ${units(c.shelf_units)} + backroom `
      + `${units(c.backroom_units)} = ${units(c.shelf_units + c.backroom_units)} · the plan was recomputed`];
  });
}

// --- the Approvals tab (UI-SPEC "Approvals (UI-04)", Copywriting "Hold") -------------------------------------------

const UNIT_WORDS: Record<Resource["unit"], [string, string]> = {
  units: ["unit", "units"],
  totes: ["tote", "totes"],
  right: ["right", "rights"], // the right to change the lot's price (LOT_PRICE)
};

// A Resources to hold amount with its unit ("120 units", "1 tote"); "—" when the API has no figure.
export function neededText(n: number | null, unit: Resource["unit"]): string {
  if (n === null) return "—";
  const [one, many] = UNIT_WORDS[unit] ?? [unit, unit];
  return `${units(n)} ${n === 1 ? one : many}`;
}

// --- Export case (UI-SPEC "Header" item 6; contract Export; T-05-24) ------------------------------------------------

// The saved file's name: the case key with only ASCII letters, digits and hyphens kept, plus ".json" ("SUS-19.json"),
// so no path separator, dot or control character from server data reaches the visitor's disk; "case.json" when
// nothing is left.
export function exportFilename(key: string): string {
  const safe = key.replace(/[^A-Za-z0-9-]/g, "");
  return `${safe === "" ? "case" : safe}.json`;
}

// --- Policy settings (UI-SPEC "Policy settings (D-06, D-07)"; contract Settings) ------------------------------------
// Every bound and unit comes from the GET /api/settings response (api/settings_bounds.json on the server); none is
// written here. The waste price is a policy value, never cash.

// The two fields the page shows; shortlist_step stays API-only (D-07).
export type SettingsField = "waste_price" | "min_sellable_days";
export const SETTINGS_FIELDS: readonly SettingsField[] = ["waste_price", "min_sellable_days"];

// The visitor's typed text per field, kept as a string so a decimal point survives typing; a field never typed into
// is absent and shows its loaded value.
export type SettingsDraft = Partial<Record<SettingsField, string>>;

// A bound as plain text: at most two fraction digits, grouped ("0", "100", "12.25").
const boundText = (v: number) => new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 2 }).format(v);

// The helper under each field, from the response's bounds: "Between {min} and {max} {unit}. A retailer policy, not a
// cash cost." for the waste price and "Between {min} and {max} days on arrival." for min sellable days (its unit is
// the bounds' own, "days").
export function boundsHelp(field: SettingsField, bounds: Settings["bounds"]): string {
  const b = bounds[field];
  const range = `Between ${boundText(b.min)} and ${boundText(b.max)} ${b.unit}`;
  return field === "waste_price" ? `${range}. A retailer policy, not a cash cost.` : `${range} on arrival.`;
}

// A value as read-only text with its unit from the bounds ("0.37 {currency}/kg", "2 days"), for a session that is not
// in demo mode.
// One day-count form for every count of days on screen (G-05-12 (e)): "day" only for exactly 1, "days" otherwise
// ("0 days", "1 day", "2 days"). Plain text, not Intl plural rules; the number goes through units().
export function dayWord(n: number): string {
  return n === 1 ? "day" : "days";
}
export function dayCount(n: number): string {
  return `${units(n)} ${dayWord(n)}`;
}

// The PATCH `changes`: both fields (UI-SPEC E12/E14 partial), each at its typed value or, when untouched, its loaded
// value, as numbers. A blank field is NaN (sent as null and refused), never 0; the browser blocks it first.
export function settingsChanges(loaded: Settings["values"], edited: SettingsDraft): Record<SettingsField, number> {
  const value = (f: SettingsField) => {
    const text = edited[f];
    if (text === undefined) return loaded[f];
    return text.trim() === "" ? Number.NaN : Number(text);
  };
  return { waste_price: value("waste_price"), min_sellable_days: value("min_sellable_days") };
}
// Reports labels (proposed); shared with the Decision board's Reports filter.
export const REPORT_FIGURES = {
  open: "Open decisions", pending_approval: "Pending approvals", unanswered_check: "Unanswered checks",
  committed: "Committed plans", cancelled: "Cancellations",
} as const;

// --- the case lot's projected waste and the plan totals' scope (quick 261018-h9t) -------------------------------------

// "If nothing changes, 18 units of lot S229-P38-2-r (9 kg; 48.24 at full price, in the session currency) are projected to expire unsold
// by Sat 17 Oct, even with the store's markdown schedule." from the server's lot figures (at_risk_units, at_risk_kg,
// at_risk_value) only; a part the detail cannot give is left out. (approved by the user 2026-10-18)
export function triggerReason(parts: { units: number | null; kg: number | null; value: string | null; lot: string; by: string | null }): string | null {
  if (parts.units === null) return null;
  const inner = [parts.kg === null ? null : `${expected(parts.kg)} kg`, parts.value === null ? null : `${parts.value} at full price`]
    .filter((x): x is string => x !== null);
  const paren = inner.length === 0 ? "" : ` (${inner.join("; ")})`;
  const by = parts.by === null ? "" : ` by ${parts.by}`;
  return `If nothing changes, ${expected(parts.units)} units of lot ${parts.lot}${paren} are projected to expire unsold${by}, `
    + "even with the store's markdown schedule."; // (approved by the user 2026-10-18)
}

// The decision page's risk fact: "18 units · 48.24 at full price, projected to expire unsold" (the amount in the session currency).
export function riskFact(units: number, value: string | null): string {
  const worth = value === null ? "" : ` · ${value} at full price`;
  return `${expected(units)} units${worth}, projected to expire unsold`; // (approved by the user 2026-10-18)
}

// Analytics' potential-waste summary and value column (approved by the user 2026-10-18).
export const wasteSummary = (n: number, lots: number) =>
  `${expected(n)} units in ${lots} lots are projected to expire unsold if nothing changes.`;
export const VALUE_AT_FULL_PRICE = "Value at full price";

// The branches the plan totals cover: exactly the ones the engine simulates, every branch with a forecast or a lot in
// the stored input (engine/simulate.py), so a destination with no stock yet counts and a branch with only a delivery
// does not. The source first, then the others sorted, each once.
export function planBranches(engineInput: Record<string, unknown> | null, source: string): string[] {
  const rows = [engineInput?.forecasts, engineInput?.lots].flatMap((x) => (Array.isArray(x) ? (x as { branch?: unknown }[]) : []));
  const others = [...new Set(rows.map((r) => r.branch).filter((b): b is string => typeof b === "string" && b !== source))].sort();
  return [source, ...others];
}

// "Sold and wasted figures are totals for S229, S189 and S241 together." under the Options table and the Compare view.
export function totalsScope(branches: string[]): string {
  if (branches.length <= 1) return `Sold and wasted figures are totals for ${branches[0] ?? ""}.`; // (proposed) 261018-h9t
  const list = `${branches.slice(0, -1).join(", ")} and ${branches[branches.length - 1]}`;
  return `Sold and wasted figures are totals for ${list} together.`; // (proposed) 261018-h9t
}

// --- 07-21: the stock decision page's verification words (07-UI-SPEC surface 1, "Fact checks and the commitment
// rule", "Request actions", "Consequential actions"; 07-CONTEXT D-10, D-12, D-36). Own block, additive. The locked
// strings are word for word; "(proposed)" marks Claude's wording, one constant each. The numbers come from the server
// (requests_left, the session's `requests`), never a constant here.
import type { Blocker } from "./derive.ts";

export const REQUIRED_CHIP = "Required for commitment";
export const ADVISORY_CHIP = "Advisory · acknowledge at approval";
export const COUNT_STOCK = "Count stock"; // locked (D-36)
export const CHECK_PRINTED_EXPIRY = "Check printed expiry date"; // locked (D-36)
export const SEND_REQUEST = "Send request"; // D-36
export const DONT_SEND_REQUEST = "Don't send request";
export const ASK_AGAIN = "Ask again";
export const SUPERSEDE_NOTE = "The earlier request becomes Superseded. A late answer to it is kept but can't be confirmed.";
export const REQUEST_PANEL_NAME = "Request a check"; // (proposed) the panel's accessible name
export const requestsLeftLine = (left: number, max: number) => `${units(left)} of ${units(max)} requests left on this version`;
export const requestsUsedLine = (max: number) => `${units(max)} of ${units(max)} requests used on this version.`;
export const responseMinutesLine = (min: number) => `The branch has ${units(min)} minutes to respond.`;
export const oneRequestLine = (max: number) => `One request · counts once toward the ${units(max)} per version`;
export const responsibleLine = (store: string) => `Responsible: ${store} manager`;
export const COMMITMENT = "Commitment"; // the facts grid tile's label
export const BLOCKS_COMMITMENT = "Blocks commitment";
export const NO_BLOCKING_CHECKS = "No blocking checks"; // never "safe", "verified" or "clear" (VER-06)
export const advisoryToAcknowledge = (n: number) => `${units(n)} advisory to acknowledge at approval`;
export const moreBlockers = (n: number) => `+${units(n)} more`;
// One blocker line: "Required: {fact title} · owner {store} manager" (with the server's Blocked label when it has
// one) or "Lot hold: lot {lot} · owner {store} manager".
export function blockerText(b: Blocker): string {
  if (b.kind === "lot_hold") return `Lot hold: lot ${b.lot} · owner ${b.owner} manager`;
  const line = `Required: ${b.title} · owner ${b.owner} manager`;
  return b.blocked === null ? line : `${line} · ${b.blocked}`;
}
export const cantApproveYet = (blocker: string) => `Can't approve yet: ${blocker}`;
export const commitmentWaits = (blocker: string) => `Your approval is recorded. Commitment waits for: ${blocker}`;
export const cantPrepareYet = (blocker: string) => `Can't prepare yet: ${blocker}`; // (proposed; Prepare's reason)
// The compact deadline's minutes, from the server's minutes_left (D-06: display only; the state is the server's).
export const minutesLeftText = (n: number, overdue: boolean) =>
  (overdue ? "Overdue" : n >= 1 ? `${units(n)} min left` : "Less than 1 min left");
// The requester's view of a complete answer not yet confirmed (07-UI-SPEC "Request and answer states").
export const awaitingConfirmText = (part: "count" | "expiry", store: string) =>
  `${part === "count" ? "Count" : "Printed date"} awaiting ${store} manager`;
// The ways forward from a blocked Required check (07-UI-SPEC "Exits from a blocked Required row", "Consequential
// actions"; D-23, D-36; "case" reads "stock decision" on screen, 06.1 D-61).
export const SELECT_ANOTHER_PLAN = "Select another plan";
export const ALSO_NEEDS_THIS_FACT = "Also needs this fact verified";
export const SELECT_OPTION = "Select"; // each option's button; its accessible name adds the option
export const selectOptionName = (label: string) => `Select ${label}`;
export const selectConfirmLine = (label: string) => `Switch this stock decision to ${label}? It goes through the normal `
  + "feasibility checks again, any pending approvals are cancelled, and approvals start over.";
export const SELECT_THIS_PLAN = "Select this plan";
export const KEEP_CURRENT_PLAN = "Keep current plan";
export const OPTION_NET = (amount: string) => `Net revenue, base ${amount}`; // the option's outcome (the engine's)
// "Selected plan · Keep · by S017 · 18 Oct 2026, 14:05 Bangkok time" (D-23; a recorded time, 06.1 D-58).
export function selectedPlanLine(label: string, by: string | null, at: string | null, timeZone: string): string {
  const when = at === null ? "" : recordedTime(at, timeZone);
  return ["Selected plan", label, by === null ? "" : `by ${by}`, when === "" ? "" : `${when} ${cityOf(timeZone)} time`]
    .filter((x) => x !== "").join(" · ");
}
// "Cancelled by S017 · 18 Oct 2026, 14:05 Bangkok time · {reason}" (D-23, D-25: the UI label is Cancelled).
export function cancelledLine(by: string | null, at: string, reason: string | null, timeZone: string): string {
  const when = recordedTime(at, timeZone);
  return [by === null ? "Cancelled" : `Cancelled by ${by}`, when === "" ? "" : `${when} ${cityOf(timeZone)} time`, reason ?? ""]
    .filter((x) => x !== "").join(" · ");
}

 // S7: count-only evaluation copy; server notes and reasons are never rewritten.
export const EVAL_COPY = {
  label: "Preliminary evaluation · 10 scenarios × 3 runs",
  empty: "AI evaluation not yet published",
  emptyBody: "The evaluation runs on the released app after its scenarios and expected results are locked. Results appear here once a person has reviewed every item.",
  scope: "Measures the Gemini assistant and report reading on fixed scenarios, separately from the engine's results. Scenarios, expected results and the scoring checklist were written and locked before any run. Each scenario ran 3 times, and every run is shown.",
  noFailures: "No failed items in this group.",
  invalid: "Invalid · not scored",
  mismatch: "Run on an earlier assistant version · not representative of the current app",
  loading: "Loading the evaluation…",
  generationFailed: "Generation failed · protection passed",
  attempted: "Attempted, not executed",
  safetyPass: "Safety gate passed",
  safetyFail: "Safety gate failed · not published",
  pass: "Pass", fail: "Fail", na: "N/A", notScored: "Not scored", notScoredYet: "Not scored yet",
  safety: "Safety gate", scenario: "Scenario", check: "Check", modelAttempted: "Model attempted",
  shownExecuted: "Shown or executed", result: "Result",
  notice: "The latest evaluation run is not published: {reason}.",
  datedNotice: "The latest evaluation run ({date}) is not published: {reason}.",
  group: "{p} of {a} applicable items passed across 3 runs · {f} failed · {x} N/A",
  usage: "{calls} Gemini calls · {tokens} tokens · run under its own limit, separate from reviewers' daily AI allowance",
} as const;
export const ANSWER_COPY = {
  title: "Read by Gemini", pending: "Pending manager check", loading: "Reading evidence…",
  countConflict: "Differs from the typed count", dateConflict: "Differs from the typed date",
  confirmedConflict: "Differs from the confirmed count", history: "Read after confirmation · not applied",
  chooseCount: "Choose which count to confirm.", chooseDate: "Choose which date to confirm.",
  countChoice: "Count to confirm", dateChoice: "Date to confirm",
  failed: "Gemini couldn't read this answer. The typed values, reply and photo are kept.",
  daily: "Today's AI limit is reached. It resets at 00:00 Bangkok time.",
  instructions: "Possible instructions to the assistant detected · treated as report text.",
} as const;

// Evidence-first report sheet (10-UI-SPEC S1). Every suggestion remains a person's choice.
export const REPORT_READING = {
  evidence: "Add evidence", review: "Review suggested fields", preview: "Preview impact", submit: "Submit",
  analyse: "Next · analyse report", manual: "Next · enter details", next: "Next · preview impact",
  helper: "Paste a message or attach a photo or PDF. Gemini suggests the report fields, and you review each one. To fill in the fields yourself, leave both empty.",
  message: "Message (optional)", reading: "Reading evidence…", back: "Back to evidence",
  kept: "Your reviewed fields are kept. A new reading only adds suggestions for you to review.",
  validation: "Review each suggested field: Accept, Edit or Clear.",
  injection: "Possible instructions to the assistant detected · treated as report text.",
  irrelevant: "Gemini didn't identify a supported operational fact. You can choose a report kind and enter the details yourself.",
  keep: "Keep Gemini's suggestion: Not relevant", choose: "Choose a report kind",
  noImpact: "A Not relevant report changes no facts and runs no impact preview. Its owner can dismiss it.",
  failed: "Gemini couldn't read this report. Your message and file are kept. Choose a report kind and enter the details yourself.",
  empty: "Gemini found no fields to suggest. Choose a report kind and enter the details yourself.",
  limit: "Today's AI limit is reached. It resets at 00:00 Bangkok time.",
  noRequest: "No AI request is used to read the form.", copyQuestion: "Copy my question into the message",
  caseTitle: "Report an observation for this case.", hq: "HQ files transport reports only",
} as const;

export const ASK_DROP = {
  hint: "Drop a photo or PDF here to report an observation.",
  dragging: "Drop to report an observation",
  open: "Report with a file",
  count: "Drop one file at a time.",
  type: "Only PNG, JPEG or PDF files can be attached.",
  size: "This file is larger than 5 MB. Choose a smaller file.",
  unavailable: "File uploads are unavailable. Reload this page and try again.",
} as const;
