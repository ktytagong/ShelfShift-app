// The stock decision page (sketch 006 A; 06.1-UI-SPEC "Stock decision page"; 06.1-CONTEXT D-20, D-24..D-26, D-36,
// D-50, D-58, D-67). "#/cases/{key}" shows a persistent summary (breadcrumb, product code and lot, chips, the one
// primary action for the signed-in role, the scenario-date line and the facts grid), the nine-step stepper, one step's
// panel at a time (the current step by default, "?step=" for another) with previous and next, and the latest history
// under every step. No nav link: a stock decision is reached from the Decision board, Today or Tasks.
// Rules kept here:
// - The current step, the stage, the owner and the next action are the server's (D-24): the stepper only chooses
//   which panel is viewed, and a step is never derived from a state here.
// - Step panels are modules under web/src/decision/steps/ (decision/types.ts), found through import.meta.glob by their
//   key: every one of the nine steps has its module, and a module may also put an action in its panel's header.
// - Loads on entry, on the tab becoming visible, on a session change and after every write (hooks.useCase); a booking
//   hold refreshes once at its end from the server's clock (useRefreshAt, D-57). No interval polling.
// - No AI brief or placeholder before Phase 9 (D-26). Times: recorded times read "Recorded … Bangkok time" and
//   operational dates "Scenario date: …" (D-58); no "today" or countdown anywhere.
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { CaseDetail, Session } from "../api.ts";
import { historyFilter, segmentStates, type HistoryFilter, type SegmentState } from "../bars.ts";
import type { CaseDetail61, Session61 } from "../contract.ts";
import { NEEDS_EVALUATION, PRODUCT_CODE, retryable, riskFact } from "../copy.ts";
import { productLabel } from "../derive.ts";
import { dateLabel, hhmm, money, units } from "../format.ts";
import { useActivity, useCase, useRefreshAt, type ActivityLoad } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { NotFound, PageHead, useSession, type Route } from "../shell.tsx";
import { STEP_KEYS, stepName, stepText, type StepKey } from "../steps.ts";
import {
  DesignedLabel, ErrorLine, HistoryList, KindChip, LoadError, Loading, NeedsEvaluation, Pill, StaleBanner, TRY_AGAIN,
} from "../ui.tsx";
import {
  BACK_TO_BOARD, DECISION_DATE_LINE, IN_STORE, LOGISTICS, NEEDS_ACTION, NEXT_ACTION_LABEL, NOT_TAKEN, ROLE_LABEL, TERMS, YOURS, historyLine,
  ownerText, type HistoryEntry, type NextActionKey,
} from "../vocab.ts";
import { HISTORY_EMPTY, LOADING_HISTORY, decisionDate, mayAct } from "../decision/shared.ts";
import { STEP_MODULES } from "../decision/modules.ts";
import type { StepProps } from "../decision/types.ts";

// --- strings ---------------------------------------------------------------------------------------------------------

export const LOADING = "Loading the stock decision…"; // (proposed; UI-SPEC "Interaction states")
export const LOAD_FAILED = "This stock decision didn't load"; // (proposed)
export const NOT_FOUND = (key: string) => `No stock decision ${key}`;
export const BACK_TO_CURRENT = "Back to the current step";
export const SHOW_FULL_HISTORY = "Show full history";
export const LATEST_HISTORY = "Latest history";
export const NO_NEXT_ACTION = "No action needed"; // (proposed) an archived decision
export const PANEL_CHIP = { done: "Done", current: "Current step", later: "Later", prepared: "Prepared" } as const;
export const FACT = {
  branch: "Branch", stock: "Stock and expiry", risk: "Risk if nothing changes", decideBy: "Decide by",
  priceStep: "Price step", // (proposed) the ESL-02 line's own fact when the decision has no decide-by
  owner: "Owner", affects: "Affects", next: "Next action",
} as const;
export const UNITS_ON_RECORD = (n: number) => `${units(n)} units on record`;
export const EXPIRES = (date: string) => `expires ${date}`;
export const PLANNED = "planned";
export const DECIDE_CAUSE_RUN = (run: string | null) => // (proposed)
  run === null ? "the last scheduled truck run" : `the last scheduled truck run (${run})`;
