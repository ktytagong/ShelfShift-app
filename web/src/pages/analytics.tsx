import { providerShort } from "../vocab.ts";
// Analytics (06.1-UI-SPEC "Analytics"; D-15, D-17, D-32, D-51): one store's public sales by product, one product's
// sales with the precomputed forecast and its hours out of stock, simulated aging stock and estimated potential waste,
// read from GET /api/analytics. Every chart is a ChartCard with its kind, its period in scenario dates, a one-line
// summary and "Show as table". The page asks for one store and one product only (a manager's own store; HQ's chosen
// branch); the server orders, cuts and labels every list, and the client never re-sorts or re-filters it. Analytics
// covers history before the scenario week (06.1-14: the period is the dataset's history window). Waste is stated
// against doing nothing only (Keep).
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { ApiError } from "../api.ts";
import { getAnalytics, type AnalyticsData, type AnalyticsSeries, type AnalyticsView, type WasteRow } from "../contract.ts";
import { PRODUCT_CODE, VALUE_AT_FULL_PRICE, wasteSummary } from "../copy.ts";
import { expected, markDate, money, shortDate, units } from "../format.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { PageHead, useSession, type Route } from "../shell.tsx";
import {
  BarChart, Button, ChartCard, ErrorLine, KindChip, LineChart, LoadError, Loading, StaleBanner, type ValueKind,
} from "../ui.tsx";

// The page's own words. "(proposed)" marks wording the user has not confirmed; one constant each. The card titles,
// the chips (ui.tsx KIND_TEXT) and "Hours out of stock (shortage)" are the UI-SPEC's.
export const ANALYTICS = "Analytics";
export const ANALYTICS_TEXT = {
  loading: "Loading analytics…", // (proposed) UI-SPEC "Interaction states"
  failed: "Analytics didn't load", // (proposed) the per-page error title
  branch: "Branch",
  show: "Show", // (proposed) the pickers' apply button
  chooseBranch: "Choose a branch to see its analytics.", // (proposed) 06.1-14's sentence
  sales: "Sales by product",
  series: (product: string) => `Sales and forecast · ${product}`,
  aging: "Aging stock",
  waste: "Potential waste",
  date: "Date", // (proposed) table column
  units: "Units", // (proposed) table column
  oos: "Hours out of stock", // (proposed) table column
  oosAxis: "Hours out of stock (shortage)",
  forecastBase: "Forecast base", // (proposed) table column and legend
  forecastRange: "Forecast 80% range", // (proposed) the band's legend entry
  forecastLow: "Forecast low", // (proposed) table column
  forecastHigh: "Forecast high", // (proposed) table column
  forecastPeriod: (from: string, to: string) => `forecast ${from} – ${to}`, // (proposed) after the history period
  daysToExpiry: "Days to expiry", // (proposed) table column
  bucket: (label: string) => `${label} days`, // (proposed) "0–1 days"
  lots: "Lots", // (proposed) table column
  lot: "Product · lot", // (proposed) table column
  atRisk: "Units at risk", // (proposed) table column
  kg: "kg", // (proposed) table column
  value: VALUE_AT_FULL_PRICE, // table column (approved by the user 2026-10-18)
  scenarioDate: (date: string) => `Scenario date: ${date}`, // D-58's label
  topSeller: (product: string, n: number) => `${product} sold the most: ${units(n)} units in this period.`, // (proposed)
  seriesSummary: (sold: number, hours: number, base: number, days: number) => // (proposed)
    `${units(sold)} units sold, ${units(hours)} hours out of stock; forecast ${expected(base)} units for the next ${days} days.`,
  agingSummary: (n: number, lots: number) => `${units(n)} units in ${lots} lots expire within 1 day.`, // (proposed)
  wasteSummary, // waste against doing nothing only, each lot once (approved by the user 2026-10-18)
  noSales: (product: string) => `No sales recorded for ${product} in this period.`, // (proposed) UI-SPEC S15
  noStoreSales: "No sales recorded at this store in this period.", // (proposed)
  noWaste: "No lots at risk of waste at this store.", // (proposed)
  topSellerLink: "Show the store's top seller", // (proposed) after an unknown product
} as const;

// The 06.1-14 contract row's keys (2026-10-17, as built), typed in contract.ts.
type Series = AnalyticsSeries;
type Available = AnalyticsData;
type Loaded = AnalyticsView;

const periodText = (p: { from: string; to: string }) => `${shortDate(p.from)} – ${markDate(p.to)}`;
const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);

