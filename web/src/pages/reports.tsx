// Reports (08 D-01/D-03/D-04/D-15/D-16/D-18). The server owns scope and currentness.
// This first slice displays workspace activity and published benchmark status only.
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, type Session } from "../api.ts";
import { getReportsBenchmark, getReportsWorkspace, type BenchmarkView, type ReportFigure,
  type BenchmarkRun, type ReportsWorkspaceView } from "../contract.ts";
import { REPORT_FIGURES } from "../copy.ts";
import { PROVIDER_NAMES, recordedLine } from "../vocab.ts";
import { dateLabel, money, signed, units } from "../format.ts";
import { axisFor, branchLine, checksLine, checksSetting, combinedLine, cumulative, forecastLine, horizonLabel, percent, plainValue, providerMix, qualityLine, runningTotals, scenarioDate, SCENARIO_IDS, signedUnits, transfersText, wasteChange, wasteSentence, WASTE_STYLE, type WaterfallStep } from "../reportsview.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { PageHead, useSession, type Route } from "../shell.tsx";
import { Banner, Button, ChartCard, Disclosure, DivergingBar, ErrorLine, FigureTile, Icon, KindChip, Kv, LoadError, Loading, Pager, Pill, Section, Segmented, StaleBanner, Table, Tabs, WaterfallChart,
  LineChart, type PlotSeries, type IconName } from "../ui.tsx";

export const REPORTS_TEXT = {
  title: "Reports", workspace: "This workspace", benchmark: "Benchmark", activity: "Workspace activity",
  tabs: "Report views", // (proposed)
  loading: "Loading reports…", benchmarkLoading: "Loading the benchmark…", // (proposed)
  failed: "Reports didn't load", benchmarkFailed: "The benchmark didn't load", // (proposed)
  none: "Evaluation not yet available",
  noneBody: "The benchmark runs once on the shipped public week before release. Until it has run, Reports shows no comparison.", // (proposed)
  historical: "No benchmark available for the current configuration.",
  historicalBody: "A new benchmark run for the current versions is needed before any current result is shown.", // (proposed)
  evaluation: "Benchmark evaluation", current: "Current configuration", // (proposed) chip
  period: (range: string) => `Simulated evaluation · ${range}`,
  scope: (branch: string | null, hq: boolean) => hq // (proposed)
    ? `Figures from your workspace for ${branch ?? "all branches"}.`
    : `Figures from your workspace for ${branch}: plans going out of and coming into your store.`,
  scopeNote: "The Benchmark tab is a saved simulated evaluation and doesn't change with this workspace.", // (proposed)
  noneOpen: "None open", noneWaiting: "None waiting", noneCommitted: "None committed", // (proposed)
  need: (n: number) => `${units(n)} ${n === 1 ? "needs" : "need"} action`, // (proposed)
  waiting: (n: number) => `${units(n)} waiting for you`, // (proposed)
  pastDue: (n: number) => `${units(n)} past the response time`, // (proposed)
  movements: (d: number, r: number) => `${units(d)} dispatched · ${units(r)} received`, // (proposed)
  cancellations: "Approved plans cancelled", // (proposed)
  versions: { engine: "Engine", dataset: "Dataset", policy: "Policy", forecast_runs: "Forecast runs",
    evaluator: "Evaluator", settings: "Settings" }, // (proposed)
  noForecast: "No forecast versions", // (proposed)
  noSettings: "not available for these settings", // (proposed) live settings an evaluation refuses have no digest
  branch: "Branch", showBranch: "Show branch", allBranches: "All branches", // (proposed) actions
} as const;

export const BENCHMARK_TEXT = {
  money: "Net revenue after operating costs",
  labels: ["Modeled store rule", "Change from equivalent stock checks", "Change from ShelfShift decisions vs modeled rule", "ShelfShift total"],
  definition: "Sales revenue − transport − handling − disposal", // (proposed)
  inbound: "The modeled store rule ignores inbound deliveries when estimating surplus. Differences reflect this limitation as well as ShelfShift's planning.",
  checks: "Simulated stock checks are answered correctly before the decision.",
  schedule: (category: string) => `The modeled store rule has no markdown steps for ${category}, which affects its results there.`, // (proposed)
  axis: (amount: string) => `Axis starts at ${amount}, not zero, so the changes are visible.`, // (proposed)
  cancel: (amount: string) => `Holding decisions and stock outcomes fixed, a cancel cost of 0 would make the difference vs the modeled store rule ${amount}.`,
} as const;

function InfoLines({ lines }: { lines: ReactNode[] }) {
  return <div className="flex flex-col gap-1">{lines.map((line, i) => <p key={i} className="m-0 flex items-start gap-1 text-label text-ink-2">
    <span className="mt-0.5 shrink-0"><Icon name="info" size={12} /></span><span className="min-w-0">{line}</span>
  </p>)}</div>;
}

function HistoricalMark() {
  return <span className="inline-flex min-w-0 max-w-full [&>span]:h-auto [&>span]:whitespace-normal"><Pill tone="warn">Different version · not representative of the current app</Pill></span>;
}

