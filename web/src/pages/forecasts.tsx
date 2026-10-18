// Forecasts (06.1-UI-SPEC "Forecasts"; D-15, D-17, D-52): the store's precomputed demand per product, read-only, from
// GET /api/forecasts. The run line names the run, its provider (the server's code, shown by its pipeline name, so the
// stand-in is never called BigQuery output), the history cutoff and the period as scenario dates, with the "Forecast ·
// precomputed" chip. The table is the server's page in the server's order; nothing here sorts or filters it. No edit,
// approval or execution control exists on this page (D-52).
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, NETWORK } from "../api.ts";
import { scaled } from "../bars.ts";
import { getForecasts, type ForecastItem, type ForecastRun, type ForecastsData, type ForecastsView } from "../contract.ts";
import { PRODUCT_CODE, PROVIDER_NAMES } from "../copy.ts";
import { providerShort } from "../vocab.ts";
import { expected, markDate } from "../format.ts";
import type { PageDef } from "../pagereg.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import { PageHead, useSession, type Route } from "../shell.tsx";
import { Button, KindChip, LoadError, Loading, Pager, Pill, SHOW_CHART, SHOW_TABLE, StaleBanner } from "../ui.tsx";

// The page's own words. "(proposed)" marks wording the user has not confirmed; one constant each.
export const FORECASTS = "Forecasts";
export const FORECASTS_TEXT = {
  loading: "Loading forecasts…", // (proposed) UI-SPEC "Interaction states"
  failed: "Forecasts didn't load", // (proposed) the per-page error title
  demand: "Demand per day",
  baseTotal: "Base total", // the period's summed base forecast
  range: "80% range",
  runLine: (id: string, provider: string, historyTo: string, from: string, to: string) =>
    `Forecast run ${id} · ${provider} · history to ${historyTo} · period ${from} – ${to}`,
  demandName: (sku: string, days: string) => `Demand per day for ${sku}: ${days}`, // (proposed) the bars' name
  analytics: "View in Analytics",
  analyticsHead: "Analytics", // (proposed) the link column's hidden header
  branch: "Branch", // HQ's store input
  chooseBranch: "Choose a branch to see its forecasts.", // (proposed) 06.1-14's server sentence for HQ without one
  emptyTitle: (store: string) => `No forecasts for ${store}`,
  emptyBody: "Forecasts appear when the precomputed run covers this branch.", // (proposed)
  simulatedHead: "Simulated inputs",
  stockSimulated: "Stock simulated",
  none: "None",
  // (proposed) Simulated inputs as one line when no row on the page has any (the public set's forecasts read public
  // sales only, 06.1-14), instead of a column of "None".
  noSimulatedInputs: (input: string | null) =>
    `Simulated inputs: none. Each forecast reads the public sales history only${input ? ` (${input})` : ""}.`,
} as const;

// The provider's name from vocab.PROVIDER_NAMES; an unknown code is shown as sent, never guessed into a BigQuery name.
const providerName = (code: string) => PROVIDER_NAMES[code] ?? code;

// GET /api/forecasts as built (04-API-CONTRACT change-log 2026-10-17, "Stock & lots, Analytics and Forecasts as
// built"), typed in contract.ts.
type Run = ForecastRun;
type Item = ForecastItem;
type Available = ForecastsData;
type Loaded = ForecastsView;

// Scenario dates: "Sat 17 Oct" for a day of the period, "Sat 17" for the period's first day when it shares the
// month and year of the last.
const DAY_MS = 86_400_000;
const addDays = (iso: string, n: number) => new Date(Date.parse(`${iso}T00:00:00Z`) + n * DAY_MS).toISOString().slice(0, 10);
const shortDay = (iso: string) => markDate(iso).replace(/ \d{4}$/, "");
function periodFrom(from: string, to: string): string {
  const [a, b] = [markDate(from).split(" "), markDate(to).split(" ")];
  if (a[2] === b[2] && a[3] === b[3]) return `${a[0]} ${a[1]}`;
  return a[3] === b[3] ? shortDay(from) : markDate(from);
}

function runText(run: Run): string {
  return FORECASTS_TEXT.runLine(run.id, providerName(run.provider), markDate(run.history_to),
    periodFrom(run.period_from, run.period_to), markDate(run.period_to))
    + (Object.keys(run.providers).length > 1 ? " · " + Object.entries(run.providers).map(([code, count]) => `${providerShort([code])} ${count}`).join(" · ") + " series" : "");
}

// Seven 8px navy-700 bars scaled to the row's largest day; the values are in the accessible name.
function DemandBars({ run, item }: { run: Pick<Run, "period_from">; item: Item }) {
  const heights = scaled(item.daily_base);
  const days = item.daily_base.map((v, i) => `${shortDay(addDays(run.period_from, i))} ${expected(v)}`).join(", ");
  return (
    <span role="img" aria-label={FORECASTS_TEXT.demandName(item.sku, days)} className="inline-flex h-6 items-end gap-0.5">
      {heights.map((h, i) => (
        <span key={i} data-bar="" className="w-2 rounded-t-sm bg-navy-700" style={{ height: `${Math.max(h * 100, 4)}%` }} />
      ))}
    </span>
  );
}