// "#/analytics?store=…&product=…" (UI-SPEC "Routes"): empty values left out; a manager's link never names a store.
function analyticsHash(store: string, product: string): string {
  const q = new URLSearchParams();
  if (store !== "") q.set("store", store);
  if (product !== "") q.set("product", product);
  const text = q.toString();
  return `#/analytics${text === "" ? "" : `?${text}`}`;
}

// A card whose chart has nothing to draw: the title, the kind chip and the period, then one line in the chart's place;
// no table toggle, because there is no table (UI-SPEC S15).
function EmptyCard({ title, kind, period, text, actions }: {
  title: string; kind?: ValueKind; period?: string; text: string; actions?: ReactNode;
}) {
  return (
    <section aria-label={title} className="flex min-w-0 flex-col gap-2 rounded-2xl border border-rule bg-surface p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="m-0 text-title font-semibold text-ink">{title}</h2>
        {kind && <KindChip kind={kind} />}
        {actions}
      </div>
      {period && <p className="num m-0 text-label text-ink-3">{period}</p>}
      <p className="m-0 py-12 text-center text-body text-ink-2">{text}</p>
    </section>
  );
}

function SalesCard({ view }: { view: Available }) {
  const rows = view.sales_by_product;
  const period = periodText(view.period);
  const top = rows[0];
  if (top === undefined) return <EmptyCard title={ANALYTICS_TEXT.sales} period={period} text={ANALYTICS_TEXT.noStoreSales} />;
  return (
    <ChartCard title={ANALYTICS_TEXT.sales} period={period} summary={ANALYTICS_TEXT.topSeller(top.product, top.units)}
      table={{
        head: [PRODUCT_CODE, ANALYTICS_TEXT.units, ANALYTICS_TEXT.oos],
        rows: rows.map((r) => [r.product, units(r.units), units(r.hours_out_of_stock)]),
      }}>
      <BarChart horizontal labels={rows.map((r) => r.product)}
        series={[{ name: ANALYTICS_TEXT.units, values: rows.map((r) => r.units), tone: "public" }]} />
    </ChartCard>
  );
}

// Public daily units (navy-800) with hours out of stock as bars on a second axis, then the forecast made on the
// scenario date: its base dashed navy-700 inside the navy-100 80% band. History days carry no forecast and forecast
// days no sales: those cells are missing, never zero.
function SeriesCard({ view, series, picker }: { view: Available; series: Series; picker: ReactNode }) {
  const title = ANALYTICS_TEXT.series(series.product);
  const fc = series.forecast;
  const last = fc[fc.length - 1];
  const period = periodText(view.period)
    + (fc.length > 0 && last !== undefined ? ` · ${ANALYTICS_TEXT.forecastPeriod(shortDate(fc[0].date), markDate(last.date))}` : "")
    + (series.forecast_cutoff ? ` · history to ${markDate(series.forecast_cutoff)}` : "");
  if (!series.days.some((d) => d.units > 0)) {
    return <EmptyCard title={title} kind="forecast" period={period} text={ANALYTICS_TEXT.noSales(series.product)}
      actions={picker} />;
  }
  const h = series.days.length;
  const pad = (n: number) => Array<null>(n).fill(null);
  const sold = [...series.days.map((d) => d.units), ...pad(fc.length)];
  const hours = [...series.days.map((d) => d.hours_out_of_stock), ...pad(fc.length)];
  const low = [...pad(h), ...fc.map((f) => f.low)];
  const base = [...pad(h), ...fc.map((f) => f.base)];
  const high = [...pad(h), ...fc.map((f) => f.high)];
  const dates = [...series.days.map((d) => d.date), ...fc.map((f) => f.date)];
  const cell = (v: number | null, f: (n: number) => string) => (v === null ? null : f(v));
  return (
    <ChartCard title={title} kind="forecast" period={period} actions={picker}
      summary={<>{ANALYTICS_TEXT.seriesSummary(sum(series.days.map((d) => d.units)),
        sum(series.days.map((d) => d.hours_out_of_stock)), sum(fc.map((f) => f.base)), fc.length)}{series.forecast_run_id && fc.some(point => point.provider) && <>
          {" · Precomputed · "}{providerShort([...new Set(fc.flatMap(point => point.provider ? [point.provider] : []))].sort((a,b) => {
            const order = ["ai_forecast", "arima_plus", "local-seasonal-naive"];
            return (order.includes(a) ? order.indexOf(a) : order.length) - (order.includes(b) ? order.indexOf(b) : order.length) || a.localeCompare(b);
          }))}{" · run "}<span className="break-all font-mono">{series.forecast_run_id}</span>
        </>}</>}
      table={{
        head: [ANALYTICS_TEXT.date, ANALYTICS_TEXT.units, ANALYTICS_TEXT.oos, ANALYTICS_TEXT.forecastLow,
          ANALYTICS_TEXT.forecastBase, ANALYTICS_TEXT.forecastHigh],
        rows: dates.map((d, i) => [shortDate(d), cell(sold[i], units), cell(hours[i], units), cell(low[i], expected),
          cell(base[i], expected), cell(high[i], expected)]),
      }}>
      <LineChart labels={dates.map(shortDate)} rightAxisTitle={ANALYTICS_TEXT.oosAxis} series={[
        { name: ANALYTICS_TEXT.units, values: sold, tone: "public" },
        { name: ANALYTICS_TEXT.forecastBase, values: base, tone: "forecast" },
        { name: ANALYTICS_TEXT.forecastRange, low, high, tone: "forecast" },
        { name: ANALYTICS_TEXT.oosAxis, values: hours, tone: "plan", type: "bar", axis: "right" },
      ]} />
    </ChartCard>
  );
}