export const DECIDE_CAUSE_MARKDOWN = "before the next price step"; // (proposed)
export const ESL_LINE = (pct: number, date: string) => `Price drops ${pct}% on ${date}`; // (proposed; replaces "tomorrow")
export const CHILLED = "Chilled";
// The line when another role acts next (proposed; replaces the Phase 5 demo panel's line, D-03).
export const WAITING_VERB: Readonly<Record<NextActionKey, string>> = {
  evaluate: "evaluate it",
  take_ownership: "take ownership",
  send: "send it for approval",
  check_availability: "check availability",
  approve: "approve the plan",
  accept: "accept the transfer",
  prepare: "prepare the work order",
  record_dispatch: "record the dispatch",
  record_receipt: "record the receipt",
  record_price: "record the price change",
  reconcile: "reconcile the discrepancy",
  close: "close the stock decision",
  answer_stock_check: "answer the stock check", // (proposed, 06.1-30)
  confirm_stock_check: "confirm the count", // (proposed, 06.1-30)
};

// --- the route and the step modules ----------------------------------------------------------------------------------

const CASE = /^\/cases\/([^/]+)$/;

// The decoded key (one path segment), or null for a malformed escape.
function keyOf(path: string): string | null {
  const m = CASE.exec(path);
  if (m === null) return null;
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return null;
  }
}

// "#/cases/{key}" (the current step) or "#/cases/{key}?step=trigger".
function hrefOf(caseId: string, step: StepKey | null): string {
  return `#/cases/${encodeURIComponent(caseId)}${step === null ? "" : `?step=${step.toLowerCase()}`}`;
}

// The step "?step=" names, or null for none or an unknown one (the current step then shows).
function stepOf(param: string | undefined): StepKey | null {
  if (param === undefined) return null;
  return STEP_KEYS.find((k) => k.toLowerCase() === param.toLowerCase()) ?? null;
}

// --- who acts next ---------------------------------------------------------------------------------------------------

type Who = CaseDetail61["next_action"]["who"][number];
const whoName = (w: Who) => (w.role === "hq" ? ROLE_LABEL.hq : `${w.branch} manager`);

function accountOf(s: Session61): string {
  return s.account ?? (s.role === "hq" || s.branch === null ? "HQ" : s.branch);
}

function waitingLine(d: CaseDetail61): string | null {
  const key = d.next_action.key;
  if (key === null || d.next_action.who.length === 0) return null;
  return `Waiting for the ${d.next_action.who.map(whoName).join(" and the ")} to ${WAITING_VERB[key]}.`;
}

// --- the page --------------------------------------------------------------------------------------------------------

function DecisionPage({ route }: { route: Route }) {
  const key = keyOf(route.path);
  if (key === null) return <NotFound />;
  return <StockDecision key={key} caseId={key} viewed={stepOf(route.query.step)} />;
}

function StockDecision({ caseId, viewed }: { caseId: string; viewed: StepKey | null }) {
  const { session } = useSession();
  const { detail: loaded, error, reload: reloadCase } = useCase(caseId, session);
  const activity = useActivity(caseId, session);
  const reloadActivity = activity.reload;
  const reload = useCallback(() => {
    reloadCase();
    reloadActivity();
  }, [reloadCase, reloadActivity]);
  // A flagged item runs the engine once on its Needs evaluation page (05.2 D-25); the evaluated detail the POST
  // answers shows at once, until the reload it triggers brings the same case back from GET.
  const [evaluated, setEvaluated] = useState<CaseDetail | null>(null);
  const detail = loaded !== null && loaded.needs_evaluation === true && evaluated !== null ? evaluated : loaded;
  const d = detail as CaseDetail61 | null;
  const hold = d?.hold ?? null;
  useRefreshAt(hold?.until ?? null, hold?.server_now ?? null, reload);

  if (error !== null && error.status === 404) {
    return (
      <div className="flex flex-col items-start gap-2 p-4 md:p-6">
        <p role="alert" className="text-body text-tx">{NOT_FOUND(caseId)}</p>
        <a href="#/board" className="text-body font-semibold text-navy-800 underline underline-offset-2">{BACK_TO_BOARD}</a>
      </div>
    );
  }
  if (error !== null && d === null) {
    return <div className="p-4 md:p-6"><LoadError title={LOAD_FAILED} error={error} onRetry={reload} /></div>;
  }
  if (d === null) return <div className="p-4 md:p-6"><Loading text={LOADING} /></div>;
  if (d.needs_evaluation === true) {
    return <NeedsEvaluation key={caseId} d={d} session={session}
      onEvaluated={(out) => { setEvaluated(out); reload(); }} />;
  }
  return <Workspace d={d} viewed={viewed ?? d.step} refreshFailed={error !== null} reload={reload} activity={activity} />;
}

