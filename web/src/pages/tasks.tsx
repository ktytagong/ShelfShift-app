// Tasks (07-19; 07-CONTEXT D-36 item 3, D-04, D-13; 07-UI-SPEC "Tasks"; 06.1-UI-SPEC "Tasks", D-50, D-52, D-58): one
// page with six tiles, "Checks to answer · Answers to confirm · Lot holds · Advisory tasks · Dispatches · Receipts",
// each a button that filters the one list below (pressed again, it clears), with Open | Closed. The list is GET
// /api/tasks' `list`: the server filters it by view and tile, orders it (urgent and blocked first) and pages it; the
// client neither sorts nor filters (07-SCREENS-MAP G5). The tiles count open items only; a 0 tile is muted and still
// a button. Each row shows its owner, its deadline or status and its one next action:
//   - a stock check (requests.tsx): answer, Can't check, confirm, with the real deadline drawn to scale;
//   - a lot hold the store owns: "Inspect and release" through its confirm step (with the inspection note the server
//     records), its stock decisions and, once released, "Hold released by {store} manager · {t}";
//   - an advisory task (a confirmed display issue): "Mark done", "Closes this task for {store}. A lot hold, if any,
//     stays.", then "Done by {store} manager · {t}" under Closed;
//   - a dispatch or a receipt to record: the stock decision's Execute step records it (D-52); a dispatch the server
//     would refuse for a hold or a review flag says "Dispatch blocked" with its reason and action (D-36 item 1).
// Freshness (05-CONTEXT): the page loads on route entry, when the tab becomes visible again, after the visitor's own
// write, when the workspace version changes and once when a visible deadline reaches 0; nothing polls. A response that
// arrives after a newer load started is dropped. "Loading tasks…" on the first load; a refetch that fails keeps the
// list under the stale banner. Typed answers and open rows live outside the list, so a refetch never wipes them.
// HQ reads one line: tasks go to store managers, transport reports are under Business events.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, NETWORK, get, post, type CaseState } from "../api.ts";
import { getExecution, type TransferItem } from "../contract.ts";
import { hhmm, units } from "../format.ts";
import { useAction, useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { blockedCaseStates, dayDate, RequestTask, useDrafts } from "../requests.tsx";
import { PageHead, useSession } from "../shell.tsx";
import {
  ADD_NOTE, ADVISORY_CHIP, ADVISORY_LINE, BUSINESS_EVENTS, INSPECT_RELEASE, KEEP_HOLD, LOT_HOLD, MARK_DONE, MIXED_HOLD_HELP,
  NO_CLOSED, NO_OPEN_BODY, NONE_IN_TILE, RELEASE_HOLD, RELEASE_NOTE, TASKS_HQ_P7, TILE_LABEL, TILE_ORDER, TILES_LABEL,
  VIEW_LABEL, VIEW_OPTIONS, advisoryTitle, blockedLine, clip, holdTitle, markDoneNote, noOpenTasks, ownerLine,
  releaseSentence, tasksPath, CLOSED_HINT, type AdvisoryView, type HoldView, type TaskItem, type TasksView, type TaskTile, type TaskViewName,
} from "../taskview.ts";
import {
  Button, ConfirmStep, ErrorLine, Icon, LinkButton, LoadError, Loading, Pager, Pill, Segmented, StaleBanner, type IconName,
} from "../ui.tsx";
import {
  DISPATCHED, NEXT_ACTION_LABEL, PLANNED, recordedLine, scenarioDateLine, shortShipment, TERMS, UNSCHEDULED,
} from "../vocab.ts";

// --- the words (06.1-UI-SPEC "Tasks"; "(proposed)" marks Claude's or the UI-SPEC's wording) --------------------------
export const TASKS_TITLE = "Tasks";
export const TASKS_LOADING = "Loading tasks…"; // proposed
export const TASKS_LOAD_FAILED = "Tasks didn't load"; // proposed
export const dispatchWhat = (t: TransferItem) => `${units(t.committed_qty)} units of ${t.sku} lot ${t.lot_id} → ${t.dest}`;
export const receiptWhat = (t: TransferItem) =>
  `${units(t.dispatched_qty ?? t.committed_qty)} units of ${t.sku} from ${t.source}`;
export function departureLine(t: Pick<TransferItem, "run_code" | "departs_min">, dateIso: string | null): string {
  if (t.run_code === null || t.departs_min === null) return UNSCHEDULED;
  return `${t.run_code} · departs ${hhmm(t.departs_min)} (planned)${dateIso === null ? "" : ` · ${scenarioDateLine(dateIso)}`}`;
}
export const dispatchedLine = (recorded: string) => (recorded === "" ? DISPATCHED : `${DISPATCHED} · ${recorded}`);

const TILE_ICON: Readonly<Record<TaskTile, IconName>> = {
  to_answer: "inbox", to_confirm: "check", lot_holds: "hold", advisory: "info", dispatches: "truck", receipts: "box",
};

type Data = { view: TasksView; receivedAt: number; caseStates: Record<string, CaseState>; dispatchedAt: Record<string, string> };

const asError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
const VIEW_LINK = "text-body font-semibold text-navy-800 underline underline-offset-2";
const executeHref = (caseId: string) => `#/cases/${encodeURIComponent(caseId)}?step=execute`;
const caseHref = (caseId: string) => `#/cases/${encodeURIComponent(caseId)}`;
const ROW = "min-w-0 rounded-2xl border border-line bg-white p-4 shadow-sm";

// When each dispatched work order left: its DISPATCH movement's recorded time in the stock decision's execution
// record, read once per work order; a failed read leaves the time out, never guesses it.
async function dispatchTimes(items: TransferItem[], known: Record<string, string>): Promise<Record<string, string>> {
  const cases = [...new Set(items.filter((t) => known[t.action_id] === undefined).map((t) => t.case_id))];
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

// --- the six tiles ---------------------------------------------------------------------------------------------------

function Tiles({ tiles, pressed, onPress }: { tiles: Record<TaskTile, number>; pressed: TaskTile | null; onPress: (t: TaskTile) => void }) {
  return (
    <div role="group" aria-label={TILES_LABEL} className="grid min-w-0 grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-6">
      {TILE_ORDER.map((t) => {
        const n = tiles[t] ?? 0;
        const on = pressed === t;
        return (
          <button key={t} type="button" aria-pressed={on} onClick={() => onPress(t)} data-testid={`tile-${t}`}
            className={`flex min-w-0 flex-col items-start gap-1 rounded-2xl border bg-white p-4 text-left shadow-card hover:shadow-float ${on ? "border-navy-800 ring-2 ring-navy-100" : "border-line"}`}>
            <span className="flex items-center gap-1 text-label font-semibold break-words text-tx2"><Icon name={TILE_ICON[t]} />{TILE_LABEL[t]}</span>
            <span data-testid="tile-count" className={`num text-display font-semibold ${n === 0 ? "text-tx3" : "text-tx"}`}>{n}</span>
          </button>
        );
      })}
    </div>
  );
}

// --- a lot hold the store owns ----------------------------------------------------------------------------------------

function HoldTask({ item, expanded, onToggle, after, onAnnounce }: {
  item: TaskItem; expanded: boolean; onToggle: (o: boolean) => void; after: () => Promise<void>; onAnnounce: (l: string[]) => void;
}) {
  const h = item.hold!;
  const [note, setNote] = useState("");
  const release = useAction(async (body: { note: string }, key: string) => {
    const out = await post<HoldView>(`/api/holds/${encodeURIComponent(h.hold_id)}/release`, body, key);
    onAnnounce([out.resolved_label ?? RELEASE_HOLD, CLOSED_HINT]);
    await after();
    return out;
  });
  const title = holdTitle(h.lot_id);
  const region = `task-hold-${h.hold_id}`;
  const mixedHold = h.kind === "MIXED_DATES";
  return (
    <article aria-labelledby={`${region}-title`} data-hold-id={h.hold_id} data-acts={item.open || undefined}
      className={`${ROW} ${item.open ? "border-l-4 border-l-navy-800" : ""}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
        <h3 id={`${region}-title`} className="m-0 min-w-0 flex-1 basis-56 text-title font-semibold">
          <button type="button" aria-expanded={expanded} aria-controls={region} title={title} onClick={() => onToggle(!expanded)}
            className="inline-flex max-w-full items-center gap-2 text-left break-words"><Icon name="hold" /><span>{clip(title)}</span></button>
        </h3>
        <span className="flex flex-wrap items-center gap-1">
          <Pill tone={item.open ? "bad" : "good"} icon={item.open ? "hold" : "check"}>{item.open ? LOT_HOLD : h.resolved_label ?? LOT_HOLD}</Pill>
        </span>
        {item.open && !expanded && <Button kind="primary" className="min-h-11 md:min-h-8" onClick={() => onToggle(true)}>{INSPECT_RELEASE}</Button>}
      </div>
      <p className="m-0 mt-1 text-label text-tx2 break-words">{h.reason} · {ownerLine(h.owner_branch)}</p>
      {expanded && (
        <div id={region} className="mt-4 flex flex-col gap-2 border-t border-line pt-4">
          <p className="num m-0 text-label text-tx2">{h.placed_label}</p>
          {h.resolved_label !== null && <p className="num m-0 rounded-md bg-good-soft px-2 py-1 text-body text-good">{h.resolved_label}</p>}
          {h.note !== null && h.note !== "" && <p className="m-0 text-label text-tx2 break-words">Note: {h.note}</p>}
          {h.cases.length > 0 && (
            <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
              {h.cases.map((c) => (
                <li key={c.case_id} className="flex items-center gap-1">
                  <a href={caseHref(c.case_id)} className={VIEW_LINK}>{c.key}</a>
                  <Pill tone="gray">{c.state}</Pill>
                </li>
              ))}
            </ul>
          )}
          {item.open && mixedHold && <p className="m-0 max-w-md text-label text-tx3">{MIXED_HOLD_HELP} <a href="#/events" className={VIEW_LINK}>{BUSINESS_EVENTS}</a></p>}
          {item.open && (
            <ConfirmStep trigger={INSPECT_RELEASE} triggerKind={mixedHold ? "ghost" : "plain"} sentence={releaseSentence(h.lot_id)}
              confirm={RELEASE_HOLD} confirmKind="primary" dismiss={KEEP_HOLD} busy={release.busy}
              disabled={note.trim() === ""} disabledReason={ADD_NOTE} onConfirm={() => void release.run({ note: note.trim() })}>
              <span className="flex flex-col gap-1">
                <label htmlFor={`${region}-note`} className="text-label text-tx3">{RELEASE_NOTE}</label>
                <textarea id={`${region}-note`} rows={2} value={note} onChange={(e) => setNote(e.target.value)}
                  className="rounded-md border border-line-2 bg-white px-2 py-1 text-body" />
              </span>
            </ConfirmStep>
          )}
          <ErrorLine error={release.error} />
        </div>
      )}
    </article>
  );
}

// --- an advisory task (a confirmed display issue) ---------------------------------------------------------------------

function AdvisoryTask({ item, after, onAnnounce }: { item: TaskItem; after: () => Promise<void>; onAnnounce: (l: string[]) => void }) {
  const t = item.advisory!;
  const store = item.owner_branch ?? t.branch;
  const done = useAction(async (_: Record<string, never>, key: string) => {
    const out = await post<AdvisoryView>(`/api/signals/${encodeURIComponent(t.signal_id)}/done`, {}, key);
    onAnnounce([out.task?.done_label ?? MARK_DONE, CLOSED_HINT]);
    await after();
    return out;
  });
  const title = advisoryTitle(item.sku);
  return (
    <article aria-label={title} data-signal-id={t.signal_id} data-acts={item.open || undefined}
      className={`${ROW} ${item.open ? "border-l-4 border-l-navy-800" : ""}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
        <h3 className="m-0 inline-flex min-w-0 flex-1 basis-56 items-center gap-2 text-title font-semibold break-words" title={title}>
          <Icon name="info" />{clip(title)}
        </h3>
        <Pill tone={item.open ? "warn" : "gray"} icon={item.open ? "info" : "check"}>{item.open ? ADVISORY_CHIP : t.task?.done_label ?? ADVISORY_CHIP}</Pill>
        {item.open && <Button kind="primary" busy={done.busy} className="min-h-11 md:min-h-8" onClick={() => void done.run({})}>{MARK_DONE}</Button>}
      </div>
      <div className="mt-2 flex flex-col gap-1">
        {t.text !== "" && <p className="m-0 text-body break-words">{t.text}</p>}
        <p className="m-0 text-label text-tx2">{ADVISORY_LINE} · {ownerLine(store)}</p>
        {t.simulated_label != null && <span><Pill tone="plan" icon="info">{t.simulated_label}</Pill></span>}
        {t.file != null && <a href={`/api/signals/${encodeURIComponent(t.signal_id)}/file`} target="_blank" rel="noreferrer" className={VIEW_LINK}>View file</a>}
        <a href={`#/events/${encodeURIComponent(t.signal_id)}`} className={`${VIEW_LINK} self-start`}>View report</a>
        {item.open && <p className="m-0 text-label text-tx3">{markDoneNote(store)}</p>}
        <ErrorLine error={done.error} />
      </div>
    </article>
  );
}

// --- a dispatch or a receipt to record (the Execute step records it, D-52) ---------------------------------------------

function TransferTask({ item, dispatchedAt }: { item: TaskItem; dispatchedAt: Record<string, string> }) {
  const { session } = useSession();
  const t = item.transfer!;
  const acts = item.open && t.next_action.who.some((w) => w.role === session.role && w.branch === session.branch);
  const outgoing = item.kind === "dispatch";
  const at = dispatchedAt[t.action_id];
  const short = t.dispatched_qty !== null && t.dispatched_qty < t.committed_qty;
  const when = outgoing && t.movement_state === "planned"
    ? departureLine(t, t.run_day === null ? null : dayDate(session.calendar, t.run_day))
    : dispatchedLine(at === undefined ? "" : recordedLine(at, session.time_zone));
  return (
    <article aria-label={`${outgoing ? "Dispatch" : "Receipt"} ${t.action_id}`} data-action-id={t.action_id} data-acts={acts || undefined}
      className={`${ROW} flex flex-wrap items-center gap-x-4 gap-y-2 ${acts ? "border-l-4 border-l-navy-800" : ""}`}>
      <div className="flex min-w-0 flex-1 basis-64 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <Icon name={outgoing ? "truck" : "box"} />
          <span className="font-mono text-body">{t.action_id}</span>
          {t.movement_state === "planned" ? <Pill tone="plan" icon="clock">{PLANNED}</Pill> : <Pill tone="info" icon="truck">{DISPATCHED}</Pill>}
          {short && <Pill tone="warn" icon="alert">{shortShipment(t.dispatched_qty ?? 0, t.committed_qty)}</Pill>}
        </div>
        <p className="num m-0 text-body font-semibold break-words">{outgoing ? dispatchWhat(t) : receiptWhat(t)}</p>
        <p data-testid="task-when" className="num m-0 text-label text-tx2 break-words">{when} · {ownerLine(item.owner_branch)}</p>
        {item.blocked_reasons.map((b, i) => (
          <p key={i} data-testid="dispatch-blocked" className="m-0 rounded-md bg-bad-soft px-2 py-1 text-body text-bad break-words">
            {blockedLine(b.kind)} {b.reason}
          </p>
        ))}
      </div>
      {acts && t.next_action.key !== null
        ? <LinkButton primary href={executeHref(t.case_id)}>{NEXT_ACTION_LABEL[t.next_action.key]}</LinkButton>
        : <a href={executeHref(t.case_id)} className={VIEW_LINK}>{TERMS.VIEW_STOCK_DECISION}</a>}
    </article>
  );
}

// --- the page ---------------------------------------------------------------------------------------------------------

function Tasks() {
  const { session, refresh } = useSession();
  const hq = session.role === "hq";
  const [view, setView] = useState<TaskViewName>("open");
  const [tile, setTile] = useState<TaskTile | null>(null);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [said, setSaid] = useState<string[]>([]); // the polite line after the visitor's own write (07-UI-SPEC "Success")
  const drafts = useDrafts();
  const latest = useRef(0);
  const sent = useRef<Record<string, string>>({});
  const loadIdentity = `${session.run_id}:${session.role}:${session.branch}:${view}:${tile}:${page}`;
  const currentIdentity = useRef(loadIdentity);
  currentIdentity.current = loadIdentity;

  const load = useCallback(async () => {
    if (hq || currentIdentity.current !== loadIdentity) return;
    const n = ++latest.current;
    try {
      const v = await get<TasksView>(tasksPath(view, tile, page));
      const receivedAt = Date.now();
      const [caseStates, dispatchedAt] = await Promise.all([
        blockedCaseStates(v.list.items),
        dispatchTimes(v.list.items.flatMap((i) => (i.transfer !== null && i.transfer.movement_state !== "planned" ? [i.transfer] : [])), sent.current),
      ]);
      if (n !== latest.current || currentIdentity.current !== loadIdentity) return;
      sent.current = dispatchedAt;
      setData({ view: v, receivedAt, caseStates, dispatchedAt });
      setError(null);
    } catch (e) {
      if (n !== latest.current || currentIdentity.current !== loadIdentity) return;
      setError(asError(e));
    }
  }, [hq, view, tile, page, loadIdentity]);
  const currentLoad = useRef(load);
  currentLoad.current = load;

  useEffect(() => {
    void load();
  }, [load, session.version]);
  useRefetchOnVisible(() => void load());

  // After the visitor's own write: the page and the session (its menu count) refetch.
  // A pending read/write may finish after a filter change while its request row stays mounted.
  const after = useCallback(async () => {
    await currentLoad.current();
    await refresh().catch(() => {});
  }, [refresh]);
  // One refetch when a visible deadline reaches 0 (the server applies the deadline on read, D-05).
  const onZero = useCallback(() => void load(), [load]);

  const store = session.branch ?? "";
  const toggle = (key: string) => (open: boolean) => setExpanded((x) => ({ ...x, [key]: open }));
  const items = data?.view.list.items ?? [];

  return (
    <div className="min-w-0">
      <PageHead title={TASKS_TITLE} />
      <div className="flex min-w-0 flex-col gap-6 p-4 md:p-6">
        {hq ? (
          <p className="m-0 text-body text-tx2">{TASKS_HQ_P7} <a href="#/events" className={VIEW_LINK}>{BUSINESS_EVENTS}</a></p>
        ) : data === null ? (
          error === null ? <Loading text={TASKS_LOADING} /> : <LoadError title={TASKS_LOAD_FAILED} error={error} onRetry={() => void load()} />
        ) : (
          <>
            {error !== null && <StaleBanner kind="refresh_failed" onAct={() => void load()} />}
            {said.length > 0 && (
              <div role="status" data-testid="task-status" className="flex flex-col gap-1">
                {said.map((line, i) => <p key={i} className="num m-0 rounded-md bg-good-soft px-2 py-1 text-body text-good break-words">{line}</p>)}
              </div>
            )}
            <Tiles tiles={data.view.tiles} pressed={tile} onPress={(t) => { setTile((x) => (x === t ? null : t)); setPage(1); }} />
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Segmented label={VIEW_LABEL} value={view} options={VIEW_OPTIONS} onPick={(v) => { setView(v); setPage(1); }} />
            </div>
            {items.length === 0 ? (
              view === "closed" ? <p className="m-0 text-body text-tx2">{NO_CLOSED}</p>
                : tile !== null ? <p className="m-0 text-body text-tx2">{NONE_IN_TILE}</p>
                : (
                  <div className="py-12 text-center">
                    <h2 className="m-0 text-title font-semibold">{noOpenTasks(store)}</h2>
                    <p className="m-0 mt-2 text-body text-tx2">{NO_OPEN_BODY}</p>
                  </div>
                )
            ) : (
              <ul data-testid="task-list" className="m-0 flex list-none flex-col gap-4 p-0">
                {items.map((item) => (
                  <li key={item.key} data-task-key={item.key} className="min-w-0">
                    {item.kind === "request" ? (
                      <RequestTask item={item} caseStates={data.caseStates} expanded={expanded[item.key] ?? false}
                        onToggle={toggle(item.key)} drafts={drafts} after={after} onZero={onZero}
                        serverNow={data.view.server_now} receivedAt={data.receivedAt} onAnnounce={setSaid} />
                    ) : item.kind === "hold" ? (
                      <HoldTask item={item} expanded={expanded[item.key] ?? false} onToggle={toggle(item.key)} after={after} onAnnounce={setSaid} />
                    ) : item.kind === "advisory" ? (
                      <AdvisoryTask item={item} after={after} onAnnounce={setSaid} />
                    ) : (
                      <TransferTask item={item} dispatchedAt={data.dispatchedAt} />
                    )}
                  </li>
                ))}
              </ul>
            )}
            <Pager page={data.view.list.page} pageSize={data.view.list.page_size} total={data.view.list.total} onPage={setPage} />
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
