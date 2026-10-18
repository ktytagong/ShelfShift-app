// Step 3 · Options (06.1-UI-SPEC "Step panel" row 3; 06.1-CONTEXT D-16, D-26, D-37, D-39): what the engine checked
// (plans checked, ranked and ruled out), the limits the recommended plan passes, every option it scored in its order
// with the "Recommended" row on gold-100, every ruled-out plan with its reasons word for word, the crossover and
// flip-price lines, the waste price, the eligibility block and the restricted-search footnote (05.2-21), and the
// version line. Options is automatic: the engine prepares it with the decision (D-25), so this panel has no action.
// Rules kept here (Phase 5's Options, regrouped):
// - Nothing here sorts, filters or recomputes: rows in the engine's order, crossovers as the engine computed them.
// - The policy-priced Score base column shows only when the stored waste price is above 0 (D-08).
// - One currency format, through format.ts with the session currency (D-39: never a currency code beside a symbol).
// - The table scrolls inside its own box under `min-w-0` parents, so the page never scrolls sideways (D-37).
// - "Engine" capitalised in the version line (D-39). The waste price links to Policies for HQ only (D-53).
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import type { ReactNode } from "react";
import type { Resource } from "../../api.ts";
import type { CaseDetail61 } from "../../contract.ts";
import {
  TRANSFERS_UNAVAILABLE, bestIfLow, flipLines, markdownLine, neededText, optionsFootnote, planBranches, totalsScope,
  transportLine, type FlipLine,
} from "../../copy.ts";
import { waitShown } from "../../derive.ts";
import { dateLabel, expected, hhmm, markDate, money, signed, units } from "../../format.ts";
import { useSession } from "../../shell.tsx";
import { EligibilityLine, Icon, KindChip, Pill, Tooltip } from "../../ui.tsx";
import type { StepModule, StepProps } from "../types.ts";

export const CHECKED = (checked: number, ranked: number, ruledOut: number): [string, string, string] => [
  `${units(checked)} plans checked`, `${units(ranked)} ranked`, `${units(ruledOut)} ruled out by a limit`,
];
export const AGAINST_LIMITS = "against every limit";
export const LIMITS_TITLE = "Limits the recommended plan passes";
export const LIMIT_TEXT = (needed: string, available: string) => `${needed} of ${available}`;
export const OPTIONS_TITLE = "Options";
export const OPTIONS_NOTE = (cur: string) => `Net contribution by demand scenario, ${cur}`;
export const RECOMMENDED = "Recommended";
export const BEST_IF_LOW = "Best if low";
export const NOT_SCORED = "Not scored";
export const RULED_OUT = "Ruled out"; // (proposed; replaces the "Rejected" pill on a plan a limit excluded)
export const WAIT_ROW = (run: string) => `Wait (${run})`;
// The Wait row's sentence (proposed; drops Phase 5's "Today's"): a planned time, from the engine's wait keys.
export const WAIT_SENTENCE = (run: string, day: string, leaves: string | null, by: string) => (leaves === null
  ? `Decide by ${by} on ${day} for the last scheduled truck run (${run}).`
  : `The last scheduled truck run (${run}) on ${day} leaves at ${leaves}; decide by ${by}.`);
export const WASTE_PRICE = (price: string) => `Waste price ${price}/kg`;
export const CHANGE_WASTE_PRICE = "Change waste price";
export const SET_BY_HQ = "(set by HQ)";
export const FLIP_TIP_LABEL = "More about the next change";
export const NO_RECOMMENDATION = "No recommendation";
export const NO_RECOMMENDATION_TEXT = "This stock decision has no recommendation"; // (proposed; D-61 form of the 05 text)

// "Snapshot Sat 17 Oct 2026 · forecast run {id} · recommendation {12} · Engine engine-3 · dataset {version}" (D-39).
function VersionLine({ d }: { d: CaseDetail61 }) {
  const { session } = useSession();
  const v = d.versions;
  const snap = v.day === null ? undefined : session.calendar.find((c) => c.day === v.day)?.date;
  const id = (x: string | null) => <span className="font-mono break-all">{x ?? "—"}</span>;
  return (
    <p data-testid="version-line" className="m-0 text-label text-tx3">
      Snapshot {snap === undefined ? "—" : markDate(snap)} · forecast run {id(v.forecast_run_id)} ·
      recommendation {id(v.rec_version === null ? null : v.rec_version.slice(0, 12))} · Engine {id(v.engine_version)} ·
      dataset {id(v.dataset_version)}
    </p>
  );
}