function Workspace({ d, viewed, refreshFailed, reload, activity }: {
  d: CaseDetail61; viewed: StepKey; refreshFailed: boolean; reload: () => void; activity: ActivityLoad;
}) {
  const { session: plain } = useSession();
  const session = plain as unknown as Session61;
  const go = useCallback((step: StepKey) => { window.location.hash = hrefOf(d.case_id, step); }, [d.case_id]);
  const props: StepProps = { d, session, reload, go, activity };
  const states = segmentStates(d.step_number, STEP_KEYS.length, d.result !== null);
  const current = d.step;
  const at = STEP_KEYS.indexOf(viewed);
  const mod = STEP_MODULES[viewed];
  const currentMod = STEP_MODULES[current];
  const acts = mayAct(d, session);
  const waiting = acts ? null : waitingLine(d);
  const Primary = acts ? currentMod?.Primary : undefined;
  const chip = panelChip(viewed, current, states[at], d.result !== null);
  const prev = at > 0 ? STEP_KEYS[at - 1] : null;
  const next = at < STEP_KEYS.length - 1 ? STEP_KEYS[at + 1] : null;
  return (
    <div className="flex min-w-0 flex-col">
      <PageHead
        breadcrumb={<>
          <a href="#/board" className="hover:underline">{TERMS.DECISION_BOARD}</a> / <span className="font-mono">{d.key}</span>
          <span className="mt-1 block">{PRODUCT_CODE}</span>
        </>}
        title={`${productLabel(d.sku, d.product)} · lot ${d.lot.lot_id}`}
        actions={(Primary !== undefined || waiting !== null) ? (
          <div data-testid="primary-slot" className="flex min-w-0 max-w-full flex-col items-start gap-1">
            {Primary !== undefined && <Primary {...props} />}
            {waiting !== null && <p data-testid="waiting-line" className="text-body text-tx2 break-words">{waiting}</p>}
          </div>
        ) : undefined} />
      <div className="flex min-w-0 flex-col gap-4 border-b border-line bg-white px-4 pt-2 pb-4 md:px-6">
        <Chips d={d} session={session} plain={plain} />
        <p className="text-label text-tx2">{DECISION_DATE_LINE(decisionDate(d, plain))}</p>
        <Facts d={d} session={plain} />
        <Stepper caseId={d.case_id} states={states} current={current} viewed={viewed} />
      </div>
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        {refreshFailed && <StaleBanner kind="refresh_failed" onAct={reload} />}
        <section data-testid="step-panel" data-step={viewed} aria-labelledby="step-panel-title"
          className="flex min-w-0 flex-col gap-4 rounded-2xl border border-line bg-white p-4 shadow-card">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="step-panel-title" className="m-0 flex items-baseline gap-2 text-title font-semibold">
              <span className="font-mono text-label font-normal text-tx3">{at + 1}</span>{stepName(viewed)}
            </h2>
            <span className="ml-auto"><Pill tone={chip.tone} icon={chip.icon}>{chip.text}</Pill></span>
            {mod?.HeaderAction !== undefined && <mod.HeaderAction {...props} />}
          </div>
          {mod !== undefined && <mod.Panel {...props} />}
        </section>
        <nav aria-label="Step navigation" className="flex min-w-0 flex-wrap items-center gap-2">
          {prev !== null && <a href={hrefOf(d.case_id, prev)} className={GHOST}>‹ {stepName(prev)}</a>}
          {next !== null && <a href={hrefOf(d.case_id, next)} className={GHOST}>{stepName(next)} ›</a>}
          {viewed !== current && <a href={hrefOf(d.case_id, null)} className={`${GHOST} ml-auto`}>{BACK_TO_CURRENT}</a>}
        </nav>
        <LatestHistory caseId={d.case_id} activity={activity} session={plain} />
      </div>
    </div>
  );
}

// A small ghost link-button (UI-SPEC "‹ {previous step}", "{next step} ›", "Back to the current step").
const GHOST = "inline-flex min-h-7 items-center rounded-md px-2 text-body font-semibold text-tx2 hover:bg-gray-soft hover:text-tx max-md:min-h-11";

function panelChip(viewed: StepKey, current: StepKey, state: SegmentState, prepared: boolean):
  { tone: "good" | "info" | "gray"; icon: "check" | "clock" | "circle"; text: string } {
  if (viewed === current) return { tone: "info", icon: "clock", text: PANEL_CHIP.current };
  if (prepared && current === "TRIGGER" && (viewed === "CONTEXT" || viewed === "OPTIONS")) {
    return { tone: "good", icon: "check", text: PANEL_CHIP.prepared };
  }
  return state === "done" ? { tone: "good", icon: "check", text: PANEL_CHIP.done }
    : { tone: "gray", icon: "circle", text: PANEL_CHIP.later };
}

// --- the summary: chips, facts ---------------------------------------------------------------------------------------

