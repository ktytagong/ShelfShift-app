// Tasks (06.1-UI-SPEC "Tasks"; D-50: the Requests page moved into the shell; Phase 7 extends it, so each section is its
// own component): the work waiting for the signed-in store, in three sections with their counts.
//   1. "Stock checks": the count requests addressed to the store (requests.tsx).
//   2. "Dispatches to record": the store's prepared branch transfers (GET /api/transfers direction outgoing, state
//      planned), each with Record dispatch opening the stock decision's Execute step.
//   3. "Receipts to record": branch transfers dispatched to the store (direction incoming, state dispatched), each with
//      Record receipt opening the Execute step.
// Rows where the signed-in account acts (the server's next_action names it) carry the 4px navy rule (R-4). The page
// records no dispatch and no receipt: those happen only in the stock decision's Execute step, through the same records
// and checks (D-52). The server scopes, filters, orders and pages every list; the client neither sorts nor filters.
// "#/requests" redirects here (main.tsx).
//
// Freshness (05-CONTEXT): the page loads on route entry, when the tab becomes visible again, after the visitor's own
// write and when the workspace version changes; nothing polls. A response that arrives after a newer load started is
// dropped. The first load shows "Loading tasks…"; a first load that fails shows the page error in place of the
// content; a refetch that fails keeps what was loaded under the stale banner.
//
// Two clocks (D-58): a planned departure reads "{12:30} (planned)" with its scenario date; a recorded dispatch reads
// "Recorded {17 Oct 2026, 02:48} Bangkok time", read from the stock decision's execution record (the transfer list
// carries no time of its own) once per work order.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, NETWORK, type Session } from "../api.ts";
import { getExecution, getTransfers, type TransferItem, type TransfersView } from "../contract.ts";
import { hhmm, units } from "../format.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { dayDate, loadStockChecks, StockChecks, type StockCheckData } from "../requests.tsx";
import { PageHead, useSession } from "../shell.tsx";
import { LinkButton, LoadError, Loading, Pager, Pill, StaleBanner } from "../ui.tsx";
import {
  DISPATCHED, NEXT_ACTION_LABEL, PLANNED, recordedLine, scenarioDateLine, shortShipment, TERMS, UNSCHEDULED,
} from "../vocab.ts";

// --- the words (06.1-UI-SPEC "Tasks", "Interaction states"; "(proposed)" marks Claude's or the UI-SPEC's wording) ---
export const TASKS_TITLE = "Tasks";
export const STOCK_CHECKS_TITLE = "Stock checks";
export const DISPATCHES_TITLE = "Dispatches to record";
export const RECEIPTS_TITLE = "Receipts to record";
export const TASKS_LOADING = "Loading tasks…"; // proposed
export const TASKS_LOAD_FAILED = "Tasks didn't load"; // proposed
export const NONE_WAITING = "None waiting."; // proposed: an empty section on a page that has other tasks
export const noTasksTitle = (store: string) => `No tasks for ${store}`; // proposed
export const noTasksBody = (store: string) => `Stock checks, dispatches and receipts for ${store} appear here.`; // proposed
export const TASKS_HQ_LINE = "Tasks go to store managers. HQ sees each branch's work on the Decision board."; // proposed
// "110 units of P0123 lot L-0042 → S017" and "104 units of P0123 from S042" (UI-SPEC "Tasks" sections 2 and 3).
export const dispatchWhat = (t: TransferItem) => `${units(t.committed_qty)} units of ${t.sku} lot ${t.lot_id} → ${t.dest}`;
export const receiptWhat = (t: TransferItem) =>
  `${units(t.dispatched_qty ?? t.committed_qty)} units of ${t.sku} from ${t.source}`;
// "R-02 · departs 12:30 (planned) · Scenario date: Sat 17 Oct 2026" (proposed); "Unscheduled" without a run or time.
export function departureLine(t: Pick<TransferItem, "run_code" | "departs_min">, dateIso: string | null): string {
  if (t.run_code === null || t.departs_min === null) return UNSCHEDULED;
  return `${t.run_code} · departs ${hhmm(t.departs_min)} (planned)${dateIso === null ? "" : ` · ${scenarioDateLine(dateIso)}`}`;
}
// "Dispatched · Recorded 17 Oct 2026, 02:48 Bangkok time" (UI-SPEC "Tasks" section 3); "Dispatched" alone when the
// recorded time cannot be read.
export const dispatchedLine = (recorded: string) => (recorded === "" ? DISPATCHED : `${DISPATCHED} · ${recorded}`);