function MoneyCard({ result, session, period, status, branch }: {
  result: BenchmarkRun; session: Session; period: string; status?: ReactNode; branch: string;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const raw = result.summary.money.steps;
  if (raw.length !== 4 || raw.some((s) => typeof s.value !== "number" || !Number.isFinite(s.value))) return null;
  const steps: WaterfallStep[] = raw.map((s, i) => ({ kind: i === 0 || i === 3 ? "total" : "change", value: s.value as number }));
  const currency = session.currency, axis = axisFor(steps), totals = runningTotals(steps);
  const value = (i: number) => steps[i].kind === "total" ? money(steps[i].value, currency, true) : signed(steps[i].value, currency, true);
  const labels = [...BENCHMARK_TEXT.labels];
  const summary = `ShelfShift total ${value(3)} vs Modeled store rule ${value(0)}: ${value(1)} from equivalent stock checks and ${value(2)} from ShelfShift decisions vs modeled rule.`; // (proposed)
  const notes = [BENCHMARK_TEXT.inbound, BENCHMARK_TEXT.checks,
    BENCHMARK_TEXT.cancel(signed(result.summary.footnote_x, currency, true)),
    ...result.summary.categories_without_schedule.map(BENCHMARK_TEXT.schedule),
    ...(axis.note ? [BENCHMARK_TEXT.axis(money(axis.min, currency, true))] : [])];
  return <ChartCard title={BENCHMARK_TEXT.money} kind="simulated" status={status} period={period} summary={summary}
    actions={<button type="button" aria-expanded={open} aria-controls={panelId}
      aria-label={`${open ? "Hide" : "View"} breakdown: ${BENCHMARK_TEXT.money}`} onClick={() => setOpen((v) => !v)}
      className="min-h-7 text-body font-semibold text-navy-700 max-md:min-h-11">{open ? "Hide breakdown" : "View breakdown"}</button>}
    table={{ head: ["Step", BENCHMARK_TEXT.money, "Running total"], rows: steps.map((_, i) => [
      i === 0 ? "Modeled store rule with original records" : i === 3 ? "ShelfShift decisions with those checks" : labels[i],
      value(i), money(totals[i], currency, true),
    ]) }} notes={<><InfoLines lines={notes} />{open && <div id={panelId}>
      <MoneyBreakdown key={JSON.stringify([result.benchmark_id, session.run_id, session.role, session.branch, branch])}
        result={result} session={session} branch={branch} />
    </div>}</>}>
    <p className="m-0 text-label text-ink-2">{BENCHMARK_TEXT.definition}</p>
    <WaterfallChart steps={steps} labels={labels} currency={currency} />
  </ChartCard>;
}

function WasteCard({ result, session, period, status, branch }: {
  result: BenchmarkRun; session: Session; period: string; status?: ReactNode; branch: string;
}) {
  const [open, setOpen] = useState(false), panelId = useId();
  const waste = result.summary.waste, series = cumulative(waste.by_day, SCENARIO_IDS);
  const scenarios = SCENARIO_IDS.map((key) => result.summary.scenarios.find((s) => s.key === key)!);
  // Colour and point style follow the scenario (UI-SPEC colour table); only the legend order is positional.
  const data: PlotSeries[] = SCENARIO_IDS.map((key, i) => ({ name: scenarios[i].label, values: series[i],
    tone: WASTE_STYLE[key].tone, pointStyle: WASTE_STYLE[key].pointStyle }));
  const first = dateLabel(session.calendar, result.window.first_day).replace(/^\w+ /, "");
  const last = dateLabel(session.calendar, result.window.last_day).replace(/^\w+ /, "");
  const range = first.replace(/ [A-Za-z]+$/, "") + "–" + last;
  return <ChartCard title="Waste within the window" kind="simulated" status={status} period={period}
    summary={wasteSentence(waste.totals, waste.change_vs_markdowns_only)}
    actions={<button type="button" aria-expanded={open} aria-controls={panelId}
      aria-label={`${open ? "Hide" : "View"} breakdown: Waste within the window`} onClick={() => setOpen((v) => !v)}
      className="min-h-11 text-body font-semibold text-navy-700">{open ? "Hide breakdown" : "View breakdown"}</button>}
    table={{ head: ["Scenario date", ...scenarios.map((s) => s.label)], rows: [
      ...waste.by_day.map((row, i) => [dateLabel(session.calendar, Number(row.day)), ...series.map((s) => units(s[i]))]),
      [`Total · ${range}`, ...SCENARIO_IDS.map((k) => `${units(waste.totals[k].units)} units · ${units(waste.totals[k].kg)} kg`)],
      ["Change vs Automatic markdowns only", ...SCENARIO_IDS.map((k) => k === "markdowns_only" ? "—" : `${signedUnits(waste.change_vs_markdowns_only[k].units)} units`)],
    ] }} notes={<><InfoLines lines={[BENCHMARK_TEXT.inbound,
      "Waste is compared with Automatic markdowns only. This evaluation makes no claim that ShelfShift wastes less than the modeled store rule.",
      ...result.summary.categories_without_schedule.map(BENCHMARK_TEXT.schedule)]} />
      {open && <div id={panelId}><MoneyBreakdown result={result} session={session} branch={branch} chart="waste" /></div>}</>}>
    <p className="m-0 text-label text-ink-2">Units wasted, cumulative</p>
    <LineChart labels={waste.by_day.map((r) => dateLabel(session.calendar, Number(r.day)))} series={data} />
    <ul aria-label="Waste scenarios" className="m-0 flex list-none flex-col gap-2 p-0 text-label text-ink-2">
      {SCENARIO_IDS.map((key, i) => <li key={key} className="flex min-w-0 items-start gap-2">
        <span aria-hidden="true" style={{ color: WASTE_STYLE[key].colour }}>{WASTE_STYLE[key].mark}</span><span>{scenarios[i].label}</span></li>)}
    </ul>
  </ChartCard>;
}

function MissingValue({ label, reason }: { label: string; reason: string }) {
  const id = useId(), [open, setOpen] = useState(false);
  return <span className="relative inline-flex max-w-full flex-col items-start whitespace-normal text-left">
    <button type="button" aria-label={`Why unavailable: ${label}`} aria-describedby={id} aria-expanded={open}
      onClick={() => setOpen((v) => !v)} className="min-h-11 min-w-11 rounded border border-line px-2 text-navy-700">—</button>
    <span id={id} className={open ? "mt-1 max-w-64 text-label text-ink-2" : "sr-only"}>{reason}</span>
  </span>;
}

function Headline({ result, session, status }: { result: BenchmarkRun; session: Session; status?: ReactNode }) {
  const [open, setOpen] = useState(false), id = useId();
  const h = result.summary.headline, s = h.shelfshift;
  const end = horizonLabel(session.calendar, result.window);
  const unavailable = (key: keyof typeof s, label: string, value: number | null, ratio = false) => value === null
    ? <MissingValue label={label} reason={s.reasons[key] ?? "Not recorded in this run"} /> : ratio ? percent(value) : units(value);
  const tiles = [
    { name: "Net revenue after operating costs", value: money(s.net, session.currency, true), note: `Modeled store rule: ${money(h.store_rule.net, session.currency, true)} · changes shown above` },
    { name: "Full-price demand coverage", value: unavailable("full_price_coverage", "Full-price demand coverage", s.full_price_coverage, true), note: `${s.shortfall_filled === null ? "Shortfall not recorded" : `${units(s.shortfall_filled)} shortfall units filled`} · Modeled store rule ${h.store_rule.full_price_coverage === null ? "not recorded" : percent(h.store_rule.full_price_coverage)}` },
    { name: `Units wasted · ${end}`, value: `${units(s.units_wasted)} units`, note: `${units(s.kg_wasted)} kg · ${signedUnits(result.summary.waste.change_vs_markdowns_only.shelfshift.units)} units vs Automatic markdowns only` },
    { name: "Sell-through of moved stock", value: unavailable("sell_through_moved", "Sell-through of moved stock", s.sell_through_moved, true), note: "Moved stock sold before expiry · ShelfShift" },
  ];
  const metrics = [
    ["net", "Net revenue after operating costs", "money"], ["full_price_coverage", "Full-price demand coverage", "percent"],
    ["shortfall_filled", "Shortfall units filled", "units"], ["action_adjusted_fulfillment", "Action-adjusted fulfillment", "percent"],
    ["incremental_units_sold_vs_markdowns_only", "Incremental units sold vs Automatic markdowns only", "units"],
    ["units_wasted", "Units wasted", "units"], ["kg_wasted", "Waste, kg", "units"], ["sell_through_moved", "Sell-through of moved stock", "percent"],
  ] as const;
  return <Section title="Headline figures" right={<div className="flex flex-wrap gap-2"><KindChip kind="simulated" />{status}</div>}>
    <p className="mb-4 text-label text-ink-2">ShelfShift decisions with those checks</p>
    <div className="grid min-w-0 grid-cols-2 gap-4 xl:grid-cols-4">{tiles.map((t) => <div key={t.name} className="min-w-0 rounded-[10px] bg-sunken p-4">
      <p className="m-0 text-label font-semibold text-ink-2">{t.name}</p><div className="my-2 break-words text-title font-semibold tabular-nums">{t.value}</div>
      <p className="m-0 break-words text-label text-ink-2 [overflow-wrap:anywhere]">{t.note}</p>
    </div>)}</div>
    <div className="my-4"><InfoLines lines={[BENCHMARK_TEXT.inbound]} /></div>
    <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen((v) => !v)} className="min-h-11 text-body font-semibold text-navy-700">{open ? "Hide scenario figures" : "Show all scenario figures"}</button>
    {open && <div id={id}><Table head={["Figure", ...SCENARIO_IDS.map((k) => result.summary.scenarios.find((v) => v.key === k)!.label)]}
      rows={metrics.map(([key, label, format]) => [label, ...SCENARIO_IDS.map((k) => {
        const value = h[k][key];
        return value === null ? <MissingValue label={`${label} · ${k}`} reason={h[k].reasons[key] ?? "Not recorded in this run"} />
          : format === "money" ? money(value, session.currency, true) : format === "percent" ? percent(value) : units(value);
      })])} /></div>}
  </Section>;
}

