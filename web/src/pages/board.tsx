// The Decision board (06.1-UI-SPEC "Decision board"; sketch 005 B; D-11, D-17, D-23, D-24, D-25, D-54): four stage
// columns (Decide, Authorise, Do, Check and close) whose cards sit where the server's `stage` and `step` keys put them,
// each card with its nine-segment step bar, owner, what it affects, the expected value (simulated), exception badges
// and the Next line; a card opens the drawer, where the source manager or HQ can Take ownership. The board polls every
// 2 s while the tab is visible (useBoardPoll), and an answer older than a later load is dropped (latestLoad). Dragging
// a card, or its Move control, requests the stock decision's next business action (D-69, boardmove.ts); it never writes
// a stage, and a card changes column only when the server's board puts it there (C-21). The server scopes, filters,
// orders and pages the list (D-17); this page never sorts or filters a page, and it never maps a state to a step (D-24).
// Strings marked "(proposed)" are Claude's wording from the UI-SPEC, one named constant each.
import { useCallback, useEffect, useId, useRef, useState, type DragEvent, type KeyboardEvent, type ReactNode } from "react";
import { ApiError, NETWORK, get, type CaseDetail, type Session } from "../api.ts";
import { segmentStates, type SegmentState } from "../bars.ts";
import {
  affectsText, badgeOverflow, blockedToggleText, cardBadges, deadlineChip, evaluatedLine, lotRoute, nextLine,
  showingText, stageGroups, type BadgeChip,
} from "../boardview.ts";
import { boardQuery61, takeOwnership, type Board61, type Card61, type ReportFigure, type Session61 } from "../contract.ts";
import {
  CLOSED_HEADING, DROP_TO_CLOSE, MOVE, MOVE_KEY, MOVE_PICK, MOVE_SAVING, MOVE_TARGETS, MOVE_TO, moveChoice, targetName,
  type MoveTarget,
} from "../boardmove.ts";
import { BoardMoveDrawer, useBoardMoves, type BoardMovement } from "../boardmove.tsx";
import { NEEDS_EVALUATION, PRODUCT_CODE, REPORT_FIGURES, RISK_OPTIONS, STORE_PLACEHOLDER, factTitle, flagBody, retryable } from "../copy.ts";
import { productLabel, type BoardRisk } from "../derive.ts";
import { signed } from "../format.ts";
import { useAction, useBoardPoll } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { PageHead, useSession, type Route } from "../shell.tsx";
import { STAGES, STEP_KEYS, stepName, type StageKey } from "../steps.ts";
import {
  Button, Drawer, ErrorLine, Icon, Kv, LabelledSelect, LoadError, Loading, Pager, Pill, Segmented, StaleBanner, StepBar,
  TRY_AGAIN,
} from "../ui.tsx";
import {
  CLEAR_FILTERS, IN_STORE, NEEDS_ACTION, NOT_TAKEN, OWNED_BY, PLANNED_NOTE, TAKE_OWNERSHIP_HELP, TERMS, YOURS,
} from "../vocab.ts";

// --- copy (proposed unless noted) -------------------------------------------------------------------------------------
export const BOARD_LOADING = "Loading the Decision board…"; // (proposed, UI-SPEC "Interaction states")
export const BOARD_LOAD_ERROR = "The Decision board didn't load"; // (proposed, UI-SPEC "Interaction states")
export const NO_DECISIONS_HERE = "No stock decisions here"; // (UI-SPEC "Decision board")
export const EMPTY_HEADING = "No open stock decisions"; // (proposed)
export const EMPTY_BODY = (where: string) => // (proposed)
  `Nothing in ${where} needs a decision. New stock decisions open when the screen finds a lot at risk.`;
export const ANY_BRANCH = "any branch"; // (proposed) HQ's word in EMPTY_BODY
export const TAKE_OWNERSHIP = "Take ownership";
export { CLOSED_HEADING }; // defined in boardmove.ts, where the moves name the Closed group too
export const EXPECTED_SIMULATED = "simulated";
export const EXPECTED_DRAWER = "(engine estimate, simulated)";
export const VS_KEEP = "vs keep";
export const NEXT = "Next";
export const BOARD_NOTE = "Drag a card or press Move to do its next step. Expected values are simulated engine estimates, "
  + "not observed results."; // approved by the user 2026-10-18 (D-69; the three-second scan)
export const NO_MATCH = "No stock decisions match these filters.";
export const LAYOUT = "Layout";
export const BOARD_VIEW = "Board";
export const TABLE_VIEW = "Table";
export const FILTER_LABEL = { owner: "Owner", product: "Product", deadline: "Deadline", branch: "Branch", risk: "Risk" } as const;
export const OWNER_OPTIONS: readonly [string, string][] = [["", "Anyone"], ["me", "Me"]];
export const DEADLINE_OPTIONS: readonly [string, string][] =
  [["", "Any"], ["scenario_date", "Due on the scenario date"], ["none", "No deadline"]];
export const TABLE_HEAD = ["Stock decision", "Product · lot", "Branch", "Step", "Owner", "Affects", "Expected (simulated)",
  "Next action", "Deadline", "Blocker"] as const;
export const LABELS = {
  why: "Why", owner: "Owner", affects: "Affects", expected: "Expected", whyOpened: "Why it opened",
  deadline: "Deadline", nextAction: "Next action",
} as const;

// --- the page ---------------------------------------------------------------------------------------------------------