// The query this page keeps in its address ("#/forecasts?branch=S042&page=2"): HQ's store and the server's page.
function hashOf(branch: string | null, page: number): string {
  const q = new URLSearchParams();
  if (branch) q.set("branch", branch);
  if (page > 1) q.set("page", String(page));
  const text = q.toString();
  return `#/forecasts${text === "" ? "" : `?${text}`}`;
}

// Analytics for that product (and HQ's store), the page's "next" link (UI-SPEC "Three-second scan").
function analyticsHref(branch: string | null, sku: string): string {
  const q = new URLSearchParams();
  if (branch) q.set("branch", branch);
  q.set("product", sku);
  return `#/analytics?${q.toString()}`;
}

// The table in its own scroll box (UI-SPEC "Table": navy-50 header band, numeric columns right-aligned). Drawn here
// rather than with ui.Table so each product row carries its test id. "Show as table" swaps the bars for one column
// per day of the period with its value. The Simulated inputs column appears only when a row on the page has one.
function ForecastTable({ view, run, asTable, hqBranch }: { view: Available; run: Run & { daily_days: string[] }; asTable: boolean; hqBranch: string | null }) {
  const days = run.daily_days;
  const simulated = view.items.some((x) => x.simulated_inputs);
  const head: [string, boolean][] = [
    [PRODUCT_CODE, false], ["Provider", false],
    ...(asTable ? days.map((d): [string, boolean] => [shortDay(d), true]) : [[FORECASTS_TEXT.demand, false] as [string, boolean]]),
    [FORECASTS_TEXT.baseTotal, true], [FORECASTS_TEXT.range, true],
    ...(simulated ? [[FORECASTS_TEXT.simulatedHead, false] as [string, boolean]] : []),
  ];
  const cell = (right: boolean) => `border-b border-line px-4 py-2 ${right ? "text-right" : "text-left"}`;
  return (
    <div className="relative min-w-0 overflow-x-auto rounded-[10px] border border-line bg-white">
      <table className="w-full border-collapse text-body">
        <thead className="bg-side">
          <tr>
            {head.map(([h, right]) => (
              <th key={h} scope="col" className={`${cell(right)} text-label font-semibold whitespace-nowrap text-tx2`}>{h}</th>
            ))}
            <th scope="col" className={cell(false)}><span className="sr-only">{FORECASTS_TEXT.analyticsHead}</span></th>
          </tr>
        </thead>
        <tbody>
          {view.items.map((item) => (
            <tr key={item.sku} data-testid="forecast-row">
              <td className={`${cell(false)} font-mono`}>{item.sku}</td>
              <td data-testid={`forecast-provider-${item.sku}`} className={cell(false)}>{providerShort(item.provider_codes ?? [])}</td>
              {asTable
                ? item.daily_base.map((v, i) => <td key={i} className={`${cell(true)} num`}>{expected(v)}</td>)
                : <td className={cell(false)}><DemandBars run={run} item={item} /></td>}
              <td data-testid="base-total" className={`${cell(true)} num`}>{expected(item.base_total)}</td>
              <td data-testid="range-80" className={`${cell(true)} num whitespace-nowrap`}>
                {`${expected(item.low_total)} – ${expected(item.high_total)}`}
              </td>
              {simulated && (
                <td data-testid="simulated-cell" className={cell(false)}>
                  {item.simulated_inputs ? <Pill tone="plan" icon="info">{FORECASTS_TEXT.stockSimulated}</Pill> : FORECASTS_TEXT.none}
                </td>
              )}
              <td className={`${cell(false)} whitespace-nowrap`}>
                <a href={analyticsHref(hqBranch, item.sku)} className="text-body font-semibold text-navy-800 underline underline-offset-2">
                  {FORECASTS_TEXT.analytics}
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// HQ's store: applied on Enter or when the field loses focus; the address keeps it.
function BranchInput({ value, focus, onPick }: { value: string; focus: boolean; onPick: (b: string) => void }) {
  const [text, setText] = useState(value);
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => setText(value), [value]);
  // After the shell's own focus move on a route change (its effect runs after this one), so the choice is ready to type.
  useEffect(() => {
    if (!focus) return;
    const t = window.setTimeout(() => box.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [focus]);
  const apply = () => { if (text.trim() !== value) onPick(text.trim()); };
  return (
    <form role="search" onSubmit={(e) => { e.preventDefault(); apply(); }}>
      <label className="flex items-center gap-2 text-label text-tx3">
        {FORECASTS_TEXT.branch}
        <input ref={box} type="text" value={text} autoComplete="off" spellCheck={false}
          onChange={(e) => setText(e.target.value)} onBlur={apply}
          className="h-8 w-32 rounded-md border border-line-2 bg-white px-2 font-mono text-body text-tx" />
      </label>
    </form>
  );
}

function Empty({ store }: { store: string }) {
  return (
    <div className="rounded-[10px] border border-line bg-white px-6 py-12 text-center">
      <h2 className="m-0 mb-2 text-title font-semibold text-tx">{FORECASTS_TEXT.emptyTitle(store)}</h2>
      <p className="m-0 text-body text-tx2">{FORECASTS_TEXT.emptyBody}</p>
    </div>
  );
}

function ForecastsPage({ route }: { route: Route }) {
  const { session } = useSession();
  const hq = session.role === "hq";
  const branch = hq ? (route.query.branch ?? "").trim() : "";
  const pageNo = Math.max(1, Number.parseInt(route.query.page ?? "1", 10) || 1);
  const blocked = hq && branch === ""; // HQ reads one store at a time (D-17); nothing is asked without one
  const [data, setData] = useState<Loaded | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [asTable, setAsTable] = useState(false);
  const latest = useRef(0);

  const load = useCallback(async () => {
    if (blocked) return;
    const ask = ++latest.current;
    try {
      const v = await getForecasts({ branch: hq ? branch : undefined, page: pageNo });
      if (ask !== latest.current) return;
      setData(v);
      setError(null);
    } catch (e) {
      if (ask !== latest.current) return;
      setError(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    }
  }, [blocked, hq, branch, pageNo]);

  // A new store or page shows its own loading line; a refetch (tab focus, workspace version) keeps the rows.
  useEffect(() => { setData(null); setError(null); }, [branch, pageNo]);
  useEffect(() => { void load(); }, [load, session.version]);
  useRefetchOnVisible(() => void load());

  const go = (b: string, p: number) => { window.location.hash = hashOf(hq ? b : null, p); };

  let body;
  if (blocked) {
    body = <p className="m-0 text-body text-tx2">{FORECASTS_TEXT.chooseBranch}</p>;
  } else if (data === null && error !== null) {
    body = <LoadError title={FORECASTS_TEXT.failed} error={error} onRetry={() => void load()} />;
  } else if (data === null) {
    body = <Loading text={FORECASTS_TEXT.loading} />;
  } else if (!data.available) {
    body = (
      <div className="flex min-w-0 flex-col gap-4">
        {error !== null && <StaleBanner kind="refresh_failed" onAct={() => void load()} />}
        <p className="m-0 text-body text-tx2">{data.reason}</p>
      </div>
    );
  } else {
    const run = data.run === null ? null : { ...data.run, daily_days: [] as string[] };
    if (run !== null) {
      const n = Math.max(0, ...data.items.map((x) => x.daily_base.length));
      run.daily_days = Array.from({ length: n }, (_, i) => addDays(run.period_from, i));
    }
    const simulated = data.items.some((x) => x.simulated_inputs);
    body = (
      <div className="flex min-w-0 flex-col gap-4">
        {error !== null && <StaleBanner kind="refresh_failed" onAct={() => void load()} />}
        {run !== null && (
          <div className="flex min-w-0 flex-col gap-1">
            <p className="m-0 flex flex-wrap items-center gap-2 text-label text-tx2">
              <span data-testid="forecast-run" className="break-words">{runText(run)}</span>
              <span data-testid="forecast-run-kind"><KindChip kind="forecast" /></span>
            </p>
            {!simulated && data.items.length > 0 && (
              <p data-testid="simulated-inputs" className="m-0 text-label text-tx2">
                {FORECASTS_TEXT.noSimulatedInputs(run.input_label)}
              </p>
            )}
          </div>
        )}
        {run === null || data.items.length === 0 ? <Empty store={data.branch} /> : (
          <>
            <div className="flex justify-end">
              <Button kind="plain" className="h-7 px-2" aria-pressed={asTable} onClick={() => setAsTable((t) => !t)}>
                {asTable ? SHOW_CHART : SHOW_TABLE}
              </Button>
            </div>
            <ForecastTable view={data} run={run} asTable={asTable} hqBranch={hq ? data.branch : null} />
          </>
        )}
        {/* Mounted with an empty page too, so a page past the last asks for the last page with records (D-17). */}
        <Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={(p) => go(branch, p)} />
      </div>
    );
  }
  return (
    <>
      <PageHead title={FORECASTS}
        actions={hq ? <BranchInput value={branch} focus={blocked} onPick={(b) => go(b, 1)} /> : undefined} />
      <div className="min-w-0 p-4 md:p-6">{body}</div>
    </>
  );
}

export const page: PageDef = {
  path: "/forecasts",
  title: FORECASTS,
  nav: { label: FORECASTS, icon: "trend", section: "Stock", order: 8 },
  Component: ForecastsPage,
};