function BenchmarkBlocks({ result, session, branch }: { result: BenchmarkRun; session: Session; branch: string }) {
  const s = result.summary, q = s.decision_quality, f = s.forecast_quality, brokeLimit = q.limit_breaks.shelfshift;
  const scenarios = SCENARIO_IDS.map((k) => s.scenarios.find((v) => v.key === k)!.label);
  const rows = (entries: [string, (key: typeof SCENARIO_IDS[number]) => ReactNode][]) => entries.map(([label, value]) => [label, ...SCENARIO_IDS.map(value)]);
  const checkKeys = [["requests", "Requests"], ["lots_counted", "Lots counted"], ["corrected_up", "Records corrected up"], ["corrected_down", "Records corrected down"], ["nothing_to_correct", "Nothing to correct"]] as const;
  const metric = (label: string, value: number | null, reason: string, ratio = false): ReactNode => value === null
    ? <MissingValue label={label} reason={reason} /> : ratio ? percent(value) : units(value);
  const forecastMissing = "Undefined for a zero-total series or when no eligible in-stock observations were recorded.";
  const maxError = Math.max(Math.abs(f.wape ?? 0), Math.abs(f.baseline_wape ?? 0), Math.abs(f.bias ?? 0), Math.abs(f.baseline_bias ?? 0));
  return <div className="flex min-w-0 flex-col gap-4">
    <Disclosure name="Stock checks & trips" result={checksLine(s.checks.shelfshift)} chip={<KindChip kind="simulated" />}>
      <Table head={["Workload", ...scenarios]} rows={checkKeys.map(([key, label]) => [label, ...SCENARIO_IDS.map((k) => s.checks[k] === null
        ? <MissingValue label={`${label} · ${k}`} reason="No stock checks in this scenario" /> : units(s.checks[k][key]))])} />
      <Table head={["Trips", ...scenarios]} rows={rows([
        ["Short trips", (k) => metric(`Short trips · ${k}`, s.trips[k].short, "No transfers in this scenario")],
        ["Failed trips", (k) => metric(`Failed trips · ${k}`, s.trips[k].failed, "No transfers in this scenario")],
        ["Transport paid on unshipped stock", (k) => s.trips[k].transport_unshipped === null ? <MissingValue label={`Transport paid on unshipped stock · ${k}`} reason="Not recorded in this run" /> : money(s.trips[k].transport_unshipped, session.currency, true)],
      ])} />
      <h3 className="mt-4 text-body font-semibold">Closing balance · end of {dateLabel(session.calendar, result.window.last_day)}</h3>
      <Table head={["Closing balance", ...scenarios]} rows={rows([
        ["Stock on hand, units", (k) => units(s.closing[k].on_hand_units)], ["Stock in transit, units", (k) => units(s.closing[k].in_transit_units)],
        ["Open transfers", (k) => units(s.closing[k].open_transfers)], ["Costs charged", (k) => money(s.closing[k].costs_charged, session.currency, true)],
      ])} />
      <InfoLines lines={[BENCHMARK_TEXT.cancel(signed(s.footnote_x, session.currency, true)), BENCHMARK_TEXT.checks]} />
    </Disclosure>
    <Disclosure name="Decision quality" result={qualityLine(q)} chip={<KindChip kind="simulated" />}>
      <Kv stacked items={[["Plans that broke a limit", <span className="flex flex-wrap items-center gap-2"><span className="num">{units(brokeLimit)}</span><Pill tone={brokeLimit === 0 ? "good" : "bad"}>{brokeLimit === 0 ? "No limit broken · measured" : `${units(brokeLimit)} ${brokeLimit === 1 ? "plan broke" : "plans broke"} a limit · measured`}</Pill></span>],
        ["Opened", units(q.opened)], ["Approved", units(q.approved)], ["Rejected", units(q.rejected)], ["Waiting at the end of the window", units(q.waiting)],
        ["Retry limit", units(q.retry_limit)], ["Time to decision", q.time_to_decision === null ? "Not recorded in this run" : units(q.time_to_decision)]]} />
      <h3 className="mt-4 text-body font-semibold">Stock decisions left open by reason</h3>
      {Object.keys(q.left_open).length ? <Kv stacked items={Object.entries(q.left_open).map(([reason, count]) => [reason.replaceAll("_", " "), units(count)])} /> : <p className="text-body">None left open</p>}
      <p className="mt-4 text-label text-ink-2">In the benchmark, ShelfShift's recommendations are committed automatically. In the app, people approve every action.</p>
    </Disclosure>
    <Disclosure name="Forecast quality" result={forecastLine(f)} chip={<KindChip kind="forecast" />}>
      <p className="break-words text-body">Backtest {scenarioDate(session.calendar, f.window.first_day)} – {scenarioDate(session.calendar, f.window.last_day)} · cutoff {scenarioDate(session.calendar, f.cutoff_day)}</p>
      <p className="break-all text-label text-ink-2">Forecast run {f.forecast_run_id}</p>
      <Table head={["Forecast metric", "Forecast · precomputed", "Seasonal baseline"]} rows={[
        ["WAPE", metric("Forecast WAPE", f.wape, forecastMissing, true), metric("Seasonal baseline WAPE", f.baseline_wape, forecastMissing, true)],
        ["Bias", metric("Forecast bias", f.bias, forecastMissing, true), metric("Seasonal baseline bias", f.baseline_bias, forecastMissing, true)],
      ]} />
      <div aria-label="Forecast errors" className="my-4 flex min-w-0 flex-col gap-2">{([ ["Forecast WAPE", f.wape], ["Seasonal baseline WAPE", f.baseline_wape], ["Forecast bias", f.bias], ["Seasonal baseline bias", f.baseline_bias] ] as const).map(([label, value]) => <div key={label} className="flex min-w-0 flex-wrap items-center gap-2 text-label"><span>{label}{value === null ? " · undefined" : label.endsWith("bias") ? value === 0 ? " · no net bias" : value < 0 ? " · forecast below sales" : " · forecast above sales" : ""}</span>{value !== null && <span aria-hidden="true" className="inline-block h-2 w-[160px] shrink-0 bg-sunken"><span className="block h-2 bg-navy-700" style={{ width: `${maxError === 0 ? 0 : Math.abs(value) / maxError * 100}%` }} /></span>}</div>)}</div>
      <Kv stacked items={[["Measured 80% range coverage", metric("Measured 80% range coverage", f.coverage_80, forecastMissing, true)], ["Scored days", units(f.scored_days)],
        ["Excluded stockout days", units(f.excluded_stockout_days)], ["Series without in-stock days", units(f.series_without_in_stock_days)],
        ["Provider mix", providerMix(f.providers, PROVIDER_NAMES) || "No providers recorded"], ...Object.entries(f.input_counts).map(([key, value]) => [`Inputs · ${key.replaceAll("_", " ")}`, units(value)] as [string, ReactNode])]} />
      <InfoLines lines={["Scoring uses recorded in-stock days only. Zero-total series have no defined percentage error; excluded stockout days limit what this backtest establishes.", "Forecasts last refreshed on day 3."]} />
    </Disclosure>
    <Disclosure name="By branch" result={branchLine(s.network.stores)} chip={<KindChip kind="simulated" />}>
      <BranchFigures result={result} session={session} branch={branch} />
    </Disclosure>
  </div>;
}

