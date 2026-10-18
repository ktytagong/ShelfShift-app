// Branch transfers (06.1-UI-SPEC "Branch transfers"; 06.1-CONTEXT D-17, D-33, D-52, D-55, D-66): the store's
// outgoing or incoming branch transfers, with their quantities, the scheduled truck run, the decision status and the
// movement status kept apart, and the next action as a link into the stock decision's Execute step. The page records
// nothing: Dispatch and Receipt happen only in Execute, through the same records and checks (D-52). It reads
// GET /api/transfers only; the server scopes (a manager's own store), filters, orders and pages (D-17), and the
// client never re-sorts or re-filters a page. A row opens a drawer with the transport details panel shared with
// Execute (decision/transport.tsx). Strings marked (proposed) are Claude's wording, one constant each.
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import type { ApiError } from "../api.ts";
import { STORE_PLACEHOLDER } from "../copy.ts";
import { getTransfers, type MovementState, type TransferItem, type TransfersView } from "../contract.ts";
import { DecisionChip, MovementChip, TransportPanel } from "../decision/transport.tsx";
import { dateLabel, hhmm } from "../format.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { PageHead, useSession } from "../shell.tsx";
import { Drawer, KindChip, LabelledSelect, LoadError, Loading, Pager, QtyBar, Segmented, StaleBanner } from "../ui.tsx";
import { ALL_BRANCHES, NEXT_ACTION_LABEL, NO_TRANSPORT, ROLE_LABEL, TERMS, type NextActionKey } from "../vocab.ts";

export const TITLE = "Branch transfers";
export const LOADING = "Loading branch transfers…"; // (proposed, UI-SPEC "Interaction states")
export const DIDNT_LOAD = "Branch transfers didn't load"; // (proposed, UI-SPEC "Error (page load)")
export const EMPTY_BODY = "Transfers appear here when a stock decision that moves stock is approved."; // (proposed)
export const emptyHeading = (store: string) => `No branch transfers for ${store}`;
export const DIRECTION_LABEL = "Direction"; // (proposed) the accessible name of Outgoing | Incoming | All
export const BRANCH_LABEL = "Branch";
export const STATUS_LABEL = "Movement status";
export const OPEN_ROW = "Show transport details"; // (proposed) the row button's accessible description
export const COLUMNS = [
  TERMS.BRANCH_TRANSFER, "Product · lot", "From → To", "Units", TERMS.SCHEDULED_TRUCK_RUN, "Decision", "Movement", "Next",
] as const;

type Direction = "outgoing" | "incoming" | "all";
const DIRECTIONS: [Direction, string][] = [["outgoing", "Outgoing"], ["incoming", "Incoming"], ["all", "All"]];
// The Movement status filter's options, in the UI-SPEC's order ("" is All).
export const STATUS_OPTIONS: [MovementState | "", string][] = [
  ["", "All"], ["planned", "Planned"], ["dispatched", "Dispatched"], ["partially_received", "Partially received"],
  ["received", "Received"], ["discrepancy", "Discrepancy"],
];

type View = TransfersView & { scope?: { branch: string | null; direction: Direction } };
type Who = TransferItem["next_action"]["who"][number];

// "S017 manager" or "HQ planner": who acts next on a row the signed-in account does not act on.
const whoName = (w: Who) => (w.role === "hq" ? ROLE_LABEL.hq : `${w.branch ?? ""} manager`);

// The stock decision's Execute step, where every next action of this page is recorded.
export const executeHref = (key: string) => `#/cases/${encodeURIComponent(key)}?step=execute`;

