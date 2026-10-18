// Step 2 · Context (06.1-UI-SPEC "Step panel" row 2; 06.1-CONTEXT D-15, D-26, D-61): the stock records the engine read
// (the lots table, simulated), the fact checks, and the Forecast, Incoming and Not recorded boxes. The fact checks are
// decision/factchecks.tsx since 07-21 (07-UI-SPEC surface 1; 07-CONTEXT D-11, D-12, D-36): each fact's class and
// exposure, "Count stock" or "Check printed expiry date" with the request panel, and the ways forward from a block.
// Rules kept here:
// - Every value is the stored engine input (the record, never physical stock) or the server's; nothing is computed
//   beyond counting and formatting. Simulated values carry "Simulated", the forecast "Forecast · precomputed" (D-15).
// - A stock check is one keyed write (hooks.useAction, one Idempotency-Key per press, kept for Try again); afterwards
//   the decision and the session refetch. A refusal shows the server's message with its mapped action.
// - Planned times say "planned"; a value the data model lacks reads "not recorded" (D-18, sketch 006).
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import type { ReactNode } from "react";
import type { CaseDetail61 } from "../../contract.ts";
import { dateLabel, expected, hhmm, units, weekday } from "../../format.ts";
import { useSession } from "../../shell.tsx";
import { Icon, KindChip, Pill } from "../../ui.tsx";
import { providerShort } from "../../vocab.ts";
import { FactChecks } from "../factchecks.tsx";
import type { StepModule, StepProps } from "../types.ts";
import { step as compare } from "./compare.tsx";
import { ReportFromDecision } from "../../report.tsx"; // 07-20

export const LOTS_TITLE = "Stock on record"; // (proposed)
export const LOT_HEAD = ["Lot", "Branch", "On the record", "Expiry", "Last verified", "Fact check"] as const;
export const NOT_COUNTED = "No count"; // (proposed) a lot with no count on record
export const FLAGGED = "Flagged"; // (proposed) a lot a fact check names
export { FACTS_TITLE, NO_FACTS } from "../factchecks.tsx";
export const FORECAST_TITLE = "Forecast";
export const FORECAST_LINE = (headline: string, cutoff: string | null) =>
  `${headline}${cutoff === null ? "" : ` · data to ${cutoff}`} · 80% range`;
export const FORECAST_ROW = "low / base / high"; // the forecast days' column of figures
export const INCOMING_TITLE = "Incoming";
export const NO_INCOMING = "No deliveries planned."; // (proposed)
export const INCOMING_LINE = (qty: number, branch: string, when: string) => `${units(qty)} units to ${branch} · ${when} planned`;
export const NOT_RECORDED_TITLE = "Not recorded";
export const NOT_RECORDED_LINES = [
  "Shelf and backroom split: not recorded; a count gives one total.",
  "Condition: not recorded.",
] as const;

// --- the stored engine input, as this panel reads it ----------------------------------------------------------------

type InputLot = { lot_id: string; branch: string; qty: number; expiry_day: number; expiry_source?: string; last_verified_day?: number | null };
type InputView = {
  lots: InputLot[];
  inbound: { inbound_id: string; branch: string; qty: number; arrival_day: number; arrival_min: number }[];
  forecasts: { branch: string; day: number; low: number; base: number; high: number }[];
  policy: { waste_price: number };
};
const inputView = (d: CaseDetail61) => d.engine_input as unknown as InputView | null;

// --- the panel -------------------------------------------------------------------------------------------------------

function Box({ title, chip, testId, children }: { title: string; chip?: ReactNode; testId?: string; children: ReactNode }) {
  return (
    <section data-testid={testId} aria-label={title} className="flex min-w-0 flex-col gap-2 rounded-xl border border-line p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{title}</h3>
        {chip}
      </div>
      {children}
    </section>
  );
}

