// Stock & lots in the page registry (06.1-23; 06.1-UI-SPEC "Stock & lots"; D-15, D-17, D-31, D-56): the store's lots
// with the simulated stock labelled, a cover bar of the recorded stock by the forecast to expiry, and the lot drawer
// with eligibility, holds and review flags, linked branch transfers and history. The server scopes, orders and pages
// the list (GET /api/lots); the row's decision action is the server's `action`, so the page never offers a second
// decision for a lot. Nothing here edits a quantity, an expiry or a hold (D-56). "#/stock?lot={id}" opens the drawer.
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, NETWORK } from "../api.ts";
import { getLot, getLots, type LotDetail, type LotRow, type LotsQuery, type LotsView } from "../contract.ts";
import { PRODUCT_CODE, STORE_PLACEHOLDER, TRANSFERS_UNAVAILABLE, retryable } from "../copy.ts";
import { markDate, shortDate, units } from "../format.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { PageHead, useSession, type Route } from "../shell.tsx";
import {
  NONE, countText, coverParts, discrepancyText, expiryText, lotHistory, markdownText, rowAction, type CoverParts,
} from "../stockview.ts";
import {
  Button, DesignedLabel, Drawer, EligibilityLine, ErrorLine, HistoryList, KindChip, LoadError, Loading, Pager, Pill,
  LinkButton, StaleBanner, TRY_AGAIN,
} from "../ui.tsx";
import type { HistoryFilter } from "../bars.ts";
import type { LotRowBannerKeys } from "../contract.ts"; // a lot row: its hold and fact label (07-22)
import { LOT_HOLD } from "../boardview.ts";
import { ReportFromLot } from "../report.tsx"; // 07-20: Report an observation on a row

const TITLE = "Stock & lots";
const LOADING = "Loading stock…"; // (proposed, UI-SPEC "Interaction states")
const LOAD_FAILED = "Stock & lots didn't load"; // (proposed, the UI-SPEC's error frame)
const VIEW_LOT = "View lot";
const NOT_RECORDED = "Not recorded";
const NONE_RECORDED = "None recorded";
const LOT_LOADING = "Loading the lot…"; // (proposed)
const LOT_HISTORY_EMPTY = "No stock decisions or stock movements for this lot yet."; // (proposed)
const NO_LINKED_TRANSFERS = "No branch transfers for this lot."; // (proposed)
const SHELF_NOT_RECORDED = "shelf and backroom not recorded";
const AT_RISK_ONLY = "At risk only";
const WITH_DECISION = "With an open stock decision";
const BRANCH = "Branch";
const PRODUCT_HINT = "Type a product code"; // (proposed)
const APPLY = "Apply"; // (proposed) sends the typed codes; Enter does the same
const FILTERS = "Stock filters"; // (proposed) the toolbar's accessible name
const EMPTY_TITLE = "No lots match"; // (proposed, UI-SPEC "Copywriting Contract")
const EMPTY_BODY = "Change the filters, or clear At risk only."; // (proposed)
const ELIGIBILITY: Record<string, string> = {
  available: "Available",
  none_eligible: "No eligible destination",
  unavailable_unmapped: TRANSFERS_UNAVAILABLE,
};

// The head of the table: the stock columns sit under one "Simulated" chip (D-15); the cover's text carries "Estimated".
const HEAD_BEFORE = ["Product code", "Lot"];
const HEAD_STOCK = ["Expiry", "Stock vs forecast", "On record", "Latest count", "Shelf / backroom", "Reserved",
  "Available", "Incoming"];
const HEAD_AFTER = ["Next markdown", "Discrepancies", "Stock decision", "Actions"];
const NUMERIC = new Set(["On record", "Reserved", "Available", "Incoming"]);

// The page's state lives in "#/stock?branch=&product=&at_risk=1&with_case=1&page=&lot=" (in that order; empty
// values and the first page left out), so Back and a reload keep the filters and the open drawer.
const KEYS = ["branch", "product", "at_risk", "with_case", "page", "lot"] as const;
type StockQuery = Partial<Record<(typeof KEYS)[number], string>>;