function TransfersPage() {
  const { session } = useSession();
  const hq = session.role === "hq";
  const [direction, setDirection] = useState<Direction>(hq ? "all" : "outgoing");
  const [branch, setBranch] = useState("");
  const [branchText, setBranchText] = useState("");
  const [state, setState] = useState<MovementState | "">("");
  const [page, setPage] = useState(1);
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null);
  const [open, setOpen] = useState<TransferItem | null>(null);
  const seq = useRef(0);

  const load = useCallback(() => {
    const mine = ++seq.current;
    const version = session.version;
    getTransfers({
      direction, branch: hq ? branch : undefined, state: state === "" ? undefined : state, page,
    }).then(
      (v) => { if (mine === seq.current) { setView(v as View); setError(null); setLoadedVersion(version); } },
      (e: ApiError) => { if (mine === seq.current) setError(e); },
    );
  }, [direction, branch, state, page, hq, session.version]);
  useEffect(() => load(), [load]);
  useRefetchOnVisible(load);

  const pick = (f: () => void) => { f(); setPage(1); setOpen(null); };
  const submitBranch = (e?: FormEvent) => {
    e?.preventDefault();
    const code = branchText.trim();
    if (code !== branch) pick(() => setBranch(code));
  };

  const toolbar = (
    <div className="flex flex-wrap items-end gap-2">
      <Segmented label={DIRECTION_LABEL} value={direction} options={DIRECTIONS.filter(([d]) => hq || d !== "all")}
        onPick={(d) => pick(() => setDirection(d))} />
      {hq && (
        <form onSubmit={submitBranch} className="flex items-center">
          <label className="flex items-center gap-1 text-label text-tx2">
            {BRANCH_LABEL}
            <input type="search" value={branchText} placeholder={STORE_PLACEHOLDER} autoComplete="off"
              onChange={(e) => setBranchText(e.target.value)} onBlur={() => submitBranch()}
              className="h-8 w-40 rounded-md border border-line-2 bg-white px-2 text-body text-tx placeholder:text-tx3 max-md:h-11" />
          </label>
        </form>
      )}
      <LabelledSelect label={STATUS_LABEL} value={state} options={STATUS_OPTIONS}
        onPick={(v) => pick(() => setState(v as MovementState | ""))} />
    </div>
  );

  let body;
  if (view === null) {
    body = error === null ? <Loading text={LOADING} /> : <LoadError title={DIDNT_LOAD} error={error} onRetry={load} />;
  } else {
    const store = view.scope?.branch ?? (hq ? (branch || ALL_BRANCHES) : (session.branch ?? ALL_BRANCHES));
    body = (
      <>
        {error !== null && <StaleBanner kind="refresh_failed" onAct={load} />}
        {error === null && loadedVersion !== null && loadedVersion !== session.version && (
          <StaleBanner kind="version_changed" onAct={load} />
        )}
        {view.items.length === 0
          ? (
            <section data-testid="transfers-empty" className="rounded-[10px] border border-line bg-white p-4">
              <h2 className="m-0 text-title font-semibold text-tx">{emptyHeading(store)}</h2>
              <p className="m-0 mt-1 text-body text-tx2">{EMPTY_BODY}</p>
            </section>
          )
          : <TransfersTable items={view.items} onOpen={setOpen} />}
        <Pager page={view.page} pageSize={view.page_size} total={view.total} onPage={(p) => { setPage(p); setOpen(null); }} />
      </>
    );
  }

  return (
    <>
      <PageHead title={TITLE} />
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        {toolbar}
        {body}
      </div>
      {open !== null && (
        <Drawer title={`${TERMS.BRANCH_TRANSFER} · ${open.key}`} onClose={() => setOpen(null)}>
          <TransportPanel caseId={open.case_id} />
          <a href={executeHref(open.key)} className="text-body font-semibold text-navy-800 underline underline-offset-2">
            {TERMS.VIEW_STOCK_DECISION}
          </a>
        </Drawer>
      )}
    </>
  );
}

