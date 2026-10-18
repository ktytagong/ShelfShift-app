// Step 2 · Context (06.1-UI-SPEC "Step panel" row 2; 06.1-CONTEXT D-15, D-26, D-61): the stock records the engine read
// (the lots table, simulated), the fact checks with "Request stock check", and the Forecast, Incoming and Not recorded
// boxes. Phase 5's fact-check behaviour is kept (05-CONTEXT D-01: who may ask, when, the recount; the engine's flags
// choose each sentence through copy.ts); only the D-61 terms change ("Request stock check" in place of "Ask this
// branch for a count", "Stock check requested from {S017}" in place of "Evidence requested from …").
// Rules kept here:
// - Every value is the stored engine input (the record, never physical stock) or the server's; nothing is computed
//   beyond counting and formatting. Simulated values carry "Simulated", the forecast "Forecast · precomputed" (D-15).
// - A stock check is one keyed write (hooks.useAction, one Idempotency-Key per press, kept for Try again); afterwards
//   the decision and the session refetch. A refusal shows the server's message with its mapped action.
// - Planned times say "planned"; a value the data model lacks reads "not recorded" (D-18, sketch 006).
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import type { ReactNode } from "react";
import { post, type FactCheck, type Request } from "../../api.ts";
import type { CaseDetail61 } from "../../contract.ts";
import { errorAction, factOutcome, factSentence, factTitle, factWhy, replacedText } from "../../copy.ts";
import { COUNT_KINDS, canAskForCount, canRequestRecount, requestForFact } from "../../derive.ts";
import { dateLabel, expected, hhmm, units, weekday } from "../../format.ts";
import { useAction } from "../../hooks.ts";
import { useSession } from "../../shell.tsx";
import { Button, ErrorLine, Icon, KindChip, Pill, TRY_AGAIN } from "../../ui.tsx";
import { RELOAD_DECISION, providerShort } from "../../vocab.ts";
import type { StepModule, StepProps } from "../types.ts";
import { step as compare } from "./compare.tsx";

export const LOTS_TITLE = "Stock on record"; // (proposed)
export const LOT_HEAD = ["Lot", "Branch", "On the record", "Expiry", "Last verified", "Fact check"] as const;
export const NOT_COUNTED = "No count"; // (proposed) a lot with no count on record
export const FLAGGED = "Flagged"; // (proposed) a lot a fact check names
export const FACTS_TITLE = "Fact checks";
export const NO_FACTS = "The engine flagged no suspect facts for this version.";
export const NOT_ALLOWED = "Plan not allowed if true";
export const REQUEST_STOCK_CHECK = "Request stock check"; // D-61
export const REQUEST_RECOUNT = "Request recount";
export const COUNT_ONLY = "Only a stock count can be requested in this version.";
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

// A stock check's status line with the D-61 terms (UI-SPEC "Step panel" row 2). A replaced request keeps copy.ts's
// words (replacedText), which carry no internal term.
export const STOCK_CHECK_REQUESTED_FROM = (to: string) => `Stock check requested from ${to}`;
export const REPLY_COUNT_MISSING = "Reply received · count missing";
export const COUNT_AWAITING = (to: string) => `Count awaiting ${to} manager`;
export const STOCK_CHECK_EXPIRED = "Stock check expired · unverified"; // (proposed; D-61 form of "Request expired")
export const COUNT_CONFIRMED = (to: string) => `Count confirmed by ${to} manager`;

export function stockCheckLines(r: Request): string[] {
  switch (r.state) {
    case "OPEN":
      return [r.partial_note === null ? STOCK_CHECK_REQUESTED_FROM(r.to_branch) : REPLY_COUNT_MISSING];
    case "ANSWERED":
      return [COUNT_AWAITING(r.to_branch)];
    case "EXPIRED":
      return [STOCK_CHECK_EXPIRED];
    case "SUPERSEDED":
      return [replacedText(r)];
    default: {
      const counts = r.answer?.fields.counts ?? {};
      const lines = r.lot_ids.flatMap((id) => {
        const c = counts[id];
        if (c === undefined || c.shelf_units === null || c.backroom_units === null) return [];
        return [`${COUNT_CONFIRMED(r.to_branch)} · shelf ${units(c.shelf_units)} + backroom ${units(c.backroom_units)} = `
          + `${units(c.shelf_units + c.backroom_units)}`];
      });
      return lines.length === 0 ? [COUNT_CONFIRMED(r.to_branch)] : lines;
    }
  }
}