function stockHash(q: StockQuery): string {
  const p = new URLSearchParams();
  for (const k of KEYS) {
    const v = q[k]?.trim();
    if (v !== undefined && v !== "" && !(k === "page" && v === "1")) p.set(k, v);
  }
  const text = p.toString();
  return text === "" ? "#/stock" : `#/stock?${text}`;
}

// The server's filters from the route: a manager's query never carries `branch` (the server scopes to the store).
function filtersOf(q: StockQuery, hq: boolean): LotsQuery {
  const page = Number(q.page);
  return {
    branch: hq ? q.branch : undefined, product: q.product, atRisk: q.at_risk === "1", withCase: q.with_case === "1",
    page: Number.isInteger(page) && page > 1 ? page : undefined,
  };
}

const asError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));

// --- the list ----------------------------------------------------------------------------------------------------

// `version` is the workspace version the shown list was read at (the stale banner compares it with the session's).
type Load = { view: LotsView | null; error: ApiError | null; version: number | null };

// GET /api/lots for these filters: on entry, on a filter or page change, when the tab becomes visible and on reload.
// A response that arrives after a newer request started is dropped; a failed refetch keeps the last list on screen.
function useLots(run: string, version: number, f: LotsQuery): Load & { reload: () => void } {
  const [state, setState] = useState<Load>({ view: null, error: null, version: null });
  const latest = useRef(0);
  const at = useRef(version);
  at.current = version;
  const key = JSON.stringify(f);
  const load = useCallback(async () => {
    const n = ++latest.current;
    const v = at.current;
    try {
      const view = await getLots(JSON.parse(key) as LotsQuery);
      if (n === latest.current) setState({ view, error: null, version: v });
    } catch (e) {
      // A refused filter (400) drops the last list: it answers another filter, so no later failure shows it again.
      const err = asError(e);
      if (n === latest.current) setState((s) => ({ ...s, view: err.status === 400 ? null : s.view, error: err }));
    }
  }, [key]);
  useEffect(() => {
    void load();
  }, [load, run]);
  useRefetchOnVisible(() => void load());
  return { ...state, reload: () => void load() };
}

// --- the cover bar (UI-SPEC "CoverBar"): recorded track, navy-700 forecast fill, hatched surplus ----------------------

const SURPLUS = "bg-[repeating-linear-gradient(45deg,var(--color-warn)_0_2px,var(--color-warn-soft)_2px_5px)]";

function CoverBar({ parts }: { parts: CoverParts }) {
  const drawn = parts.fill + parts.surplus > 0;
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {drawn && (
        <div aria-hidden="true" className="flex h-2 w-30 overflow-hidden rounded-full bg-sunken">
          <span className="h-full bg-navy-700" style={{ width: `${parts.fill * 100}%` }} />
          <span className={`h-full ${SURPLUS}`} style={{ width: `${parts.surplus * 100}%` }} />
        </div>
      )}
      <span className="flex flex-wrap items-center gap-1">
        <span className="num text-label text-ink-2">{parts.text}</span>
        {drawn && <KindChip kind="estimated" />}
      </span>
    </div>
  );
}


// Phase 7 (07-UI-SPEC surface 6; 07-22): a held lot's "Lot hold" chip, and the confirmed fact's label, readable on
// hover (the row's title) and by keyboard focus and screen readers (a focusable note under the lot).
function LotMarks({ row }: { row: LotRow & LotRowBannerKeys }) {
  const hold = row.lot_hold ?? null;
  const label = row.fact_label ?? null;
  if (hold === null && label === null) return null;
  return (
    <span className="mt-1 flex flex-wrap items-center gap-1">
      {hold !== null && <span data-testid="lot-hold-chip" title={hold.reason}><Pill tone="bad" icon="hold">{LOT_HOLD}</Pill></span>}
      {label !== null && (
        <span data-testid="fact-label" tabIndex={0} title={label} aria-label={label}
          className="block max-w-48 truncate text-label text-ink-2 focus:whitespace-normal">{label}</span>
      )}
    </span>
  );
}