function BranchFigures({ result, session, branch }: { result: BenchmarkRun; session: Session; branch: string }) {
  const [page, setPage] = useState(1);
  const identity = JSON.stringify([result.benchmark_id, (session as Session & { account?: string }).account, session.run_id, session.role, session.branch, branch, page, "money", "branch"]);
  const read = useReportRead(true, identity, identity, async () => {
    const response = await getReportsBenchmark({ benchmark_id: result.benchmark_id, branch: branch || undefined, breakdown: "branch", chart: "money", page });
    const run = response.current ?? response.historical, data = run?.breakdown;
    if (run?.benchmark_id !== result.benchmark_id || data?.benchmark_id !== result.benchmark_id || data.chart !== "money" || data.by !== "branch" || data.page !== page)
      throw new ApiError(502, "invalid_response", "The breakdown does not match this benchmark. Try again.");
    return data;
  });
  const data = read.data;
  const scenarios = SCENARIO_IDS.map((k) => result.summary.scenarios.find((v) => v.key === k)!.label);
  const value = (row: Record<string, unknown>, key: string, format: "money" | "units", nested?: string) => {
    const raw = nested ? (row[nested] as Record<string, unknown> | undefined)?.[key] : row[key];
    return typeof raw === "number" ? format === "money" ? money(raw, session.currency, true) : units(raw)
      : <MissingValue label={`${String(row.branch)} · ${nested ?? ""} ${key}`} reason="Not recorded in this published breakdown" />;
  };
  return <div className="flex min-w-0 flex-col gap-4">{read.error ? <ErrorLine error={read.error} action={{ label: "Try again", onClick: read.retry }} /> : !data ? <Loading text="Loading branch figures…" /> : <>
    <div className="max-w-full overflow-x-auto rounded border border-line" tabIndex={0} aria-label="Branch figures table">
      <table className="w-full min-w-[1200px] border-collapse text-left text-label"><thead><tr>{["Branch", ...BENCHMARK_TEXT.labels, ...scenarios.map((v) => `Waste, units · ${v}`), "Moved out, units · ShelfShift", "Moved in, units · ShelfShift", "Demand not met, units · Modeled store rule", "Demand not met, units · ShelfShift"].map((v, i) => <th key={v} className={`border-b border-line bg-sunken p-3 align-bottom ${i === 0 ? "sticky left-0 z-10" : ""}`}>{v}</th>)}</tr></thead>
      <tbody>{data.rows.map((row) => <tr key={String(row.branch)} className={row.pinned === true ? "bg-navy-50" : "bg-white"}>
        <th scope="row" className={`sticky left-0 z-10 border-b border-line p-3 ${row.pinned === true ? "bg-navy-50" : "bg-white"}`}><span className="font-mono">{String(row.branch)}</span>{row.pinned === true && <Pill>Your store</Pill>}</th>
        <td className="border-b border-line p-3 num">{value(row, "store_rule", "money")}</td>
        <td className="border-b border-line p-3 num">{typeof row.checks_change === "number" ? signed(row.checks_change, session.currency, true) : value(row, "checks_change", "money")}</td>
        <td className="border-b border-line p-3 num">{typeof row.decisions_change === "number" ? <>{signed(row.decisions_change, session.currency, true)}<DivergingBar value={row.decisions_change} max={Math.max(...data.rows.map((r) => Math.abs(Number(r.decisions_change) || 0)))} /></> : value(row, "decisions_change", "money")}</td>
        <td className="border-b border-line p-3 num">{value(row, "shelfshift", "money")}</td>
        {SCENARIO_IDS.map((k) => <td key={k} className="border-b border-line p-3 num">{value(row, k, "units", "waste_units")}</td>)}
        {["moved_out", "moved_in"].map((k) => <td key={k} className="border-b border-line p-3 num">{value(row, k, "units")}</td>)}
        {["store_rule", "shelfshift"].map((k) => <td key={k} className="border-b border-line p-3 num">{value(row, k, "units", "demand_not_met")}</td>)}
      </tr>)}</tbody></table>
    </div><Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={setPage} />
  </>}<InfoLines lines={["Branch amounts follow the outcome ledger: transport and handling at the source; revenue, waste and disposal at the branch that sells or holds the stock. They add up to the network totals.", BENCHMARK_TEXT.inbound]} /></div>;
}