// --- the stored engine input, as this panel reads it ----------------------------------------------------------------

type InputLot = { lot_id: string; branch: string; qty: number; expiry_day: number; expiry_source?: string; last_verified_day?: number | null };
type InputView = {
  lots: InputLot[];
  inbound: { inbound_id: string; branch: string; qty: number; arrival_day: number; arrival_min: number }[];
  forecasts: { branch: string; day: number; low: number; base: number; high: number }[];
  policy: { waste_price: number };
};
const inputView = (d: CaseDetail61) => d.engine_input as unknown as InputView | null;

// --- the fact checks -------------------------------------------------------------------------------------------------

function StockCheckAction({ f, d, reload }: { f: FactCheck; d: CaseDetail61; reload: () => void }) {
  const { session, refresh } = useSession();
  const write = async (body: { rec_version: string; fact_id: string; recount_of?: string }, key: string) => {
    const out = await post<Request>(`/api/cases/${encodeURIComponent(d.case_id)}/requests`, body, key);
    reload();
    await refresh().catch(() => {}); // the next page load or write shows a session failure
    return out;
  };
  const ask = useAction(write);
  const recount = useAction(write);
  const actionOf = (act: typeof ask) => {
    const next = act.error === null ? "none" : errorAction(act.error.code);
    return next === "reload_case" ? { label: RELOAD_DECISION, onClick: () => { act.clear(); reload(); } }
      : next === "retry" ? { label: TRY_AGAIN, onClick: () => void act.retry() }
      : undefined;
  };
  const r = requestForFact(d, f.fact_id);
  const version = d.rec_version;
  if (r !== null) {
    return (
      <div className="flex flex-col items-start gap-1">
        {stockCheckLines(r).map((line, i) => (
          <p key={i} data-testid="stock-check-status" className="num m-0 text-label text-tx2 break-words">{line}</p>
        ))}
        {canRequestRecount(r, session) && version !== null && (
          <Button kind="plain" icon="redo" busy={recount.busy}
            onClick={() => void recount.run({ rec_version: version, fact_id: f.fact_id, recount_of: r.request_id })}>
            {REQUEST_RECOUNT}
          </Button>
        )}
        <ErrorLine error={recount.error} action={actionOf(recount)} />
      </div>
    );
  }
  if (!COUNT_KINDS.includes(f.kind)) return <p className="m-0 text-label text-tx2">{COUNT_ONLY}</p>;
  if (!canAskForCount(f, session, d) || version === null) return null;
  return (
    <div className="flex flex-col items-start gap-1">
      <Button kind="plain" icon="inbox" busy={ask.busy}
        onClick={() => void ask.run({ rec_version: version, fact_id: f.fact_id })}>{REQUEST_STOCK_CHECK}</Button>
      <ErrorLine error={ask.error} action={actionOf(ask)} />
    </div>
  );
}

function FactChecks({ d, reload }: { d: CaseDetail61; reload: () => void }) {
  const { session } = useSession();
  const cal = session.calendar;
  const price = inputView(d)?.policy.waste_price ?? 0;
  return (
    <Box title={FACTS_TITLE} testId="fact-checks">
      {d.fact_checks.length === 0 ? <p className="m-0 text-body text-tx2">{NO_FACTS}</p> : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {d.fact_checks.map((f) => {
            const why = factWhy(f, d.engine_input, session.day, cal);
            return (
              <li key={f.fact_id} data-fact-id={f.fact_id} className="flex min-w-0 flex-col gap-1 rounded-lg border border-line p-4">
                <p className="num m-0 text-body font-semibold break-words">{factTitle(f, cal)}</p>
                {why !== null && <p className="num m-0 text-body text-tx2 break-words">{why}</p>}
                {factOutcome(f) === "not_allowed" && <span><Pill tone="bad" icon="alert">{NOT_ALLOWED}</Pill></span>}
                <p data-testid="fact-outcome" className="num m-0 text-body text-tx break-words">
                  {factSentence(f, session.currency, price, cal)}
                </p>
                <StockCheckAction f={f} d={d} reload={reload} />
              </li>
            );
          })}
        </ul>
      )}
    </Box>
  );
}

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
export const step: StepModule = { key: "CONTEXT", Panel, Primary: compare.Primary };