type Me = { account: string; role: Session["role"]; branch: string | null };
const meOf = (s: Session): Me => ({
  account: (s as Session & Partial<Session61>).account ?? (s.role === "hq" || s.branch === null ? "HQ" : s.branch),
  role: s.role, branch: s.branch,
});

// The decision's link: a flagged item by its key (the page evaluates it on open), a case by its id (05.2-15 rows).
const decisionHref = (c: Pick<Card61, "case_id" | "key" | "needs_evaluation">) =>
  `#/cases/${encodeURIComponent(c.needs_evaluation === true ? c.key : c.case_id)}`;

// --- the route's query: the layout, the filters and the page live in "#/board?view=table&owner=me&…" -----------------

type Layout = "board" | "table";
type Filters = { owner: "" | "me"; product: string; deadline: "" | "scenario_date" | "none"; blocked: boolean;
  branch: string; risk: BoardRisk };
const FILTER_KEYS = ["owner", "product", "deadline", "blocked", "branch", "risk"] as const;

function filtersOf(q: Record<string, string>, hq: boolean): Filters {
  const risk = q.risk === "waste" || q.risk === "stock" ? q.risk : "all";
  return {
    owner: q.owner === "me" ? "me" : "",
    product: q.product ?? "",
    deadline: q.deadline === "scenario_date" || q.deadline === "none" ? q.deadline : "",
    blocked: q.blocked === "1",
    branch: hq ? (q.branch ?? "") : "",
    risk: hq ? risk : "all",
  };
}
const filtering = (f: Filters) =>
  f.owner !== "" || f.product !== "" || f.deadline !== "" || f.blocked || f.branch !== "" || f.risk !== "all";

// A new hash for the board with `patch` applied to the query; empty values leave the query, and a filter change goes
// back to page 1. The hash router (main.tsx) renders the same page again, so its state and the drawer stay.
function boardHash(q: Record<string, string>, patch: Record<string, string>): string {
  const next = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...q, ...patch })) if (v !== "" && !(k === "page" && v === "1")) next.set(k, v);
  const text = next.toString();
  return `#/board${text === "" ? "" : `?${text}`}`;
}