function Lots({ d, inp }: { d: CaseDetail61; inp: InputView }) {
  const { session } = useSession();
  const cal = session.calendar;
  const flagged = new Set(d.fact_checks.flatMap((f) => [f.lot_id, ...f.lot_ids]));
  return (
    <Box title={LOTS_TITLE} chip={<KindChip kind="simulated" />} testId="lots">
      <div className="relative min-w-0 overflow-x-auto">
        <table className="w-full min-w-[560px] border-collapse text-body">
          <thead className="bg-navy-50">
            <tr>{LOT_HEAD.map((h, i) => (
              <th key={h} scope="col" className={`border-b border-line px-4 py-2 text-label font-semibold text-tx3 ${i === 2 ? "text-right" : "text-left"}`}>{h}</th>
            ))}</tr>
          </thead>
          <tbody>
            {inp.lots.map((l) => (
              <tr key={l.lot_id}>
                <td className="border-b border-line px-4 py-2 font-mono break-all">{l.lot_id}</td>
                <td className="border-b border-line px-4 py-2">{l.branch}</td>
                <td className="num border-b border-line px-4 py-2 text-right">{units(l.qty)}</td>
                <td className="num border-b border-line px-4 py-2">
                  {dateLabel(cal, l.expiry_day)}{l.expiry_source === undefined ? "" : ` · ${l.expiry_source.toLowerCase()}`}
                </td>
                <td className="num border-b border-line px-4 py-2">
                  {l.last_verified_day == null ? NOT_COUNTED : dateLabel(cal, l.last_verified_day)}
                </td>
                <td className="border-b border-line px-4 py-2">
                  {flagged.has(l.lot_id) && <Pill tone="warn" icon="flag">{FLAGGED}</Pill>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Box>
  );
}

function Panel({ d, reload }: StepProps) {
  const { session } = useSession();
  const cal = session.calendar;
  const inp = inputView(d);
  const v = d.versions;
  const days = inp === null ? [] : inp.forecasts.filter((f) => f.branch === d.source).sort((a, b) => a.day - b.day).slice(0, 4);
  const incoming = inp === null ? [] : inp.inbound;
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {inp !== null && <Lots d={d} inp={inp} />}
      <FactChecks d={d} reload={reload} />
      <div className="grid min-w-0 gap-4 md:grid-cols-3">
        <Box title={FORECAST_TITLE} chip={<KindChip kind="forecast" />} testId="forecast">
          <p data-testid="context-forecast-provenance" className="m-0 min-w-0 break-words text-label text-tx2">{v.forecast_series_providers?.length ? <>
            Precomputed · {providerShort(v.forecast_series_providers)} · run <span className="break-all font-mono">{v.forecast_run_id}</span> · history to {v.forecast_cutoff}
          </> : FORECAST_LINE(v.forecast_headline, v.forecast_cutoff)}</p>
          {days.length > 0 && (
            <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1">
              <dt className="text-label text-tx3">{d.source}</dt>
              <dd className="m-0 text-right text-label text-tx3">{FORECAST_ROW}</dd>
              {days.flatMap((f) => [
                <dt key={`k${f.day}`} className="text-label text-tx3">{dateLabel(cal, f.day)}</dt>,
                <dd key={`v${f.day}`} className="num m-0 text-right text-body">
                  {expected(f.low)} / {expected(f.base)} / {expected(f.high)}
                </dd>,
              ])}
            </dl>
          )}
        </Box>
        <Box title={INCOMING_TITLE} testId="incoming">
          {incoming.length === 0 ? <p className="m-0 text-body text-tx2">{NO_INCOMING}</p> : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0">
              {incoming.map((i) => (
                <li key={i.inbound_id} className="num flex items-center gap-1 text-body text-tx break-words">
                  <Icon name="truck" className="text-tx3" />
                  {INCOMING_LINE(i.qty, i.branch, `${weekday(cal, i.arrival_day)} ${hhmm(i.arrival_min)}`)}
                </li>
              ))}
            </ul>
          )}
        </Box>
        <Box title={NOT_RECORDED_TITLE} testId="not-recorded">
          {NOT_RECORDED_LINES.map((x) => <p key={x} className="m-0 text-body text-tx2">{x}</p>)}
        </Box>
      </div>
    </div>
  );
}

// While a stock check is open the server keeps the decision at Context with Send (or Check availability) as its next
// action (C-7); Context has no form of its own, so its action slot is Compare's: Send for approval with Reject beside
// it, for the owner, exactly as the board's move drawer offers it.
// Report an observation from this stock decision (07-20; 07-SCREENS-MAP 07-20 T2: the store app has no ⋯ menu).
export const step: StepModule = { key: "CONTEXT", Panel, Primary: compare.Primary, HeaderAction: ReportFromDecision };