function Sensitivity({ result }: { result: BenchmarkRun }) {
  const { assumptions, settings } = result.summary;
  const assumed = "Recorded with this benchmark's assumptions", configured = "Recorded in this benchmark's evaluation settings";
  const values = [
    ["Markdown response", plainValue(assumptions.markdown_response), assumed, null],
    ["Stock-record errors", plainValue(assumptions.stock_record_errors), assumed, null],
    ["Stock checks", checksSetting(settings.checks), configured, BENCHMARK_TEXT.checks],
    ["Forecasts", plainValue(settings.forecast_rule), configured, "Forecasts last refreshed on day 3."],
  ] as const;
  return <Section className="min-w-0" title="How sensitive are these results?" right={<div className="flex min-w-0 max-w-full flex-wrap items-center gap-2"><KindChip kind="simulated" /><span className="inline-flex max-w-full items-start gap-1 rounded-md bg-gray-soft px-1 py-0.5 text-label font-semibold text-gray"><span className="shrink-0"><Icon name="info" size={12} /></span><span>Sensitivity checks not run for this prototype.</span></span></div>}>
    <p className="mb-4 text-body text-ink-2">This release ran the benchmark once, under the assumptions below. It makes no claim about how the results change if they don't hold.</p>
    <Kv stacked items={values.map(([label, value, source, note]) => [label, <div className="flex min-w-0 flex-col items-start gap-2">
      <span className="max-w-full whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{value}</span>
      {note && <span className="text-label text-ink-2">{note}</span>}<KindChip kind="assumption" source={source} />
    </div>])} />
  </Section>;
}

function MoneyBreakdown({ result, session, branch, chart = "money" }: {
  result: BenchmarkRun; session: Session; branch: string; chart?: "money" | "waste";
}) {
  const [by, setBy] = useState<"day" | "branch">("day");
  const [page, setPage] = useState(1);
  // Query transitions have their own identity, so an error cannot reveal the previous page or scope.
  const identity = JSON.stringify([result.benchmark_id, (session as Session & { account?: string }).account,
    session.run_id, session.role, session.branch, branch, by, page, chart]);
  const read = useReportRead(true, identity, identity, async () => {
    const response = await getReportsBenchmark({ benchmark_id: result.benchmark_id, branch: branch || undefined,
      breakdown: by, chart, page });
    const run = response.current ?? response.historical;
    const data = run?.breakdown;
    if (run?.benchmark_id !== result.benchmark_id || data?.benchmark_id !== result.benchmark_id
      || data.chart !== chart || data.by !== by || data.page !== page)
      throw new ApiError(502, "invalid_response", "The breakdown does not match this benchmark. Try again.");
    return data;
  });
  const data = read.data;
  const amount = (row: Record<string, unknown>, key: string, sign = false) => typeof row[key] === "number"
    ? (sign ? signed : money)(row[key], session.currency, true) : "—";
  return <section aria-label={chart === "money" ? "Money breakdown" : "Waste breakdown"} className="mt-4 flex min-w-0 flex-col gap-2 border-t border-line pt-4">
    <Segmented label="Breakdown grouping" value={by} options={[["day", "By day"], ["branch", "By branch"]]}
      onPick={(v) => { setBy(v); setPage(1); }} />
    {read.error ? <ErrorLine error={read.error} action={{ label: "Try again", onClick: read.retry }} />
      : !data ? <Loading text="Loading the breakdown…" /> : <>
        {chart === "waste" ? <Table head={[by === "day" ? "Scenario date" : "Branch",
          ...SCENARIO_IDS.map((k) => result.summary.scenarios.find((s) => s.key === k)!.label), ...(by === "branch" ? ["Change vs Automatic markdowns only"] : [])]}
          rows={data.rows.map((row) => [by === "day" ? dateLabel(session.calendar, Number(row.day)) : <span>{String(row.branch)} {row.pinned === true && <Pill>Your store</Pill>}</span>,
            ...SCENARIO_IDS.map((k) => typeof row[k] === "number" ? units(row[k]) : "—"),
            ...(by === "branch" ? [typeof row.change_vs_markdowns_only === "number" ? signedUnits(row.change_vs_markdowns_only) : "—"] : [])])} /> : <Table head={[by === "day" ? "Scenario date" : "Branch", ...BENCHMARK_TEXT.labels]}
          numeric={[false, true, true, true, true]} rows={data.rows.map((row) => [
            by === "day" ? dateLabel(session.calendar, Number(row.day)) : <span className="flex items-center gap-2 font-mono">{String(row.branch)}
              {session.role === "manager" && row.pinned === true && <Pill>Your store</Pill>}</span>,
            amount(row, "store_rule"), amount(row, "checks_change", true), amount(row, "decisions_change", true), amount(row, "shelfshift"),
          ])} />}
        {by === "branch" && <Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={setPage} />}
      </>}
  </section>;
}

type Tab = "workspace" | "benchmark";
export function reportsHash(tab: Tab, branch: string): string {
  const p = new URLSearchParams({ tab });
  if (branch) p.set("branch", branch);
  return `#/reports?${p}`;
}