// Recorded stock by days to expiry from the scenario date (simulated), in the server's four buckets.
function AgingCard({ view, period }: { view: Available; period?: string }) {
  const rows = view.aging;
  const first = rows[0];
  return (
    <ChartCard title={ANALYTICS_TEXT.aging} kind="simulated" period={period}
      summary={first === undefined ? "" : ANALYTICS_TEXT.agingSummary(first.units, first.lots)}
      table={{
        head: [ANALYTICS_TEXT.daysToExpiry, ANALYTICS_TEXT.units, ANALYTICS_TEXT.lots],
        rows: rows.map((r) => [ANALYTICS_TEXT.bucket(r.bucket), units(r.units), units(r.lots)]),
      }}>
      <BarChart labels={rows.map((r) => ANALYTICS_TEXT.bucket(r.bucket))}
        series={[{ name: ANALYTICS_TEXT.units, values: rows.map((r) => r.units), tone: "plan" }]} />
    </ChartCard>
  );
}

// The top 10 lots by units at risk of waste if nothing changes (estimated, Keep only), with kg and value in the table.
function WasteCard({ view, period, currency }: { view: Available; period?: string; currency: string }) {
  const rows = view.waste;
  if (rows.length === 0) {
    return <EmptyCard title={ANALYTICS_TEXT.waste} kind="estimated" period={period} text={ANALYTICS_TEXT.noWaste} />;
  }
  const label = (r: WasteRow) => `${r.product} · ${r.lot_id}`;
  return (
    <ChartCard title={ANALYTICS_TEXT.waste} kind="estimated" period={period}
      summary={ANALYTICS_TEXT.wasteSummary(sum(rows.map((r) => r.units)), rows.length)}
      table={{
        head: [ANALYTICS_TEXT.lot, ANALYTICS_TEXT.atRisk, ANALYTICS_TEXT.kg, ANALYTICS_TEXT.value],
        rows: rows.map((r) => [label(r), expected(r.units), units(r.kg), money(r.value, currency)]),
      }}>
      <BarChart horizontal labels={rows.map(label)}
        series={[{ name: ANALYTICS_TEXT.atRisk, values: rows.map((r) => r.units), tone: "waste" }]} />
    </ChartCard>
  );
}