function Chips({ d, session, plain }: { d: CaseDetail61; session: Session61; plain: Session }) {
  const owner = d.owner;
  const text = stepText(d);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1">
      {d.zone === "chilled" && <Pill tone="chill" icon="snow">{CHILLED}</Pill>}
      {d.first_stage === "needs_action" && <Pill tone="warn" icon="alert">{NEEDS_ACTION}</Pill>}
      {d.first_stage === "needs_evaluation" && <Pill tone="info" icon="clock">{NEEDS_EVALUATION}</Pill>}
      {owner === null ? <Pill tone="gray" icon="user">{NOT_TAKEN}</Pill>
        : owner.account === accountOf(session) ? <Pill tone="info" icon="user">{YOURS}</Pill>
        : <span className="px-1 text-body text-tx">{ownerText(owner)}</span>}
      {text !== null && <Pill tone="gray">{text}</Pill>}
      <DesignedLabel lot={d.lot} session={plain} />
    </div>
  );
}

type Fact = { label: string; value: ReactNode; note?: ReactNode };

// The run of the recommended plan, when it is a transfer.
function recommendedRun(d: CaseDetail61): string | null {
  const r = d.result;
  const best = r?.options.find((o) => o.key === r.recommended);
  return best?.kind === "TRANSFER" ? best.run_code : null;
}

function Facts({ d, session }: { d: CaseDetail61; session: Session }) {
  const cal = session.calendar;
  const cur = session.currency;
  const lot = d.lot;
  const run = recommendedRun(d);
  const facts: Fact[] = [];
  facts.push({ label: FACT.branch, value: d.dest === null ? `${d.source} · ${IN_STORE}` : `${d.source} → ${d.dest}` });
  if (lot.qty !== null) {
    const expiry = lot.expiry_day === null ? "" : ` · ${EXPIRES(dateLabel(cal, lot.expiry_day))}`;
    facts.push({
      label: FACT.stock, value: `${UNITS_ON_RECORD(lot.qty)}${expiry}`,
      note: <>{lot.expiry_source === null ? null : <span>{lot.expiry_source.toLowerCase()}</span>}<KindChip kind="simulated" /></>,
    });
  }
  if (d.at_risk_units !== null) {
    const value = d.at_risk_value === null ? null : money(d.at_risk_value, cur);
    facts.push({ label: FACT.risk, value: riskFact(d.at_risk_units, value), note: <KindChip kind="estimated" /> });
  }
  // The ESL-02 line only while the decision can still be decided (D-36: never on a rejected one).
  const step = d.markdown_step;
  const esl = step !== null && ["OPEN", "WAITING", "PENDING_APPROVAL"].includes(d.state)
    ? ESL_LINE(step.pct, dateLabel(cal, step.day)) : null;
  if (d.decide_by !== null) {
    const by = d.decide_by;
    const cause = by.source === "run" ? DECIDE_CAUSE_RUN(run ?? d.wait?.run_code ?? null) : DECIDE_CAUSE_MARKDOWN;
    facts.push({
      label: FACT.decideBy, value: `${hhmm(by.latest_decision_min)} · ${dateLabel(cal, by.day)}`,
      note: <><span>{PLANNED} · {cause}</span>{esl !== null && <span data-testid="esl-line" className="block">{esl}</span>}</>,
    });
  } else if (esl !== null) {
    facts.push({ label: FACT.priceStep, value: <span data-testid="esl-line">{esl}</span> });
  }
  facts.push({
    label: FACT.owner, value: d.owner?.label ?? `${d.source} manager`,
    note: d.owner === null ? NOT_TAKEN : undefined,
  });
  const affects = [d.source, d.dest, run === null ? null : LOGISTICS(run), d.zone === "chilled" ? CHILLED : null]
    .filter((x): x is string => x !== null);
  facts.push({ label: FACT.affects, value: affects.join(" · ") });
  const key = d.next_action.key;
  facts.push({
    label: FACT.next, value: key === null ? NO_NEXT_ACTION : NEXT_ACTION_LABEL[key],
    note: d.next_action.who.length === 0 ? undefined : d.next_action.who.map(whoName).join(" · "),
  });
  return (
    <dl data-testid="facts" className="m-0 grid min-w-0 grid-cols-[repeat(auto-fit,minmax(176px,1fr))] gap-x-4 gap-y-2">
      {facts.map((f) => (
        <div key={f.label} className="min-w-0">
          <dt className="text-label text-tx3">{f.label}</dt>
          <dd className="num m-0 text-body font-semibold break-words text-tx">{f.value}</dd>
          {f.note !== undefined && (
            <dd className="num m-0 flex flex-wrap items-center gap-1 text-label text-tx2 break-words">{f.note}</dd>
          )}
        </div>
      ))}
    </dl>
  );
}