// Invalidate on every identity, scope or selector transition, including departure/unmount. The render key
// masks old-identity content immediately, before the effect runs. A failed scope lookup may retain the prior
// scope's labelled figures only for the SAME identity. No client version comparison decides benchmark status.
// `page` is a later read of the same view: the shown content stays until that page answers (UI-SPEC "Later
// refetches keep the old content on screen"), and `pending` says the requested page has not answered yet.
function useReportRead<T>(active: boolean, identity: string, selector: string, load: () => Promise<T>, cached = false,
  page = "") {
  const [state, setState] = useState<{ identity: string; selector: string; shown: string; page: string; data: T | null; error: ApiError | null }>({
    identity, selector, shown: selector, page, data: null, error: null,
  });
  const [attempt, setAttempt] = useState(0);
  const latest = useRef(0);
  const completed = useRef<string | null>(null);
  const cacheKey = JSON.stringify([identity, selector, page, attempt]);
  const key = JSON.stringify([identity, selector, page, active, attempt]);
  const current = useRef(key);
  current.current = key;
  useEffect(() => {
    const generation = ++latest.current;
    if (!active) return;
    if (cached && completed.current === cacheKey) return;
    let alive = true;
    setState((s) => ({ identity, selector, shown: s.shown, page: s.page, data: s.identity === identity ? s.data : null,
      error: s.identity === identity && s.selector === selector ? s.error : null }));
    const applies = () => alive && latest.current === generation && current.current === key;
    load().then((data) => {
      if (applies()) {
        completed.current = cacheKey;
        setState({ identity, selector, shown: selector, page, data, error: null });
      }
    }).catch((error: ApiError) => {
      if (applies()) setState((s) => ({ ...s, error }));
    });
    return () => { alive = false; ++latest.current; };
  // load is represented by identity and the complete selector, not a new closure on every render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, identity, selector, page, attempt, key, cached, cacheKey]);
  const same = state.identity === identity;
  const data = same && (state.shown === selector || (state.selector === selector && state.error !== null)) ? state.data : null;
  const error = same && state.selector === selector ? state.error : null;
  return { data, error, pending: data !== null && error === null && state.page !== page,
    retry: () => setAttempt((n) => n + 1) };
}

function Activity({ view, hq }: { view: ReportsWorkspaceView; hq: boolean }) {
  const a = view.activity;
  const tiles: { figure: ReportFigure; icon: IconName; note: string; action?: boolean }[] = [
    { figure: "open", icon: "board", note: a.open.count ? REPORTS_TEXT.need(a.open.need_action) : REPORTS_TEXT.noneOpen },
    { figure: "pending_approval", icon: "check", note: a.pending_approval.count
      ? REPORTS_TEXT.waiting(a.pending_approval.waiting_for_you) : REPORTS_TEXT.noneWaiting,
    action: a.pending_approval.waiting_for_you > 0 },
    { figure: "unanswered_check", icon: "inbox", note: a.unanswered_check.count
      ? REPORTS_TEXT.pastDue(a.unanswered_check.past_due) : REPORTS_TEXT.noneOpen,
    action: a.unanswered_check.addressed_to_you > 0 },
    { figure: "committed", icon: "swap", note: a.committed.count
      ? REPORTS_TEXT.movements(a.committed.dispatched, a.committed.received) : REPORTS_TEXT.noneCommitted },
    { figure: "cancelled", icon: "x", note: REPORTS_TEXT.cancellations },
  ];
  return <div className="flex min-w-0 flex-col gap-6">
    <p className="m-0 flex flex-wrap items-start gap-1 text-label text-ink-2"><Icon name="info" size={12} />
      <span>{REPORTS_TEXT.scope(view.scope.branch, hq)} {REPORTS_TEXT.scopeNote}</span></p>
    <section aria-label={REPORTS_TEXT.activity}>
      <h2 className="mb-4 text-title font-semibold">{REPORTS_TEXT.activity}</h2>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-[repeat(auto-fit,minmax(160px,1fr))]">
        {tiles.map((t) => <FigureTile key={t.figure} label={REPORT_FIGURES[t.figure]} icon={t.icon}
          count={a[t.figure].count} note={t.note} needsAction={t.action}
          href={`#/board?view=table&report=${t.figure}${hq && view.scope.branch
            ? `&branch=${encodeURIComponent(view.scope.branch)}` : ""}`} />)}
      </div>
    </section>
  </div>;
}

function Outcomes({ view, session, onPage, pending = false }: {
  view: ReportsWorkspaceView; session: Session; onPage: (n: number) => void; pending?: boolean;
}) {
  const e = view.expected, a = view.actual, combined = e.combined;
  const line = combinedLine(combined);
  const max = Math.max(0, ...e.plans.map((p) => p.computed && p.net_change !== null ? Math.abs(p.net_change) : 0));
  const headings = ["Stock decision", "Plan", "Horizon", "Net revenue after operating costs · change vs Keep",
    "Waste · change vs Keep", "Transport and handling cost"];
  const breakdown = `#/board?view=table&report=committed${session.role === "hq" && view.scope.branch
    ? `&branch=${encodeURIComponent(view.scope.branch)}` : ""}`;
  return <div className="@container min-w-0">
    <div className="grid min-w-0 gap-4 @min-[960px]:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <Section className="min-w-0" title="Expected outcomes" right={<div className="flex flex-wrap items-center gap-2">
        <KindChip kind="simulated" />{e.total > 0 && <a href={breakdown} className="text-label max-md:min-h-11 max-md:content-center">View breakdown</a>}
      </div>}>
        <p className="text-label text-ink-2">{session.role === "hq" ? "Expected network impact of active committed plans"
          : "Expected network impact of plans involving this store"}</p>
        <p className="mb-4 text-label text-ink-2">Engine estimates at commitment. Each value is the plan's change versus Keep (store markdowns only), over the plan's horizon.</p>
        {e.total === 0 ? <><h3 className="text-title font-semibold">No committed plans yet</h3>
          <p className="text-body text-ink-2">Expected outcomes appear here when a stock decision is committed. They are engine estimates, not recorded results.</p></>
          : <>{pending && <div className="mb-2"><Loading text="Loading the breakdown…" /></div>}
            <div className="max-w-full overflow-x-auto" role="region" aria-label="Expected outcomes table" tabIndex={0}
              aria-busy={pending || undefined}>
            <table className="w-full min-w-[880px] border-collapse text-body">
              <thead className="bg-side"><tr>{headings.map((h) => <th key={h} scope="col"
                className="border-b border-line px-4 py-2 text-left text-label font-semibold text-tx2">{h}</th>)}</tr></thead>
              <tbody>{e.plans.map((p) => <tr key={p.case_id}>
                <td className="max-w-48 break-words border-b border-line px-4 py-2 font-mono [overflow-wrap:anywhere]">
                  <a href={`#/cases/${encodeURIComponent(p.key)}`}>{p.key}</a></td>
                <td className="max-w-60 break-words border-b border-line px-4 py-2 [overflow-wrap:anywhere]">{p.plan_label}</td>
                <td className="border-b border-line px-4 py-2">{horizonLabel(session.calendar, p.horizon)}</td>
                <td className="num border-b border-line px-4 py-2">{p.computed ? <div className="flex flex-col gap-1">
                  {signed(p.net_change, session.currency)}{p.net_change !== null && <DivergingBar value={p.net_change} max={max} />}
                </div> : "Not computed"}</td>
                <td className="num border-b border-line px-4 py-2">{p.computed ? wasteChange(p.waste_change_units, p.waste_change_kg) : "Not computed"}</td>
                <td className="num border-b border-line px-4 py-2">{p.computed ? money(p.transport_and_handling, session.currency) : "Not computed"}</td>
              </tr>)}</tbody>
              {combined && <tfoot><tr className="font-semibold">
                {combined.additive ? <><th scope="row" colSpan={3} className="px-4 py-2 text-left">Combined · {units(combined.plans)} {combined.plans === 1 ? "plan" : "plans"}</th>
                  <td className="num px-4 py-2">{signed(combined.net_change, session.currency)}</td>
                  <td className="num px-4 py-2">{wasteChange(combined.waste_change_units, combined.waste_change_kg)}</td>
                  <td className="num px-4 py-2">{money(combined.transport_and_handling, session.currency)}</td></>
                  : <td colSpan={6} className="px-4 py-2 text-label font-normal text-ink-2">
                    {line === "not_additive" ? <><Pill>Not additive</Pill><p className="mt-2">These plans project some of the same products and branches, so their figures overlap and can't be added.</p></>
                      : "Some plans have no stored estimate, so no total is shown."}</td>}
              </tr></tfoot>}
            </table>
          </div><Pager page={e.page} pageSize={e.page_size} total={e.total} onPage={onPage} /></>}
      </Section>
      <Section className="min-w-0" title="Actual outcomes">
        <Kv stacked items={[
          ["Dispatched", a.dispatched_transfers > 0 ? `${units(a.dispatched_units)} units in ${transfersText(a.dispatched_transfers)}` : "None yet"],
          ["Received", a.received_units > 0 ? `${units(a.received_units)} units` : "None yet"],
          ["Receipt discrepancies", a.discrepancies_open > 0 ? `${units(a.discrepancies_open)} open` : "None"],
          ["Sales after receipt", <span className="text-tx3">Not yet recorded.</span>],
          ["Waste after receipt", <span className="text-tx3">Not yet recorded.</span>],
        ]} />
        <p className="mt-4 text-label text-tx3">Product mode records dispatch and receipt. Sales and waste after receipt aren't recorded in this workspace.</p>
      </Section>
    </div>
  </div>;
}

function benchmarkPeriod(result: BenchmarkRun, session: Session) {
  const first = dateLabel(session.calendar, result.window.first_day).replace(/^\w+ /, "");
  const last = dateLabel(session.calendar, result.window.last_day).replace(/^\w+ /, "");
  const end = session.calendar.find((d) => d.day === result.window.last_day)?.date;
  return REPORTS_TEXT.period(`${first} – ${last}${end ? ` ${end.slice(0, 4)}` : ""}`);
}

function Evaluation({ result, session, view, branch }: {
  result: BenchmarkRun; session: Session; view: BenchmarkView; branch: string;
}) {
  const period = benchmarkPeriod(result, session);
  const historical = view.status === "historical";
  const status = historical ? <HistoricalMark /> : <Pill>{REPORTS_TEXT.current}</Pill>;
  const lots = result.summary.network.at_risk_lots_by_day.find((r) => r.day === 3)?.lots;
  const end = dateLabel(session.calendar, result.window.last_day).replace(/^\w+ /, "");
  // Every version in full, except the settings digest: its first 12 characters (UI-SPEC B1 item 5).
  const shortVersion = (key: string, v: string | string[] | null) => v === null ? REPORTS_TEXT.noSettings
    : Array.isArray(v) ? v.join(", ") || REPORTS_TEXT.noForecast : key === "settings" ? v.slice(0, 12) : v;
  const conditionLines = [BENCHMARK_TEXT.checks, "Forecasts last refreshed on day 3.",
    "The evaluation uses recorded units sold as a demand proxy. It does not estimate unmet demand during stockouts or remove historical promotion effects.",
    `The window ends at the end of ${end} for every scenario. Stock still on hand or in transit is not counted as revenue or waste.`,
    "Deliveries are as in the dataset in every scenario, and no scenario places orders."];
  const settingValue = (field: string, value: unknown) => typeof value === "number" && field === "waste_price"
    ? `${money(value, session.currency)}/kg` : String(value);
  return <div className="flex min-w-0 flex-col gap-2"><Section title={REPORTS_TEXT.evaluation} right={<div className="flex flex-wrap gap-2">
    <KindChip kind="simulated" />{status}</div>}>
    <p className="mb-1 text-body font-semibold">{period}</p>
    <p className="mb-1 text-label text-ink-2">Network benchmark · {units(result.summary.network.stores)} stores
      {typeof lots === "number" ? ` · ${units(lots)} at-risk lots on day 3` : ""} · public sales data with simulated stock and operations</p>
    <p className="mb-2 text-label text-ink-2">{session.role === "manager"
      ? "Read-only. A saved simulated evaluation, not results of your store's decisions."
      : `A saved simulated evaluation, not results of this workspace's decisions.${branch ? " Network totals. The branch filter applies to the by-branch tables." : ""}`}</p>
  </Section><div className="grid min-w-0 gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 440px), 1fr))" }}>
    <MoneyCard key={JSON.stringify([result.benchmark_id, session.run_id, session.role, session.branch, branch])}
      result={result} session={session} period={period} branch={branch} status={historical ? status : undefined} />
    <WasteCard key={`waste-${JSON.stringify([result.benchmark_id, session.run_id, session.role, session.branch, branch])}`}
      result={result} session={session} period={period} branch={branch} status={historical ? status : undefined} />
  </div><section aria-label="Evaluation conditions and versions" className="grid min-w-0 gap-4 rounded-[10px] border border-line bg-white p-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <InfoLines lines={conditionLines} />
      <div className="min-w-0 text-label"><dl className="flex flex-wrap gap-x-4 gap-y-1">{Object.entries(REPORTS_TEXT.versions).map(([key, text]) =>
        <div key={key} className="min-w-0"><dt className="inline text-ink-2">{text} </dt><dd className="m-0 inline break-all font-mono">{shortVersion(key, result.versions[key as keyof typeof result.versions])}
          {historical && result.differs.includes(key as keyof typeof result.versions) && <span className="ml-1 text-warn">Differs · app {shortVersion(key, view.app[key as keyof typeof view.app])}</span>}</dd></div>)}</dl>
        <p className="mt-1 break-words text-ink-2 [overflow-wrap:anywhere]">Benchmark {result.benchmark_id} · published {recordedLine(result.published_at, session.time_zone)} · code {result.git_commit.slice(0, 7)}, stored for provenance, not compared</p>
        {!historical && view.settings_note.length > 0 && <p className="mt-2 text-ink-2"><Icon name="sliders" size={12} /> This workspace's settings differ from the benchmark's: {view.settings_note.map((s) =>
          `${s.field === "waste_price" ? "Waste price" : s.field.replaceAll("_", " ")} ${settingValue(s.field, s.workspace)} (benchmark ${settingValue(s.field, s.benchmark)})`).join("; ")}.</p>}
        <p className="mt-2 text-ink-2">Modeled store rule = the Simple rule (store markdowns + earliest transfer) on a stock decision. Automatic markdowns only = Keep (store markdowns only).</p>
      </div>
  </section><Headline result={result} session={session} status={historical ? status : undefined} />
  <BenchmarkBlocks key={JSON.stringify([result.benchmark_id, session.run_id, session.role, session.branch, branch])} result={result} session={session} branch={branch} />
  <Sensitivity result={result} /></div>;
}

