// Step 4 · Compare (06.1-UI-SPEC "Comparison (Compare step)"; 06.1-CONTEXT D-27, D-28, D-54): the server's three-way
// comparison (Recommended, the store's Simple rule, Keep), simulated with the engine's forecast, and the owner's Send
// for approval with Reject stock decision….
// Rules kept here:
// - The columns render detail.comparison only (T-06.1-74): nothing here computes a figure the engine did not give,
//   except the net difference "Recommended is" names, which subtracts two of the server's net figures.
// - Waste is compared only against Keep: the waste bars are scaled to Keep's waste, and no line says the Recommended
//   plan wastes less than the Simple rule (D-28).
// - Every figure can be traced: the figure-ref chips F1 (Fact check → Context), L1 (Limits → Options), N1 (Net
//   revenue) and X1 (What would change it) open their source; N1 and X1 bring this panel's row into view with a 1.2 s
//   navy-800 ring, none under reduced motion (UI-SPEC "Interaction states": the figure-ref flash).
// - A missing result is said, never invented: "Not computed: {reason}" in each cell, or "Comparison not computed for
//   this version." for a version stored before the comparison existed.
// - Send is offered to the owner only while the server's next action is send or check_availability (D-54); the
//   server refuses anyone else (not_owned, forbidden), and the refusal shows with its action.
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { scaled } from "../../bars.ts";
import type { CaseDetail61, ComparisonColumn, ComparisonColumnKey } from "../../contract.ts";
import { flipLines, planBranches, totalsScope, transportLine } from "../../copy.ts";
import { dateLabel, expected, hhmm, money, units } from "../../format.ts";
import { useSession } from "../../shell.tsx";
import { Button, KindChip, Provenance } from "../../ui.tsx";
import {
  ApprovalCancelled, BookingLine, CHECK_AVAILABILITY, REJECT_OPEN, ReasonForm, SEND_FOR_APPROVAL, WriteError, isOwner,
  rejectWrite, sendWrite, useFormOpener, useRefetchOnFocus, useStepWrite,
} from "../approval.tsx";
import type { StepModule, StepProps } from "../types.ts";

// --- strings ---------------------------------------------------------------------------------------------------------

export const SIMULATED_LINE = "Simulated with the engine's forecast. Not observed results.";
export const COMPARISON_NAME = "Comparison"; // the grid's accessible name (proposed)
export const COLUMN_TITLE: Readonly<Record<ComparisonColumnKey, string>> = {
  recommended: "Recommended",
  simple_rule: "Simple rule (store markdowns + earliest transfer)",
  keep: "Keep (store markdowns only)",
};
export const ROW_LABEL = {
  plan: "Plan",
  moved: "Units moved",
  markdown: "Markdown",
  net: "Net revenue, base",
  range: "80% range",
  waste: "Waste",
  transport: "Transport",
  vs: "Recommended is",
} as const;
export const FIGURE_COLUMN = "Figure"; // (proposed) the label column's hidden header
export const MARKDOWN_NOW = (pct: number) => `${pct}% now`;
export const STORE_SCHEDULE = (step: string | null) => (step === null ? "Store schedule" : `Store schedule (${step})`);
export const WASTE_TEXT = (u: string, kg: string) => `${u} units · ${kg} kg`;
export const RANGE_TEXT = (low: string, high: string) => `${low} – ${high}`;
export const BETTER_NET = (amount: string) => `${amount} better net`;
export const LOWER_NET = (amount: string) => `${amount} lower net`; // (proposed) when the recommended net is lower
export const SAME_NET = "Same net"; // (proposed)
export const NONE = "—";
export const DECIDE_BY = (time: string) => `Decide by ${time}.`;
export const NOT_COMPUTED = (reason: string | null) => (reason === null || reason === "" ? "Not computed" : `Not computed: ${reason}`);
export const NOT_FOR_VERSION = "Comparison not computed for this version."; // (proposed)
export const REFS = { F1: "Fact check", L1: "Limits", N1: "Net revenue", X1: "What would change it" } as const;
export const SOURCE_REF = (name: string) => `Source: ${name}`;
export const SOURCES = "Sources:";
export const EXPLANATION = "Explanation";
export const HOW_SURE = "How sure";
export const WHAT_WOULD_CHANGE = "What would change it";
export const SCENARIO_NAME: Readonly<Record<string, string>> = { low: "Low sales", base: "Base forecast", high: "High sales" };
export const WINS = (n: number) => (n === 3 // (proposed)
  ? "Best in all three demand scenarios (3 of 3)."
  : n === 1 ? "Best in one of the three demand scenarios (1 of 3)." : `Best in ${n} of the three demand scenarios (${n} of 3).`);