function AnalyticsPage({ route }: { route: Route }) {
  const { session } = useSession();
  const hq = session.role === "hq";
  // HQ's store from the address: "store" (this page's own links) or "branch" (Forecasts' "View in Analytics", 06.1-25).
  const store = hq ? (route.query.store ?? route.query.branch ?? "").trim() : "";
  const product = (route.query.product ?? "").trim();
  const needsBranch = hq && store === "";

  const [data, setData] = useState<Loaded | null>(null);
  const [shownFor, setShownFor] = useState<string | null>(null); // the query the shown data answers
  const [error, setError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);
  const query = JSON.stringify([session.run_id, hq, store, product]);
  const latest = useRef(0);
  const [branchText, setBranchText] = useState(store);
  const [productText, setProductText] = useState(product);
  const branchInput = useRef<HTMLInputElement>(null);
  useEffect(() => setBranchText(store), [store]);
  useEffect(() => setProductText(product), [product]);

  // One store and one product per request (D-17): nothing is asked until HQ has chosen a branch. A newer request
  // replaces an older one's answer. A refetch of the same query (Try again, the tab visible again) keeps the charts on
  // screen, and a failed one keeps them under the stale banner (D-12); a new store or product shows its own loading
  // line, so one query's charts never sit under another's choice.
  useEffect(() => {
    const ask = ++latest.current;
    setError(null);
    if (needsBranch) {
      setData(null);
      return;
    }
    getAnalytics({ branch: hq ? store : undefined, product: product || undefined }).then((v) => {
      if (ask === latest.current) {
        setData(v);
        setShownFor(query);
      }
    }).catch((e: ApiError) => {
      if (ask === latest.current) setError(e);
    });
  }, [session.run_id, hq, store, product, needsBranch, attempt, query]);
  const again = () => setAttempt((n) => n + 1);
  useRefetchOnVisible(again);

  // HQ with no branch: the Branch input takes focus. After the shell moves focus to the content on a route change
  // (its effect runs after this page's), so the focus waits for the next task.
  useEffect(() => {
    if (!needsBranch) return;
    const t = window.setTimeout(() => branchInput.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [needsBranch]);

  // Each form sends its own field as typed (the server checks it) and keeps the other choice as committed.
  const submitBranch = (e: FormEvent) => {
    e.preventDefault();
    window.location.hash = analyticsHash(branchText.trim(), product);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    window.location.hash = analyticsHash(hq ? store : "", productText.trim());
  };
  const products = data !== null && data.available ? data.sales_by_product : [];
  const inputClass = "h-8 w-28 rounded-lg border border-rule-2 bg-surface px-2 font-mono text-body text-ink max-md:h-11";
  // HQ's Branch drives the whole page, so it stays in the header; the Product code drives only the Sales and forecast
  // card, so it sits in that card (and above the error when the server refuses the code).
  const actions = !hq ? undefined : (
    <form onSubmit={submitBranch} className="flex flex-wrap items-end gap-2">
      <label className="flex flex-col gap-1 text-label text-ink-2">
        {ANALYTICS_TEXT.branch}
        <input ref={branchInput} value={branchText} onChange={(e) => setBranchText(e.target.value)} autoComplete="off"
          spellCheck={false} className={inputClass} />
      </label>
      <Button kind="plain" type="submit">{ANALYTICS_TEXT.show}</Button>
    </form>
  );
  const picker = (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
      <label className="flex flex-col gap-1 text-label text-ink-2">
        {PRODUCT_CODE}
        <input value={productText} onChange={(e) => setProductText(e.target.value)} list="analytics-products"
          autoComplete="off" autoCapitalize="characters" spellCheck={false} className={inputClass} />
      </label>
      <datalist id="analytics-products">
        {products.map((p) => <option key={p.sku} value={p.sku}>{p.product}</option>)}
      </datalist>
      <Button kind="plain" type="submit">{ANALYTICS_TEXT.show}</Button>
    </form>
  );

  const scenario = session.data_as_of === null ? undefined : ANALYTICS_TEXT.scenarioDate(markDate(session.data_as_of));
  const notFound = error !== null && error.code === "not_found" && product !== "";
  const stale = error !== null && !notFound && !needsBranch && data !== null && shownFor === query;
  let body;
  if (needsBranch) body = <p className="m-0 py-12 text-center text-body text-tx2">{ANALYTICS_TEXT.chooseBranch}</p>;
  else if (notFound) {
    body = (
      <div className="flex flex-col items-start gap-2">
        {picker}
        <ErrorLine error={error} />
        <a href={analyticsHash(store, "")} className="text-body font-semibold text-navy-800 underline underline-offset-2">
          {ANALYTICS_TEXT.topSellerLink}
        </a>
      </div>
    );
  } else if (error !== null && !stale) {
    body = <>{product !== "" && picker}<LoadError title={ANALYTICS_TEXT.failed} error={error} onRetry={again} /></>;
  } else if (data === null || shownFor !== query) body = <Loading text={ANALYTICS_TEXT.loading} />;
  else if (!data.available) body = <p className="m-0 py-12 text-center text-body text-tx2">{data.reason}</p>;
  else {
    body = (
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SalesCard view={data} />
        {data.series !== null && <SeriesCard view={data} series={data.series} picker={picker} />}
        <AgingCard view={data} period={scenario} />
        <WasteCard view={data} period={scenario} currency={session.currency} />
      </div>
    );
  }

  return (
    <>
      <PageHead title={ANALYTICS} actions={actions} />
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        {stale && <StaleBanner kind="refresh_failed" onAct={again} />}
        {body}
      </div>
    </>
  );
}

export const page: PageDef = {
  path: "/analytics",
  title: ANALYTICS,
  nav: { label: ANALYTICS, icon: "chart", section: "Stock", order: 7 },
  Component: AnalyticsPage,
};