// --- the stepper -----------------------------------------------------------------------------------------------------

const SEGMENT: Record<SegmentState, string> = { done: "bg-navy-700", current: "bg-gold-600", later: "bg-rule" };
const STATE_TEXT: Record<SegmentState, string> = { done: "done", current: "current step", later: "later" };

// Nine buttons (`flex: 1 0 96px`): done, current (gold, 600, aria-current="step") and later, and the viewed one when it
// is not the current one (navy-50, aria-pressed). The row scrolls inside its own box below 900px and keeps the
// current step in view (its own scroll only: the page never jumps). A click shows that step's panel (?step=).
function Stepper({ caseId, states, current, viewed }: {
  caseId: string; states: SegmentState[]; current: StepKey; viewed: StepKey;
}) {
  const box = useRef<HTMLElement>(null);
  const cur = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const nav = box.current;
    const b = cur.current;
    if (nav === null || b === null) return;
    if (b.offsetLeft < nav.scrollLeft) nav.scrollLeft = b.offsetLeft;
    else if (b.offsetLeft + b.offsetWidth > nav.scrollLeft + nav.clientWidth) {
      nav.scrollLeft = b.offsetLeft + b.offsetWidth - nav.clientWidth;
    }
  }, [current]);
  return (
    <nav ref={box} aria-label="Steps" className="relative min-w-0 overflow-x-auto">
      <ol className="m-0 flex list-none gap-1 p-0 pb-1">
        {STEP_KEYS.map((k, i) => {
          const isCurrent = k === current;
          const state: SegmentState = isCurrent ? "current" : states[i] === "current" ? "later" : states[i];
          const isViewed = k === viewed && !isCurrent;
          const tone = isCurrent ? "font-semibold text-tx" : state === "done" ? "text-tx2" : "text-tx3";
          return (
            <li key={k} className="flex shrink-0 grow basis-24">
              <button ref={isCurrent ? cur : undefined} type="button" data-state={state}
                aria-current={isCurrent ? "step" : undefined} aria-pressed={isViewed ? true : undefined}
                onClick={() => { window.location.hash = hrefOf(caseId, k); }}
                className={`flex w-full flex-col items-stretch gap-1 rounded-lg p-2 text-left text-label max-md:min-h-11 ${tone} ${isViewed ? "bg-navy-50" : "hover:bg-navy-50"}`}>
                <span aria-hidden="true" className={`h-1 rounded-full ${SEGMENT[state]}`} />
                <span>{i + 1}. {stepName(k)}<span className="sr-only"> {STATE_TEXT[state]}</span></span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

// --- the latest history ---------------------------------------------------------------------------------------------

// The three most recent entries (under the chosen filter), newest first, each with both clocks (D-58, D-63), and the
// link to the full history at Archive.
function LatestHistory({ caseId, activity, session }: { caseId: string; activity: ActivityLoad; session: Session }) {
  const [filter, setFilter] = useState<HistoryFilter>("All");
  const { entries, error, reload } = activity;
  const lines = entries === null ? null : entries.map((e) => {
    const h = historyLine(e as unknown as HistoryEntry, session.time_zone);
    // The API's own group names ("Decisions", "Data"), which bars.historyFilter compares exactly.
    return { title: h.title, meta: h.meta, detail: h.detail === "" ? undefined : h.detail, group: h.group };
  });
  const latest = lines === null ? null : historyFilter(lines, filter).slice(-3).reverse();
  return (
    <section data-testid="latest-history" aria-labelledby="latest-history-title"
      className="flex min-w-0 flex-col gap-2 rounded-2xl border border-line bg-white p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="latest-history-title" className="m-0 text-title font-semibold">{LATEST_HISTORY}</h2>
        <a href={hrefOf(caseId, "ARCHIVE")} className="ml-auto text-body font-semibold text-navy-800 underline underline-offset-2">
          {SHOW_FULL_HISTORY}
        </a>
      </div>
      {error !== null && (
        <ErrorLine error={error} action={retryable(error.code) || error.status === 0 ? { label: TRY_AGAIN, onClick: reload } : undefined} />
      )}
      {latest === null
        ? error === null && <Loading text={LOADING_HISTORY} />
        : <HistoryList entries={latest} filter={filter} onFilter={setFilter} emptyText={HISTORY_EMPTY(filter)} />}
    </section>
  );
}

export const page: PageDef = {
  match: CASE,
  title: TERMS.STOCK_DECISION,
  Component: DecisionPage,
};