export const SCENARIO_BEST = (scenario: string, plan: string) => `${scenario}: ${plan}.`;
export const NO_CROSSOVERS = "The engine found no change point for this plan."; // (proposed)
const FLASH_MS = 1200;

// --- the grid --------------------------------------------------------------------------------------------------------

const COLUMNS: ComparisonColumnKey[] = ["recommended", "simple_rule", "keep"];
type RowKey = keyof typeof ROW_LABEL;
const ROWS: RowKey[] = ["plan", "moved", "markdown", "net", "range", "waste", "transport", "vs"];
const GRID = "grid grid-cols-[152px_repeat(3,minmax(152px,1fr))]";

function Bar({ value, tone }: { value: number; tone: "net" | "waste" }) {
  return (
    <span aria-hidden="true" className="block h-2 w-full overflow-hidden rounded-full bg-sunken">
      <span className={`block h-2 rounded-full ${tone === "net" ? "bg-navy-700" : "bg-warn"}`} style={{ width: `${Math.round(value * 100)}%` }} />
    </span>
  );
}

type RefKey = keyof typeof REFS;
type Flash = "net" | "change" | null;

// A figure-ref chip (UI-SPEC "FigureRef"): a short mono code whose accessible name names its source.
function FigureRef({ id, onOpen }: { id: RefKey; onOpen: (id: RefKey) => void }) {
  return (
    <button type="button" aria-label={SOURCE_REF(REFS[id])} onClick={() => onOpen(id)}
      className="inline-flex h-5 min-w-7 items-center justify-center rounded-md border border-navy-100 bg-white px-1 font-mono text-label font-semibold text-navy-800 hover:bg-navy-50 max-md:min-h-11 max-md:min-w-11">
      {id}
    </button>
  );
}

// The 1.2 s ring a flashed row or box carries; motion-safe only, so reduced motion draws none.
const RING = "motion-safe:ring-2 motion-safe:ring-inset motion-safe:ring-navy-800";

// Every cell's text when the comparison has no columns (missing data, or a version stored before it existed).
function emptyText(d: CaseDetail61): string | null {
  const cmp = d.comparison;
  if (cmp.status === "not_computed") return NOT_FOR_VERSION;
  if (cmp.status === "missing_data") return NOT_COMPUTED(cmp.reason);
  return null;
}