function BenchmarkStatus({ view, session, branch }: { view: BenchmarkView; session: Session; branch: string }) {
  if (view.status === "none") return <Section className="py-12 text-center">
    <span className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-full bg-sunken"><Icon name="chart" size={20} /></span>
    <h2 className="text-title font-semibold">{REPORTS_TEXT.none}</h2>
    <p className="m-0 text-body text-ink-2">{REPORTS_TEXT.noneBody}</p>
  </Section>;
  if (view.status === "historical") return <div className="flex min-w-0 flex-col gap-4"><Banner tone="warn" icon="alert"
    title={REPORTS_TEXT.historical} meta={REPORTS_TEXT.historicalBody} />{view.historical &&
      <Disclosure key={view.historical.benchmark_id} name={`Historical benchmark · ${benchmarkPeriod(view.historical, session)}`}
        chip={<HistoricalMark />}
        result={`${view.historical.differs.length} of ${Object.keys(REPORTS_TEXT.versions).length} versions differ from the app`}>
        <Evaluation result={view.historical} session={session} view={view} branch={branch} />
      </Disclosure>}</div>;
  const result = view.current;
  if (result === null) return null; // malformed data is never turned into invented figures
  return <Evaluation result={result} session={session} view={view} branch={branch} />;
}

function ReportsPage({ route }: { route: Route }) {
  const { session } = useSession();
  const hq = session.role === "hq";
  const branch = hq ? (route.query.branch ?? "").trim() : "";
  const tab: Tab = route.query.tab === "benchmark" ? "benchmark" : "workspace";
  const identity = JSON.stringify([(session as Session & { account?: string }).account, session.run_id,
    session.role, session.branch]);
  const [paging, setPaging] = useState({ identity, branch, page: 1 });
  const pageNo = paging.identity === identity && paging.branch === branch ? paging.page : 1;
  // The Expected outcomes page is a later read of the same tab, never a new scope: it keeps the tab on screen.
  const selector = JSON.stringify([branch, tab, route.query.breakdown, route.query.chart, route.query.page,
    route.query.benchmark_id, route.path]);
  const workspace = useReportRead(tab === "workspace", identity, selector,
    () => getReportsWorkspace({ branch: branch || undefined, ...(pageNo > 1 ? { page: pageNo } : {}) }), false,
    String(pageNo));
  const benchmarkSelector = JSON.stringify([branch, route.query.breakdown, route.query.chart, route.query.page,
    route.query.benchmark_id, route.path]);
  const benchmark = useReportRead(tab === "benchmark", JSON.stringify([identity, route.query.benchmark_id]), benchmarkSelector,
    () => getReportsBenchmark({ branch: branch || undefined, benchmark_id: route.query.benchmark_id || undefined }), true);
  useRefetchOnVisible(() => { if (tab === "workspace") workspace.retry(); });
  const read = tab === "workspace" ? workspace : benchmark;
  const branchError = hq && read.error && (read.error.code === "not_found" || read.error.status === 400) ? read.error : null;
  const [branchText, setBranchText] = useState(branch);
  useEffect(() => setBranchText(branch), [branch, identity]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    window.location.hash = reportsHash(tab, branchText.trim());
  };
  const actions = hq ? <div className="flex min-w-0 flex-col gap-1">
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
      <label className="flex flex-col gap-1 text-label text-ink-2">{REPORTS_TEXT.branch}
        <input value={branchText} onChange={(e) => setBranchText(e.target.value)}
          autoComplete="off" autoCapitalize="none" spellCheck={false}
          aria-invalid={branchError !== null || undefined}
          aria-describedby={branchError ? "reports-branch-error" : undefined}
          className="h-8 w-28 rounded-lg border border-rule-2 bg-surface px-2 font-mono text-body text-ink max-md:h-11" />
      </label>
      <Button kind="plain" type="submit" className="max-md:min-h-11">{REPORTS_TEXT.showBranch}</Button>
      <Button kind="ghost" className="max-md:min-h-11" onClick={() => { setBranchText(""); window.location.hash = reportsHash(tab, ""); }}>
        {REPORTS_TEXT.allBranches}</Button>
    </form>
    {branchError && <div id="reports-branch-error"><ErrorLine error={branchError} /></div>}
  </div> : undefined;
  const again = () => read.retry();
  let body;
  if (read.error && !read.data) body = <LoadError title={tab === "workspace" ? REPORTS_TEXT.failed : REPORTS_TEXT.benchmarkFailed}
    error={read.error} onRetry={again} />;
  else if (!read.data) body = <Loading text={tab === "workspace" ? REPORTS_TEXT.loading : REPORTS_TEXT.benchmarkLoading} />;
  else body = <>{read.error && <StaleBanner kind="refresh_failed" onAct={again} />}
    {tab === "workspace" ? <div className="flex min-w-0 flex-col gap-6"><Activity view={workspace.data!} hq={hq} />
      <Outcomes view={workspace.data!} session={session} pending={workspace.pending}
        onPage={(page) => setPaging({ identity, branch, page })} /></div>
      : <BenchmarkStatus view={benchmark.data!} session={session} branch={branch} />}</>;
  return <><PageHead title={REPORTS_TEXT.title} actions={actions} />
    <Tabs label={REPORTS_TEXT.tabs} value={tab} options={[
      { value: "workspace", label: REPORTS_TEXT.workspace }, { value: "benchmark", label: REPORTS_TEXT.benchmark },
    ]} onPick={(value) => { window.location.hash = reportsHash(value, branch); }}>{body}</Tabs>
  </>;
}

export const page: PageDef = {
  path: "/reports", title: "Reports",
  nav: { label: "Reports", icon: "report", section: "Stock", order: 20 },
  Component: ReportsPage,
};