function BoardPage({ route }: { route: Route }) {
  const { session } = useSession();
  const me = meOf(session);
  const hq = session.role === "hq";
  const report = route.query.report;
  const reportLabel = report !== undefined && Object.hasOwn(REPORT_FIGURES, report)
    ? REPORT_FIGURES[report as ReportFigure] : null;
  const layout: Layout = route.query.view === "table" ? "table" : "board";
  const f = filtersOf(route.query, hq);
  const pageNo = Math.max(1, Number.parseInt(route.query.page ?? "1", 10) || 1);
  const go = (patch: Record<string, string>) => {
    window.location.hash = boardHash(route.query, patch);
  };
  const setFilter = (patch: Record<string, string>) => go({ ...patch, page: "1" });
  const clearFilters = () => go(Object.fromEntries([...FILTER_KEYS.map((k) => [k, ""]), ["report", ""], ["page", "1"]]));

  const path = boardQuery61(session, {
    store: f.branch, risk: f.risk, owner: f.owner, product: f.product, deadline: f.deadline, blocked: f.blocked,
    page: pageNo, report: report as ReportFigure | undefined,
  });
  const wanted = useRef(path);
  const latestLoad = useRef(0);
  wanted.current = path;
  const [board, setBoard] = useState<Board61 | null>(null);
  const [failed, setFailed] = useState<ApiError | null>(null);
  const [selected, setSelected] = useState<string | null>(null); // the open drawer's card key

  // A failed poll keeps the last answer on screen with the stale banner; an answer for an older query is dropped, and so
  // is an answer to a load that a later load overtook (a poll sent before a write must never replace the board read
  // after it; 261017-wzs Codex finding 8). A page past the last (the list shrank) moves to the last page.
  const load = useCallback(async () => {
    const seq = ++latestLoad.current;
    try {
      const b = await get<Board61>(path);
      if (wanted.current !== path || seq !== latestLoad.current) return;
      setBoard(b);
      setFailed(null);
      const size = b.page_size ?? 0;
      const total = b.total ?? 0;
      if (b.cases.length === 0 && total > 0 && size > 0 && (b.page ?? 1) > 1) {
        window.location.replace(boardHash(route.query, { page: String(Math.ceil(total / size)) }));
      }
    } catch (e) {
      if (wanted.current !== path || seq !== latestLoad.current) return;
      setFailed(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    }
  }, [path, route.query]);
  useBoardPoll(load, 2000);
  const moves = useBoardMoves(session, load);

  const cards = board?.cases ?? null;
  const open = cards?.find((c) => c.key === selected) ?? null;
  const [snapshot, setSnapshot] = useState<Card61 | null>(null);
  useEffect(() => {
    if (open !== null) setSnapshot(open);
  }, [open]);
  const drawerCard = open ?? (selected !== null && snapshot?.key === selected ? snapshot : null);
  // A refused filter (400: an unknown product or store code) is the server's message next to the toolbar; any other
  // failure after a load is the stale banner over the cards that were loaded before.
  const refused = failed !== null && failed.status === 400 ? failed : null;
  const props = { session, me, selected, onOpen: setSelected };

  return (
    <div className="flex min-w-0 flex-col">
      <PageHead title={TERMS.DECISION_BOARD} />
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        <Toolbar layout={layout} f={f} hq={hq} board={board} onLayout={(l) => go({ view: l === "table" ? "table" : "" })}
          onFilter={setFilter} onClear={clearFilters} onPage={(p) => go({ page: String(p) })} />
        {reportLabel !== null && <p className="m-0 text-body text-ink-2">Reports: {reportLabel} · <a href="#/reports">Back to Reports</a></p>}
        <ErrorLine error={refused} />
        <ErrorLine error={moves.error} action={moves.action} />
        {moves.reasons.length > 0 && (
          <ul data-testid="move-conflict-reasons" className="m-0 list-disc pl-6 text-body text-ink">
            {moves.reasons.map((x, i) => <li key={i} className="break-words">{x}</li>)}
          </ul>
        )}
        <p ref={moves.status} tabIndex={-1} role="status" aria-live="polite" data-testid="move-status"
          className="m-0 empty:hidden text-body text-ink-2">
          {moves.movement.busy ? MOVE_SAVING : moves.message}
        </p>
        <p className="m-0 flex items-start gap-1 text-label text-ink-2"><Icon name="lock" size={12} className="mt-0.5" />{BOARD_NOTE}</p>
        {board === null && failed === null && <Loading text={BOARD_LOADING} />}
        {board === null && failed !== null && refused === null && (
          <LoadError title={BOARD_LOAD_ERROR} error={failed} onRetry={() => void load()} />
        )}
        {board !== null && failed !== null && refused === null && (
          <StaleBanner kind="refresh_failed" onAct={() => void load()} />
        )}
        {board !== null && board.cases.length === 0 && (filtering(f) || report !== undefined
          ? <Empty action={<Button kind="ghost" onClick={clearFilters}>{CLEAR_FILTERS}</Button>}>{NO_MATCH}</Empty>
          : <Empty heading={EMPTY_HEADING}>{EMPTY_BODY(hq ? ANY_BRANCH : me.account)}</Empty>)}
        {board !== null && board.cases.length > 0 && (layout === "table"
          ? <DecisionTable cards={board.cases} {...props} />
          : <Columns board={board} {...props} movement={moves.movement} />)}
        {board !== null && board.cases.length > 0 && (
          <p className="m-0 flex items-center gap-1 text-label text-ink-2"><Icon name="clock" size={12} />{PLANNED_NOTE}</p>
        )}
      </div>
      {moves.guided !== null ? <BoardMoveDrawer key={`${moves.guided.card.key}:${moves.guided.target}`} move={moves.guided}
        live={cards?.find((c) => c.key === moves.guided?.card.key) ?? null} poll={board} onClose={moves.close}
        onChanged={load} />
        : drawerCard !== null && (
        <CardDrawer key={drawerCard.key} card={drawerCard} session={session} me={me}
          onClose={() => setSelected(null)} onChanged={() => void load()} />
      )}
    </div>
  );
}

// --- the toolbar (UI-SPEC "Toolbar"): the layout switch, the filters with visible labels, the server's counts --------

const CONTROL = "h-8 rounded-lg border border-rule-2 bg-surface px-2 text-body text-ink max-md:h-11";

// A text filter (a product or store code) applies when it is committed (Enter or leaving the box), so a half-typed
// code sends nothing; the server checks it (400 with its message for an unknown code).
function CodeInput({ label, value, onCommit, placeholder }: {
  label: string; value: string; onCommit: (v: string) => void; placeholder: string;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const commit = () => {
    const v = text.trim();
    if (v !== value) onCommit(v);
  };
  const id = useId();
  return (
    <span className="flex items-center gap-1">
      <label htmlFor={id} className="text-label text-ink-2">{label}</label>
      <input id={id} type="search" value={text} placeholder={placeholder} autoComplete="off" spellCheck={false}
        onChange={(e) => setText(e.target.value)} onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } }}
        className={`${CONTROL} w-32 placeholder:text-ink-3`} />
    </span>
  );
}

function Toolbar({ layout, f, hq, board, onLayout, onFilter, onClear, onPage }: {
  layout: Layout; f: Filters; hq: boolean; board: Board61 | null; onLayout: (l: Layout) => void;
  onFilter: (patch: Record<string, string>) => void; onClear: () => void; onPage: (p: number) => void;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <Segmented label={LAYOUT} value={layout} onPick={onLayout}
        options={[["board", BOARD_VIEW, "board"], ["table", TABLE_VIEW, "list"]]} />
      <LabelledSelect label={FILTER_LABEL.owner} value={f.owner} options={OWNER_OPTIONS} onPick={(v) => onFilter({ owner: v })} />
      <CodeInput label={FILTER_LABEL.product} value={f.product} placeholder={PRODUCT_CODE}
        onCommit={(v) => onFilter({ product: v })} />
      <LabelledSelect label={FILTER_LABEL.deadline} value={f.deadline} options={DEADLINE_OPTIONS}
        onPick={(v) => onFilter({ deadline: v })} />
      <button type="button" aria-pressed={f.blocked} onClick={() => onFilter({ blocked: f.blocked ? "" : "1" })}
        className={`inline-flex h-8 items-center gap-1 rounded-lg border px-2 text-label font-semibold max-md:h-11 ${f.blocked ? "border-navy-800 bg-navy-50 text-ink" : "border-rule-2 bg-surface text-ink-2"}`}>
        <Icon name="lock" size={12} />{blockedToggleText(board?.blocked_total ?? 0)}
      </button>
      {hq && (
        <>
          <CodeInput label={FILTER_LABEL.branch} value={f.branch} placeholder={STORE_PLACEHOLDER}
            onCommit={(v) => onFilter({ branch: v })} />
          <LabelledSelect label={FILTER_LABEL.risk} value={f.risk} options={RISK_OPTIONS}
            onPick={(v) => onFilter({ risk: v === "all" ? "" : v })} />
        </>
      )}
      {filtering(f) && <Button kind="ghost" onClick={onClear}>{CLEAR_FILTERS}</Button>}
      {board !== null && (
        <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1">
          {board.evaluated !== undefined && board.at_risk !== undefined && (
            <span className="num text-label text-ink-2">{evaluatedLine(board.evaluated, board.at_risk)}</span>
          )}
          <span className="num text-label text-ink-2">{showingText(board.cases.length, board.total ?? board.cases.length)}</span>
          <Pager page={board.page ?? 1} pageSize={board.page_size ?? 0} total={board.total ?? 0} onPage={onPage} />
        </div>
      )}
    </div>
  );
}