// The table, in its own scroll box (min width 960px), rows in the server's order.
function TransfersTable({ items, onOpen }: { items: TransferItem[]; onOpen: (t: TransferItem) => void }) {
  const { session } = useSession();
  return (
    <div data-testid="transfers-scroll" className="min-w-0 overflow-x-auto rounded-[10px] border border-line bg-white">
      <table className="w-full min-w-[960px] border-collapse text-body">
        <thead className="bg-side">
          <tr>
            {COLUMNS.map((h) => (
              <th key={h} scope="col" className="border-b border-line px-4 py-2 text-left text-label font-semibold text-tx3">
                {h === TERMS.SCHEDULED_TRUCK_RUN
                  ? <span className="inline-flex flex-wrap items-center gap-1">{h} <KindChip kind="simulated" /></span>
                  : h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((t) => {
            const key = t.next_action.key as NextActionKey | "view" | null;
            const mine = t.next_action.who.some((w) => w.role === session.role
              && (w.role === "hq" || w.branch === session.branch));
            const acts = key !== null && key !== "view" && mine;
            const label = key !== null && key !== "view" ? NEXT_ACTION_LABEL[key] ?? null : null;
            return (
              <tr key={t.action_id} data-testid={`transfer-row-${t.action_id}`} onClick={() => onOpen(t)}
                className="cursor-pointer align-top hover:bg-side">
                <td className={`border-b border-line px-4 py-2 ${acts ? "border-l-4 border-l-navy-800" : ""}`}>
                  <button type="button" aria-label={`${t.action_id}, ${OPEN_ROW}`}
                    onClick={(e) => { e.stopPropagation(); onOpen(t); }}
                    className="font-mono text-label break-all text-navy-800 underline underline-offset-2">
                    {t.action_id}
                  </button>
                </td>
                <td className="border-b border-line px-4 py-2">
                  <span className="font-mono">{t.sku}</span> · <span className="font-mono text-label">{t.lot_id}</span>
                </td>
                <td className="border-b border-line px-4 py-2 whitespace-nowrap">{t.source} → {t.dest}</td>
                <td data-testid="transfer-units" className="border-b border-line px-4 py-2">
                  <QtyBar committed={t.committed_qty} dispatched={t.dispatched_qty} received={t.received_qty}
                    damaged={t.damaged_qty} />
                </td>
                <td className="border-b border-line px-4 py-2">
                  {t.run_code === null
                    ? <span className="text-tx2">{NO_TRANSPORT}</span>
                    : (
                      <span className="flex flex-col">
                        <span className="num">
                          <span className="font-mono">{t.run_code}</span>
                          {t.departs_min !== null && ` · departs ${hhmm(t.departs_min)}`}
                          {t.arrives_min !== null && ` · arrives ${hhmm(t.arrives_min)}`}
                        </span>
                        <span className="text-label text-tx2">
                          {["Planned", t.run_day === null ? "" : dateLabel(session.calendar, t.run_day)]
                            .filter((x) => x !== "").join(" · ")}
                        </span>
                      </span>
                    )}
                </td>
                <td className="border-b border-line px-4 py-2"><DecisionChip state={t.decision_state} /></td>
                <td className="border-b border-line px-4 py-2">
                  <MovementChip state={t.movement_state} committed={t.committed_qty} dispatched={t.dispatched_qty} />
                </td>
                <td className="border-b border-line px-4 py-2" onClick={(e) => e.stopPropagation()}>
                  {acts && label !== null
                    ? <a href={executeHref(t.key)} className="font-semibold text-navy-800 underline underline-offset-2">{label}</a>
                    : (
                      <span className="flex flex-col gap-0.5">
                        {label !== null && (
                          <span className="text-label text-tx2">
                            {label} · {t.next_action.who.map(whoName).join(" · ")}
                          </span>
                        )}
                        <a href={executeHref(t.key)} className="text-navy-800 underline underline-offset-2">
                          {TERMS.VIEW_STOCK_DECISION}
                        </a>
                      </span>
                    )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export const page: PageDef = {
  path: "/transfers",
  title: TITLE,
  nav: { label: TITLE, icon: "swap", section: "Movement", order: 5 },
  Component: TransfersPage,
};