// The resources the recommended plan holds, each against what is available (available excludes this decision's own
// bookings): a bar and "110 of 120 units". The engine already checked every limit; these are the ones with a figure.
function Limits({ resources }: { resources: Resource[] }) {
  const rows = resources.filter((x) => x.available !== null);
  if (rows.length === 0) return null;
  return (
    <section data-testid="limits" aria-label={LIMITS_TITLE} className="flex min-w-0 flex-col gap-2">
      <h3 className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{LIMITS_TITLE}</h3>
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {rows.map((x) => {
          const available = x.available as number;
          const fits = x.needed <= available;
          const width = available <= 0 ? 1 : Math.min(1, x.needed / available);
          return (
            <li key={x.kind} className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 md:grid-cols-[minmax(0,16rem)_minmax(0,1fr)_auto]">
              <span className="text-body break-words">{x.label}</span>
              <span aria-hidden="true" className="col-span-2 h-2 overflow-hidden rounded-full bg-sunken md:col-span-1">
                <span className="block h-full rounded-full bg-navy-700" style={{ width: `${width * 100}%` }} />
              </span>
              <span className={`num flex items-center gap-1 text-label ${fits ? "text-good" : "text-bad"}`}>
                <Icon name={fits ? "check" : "alert"} size={12} />
                {LIMIT_TEXT(neededText(x.needed, x.unit), neededText(available, x.unit))}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

type Row = { cells: ReactNode[]; recommended?: boolean; testId?: string };

function Panel({ d }: StepProps) {
  const { session } = useSession();
  const r = d.result;
  if (r === null) {
    return (
      <div className="flex flex-col gap-1">
        <p className="m-0 text-body font-semibold">{NO_RECOMMENDATION}</p>
        <p className="m-0 text-body text-tx2 break-words">{d.reason ?? NO_RECOMMENDATION_TEXT}</p>
      </div>
    );
  }
  const cur = session.currency;
  const cal = session.calendar;
  const inp = d.engine_input as { policy?: { waste_price?: number } } | null;
  const price = inp?.policy?.waste_price ?? 0;
  const scored = price > 0;
  const low = bestIfLow(r.options, r.recommended);
  const dash = Array<string>(scored ? 7 : 6).fill("—");
  const rows: Row[] = r.options.map((o) => {
    const best = o.key === r.recommended;
    return {
      recommended: best,
      testId: best ? "recommended-row" : undefined,
      cells: [
        <span className={`block min-w-64 break-words ${best ? "font-semibold" : ""}`}>{o.label}</span>,
        expected(o.sold), expected(o.wasted_units), money(o.net.low, cur), money(o.net.base, cur), money(o.net.high, cur),
        signed(o.delta_vs_keep, cur),
        ...(scored ? [money(o.score.base, cur)] : []),
        <span className="flex gap-1">
          {best && <Pill tone="info" icon="check">{RECOMMENDED}</Pill>}
          {o.key === low && <Pill tone="gray">{BEST_IF_LOW}</Pill>}
        </span>,
      ],
    };
  });
  if (r.wait !== null) {
    const w = d.wait;
    const sentence = w !== null && waitShown(d, session.day)
      ? WAIT_SENTENCE(w.run_code, dateLabel(cal, w.day), w.departs_min === null ? null : hhmm(w.departs_min), hhmm(w.latest_decision_min))
      : null;
    rows.push({
      cells: [
        <span className="block min-w-64">
          <span className="block">{WAIT_ROW(r.wait.run_code)}</span>
          {sentence !== null && <span className="block text-label text-tx2 break-words">{sentence}</span>}
        </span>,
        ...dash, <Pill tone="gray">{NOT_SCORED}</Pill>,
      ],
    });
  }
  // A source store with no location or route data has no transfer (05.2 D-31): its row stands where transfers would.
  if (d.eligibility?.applies === true && d.eligibility.status === "unavailable_unmapped") {
    rows.push({
      testId: "transfers-unavailable",
      cells: [<span className="block min-w-64 text-tx2 break-words">{TRANSFERS_UNAVAILABLE}</span>, ...dash, <span />],
    });
  }
  for (const x of r.rejected) {
    rows.push({
      cells: [
        <span className="block min-w-64">
          <span className="block text-tx2 break-words">{x.label}</span>
          <ul className="m-0 pl-0 text-label text-tx2">
            {x.reasons.map((reason, i) => <li key={i} className="list-none break-words">{reason}</li>)}
          </ul>
        </span>,
        ...dash, <Pill tone="bad" icon="lock">{RULED_OUT}</Pill>,
      ],
    });
  }
  const c = r.crossovers;
  const transport = c === undefined ? null : transportLine(c.transport, cur);
  const lines: FlipLine[] = [
    ...(transport === null ? [] : [{ text: transport, tip: null }]),
    ...(c === undefined ? [] : [{ text: markdownLine(c.markdown_response), tip: null }, ...flipLines(c.waste_price, cur)]),
  ];
  const head: ReactNode[] = ["Option", "Sold", "Wasted", "Net low", "Net base", "Net high", "vs keep",
    ...(scored ? ["Score base"] : []), <span className="sr-only">Flags</span>];
  const numeric = (i: number) => i > 0 && i < head.length - 1;
  const [checked, ranked, ruledOut] = CHECKED(r.candidates_checked, r.options.length, r.rejected.length);
  const hq = session.role === "hq";
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <p data-testid="options-checked" className="num m-0 text-body text-tx">
        <strong className="font-semibold">{checked}</strong> {AGAINST_LIMITS} · <strong className="font-semibold">{ranked}</strong>
        {" · "}<strong className="font-semibold">{ruledOut}</strong>
      </p>
      <Limits resources={d.approvals.resources} />
      <section aria-labelledby="options-table-title" className="flex min-w-0 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id="options-table-title" className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{OPTIONS_TITLE}</h3>
          <KindChip kind="simulated" />
          <span className="ml-auto text-label text-tx3">{OPTIONS_NOTE(cur)}</span>
        </div>
        <div data-testid="options-scroll" className="relative min-w-0 overflow-x-auto">
          <table className="w-full border-collapse text-body">
            <thead className="bg-navy-50">
              <tr>{head.map((h, i) => (
                <th key={i} scope="col" className={`border-b border-line px-4 py-2 text-label font-semibold text-tx3 ${numeric(i) ? "text-right" : "text-left"}`}>{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i} data-testid={row.testId} className={row.recommended ? "bg-gold-100" : undefined}>
                  {row.cells.map((cell, j) => (
                    <td key={j} className={`num border-b border-line px-4 py-2 ${numeric(j) ? "text-right" : "text-left"}`}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <EligibilityLine e={d.eligibility} />
      <ul data-testid="crossovers" className="num m-0 list-disc pl-6 text-body text-tx2">
        {lines.map((x) => (
          <li key={x.text} className="break-words">
            {x.text}
            {x.tip !== null && <Tooltip label={FLIP_TIP_LABEL} text={x.tip} />}
          </li>
        ))}
        <li data-testid="waste-price" className="break-words">
          {WASTE_PRICE(money(price, cur))} ·{" "}
          {hq ? <a href="#/policies" className="font-semibold text-navy-800 underline underline-offset-2">{CHANGE_WASTE_PRICE}</a>
            : <span>{SET_BY_HQ}</span>}
        </li>
      </ul>
      <p data-testid="totals-scope" className="num m-0 text-label text-tx3 break-words">{totalsScope(planBranches(d.engine_input, d.source))}</p>
      <p data-testid="options-footnote" className="num m-0 text-label text-tx3 break-words">
        {optionsFootnote(r, price, d.eligibility?.restricted === true || r.shortlist !== undefined)}
      </p>
      <VersionLine d={d} />
    </div>
  );
}

export const step: StepModule = { key: "OPTIONS", Panel };