function LotsTable({ rows, day, hq, onView }: {
  rows: LotRow[]; day: number; hq: boolean; onView: (lotId: string) => void;
}) {
  const { session } = useSession();
  const th = (h: string) => (
    <th key={h} scope="col" className={`border-b border-line px-4 py-2 text-label font-semibold whitespace-nowrap text-tx2 ${NUMERIC.has(h) ? "text-right" : "text-left"}`}>{h}</th>
  );
  const td = "border-b border-line px-4 py-2 align-top";
  return (
    <div className="min-w-0 overflow-x-auto rounded-2xl border border-line bg-white">
      <table data-testid="lots-table" className="w-full min-w-[1120px] border-collapse text-body">
        <thead className="bg-side">
          <tr>
            <td colSpan={HEAD_BEFORE.length} />
            <th scope="colgroup" colSpan={HEAD_STOCK.length} className="px-4 pt-2 text-left">
              <span data-testid="stock-kind"><KindChip kind="simulated" /></span>
            </th>
            <td colSpan={HEAD_AFTER.length} />
          </tr>
          <tr>{[...HEAD_BEFORE, ...HEAD_STOCK, ...HEAD_AFTER].map(th)}</tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const act = rowAction(r);
            const disc = discrepancyText(r.discrepancies);
            return (
              <tr key={r.lot_id} data-lot={r.lot_id} title={(r as LotRow & LotRowBannerKeys).fact_label ?? undefined}>
                <td className={`${td} font-mono ${act?.primary ? "shadow-[inset_4px_0_0_var(--color-navy-800)]" : ""}`}>{r.sku}</td>
                <td className={td}>
                  <span className="font-mono">{r.lot_id}</span>
                  <DesignedLabel lot={{ origin: r.origin }} session={session} />
                  {hq && <span className="block text-label text-ink-2">{r.branch}</span>}
                  <LotMarks row={r as LotRow & LotRowBannerKeys} />
                </td>
                <td className={`${td} num whitespace-nowrap`}>{expiryText(r)}</td>
                <td className={td}><CoverBar parts={coverParts(r)} /></td>
                <td className={`${td} num text-right`}>{units(r.qty)}</td>
                <td className={`${td} num whitespace-nowrap`}>{countText(r, day)}</td>
                <td className={`${td} text-ink-2`}>{NOT_RECORDED}</td>
                <td className={`${td} num text-right`}>{units(r.reserved)}</td>
                <td className={`${td} num text-right`}>{units(r.available)}</td>
                <td className={`${td} num text-right`}>{units(r.incoming)}</td>
                <td className={`${td} num whitespace-nowrap`}>{markdownText(r.next_markdown)}</td>
                <td className={td}>{disc === null ? NONE : <Pill tone="bad" icon="alert">{disc}</Pill>}</td>
                <td className={td}>
                  {r.action === "view" && r.case_key !== null
                    ? <a href={`#/cases/${encodeURIComponent(r.case_key)}`} className="font-mono text-navy-800 underline underline-offset-2">{r.case_key}</a>
                    : r.action === "open" ? <Pill tone="info" icon="clock">Needs evaluation</Pill> : NONE}
                </td>
                <td className={td}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button kind="ghost" onClick={() => onView(r.lot_id)}>{VIEW_LOT}</Button>
                    {act !== null && <LinkButton href={act.href} primary={act.primary}>{act.label}</LinkButton>}
                    <ReportFromLot lot={r} />
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// --- the toolbar (D-17): every filter is a server parameter; the typed codes go on Enter or Apply -----------------

function Toggle({ on, label, onClick }: { on: boolean; label: string; onClick: () => void }) {
  return (
    <button type="button" aria-pressed={on} onClick={onClick}
      className={`inline-flex min-h-8 items-center gap-1 rounded-md border px-2 text-body font-semibold max-md:min-h-11 ${on ? "border-navy-800 bg-navy-50 text-navy-800" : "border-line-2 bg-white text-tx2 hover:bg-side"}`}>
      {label}
    </button>
  );
}

function Toolbar({ q, hq, go }: { q: StockQuery; hq: boolean; go: (next: StockQuery) => void }) {
  const [branch, setBranch] = useState(q.branch ?? "");
  const [product, setProduct] = useState(q.product ?? "");
  useEffect(() => setBranch(q.branch ?? ""), [q.branch]);
  useEffect(() => setProduct(q.product ?? ""), [q.product]);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    go({ ...q, branch: hq ? branch : undefined, product, page: undefined, lot: undefined });
  };
  const flip = (k: "at_risk" | "with_case") => go({ ...q, [k]: q[k] === "1" ? undefined : "1", page: undefined, lot: undefined });
  const input = "h-8 w-40 rounded-md border border-line-2 bg-white px-2 font-mono text-body max-md:h-11";
  return (
    <form role="search" aria-label={FILTERS} onSubmit={submit} className="flex min-w-0 flex-wrap items-end gap-2">
      {hq && (
        <label className="flex flex-col gap-1 text-label text-tx2">
          {BRANCH}
          <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder={STORE_PLACEHOLDER}
            autoComplete="off" className={input} />
        </label>
      )}
      <label className="flex flex-col gap-1 text-label text-tx2">
        {PRODUCT_CODE}
        <input value={product} onChange={(e) => setProduct(e.target.value)} placeholder={PRODUCT_HINT}
          autoComplete="off" className={input} />
      </label>
      <Button type="submit" kind="plain">{APPLY}</Button>
      <Toggle on={q.at_risk === "1"} label={AT_RISK_ONLY} onClick={() => flip("at_risk")} />
      <Toggle on={q.with_case === "1"} label={WITH_DECISION} onClick={() => flip("with_case")} />
    </form>
  );
}

// --- the lot drawer (D-56): read-only facts, eligibility, holds and review flags, transfers, history ----------------

function Fact({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div data-testid={testId} className="min-w-0">
      <dt className="text-label text-tx3">{label}</dt>
      <dd className="num m-0 text-body break-words text-tx">{children}</dd>
    </div>
  );
}

function LotFacts({ lot }: { lot: LotDetail }) {
  const { session } = useSession();
  const [filter, setFilter] = useState<HistoryFilter>("All");
  const marks = [...lot.holds.map((h) => `${h.kind} hold · ${h.reason}`),
    ...lot.review_flags.map((f) => `Review flag${f.key === null ? "" : ` on ${f.key}`} · ${f.reason}`)];
  const eligibility = ELIGIBILITY[lot.eligibility.status] ?? lot.eligibility.status;
  return (
    <>
      <div><KindChip kind="simulated" /></div>
      <dl className="m-0 flex max-w-md flex-col gap-2">
        <Fact label="Stock quantity">{units(lot.qty)} units on record</Fact>
        <Fact label="Expiry">
          {lot.expiry_date === null ? NONE : `${markDate(lot.expiry_date)} · ${lot.expiry_source.toLowerCase()}`}
        </Fact>
        <Fact label="Location">{`${lot.branch} · ${SHELF_NOT_RECORDED}`}</Fact>
        <Fact label="Transfer eligibility">
          {eligibility}
          <EligibilityLine e={lot.eligibility} />
        </Fact>
        <Fact label="Holds and review flags" testId="lot-holds">
          {marks.length === 0 ? NONE_RECORDED : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0">{marks.map((m, i) => <li key={i} className="break-words">{m}</li>)}</ul>
          )}
        </Fact>
        <Fact label="Linked branch transfers">
          {lot.transfers.length === 0 ? NO_LINKED_TRANSFERS : (
            <ul className="m-0 flex list-none flex-col gap-1 p-0">
              {lot.transfers.map((t) => (
                <li key={t.action_id} className="break-words">
                  <span className="font-mono">{t.action_id}</span>
                  {` · ${t.source} → ${t.dest} · ${units(t.qty)} units`}
                  {t.run_code !== null && ` · ${t.run_code}${t.run_date === null ? "" : ` · ${shortDate(t.run_date)}`} (planned)`}
                  {t.key !== null && <> · <a href={`#/cases/${encodeURIComponent(t.key)}`} className="font-mono text-navy-800 underline underline-offset-2">{t.key}</a></>}
                </li>
              ))}
            </ul>
          )}
        </Fact>
      </dl>
      <section data-testid="lot-history" aria-labelledby="lot-history-title" className="mt-2 flex min-w-0 flex-col gap-2">
        <h3 id="lot-history-title" className="m-0 text-body font-semibold text-ink">History</h3>
        <HistoryList entries={lotHistory(lot.history, session.time_zone)} filter={filter} onFilter={setFilter}
          emptyText={LOT_HISTORY_EMPTY} />
      </section>
    </>
  );
}

// GET /api/lots/{id} on open, when the address names another lot, on Try again and when the tab becomes visible again.
// Only the latest request may set the drawer, so an earlier lot's late answer never replaces the lot the address names;
// a failed refetch keeps the last details on screen with Try again above them (D-12).
function LotDrawer({ lotId, onClose }: { lotId: string; onClose: () => void }) {
  const [lot, setLot] = useState<LotDetail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const latest = useRef(0);
  const load = useCallback(async () => {
    const n = ++latest.current;
    setError(null);
    try {
      const answer = await getLot(lotId);
      if (n === latest.current) setLot(answer);
    } catch (e) {
      if (n === latest.current) setError(asError(e));
    }
  }, [lotId]);
  useEffect(() => {
    setLot(null);
    void load();
  }, [load]);
  useRefetchOnVisible(() => void load());
  const failed = error !== null
    ? <ErrorLine error={error} action={retryable(error.code) ? { label: TRY_AGAIN, onClick: () => void load() } : undefined} />
    : null;
  return (
    <Drawer title={`Lot ${lotId}`} onClose={onClose}>
      {lot === null
        ? failed ?? <Loading text={LOT_LOADING} />
        : <>{failed}<LotFacts lot={lot} /></>}
    </Drawer>
  );
}

// --- the page ------------------------------------------------------------------------------------------------------

function StockPage({ route }: { route: Route }) {
  const { session } = useSession();
  const hq = session.role === "hq";
  const q = route.query as StockQuery;
  const { view, error, version, reload } = useLots(session.run_id, session.version, filtersOf(q, hq));
  const go = (next: StockQuery) => { window.location.hash = stockHash(next); };
  const lotId = q.lot;
  // A refused filter (an unknown product code: 400) is shown word for word next to the toolbar, alone: the last list
  // belongs to another filter, so neither it nor its pager stays under the error (QA F3). Any other failed refetch
  // keeps the list under the stale banner.
  const refused = error !== null && error.status === 400;
  return (
    <div className="min-w-0">
      <PageHead title={TITLE} />
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        <Toolbar q={q} hq={hq} go={go} />
        {refused && <ErrorLine error={error} />}
        {view !== null && error !== null && !refused && <StaleBanner kind="refresh_failed" onAct={reload} />}
        {view !== null && error === null && version !== null && version !== session.version
          && <StaleBanner kind="version_changed" onAct={reload} />}
        {refused ? null : view === null
          ? error !== null ? <LoadError title={LOAD_FAILED} error={error} onRetry={reload} />
            : <Loading text={LOADING} />
          : view.items.length === 0
            ? (
              <section className="rounded-2xl border border-line bg-white p-4">
                <h2 className="m-0 text-title font-semibold text-ink">{EMPTY_TITLE}</h2>
                <p className="m-0 text-body text-ink-2">{EMPTY_BODY}</p>
              </section>
            )
            : <LotsTable rows={view.items} day={session.day} hq={hq} onView={(id) => go({ ...q, lot: id })} />}
      </div>
      {view !== null && !refused && (
        <Pager page={view.page} pageSize={view.page_size} total={view.total}
          onPage={(n) => go({ ...q, page: String(n), lot: undefined })} />
      )}
      {lotId !== undefined && lotId !== "" && <LotDrawer lotId={lotId} onClose={() => go({ ...q, lot: undefined })} />}
    </div>
  );
}

export const page: PageDef = {
  path: "/stock",
  title: TITLE,
  nav: { label: TITLE, icon: "box", section: "Stock", order: 6 },
  Component: StockPage,
};