type Data = { checks: StockCheckData; dispatches: TransfersView; receipts: TransfersView; dispatchedAt: Record<string, string> };

const asError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
const unique = (xs: string[]) => [...new Set(xs)];

// When each dispatched work order was dispatched: its DISPATCH movement's recorded time in the stock decision's
// execution record. `known` holds the times read before (a recorded dispatch never changes). A failed read leaves the
// time out, never guesses it.
async function dispatchTimes(items: TransferItem[], known: Record<string, string>): Promise<Record<string, string>> {
  const cases = unique(items.filter((t) => known[t.action_id] === undefined).map((t) => t.case_id));
  const found = await Promise.all(cases.map(async (id) => {
    try {
      const view = await getExecution(id);
      return view.movements.flatMap((m) => (m.kind === "DISPATCH" && typeof m.recorded_at === "string"
        ? [[m.action_id, m.recorded_at] as const] : []));
    } catch {
      return [];
    }
  }));
  return { ...known, ...Object.fromEntries(found.flat()) };
}

// True when the server names the signed-in account as the one who acts next on this row.
const actsOn = (t: TransferItem, s: Pick<Session, "role" | "branch">) =>
  t.next_action.who.some((w) => w.role === s.role && w.branch === s.branch);

const VIEW_LINK = "text-body font-semibold text-navy-800 underline underline-offset-2";
const executeHref = (caseId: string) => `#/cases/${encodeURIComponent(caseId)}?step=execute`;

// One section: its title with the count beside it (the page's "what's happening" at a glance), then its rows.
export function TaskSection({ id, title, count, children }: { id: string; title: string; count: number; children: ReactNode }) {
  return (
    <section aria-labelledby={id} data-testid={`tasks-${id}`} className="flex min-w-0 flex-col gap-2">
      <div className="flex items-center gap-2">
        <h2 id={id} className="m-0 text-title font-semibold">{title}</h2>
        <span data-testid="task-count"
          className="num inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-sunken px-2 text-label font-semibold text-tx2">
          {count}
        </span>
      </div>
      {count === 0 ? <p className="m-0 text-body text-tx2">{NONE_WAITING}</p> : children}
    </section>
  );
}

// One transfer task: what moves where, when (planned, or when it was dispatched), its movement chip, and the one
// primary that opens the stock decision's Execute step where the record is made (D-52). The label is the server's
// next action; a row the account does not act on gets the plain View stock decision link instead.
function TransferRow({ t, chips, when }: { t: TransferItem; chips: ReactNode; when: string }) {
  const { session } = useSession();
  const acts = actsOn(t, session);
  const what = t.movement_state === "planned" ? dispatchWhat(t) : receiptWhat(t);
  return (
    <li data-action-id={t.action_id} data-acts={acts || undefined}
      className={`flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-line bg-white p-4 shadow-sm ${acts ? "border-l-4 border-l-navy-800" : ""}`}>
      <div className="flex min-w-0 flex-1 basis-64 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-body">{t.action_id}</span>
          {chips}
        </div>
        <p className="num m-0 text-body font-semibold break-words">{what}</p>
        <p data-testid="task-when" className="num m-0 text-label text-tx2 break-words">{when}</p>
      </div>
      {acts && t.next_action.key !== null
        ? <LinkButton primary href={executeHref(t.case_id)}>{NEXT_ACTION_LABEL[t.next_action.key]}</LinkButton>
        : <a href={executeHref(t.case_id)} className={VIEW_LINK}>{TERMS.VIEW_STOCK_DECISION}</a>}
    </li>
  );
}

// Section 2: the store's prepared transfers waiting for their dispatch record.
export function DispatchTasks({ view, onPage }: { view: TransfersView; onPage: (page: number) => void }) {
  const { session } = useSession();
  return (
    <>
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {view.items.map((t) => (
          <TransferRow key={t.action_id} t={t} chips={<Pill tone="plan" icon="clock">{PLANNED}</Pill>}
            when={departureLine(t, t.run_day === null ? null : dayDate(session.calendar, t.run_day))} />
        ))}
      </ul>
      <Pager page={view.page} pageSize={view.page_size} total={view.total} onPage={onPage} />
    </>
  );
}