function CompareGrid({ d, flash, netRow, onRef }: {
  d: CaseDetail61; flash: Flash; netRow: RefObject<HTMLDivElement | null>; onRef: (id: RefKey) => void;
}) {
  const { session } = useSession();
  const cur = session.currency;
  const cmp = d.comparison;
  const byKey = new Map(cmp.columns.map((c) => [c.column, c]));
  const cols = COLUMNS.map((k) => byKey.get(k) ?? null);
  const rec = byKey.get("recommended") ?? null;
  const keep = byKey.get("keep") ?? null;
  const nets = scaled(cols.map((c) => c?.net.base ?? 0));
  const wastes = keep === null ? cols.map(() => 0) : scaled(cols.map((c) => c?.wasted_units ?? 0), keep.wasted_units);
  const step = d.markdown_step;
  const schedule = step === null ? null : `${step.pct}% ${dateLabel(session.calendar, step.day)}`;
  const empty = emptyText(d);
  const missing = empty ?? NOT_COMPUTED(cmp.reason);
  const cell = (row: RowKey, c: ComparisonColumn, i: number): ReactNode => {
    switch (row) {
      case "plan":
        return c.label;
      case "moved":
        return units(c.moved.base);
      case "markdown":
        return c.pct > 0 ? MARKDOWN_NOW(c.pct) : STORE_SCHEDULE(schedule);
      case "net":
        return <span className="flex flex-col gap-1"><span className="text-title font-semibold">{money(c.net.base, cur)}</span><Bar value={nets[i]} tone="net" /></span>;
      case "range":
        return RANGE_TEXT(money(c.net.low, cur), money(c.net.high, cur));
      case "waste":
        return <span className="flex flex-col gap-1"><span>{WASTE_TEXT(expected(c.wasted_units), expected(c.wasted_kg))}</span><Bar value={wastes[i]} tone="waste" /></span>;
      case "transport":
        return money(c.transport.base, cur);
      case "vs": {
        if (c.column === "recommended" || rec === null) return NONE;
        const diff = rec.net.base - c.net.base;
        return Math.abs(diff) < 0.005 ? SAME_NET : diff > 0 ? BETTER_NET(money(diff, cur)) : LOWER_NET(money(-diff, cur));
      }
    }
  };
  return (
    <div data-testid="compare-scroll" className="relative min-w-0 overflow-x-auto rounded-xl border border-line">
      <div role="table" aria-label={COMPARISON_NAME} className="min-w-[620px] text-body">
        <div role="row" className={GRID}>
          <div role="columnheader" className="sticky left-0 z-10 bg-white px-4 py-2 text-label text-tx3"><span className="sr-only">{FIGURE_COLUMN}</span></div>
          {COLUMNS.map((k) => (
            <div key={k} role="columnheader" data-column={k}
              className={`px-4 py-2 font-semibold break-words ${k === "recommended" ? "bg-navy-800 text-white" : "bg-navy-50 text-tx"}`}>
              {COLUMN_TITLE[k]}
            </div>
          ))}
        </div>
        {ROWS.map((row) => (
          <div key={row} ref={row === "net" ? netRow : undefined} role="row" data-row={row}
            data-flash={row === "net" && flash === "net" ? "true" : undefined}
            className={`${GRID} border-t border-line ${row === "net" && flash === "net" ? RING : ""}`}>
            <div role="rowheader" className="sticky left-0 z-10 flex flex-wrap items-start gap-1 bg-white px-4 py-2 text-label text-tx3">
              <span>{ROW_LABEL[row]}</span>
              {row === "net" && <FigureRef id="N1" onOpen={onRef} />}
            </div>
            {cols.map((c, i) => (
              <div key={COLUMNS[i]} role="cell"
                className={`num min-w-0 px-4 py-2 break-words ${COLUMNS[i] === "recommended" ? "bg-gold-100" : "bg-white"} ${c === null || empty !== null ? "text-tx3" : ""}`}>
                {empty !== null ? empty : c === null ? missing : cell(row, c, i)}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

// --- the panel and the primary ---------------------------------------------------------------------------------------

// "Best in 1 of the three demand scenarios (1 of 3). Low sales: Transfer 94 to S017." from the engine's scenario wins;
// the other scenarios' best plans by their labels (the key when the engine kept no row for it).
function sureLines(d: CaseDetail61): string[] {
  const r = d.result;
  if (r === null) return [];
  const label = (key: string) => r.options.find((o) => o.key === key)?.label ?? key;
  const others = Object.entries(r.scenario_wins.by_scenario)
    .filter(([, key]) => key !== r.recommended)
    .map(([sc, key]) => SCENARIO_BEST(SCENARIO_NAME[sc] ?? sc, label(key)));
  return [WINS(r.scenario_wins.wins), ...others];
}

// The transport and waste-price crossovers as the engine computed them (the client never recomputes one).
function changeLines(d: CaseDetail61, currency: string): string[] {
  const c = d.result?.crossovers;
  if (c === undefined) return [];
  const transport = transportLine(c.transport, currency);
  return [...(transport === null ? [] : [transport]), ...flipLines(c.waste_price, currency).map((x) => x.text)];
}

function Panel(props: StepProps) {
  const { d, session, activity, reload, go } = props;
  const { session: plain } = useSession();
  useRefetchOnFocus(reload);
  const [flash, setFlash] = useState<Flash>(null);
  const netRow = useRef<HTMLDivElement>(null);
  const changeBox = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (flash === null) return;
    const timer = setTimeout(() => setFlash(null), FLASH_MS);
    return () => clearTimeout(timer);
  }, [flash]);
  const open = (id: RefKey) => {
    if (id === "F1") go("CONTEXT");
    else if (id === "L1") go("OPTIONS");
    else {
      const target = id === "N1" ? netRow.current : changeBox.current;
      target?.scrollIntoView({ block: "center", inline: "nearest" });
      setFlash(null);
      requestAnimationFrame(() => setFlash(id === "N1" ? "net" : "change"));
    }
  };
  const keep = d.result?.options.find((o) => o.key === d.result?.recommended)?.kind === "KEEP";
  const sure = sureLines(d);
  const change = changeLines(d, plain.currency);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <ApprovalCancelled d={d} activity={activity} />
      <BookingLine d={d} session={session} activity={activity} keep={keep} />
      <p className="m-0 flex flex-wrap items-center gap-2 text-label text-tx2">
        <KindChip kind="simulated" /><span>{SIMULATED_LINE}</span>
      </p>
      <CompareGrid d={d} flash={flash} netRow={netRow} onRef={open} />
      <p data-testid="totals-scope" className="num m-0 text-label text-tx3 break-words">{totalsScope(planBranches(d.engine_input, d.source))}</p>
      <div className="grid min-w-0 gap-4 md:grid-cols-2">
        <div data-testid="how-sure" className="flex min-w-0 flex-col gap-2 rounded-xl border border-line p-4">
          <h3 className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{HOW_SURE}</h3>
          {sure.map((x) => <p key={x} className="num m-0 text-body text-tx break-words">{x}</p>)}
        </div>
        <div ref={changeBox} data-testid="what-would-change" data-flash={flash === "change" ? "true" : undefined}
          className={`flex min-w-0 flex-col gap-2 rounded-xl border border-line p-4 ${flash === "change" ? RING : ""}`}>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{WHAT_WOULD_CHANGE}</h3>
            <FigureRef id="X1" onOpen={open} />
          </div>
          {change.length === 0 ? <p className="m-0 text-body text-tx2">{NO_CROSSOVERS}</p>
            : change.map((x) => <p key={x} className="num m-0 text-body text-tx break-words">{x}</p>)}
        </div>
      </div>
      <section data-testid="explanation" aria-label={EXPLANATION} className="flex min-w-0 flex-col gap-2 rounded-xl bg-navy-50 p-4">
        <span><Provenance /></span>
        <p data-testid="template-summary" className="m-0 text-body text-tx break-words">{d.summary}</p>
        <p className="m-0 flex flex-wrap items-center gap-2 text-label text-tx2">
          <span>{SOURCES}</span>
          {(Object.keys(REFS) as RefKey[]).map((id) => (
            <span key={id} className="inline-flex items-center gap-1"><FigureRef id={id} onOpen={open} /><span>{REFS[id]}</span></span>
          ))}
        </p>
      </section>
    </div>
  );
}

// Send for approval (Check availability after an expired capacity hold) by the owner, while the server says send is
// next; Reject stock decision… beside it only for the source store's manager, the one account the server lets reject
// (api/workflow.reject), so HQ as the owner is never offered a control that can only answer 403.
function Primary({ d, session, reload }: StepProps) {
  const write = useStepWrite(d, reload);
  const form = useFormOpener();
  const key = d.next_action.key;
  if ((key !== "send" && key !== "check_availability") || !isOwner(d, session)) return null;
  const send = () => void write.run(sendWrite(d));
  const mayReject = session.role === "manager" && session.branch === d.source;
  const by = d.decide_by;
  return (
    <div className="flex min-w-0 max-w-full flex-col items-start gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="primary" busy={write.busy} onClick={send}>
          {key === "check_availability" ? CHECK_AVAILABILITY : SEND_FOR_APPROVAL}
        </Button>
        {mayReject && (
          <Button ref={form.opener} kind="plain" aria-expanded={form.open} disabled={write.busy}
            onClick={() => form.setOpen(true)}>{REJECT_OPEN}</Button>
        )}
      </div>
      {by !== null && <p className="m-0 text-label text-tx2">{DECIDE_BY(hhmm(by.latest_decision_min))}</p>}
      <WriteError write={write} d={d} session={session} reload={reload} check={send} />
      {mayReject && form.open && (
        <ReasonForm kind="reject" d={d} session={session} reload={reload} onClose={form.close}
          write={(reason) => rejectWrite(d, reason)} />
      )}
    </div>
  );
}

export const step: StepModule = { key: "COMPARE", Panel, Primary };