// --- the table (UI-SPEC "Table"): the same records, server order and pages, in its own scroll box --------------------

function DecisionTable({ cards, session, me, selected, onOpen }: CardProps & { cards: Card61[] }) {
  const th = "border-b border-rule px-4 py-2 text-left text-label font-semibold text-ink-2 whitespace-nowrap";
  const td = "border-b border-rule px-4 py-2 align-top";
  return (
    <div className="min-w-0 overflow-x-auto rounded-2xl border border-rule bg-surface">
      <table className="w-full min-w-[1080px] border-collapse text-body">
        <thead className="bg-navy-50">
          <tr>{TABLE_HEAD.map((h) => <th key={h} scope="col" className={th}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {cards.map((c) => {
            const closed = c.step === "ARCHIVE";
            const flag = c.needs_evaluation === true;
            const next = nextLine(c);
            const deadline = deadlineChip(c, session.day, session.calendar);
            return (
              <tr key={c.key} data-testid={`decision-row-${c.key}`} onClick={() => onOpen(c.key)}
                className={`cursor-pointer hover:bg-navy-50 ${selected === c.key ? "bg-navy-50" : ""} ${closed ? "text-ink-2" : "text-ink"}`}>
                <td className={td}>
                  <button type="button" onClick={(e) => { e.stopPropagation(); onOpen(c.key); }}
                    className="font-mono text-body text-navy-800 underline underline-offset-2">{c.key}</button>
                </td>
                <td className={td}>
                  <span className="block">{productLabel(c.sku, c.product)}</span>
                  <span className="block text-label text-ink-2">Lot {c.lot_id}</span>
                </td>
                <td className={`${td} whitespace-nowrap`}>{c.dest === null ? `${c.source} · ${IN_STORE}` : `${c.source} → ${c.dest}`}</td>
                <td className={td}>
                  {flag ? <Pill tone="info" icon="clock">{NEEDS_EVALUATION}</Pill>
                    : <div className="w-28"><StepBar current={c.step_number} optionsDone={c.rec_version !== null} label={stepName(c.step)} /></div>}
                </td>
                <td className={td}>{flag ? "" : <OwnerValue card={c} me={me} />}</td>
                <td className={td}>{flag ? "" : affectsText(c)}</td>
                <td className={`${td} num whitespace-nowrap`}>{flag ? "" : <ExpectedValue card={c} session={session} note={EXPECTED_SIMULATED} />}</td>
                <td className={td}>
                  {next !== null && <><span className="block font-semibold">{next.action}</span><span className="block text-label text-ink-2">{next.who}</span></>}
                </td>
                <td className={`${td} whitespace-nowrap`}>{flag || closed ? "" : deadline.text}</td>
                <td className={td}><Badges chips={cardBadges(c, session.currency)} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Empty({ heading, action, children }: { heading?: string; action?: ReactNode; children: ReactNode }) {
  return (
    <div className="px-4 py-12 text-center">
      {heading && <h2 className="mb-2 text-title font-semibold text-ink">{heading}</h2>}
      <p className="m-0 text-body text-ink-2">{children}</p>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

// --- the four columns -------------------------------------------------------------------------------------------------

// "1 · Trigger · new", "2 · Context", "4 · Compare": the step's number in the nine and its name (sketch 005 B).
function decideLabel(step: string): string {
  const n = STEP_KEYS.indexOf(step as (typeof STEP_KEYS)[number]) + 1;
  const name = stepName(step);
  return `${n > 0 ? `${n} · ` : ""}${name}${step === "TRIGGER" ? " · new" : ""}`;
}

type CardProps = { session: Session; me: Me; selected: string | null; onOpen: (key: string) => void; movement?: BoardMovement };

function Columns({ board, ...props }: CardProps & { board: Board61 }) {
  const g = stageGroups(board.cases);
  const counts = board.stage_counts;
  const listOf = (key: StageKey) => (key === "decide" ? [] : g[key]);
  return (
    <section aria-label={TERMS.DECISION_BOARD} className="min-w-0 overflow-x-auto pb-2">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:gap-2">
        {STAGES.map((s) => {
          const total = s.key === "check" ? counts.check + counts.closed : counts[s.key];
          const empty = s.key === "decide"
            ? g.decide.every((d) => d.cards.length === 0)
            : listOf(s.key).length === 0 && (s.key !== "check" || g.closed.length === 0);
          return (
            <Column key={s.key} name={s.name} hint={s.hint} count={total} target={s.key} movement={props.movement} session={props.session}>
              {empty && <p className="m-0 p-4 text-center text-label text-ink-2">{NO_DECISIONS_HERE}</p>}
              {s.key === "decide" && !empty && g.decide.map((d) => (
                <SubGroup key={d.step} label={decideLabel(d.step)} count={d.cards.length}>
                  {d.cards.map((c) => <AnyCard key={c.key} card={c} {...props} />)}
                </SubGroup>
              ))}
              {s.key !== "decide" && listOf(s.key).map((c) => <AnyCard key={c.key} card={c} {...props} />)}
              {s.key === "check" && (g.closed.length > 0 || props.movement?.dragging != null) && (
                <ClosedZone movement={props.movement} session={props.session} count={counts.closed}>
                  {g.closed.map((c) => <ClosedCard key={c.key} card={c} {...props} />)}
                </ClosedZone>
              )}
            </Column>
          );
        })}
      </div>
    </section>
  );
}

function useDropZone(target: MoveTarget, movement: BoardMovement | undefined, session: Session) {
  const [over, setOver] = useState(false);
  const card = movement?.dragging ?? null;
  const choice = card === null ? null : moveChoice(card, target, session);
  const allowed = choice?.kind === "direct" || choice?.kind === "guided";
  return {
    "data-drop-target": target,
    "data-drop-state": card === null ? undefined : over ? allowed ? "over" : "blocked" : allowed ? "available" : undefined,
    onDragOver: (e: DragEvent<HTMLElement>) => {
      if (card === null || movement?.busy) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "move";
      setOver(true);
    },
    onDragLeave: (e: DragEvent<HTMLElement>) => {
      if (!(e.relatedTarget instanceof Node) || !e.currentTarget.contains(e.relatedTarget)) setOver(false);
    },
    onDrop: (e: DragEvent<HTMLElement>) => {
      if (card === null) return;
      e.preventDefault();
      e.stopPropagation();
      setOver(false);
      movement?.onMove(card, target);
    },
  };
}

const DROP_STYLE = "data-[drop-state=available]:outline-1 data-[drop-state=available]:outline-dashed data-[drop-state=available]:outline-navy-700 data-[drop-state=over]:outline-2 data-[drop-state=over]:outline-navy-800 data-[drop-state=over]:bg-navy-50 data-[drop-state=blocked]:outline-2 data-[drop-state=blocked]:outline-gold-600";

function Column({ name, hint, count, target, movement, session, children }: {
  name: string; hint: string; count: number; target: StageKey; movement?: BoardMovement; session: Session; children: ReactNode;
}) {
  const id = useId();
  const drop = useDropZone(target, movement, session);
  return (
    <section {...drop} aria-labelledby={id} className={`flex min-h-48 min-w-0 flex-col gap-2 rounded-xl bg-sunken p-2 md:max-h-[calc(100dvh-280px)] md:min-w-[272px] md:flex-[1_0_272px] ${DROP_STYLE}`}>
      <div className="px-1">
        <h2 id={id} className="m-0 text-body font-semibold text-ink">{name}</h2>
        <p className="num m-0 text-label text-ink-2"><span data-testid="stage-count">{count}</span> · {hint}</p>
      </div>
      <div className="flex min-h-0 flex-col gap-2 md:overflow-y-auto">{children}</div>
    </section>
  );
}

function ClosedZone({ movement, session, count, children }: {
  movement?: BoardMovement; session: Session; count: number; children: ReactNode;
}) {
  const drop = useDropZone("closed", movement, session);
  return <div {...drop} className={`min-h-20 rounded-lg p-1 ${DROP_STYLE}`}>
    <SubGroup label={CLOSED_HEADING} count={count}>{children}</SubGroup>
    {movement?.dragging != null
      && <p className="m-0 p-2 text-label text-ink-2">{DROP_TO_CLOSE}</p>}
  </div>;
}

function SubGroup({ label, count, children }: { label: string; count: number; children: ReactNode }) {
  const id = useId();
  return (
    <div role="group" aria-labelledby={id} className="flex flex-col gap-2">
      <h3 id={id} className="m-0 px-1 text-label font-semibold tracking-[.06em] text-ink-2 uppercase">
        {label}{" "}<span className="num">{count}</span>
      </h3>
      {children}
    </div>
  );
}

function AnyCard({ card, ...props }: CardProps & { card: Card61 }) {
  return <div className="flex min-w-0 flex-col gap-1">
    {card.needs_evaluation === true ? <FlagCard card={card} {...props} /> : <DecisionCard card={card} {...props} />}
    {props.movement !== undefined && <MoveControl card={card} movement={props.movement} />}
  </div>;
}

// Every draggable card's keyboard and touch way to move (D-69): the select only chooses a column, and the Move button
// requests the move. A select that wrote on change would send a stock decision for approval while the arrow keys
// browse its options (WCAG 3.2.2). After a save, focus returns to this select in the card's new column (boardmove.tsx).
function MoveControl({ card, movement }: { card: Card61; movement: BoardMovement }) {
  const [target, setTarget] = useState<MoveTarget | "">("");
  return (
    <div className="flex min-w-0 flex-wrap items-center justify-end gap-1 px-1">
      <select data-move-for={card.key} aria-label={MOVE_TO(card.key)} value={target}
        onChange={(e) => setTarget(e.target.value as MoveTarget | "")}
        className="min-h-8 min-w-0 max-w-full cursor-pointer rounded-md border border-rule bg-surface px-2 text-label text-ink-2 max-md:min-h-11">
        <option value="">{MOVE_PICK}</option>
        {MOVE_TARGETS.filter((t) => t !== card.stage).map((t) => <option key={t} value={t}>{targetName(t)}</option>)}
      </select>
      <Button data-move-for={card.key} aria-label={MOVE_KEY(card.key)} disabled={target === "" || movement.busy}
        onClick={() => { if (target !== "") movement.onMove(card, target); }} className="max-md:min-h-11">
        {MOVE}
      </Button>
    </div>
  );
}

function draggable(card: Card61, movement?: BoardMovement) {
  if (movement === undefined) return {};
  return {
    draggable: !movement.busy,
    onDragStart: (e: DragEvent<HTMLDivElement>) => {
      if (movement.busy) { e.preventDefault(); return; }
      e.dataTransfer.setData("application/x-shelfshift-decision", card.key);
      e.dataTransfer.effectAllowed = "move";
      movement.onDrag(card);
    },
    onDragEnd: () => movement.onDrag(null),
  };
}

// Enter or Space on a focused card opens it, as a click does.
function opener(key: string, onOpen: (k: string) => void) {
  return {
    role: "button", tabIndex: 0, onClick: () => onOpen(key),
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onOpen(key);
      }
    },
  } as const;
}

const CARD = "flex min-w-0 cursor-pointer flex-col gap-2 rounded-xl border border-rule bg-surface p-4 text-left shadow-card hover:border-rule-2 hover:shadow-raise";
const SELECTED = "outline-2 outline-navy-800";

function FirstStage({ card }: { card: Card61 }) {
  if (card.first_stage === "needs_evaluation") return <Pill tone="info" icon="clock">{NEEDS_EVALUATION}</Pill>;
  if (card.first_stage === "needs_action") return <Pill tone="warn" icon="alert">{NEEDS_ACTION}</Pill>;
  return null;
}

export function OwnerValue({ card, me }: { card: Card61; me: Me }) {
  if (card.owner === null) return <Pill tone="gray" icon="user">{NOT_TAKEN}</Pill>;
  if (card.owner.account === me.account) {
    return <span className="inline-flex flex-wrap items-center gap-1">{card.owner.label}<Pill tone="info" icon="user">{YOURS}</Pill></span>;
  }
  return <>{card.owner.label}</>;
}

export function Badges({ chips }: { chips: BadgeChip[] }) {
  const { shown, more, moreLabel } = badgeOverflow(chips.map((c) => c.text));
  if (chips.length === 0) return null;
  return (
    <div className="flex min-w-0 flex-wrap gap-1 [&>span]:h-auto [&>span]:min-h-5 [&>span]:max-w-full [&>span]:whitespace-normal">
      {chips.slice(0, shown.length).map((c) => <Pill key={c.kind + c.text} tone={c.tone} icon={c.icon ?? undefined}>{c.text}</Pill>)}
      {more > 0 && <span role="img" aria-label={`+${more}: ${moreLabel}`} title={moreLabel}><Pill tone="gray">+{more}</Pill></span>}
    </div>
  );
}

function ExpectedValue({ card, session, note }: { card: Card61; session: Session; note: string }) {
  if (card.est_benefit === null) return <>—</>;
  return <>{signed(card.est_benefit, session.currency, true)} {VS_KEEP} <span className="text-label text-ink-2">{note}</span></>;
}

function DecisionCard({ card, session, me, selected, onOpen, movement }: CardProps & { card: Card61 }) {
  const deadline = deadlineChip(card, session.day, session.calendar);
  const next = nextLine(card);
  const label = productLabel(card.sku, card.product);
  const name = [card.key, label, stepName(card.step), next === null ? null : `next: ${next.action}`]
    .filter((x) => x !== null).join(", ");
  const rows: [ReactNode, ReactNode][] = [];
  if (card.stage === "decide" && card.reason !== null) {
    rows.push([LABELS.why, <span data-testid="card-why" className="block truncate" title={card.reason}>{card.reason}</span>]);
  }
  rows.push([LABELS.owner, <OwnerValue card={card} me={me} />]);
  rows.push([LABELS.affects, affectsText(card)]);
  rows.push([LABELS.expected, <ExpectedValue card={card} session={session} note={EXPECTED_SIMULATED} />]);
  return (
    <div {...opener(card.key, onOpen)} {...draggable(card, movement)} data-testid={`decision-card-${card.key}`} aria-label={name}
      className={`${CARD} ${movement !== undefined ? "cursor-grab active:cursor-grabbing" : ""} ${movement?.dragging?.key === card.key ? "opacity-50" : ""} ${selected === card.key ? SELECTED : ""}`}>
      <div className="flex flex-wrap items-center gap-1">
        <span className="font-mono text-label text-ink-3">{card.key}</span>
        <FirstStage card={card} />
        {card.zone === "chilled" && <Pill tone="chill" icon="snow">Chilled</Pill>}
        <span className="ml-auto"><Pill tone={deadline.tone} icon="clock">{deadline.text}</Pill></span>
      </div>
      <div className="min-w-0">
        <p className="m-0 line-clamp-2 text-body font-semibold break-words text-ink">{label}</p>
        <p className="m-0 text-label break-words text-ink-2">{lotRoute(card)}</p>
      </div>
      <StepBar current={card.step_number} optionsDone={card.rec_version !== null} label={stepName(card.step)} />
      <Kv items={rows} stacked />
      <Badges chips={cardBadges(card, session.currency)} />
      {next !== null && (
        <div className="min-w-0 border-t border-dashed border-rule pt-2">
          <p className="m-0 text-label text-ink-3">{NEXT}</p>
          <p className="m-0 truncate text-body font-semibold text-ink" title={next.action}>{next.action}</p>
          <p className="m-0 truncate text-label text-ink-2" title={next.who}>{next.who}</p>
        </div>
      )}
    </div>
  );
}

// A flagged item the engine has not evaluated (05.2-21's flag card; D-54): the key, the store, the product code, the
// Needs evaluation chip and the screen's figures (estimated). No plan, amount, owner or deadline.
export const ESTIMATED = "estimated";
function FlagCard({ card, session, selected, onOpen, movement }: CardProps & { card: Card61 }) {
  const figures = flagBody(card, session.calendar).flatMap((r) => (r.kind === "kv" ? [[r.label, r.value] as [string, string]] : []));
  const label = productLabel(card.sku, card.product);
  return (
    <div {...opener(card.key, onOpen)} {...draggable(card, movement)} data-testid={`decision-card-${card.key}`}
      aria-label={[card.key, label, NEEDS_EVALUATION].join(", ")}
      className={`${CARD} border-dashed ${selected === card.key ? SELECTED : ""}`}>
      <div className="flex flex-wrap items-center gap-1">
        <span className="font-mono text-label text-ink-3">{card.key}</span>
        <Pill tone="info" icon="clock">{NEEDS_EVALUATION}</Pill>
      </div>
      <div className="min-w-0">
        <p className="m-0 line-clamp-2 text-body font-semibold break-words text-ink">{label}</p>
        <p className="m-0 text-label break-words text-ink-2">{card.source}</p>
      </div>
      <Kv items={figures} stacked />
      <p className="m-0 text-label text-ink-3">{ESTIMATED}</p>
      <Badges chips={cardBadges(card, session.currency)} />
    </div>
  );
}

// A closed card (UI-SPEC "Closed card"): transparent, the key, the closing chip, product · branch, the closing line
// (the server's reason, word for word, two lines at most).
export const CLOSED_LABEL = { closed: "Closed", rejected: "Rejected", cancelled: "Cancelled" } as const;
function closedLabel(card: Card61): string {
  if (card.state === "REJECTED") return CLOSED_LABEL.rejected;
  if (card.closed_as === "CANCELLED") return CLOSED_LABEL.cancelled;
  return CLOSED_LABEL.closed;
}
function ClosedCard({ card, selected, onOpen }: CardProps & { card: Card61 }) {
  const label = productLabel(card.sku, card.product);
  return (
    <div {...opener(card.key, onOpen)} data-testid={`decision-card-${card.key}`}
      aria-label={[card.key, label, closedLabel(card)].join(", ")}
      className={`flex min-w-0 cursor-pointer flex-col gap-1 rounded-xl p-2 text-left hover:bg-surface ${selected === card.key ? SELECTED : ""}`}>
      <div className="flex flex-wrap items-center gap-1">
        <span className="font-mono text-label text-ink-2">{card.key}</span>
        <Pill tone="gray">{closedLabel(card)}</Pill>
      </div>
      <p className="m-0 text-label break-words text-ink-2">{label} · {card.source}</p>
      {card.reason !== null && <p className="m-0 line-clamp-2 text-label break-words text-ink-2">{card.reason}</p>}
    </div>
  );
}

// --- the drawer's Trigger/Context/Options box and the nine-step progress list ------------------------------------------

export const BOX_TITLE = { trigger: "Trigger", context: "Context", options: "Options" } as const;
export const FACTS_CHECKED = (n: number) => `${n} ${n === 1 ? "fact" : "facts"} checked`; // (proposed)
export const PLANS_CHECKED = (checked: number, ranked: number, ruledOut: number) => // sketch 005's line
  `${checked} plans checked · ${ranked} ranked · ${ruledOut} ruled out by a limit`;
export const PROGRESS = "Progress"; // (proposed) the progress list's name
export const STEP_STATE_TEXT: Record<SegmentState, string> = { done: "done", current: "current step", later: "later" };
export const SOURCE_LINE = (state: string) => // (proposed, UI-SPEC "Drawer")
  `Step from the stock decision's state (${state}). Expected value from the engine's recommendation.`;

// The stock decision's detail, read once when the drawer opens (and again when the recommendation changes), for what
// the morning pass checked: the facts, the plans checked and ranked, and each plan a limit ruled out with its reason.
function useDetail(card: Card61): CaseDetail | null {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const id = card.needs_evaluation === true ? card.key : card.case_id;
  useEffect(() => {
    let live = true;
    get<CaseDetail>(`/api/cases/${encodeURIComponent(id)}`).then((d) => { if (live) setDetail(d); }, () => {});
    return () => { live = false; };
  }, [id, card.rec_version]);
  return detail;
}

function CheckedBox({ card, detail, session }: { card: Card61; detail: CaseDetail | null; session: Session }) {
  const result = detail?.result ?? null;
  if (result === null) return null;
  const facts = detail?.fact_checks ?? [];
  return (
    <div data-testid="drawer-checked" className="flex flex-col gap-2 rounded-lg border border-rule p-2">
      <div>
        <p className="m-0 text-label text-ink-3">{BOX_TITLE.trigger}</p>
        <p className="m-0 text-body break-words text-ink">{card.reason ?? "—"}</p>
      </div>
      <div>
        <p className="m-0 text-label text-ink-3">{BOX_TITLE.context}</p>
        <p className="m-0 text-body text-ink">{FACTS_CHECKED(facts.length)}</p>
        {facts.length > 0 && (
          <ul className="m-0 list-disc pl-4 text-label text-ink-2">
            {facts.map((f) => <li key={f.fact_id} className="break-words">{factTitle(f, session.calendar)}</li>)}
          </ul>
        )}
      </div>
      <div>
        <p className="m-0 text-label text-ink-3">{BOX_TITLE.options}</p>
        <p className="num m-0 text-body text-ink">
          {PLANS_CHECKED(result.candidates_checked, result.options.length, result.rejected.length)}
        </p>
        {result.rejected.length > 0 && (
          <ul className="m-0 list-disc pl-4 text-label text-ink-2">
            {result.rejected.map((r, i) => <li key={i} className="break-words">{r.label}: {r.reasons.join("; ")}</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}

const DOT: Record<SegmentState, string> = { done: "bg-navy-700", current: "bg-gold-600", later: "bg-rule" };

function ProgressList({ card }: { card: Card61 }) {
  const states = segmentStates(card.step_number, STEP_KEYS.length, card.rec_version !== null);
  return (
    <ol aria-label={PROGRESS} className="m-0 flex list-none flex-col gap-1 p-0">
      {STEP_KEYS.map((k, i) => (
        <li key={k} aria-current={states[i] === "current" ? "step" : undefined}
          className={`flex items-center gap-2 text-label ${states[i] === "current" ? "font-semibold text-ink" : "text-ink-2"}`}>
          <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${DOT[states[i]]}`} />
          {i + 1}. {stepName(k)}
          <span className="sr-only">, {STEP_STATE_TEXT[states[i]]}</span>
        </li>
      ))}
    </ol>
  );
}

// --- the drawer -------------------------------------------------------------------------------------------------------

// Take ownership is offered when the server's next action is Take ownership and the signed-in account is among who
// does it, or is HQ (D-67). The server decides anyway: a 403 or 409 shows its message word for word.
function mayTake(card: Card61, me: Me): boolean {
  if (card.next_action?.key !== "take_ownership") return false;
  if (me.role === "hq") return true;
  return card.next_action.who.some((w) => w.role === "manager" && w.branch === me.branch);
}

function CardDrawer({ card, session, me, onClose, onChanged }: {
  card: Card61; session: Session; me: Me; onClose: () => void; onChanged: () => void;
}) {
  const take = useAction((_: Record<string, never>, key: string) => takeOwnership(card.case_id, key));
  const press = () => void take.run({}).then((out) => { if (out !== undefined) onChanged(); });
  const err = take.error;
  const ownedBy = err?.code === "already_owned" && typeof (err.extra.owner as { label?: unknown } | undefined)?.label === "string"
    ? OWNED_BY((err.extra.owner as { label: string }).label) : null;
  const canTake = mayTake(card, me);
  const next = nextLine(card);
  const deadline = deadlineChip(card, session.day, session.calendar);
  const flag = card.needs_evaluation === true;
  const rows: [ReactNode, ReactNode][] = [];
  if (card.reason !== null) rows.push([LABELS.whyOpened, card.reason]);
  if (!flag) {
    rows.push([LABELS.deadline, deadline.text]);
    rows.push([LABELS.owner, <OwnerValue card={card} me={me} />]);
    rows.push([LABELS.affects, affectsText(card)]);
    rows.push([LABELS.expected, <ExpectedValue card={card} session={session} note={EXPECTED_DRAWER} />]);
  }
  if (next !== null) rows.push([LABELS.nextAction, `${next.action} · ${next.who}`]);
  const detail = useDetail(card);
  const open = () => { window.location.hash = decisionHref(card); };
  return (
    <Drawer title={productLabel(card.sku, card.product)} onClose={onClose}>
      <p className="m-0 font-mono text-label text-ink-3">{card.key}</p>
      <p className="m-0 text-label break-words text-ink-2">{lotRoute(card)}</p>
      {!flag && <StepBar current={card.step_number} optionsDone={card.rec_version !== null} label={stepName(card.step)} />}
      <Kv items={rows} stacked />
      {!flag && <CheckedBox card={card} detail={detail} session={session} />}
      <Badges chips={cardBadges(card, session.currency)} />
      {canTake && (
        <div className="flex flex-col items-start gap-1">
          <Button kind="primary" busy={take.busy} onClick={press}>{TAKE_OWNERSHIP}</Button>
          <p className="m-0 text-label text-ink-3">{TAKE_OWNERSHIP_HELP}</p>
        </div>
      )}
      <ErrorLine error={err} action={err !== null && retryable(err.code) ? { label: TRY_AGAIN, onClick: () => void take.retry().then((o) => { if (o !== undefined) onChanged(); }) } : undefined} />
      {ownedBy !== null && <p className="m-0 text-body text-ink">{ownedBy}</p>}
      {!flag && <ProgressList card={card} />}
      <div>
        <Button kind={canTake ? "plain" : "primary"} onClick={open}>{TERMS.OPEN_STOCK_DECISION}</Button>
      </div>
      {!flag && <p className="m-0 border-t border-dashed border-rule pt-2 text-label text-ink-3">{SOURCE_LINE(card.state)}</p>}
    </Drawer>
  );
}

export const page: PageDef = {
  path: "/board",
  title: TERMS.DECISION_BOARD,
  nav: { label: TERMS.DECISION_BOARD, icon: "board", section: "", order: 2, countKey: "board" },
  Component: BoardPage,
};