// Section 3: transfers dispatched to the store, waiting for their receipt record.
export function ReceiptTasks({ view, dispatchedAt, onPage }: {
  view: TransfersView; dispatchedAt: Record<string, string>; onPage: (page: number) => void;
}) {
  const { session } = useSession();
  return (
    <>
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {view.items.map((t) => {
          const at = dispatchedAt[t.action_id];
          const short = t.dispatched_qty !== null && t.dispatched_qty < t.committed_qty;
          return (
            <TransferRow key={t.action_id} t={t}
              chips={(
                <>
                  <Pill tone="info" icon="truck">{DISPATCHED}</Pill>
                  {short && <Pill tone="warn" icon="alert">{shortShipment(t.dispatched_qty ?? 0, t.committed_qty)}</Pill>}
                </>
              )}
              when={dispatchedLine(at === undefined ? "" : recordedLine(at, session.time_zone))} />
          );
        })}
      </ul>
      <Pager page={view.page} pageSize={view.page_size} total={view.total} onPage={onPage} />
    </>
  );
}

function Tasks() {
  const { session, refresh } = useSession();
  const hq = session.role === "hq";
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [dispatchPage, setDispatchPage] = useState(1);
  const [receiptPage, setReceiptPage] = useState(1);
  const latest = useRef(0);
  const assigned = useRef<Record<string, string>>({}); // request_id → when it was assigned (read once)
  const sent = useRef<Record<string, string>>({}); // action_id → when it was dispatched (read once)

  const load = useCallback(async () => {
    if (hq) return;
    const n = ++latest.current;
    try {
      const [checks, dispatches, receipts] = await Promise.all([
        loadStockChecks(assigned.current),
        getTransfers({ direction: "outgoing", state: "planned", page: dispatchPage }),
        getTransfers({ direction: "incoming", state: "dispatched", page: receiptPage }),
      ]);
      const dispatchedAt = await dispatchTimes(receipts.items, sent.current);
      if (n !== latest.current) return;
      assigned.current = checks.assignedAt;
      sent.current = dispatchedAt;
      setData({ checks, dispatches, receipts, dispatchedAt });
      setError(null);
    } catch (e) {
      if (n !== latest.current) return;
      setError(asError(e));
    }
  }, [hq, dispatchPage, receiptPage]);

  useEffect(() => {
    void load();
  }, [load, session.version]);
  useRefetchOnVisible(() => void load());

  // After the visitor's own answer, confirm or recount: the page and the session (its counts) refetch.
  const after = useCallback(async () => {
    await load();
    await refresh().catch(() => {});
  }, [load, refresh]);

  const store = session.branch ?? "";
  const counts = data === null ? null
    : { checks: data.checks.list.length, dispatches: data.dispatches.total, receipts: data.receipts.total };

  return (
    <div className="min-w-0">
      <PageHead title={TASKS_TITLE} />
      <div className="flex min-w-0 flex-col gap-6 p-4 md:p-6">
        {hq ? (
          <p className="m-0 text-body text-tx2">{TASKS_HQ_LINE}</p>
        ) : data === null || counts === null ? (
          error === null ? <Loading text={TASKS_LOADING} /> : <LoadError title={TASKS_LOAD_FAILED} error={error} onRetry={() => void load()} />
        ) : (
          <>
            {error !== null && <StaleBanner kind="refresh_failed" onAct={() => void load()} />}
            {counts.checks + counts.dispatches + counts.receipts === 0 ? (
              <div className="py-12 text-center">
                <h2 className="m-0 text-title font-semibold">{noTasksTitle(store)}</h2>
                <p className="m-0 mt-2 text-body text-tx2">{noTasksBody(store)}</p>
              </div>
            ) : (
              <>
                <TaskSection id="stock-checks" title={STOCK_CHECKS_TITLE} count={counts.checks}>
                  <StockChecks data={data.checks} after={after} />
                </TaskSection>
                <TaskSection id="dispatches" title={DISPATCHES_TITLE} count={counts.dispatches}>
                  <DispatchTasks view={data.dispatches} onPage={setDispatchPage} />
                </TaskSection>
                <TaskSection id="receipts" title={RECEIPTS_TITLE} count={counts.receipts}>
                  <ReceiptTasks view={data.receipts} dispatchedAt={data.dispatchedAt} onPage={setReceiptPage} />
                </TaskSection>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export const page: PageDef = {
  path: "/tasks",
  title: TASKS_TITLE,
  nav: { label: TASKS_TITLE, icon: "check", section: "", order: 4, countKey: "tasks" },
  Component: () => <Tasks />,
};
