// Business events (07-20; 07-CONTEXT D-04, D-13..D-17; 07-UI-SPEC surface 4; 07-SCREENS-MAP 07-20 rows): the field
// reports of the signed-in store (HQ: every store) as a timeline grouped by real date, newest first, in the server's
// order and pages; and one report's detail at "#/events/{id}" with its owner's and reporter's actions.
// - The segmented control "Needs my confirmation (N) | All" opens on the first when N > 0; the filters Kind, Store,
//   State and Stock decision are the server's query (G5: the client never sorts or filters a list).
// - A row: its time, the kind marker, "{Kind} · {store} · {product}", a one-line summary (≤ 90 characters), its state
//   chip, "Simulated report" when preloaded (never drawn as observed, R-8), "Affects {N} stock decisions"; the one
//   primary "Review report" when the signed-in account confirms it.
// - The detail: Source · Fields · Potential impact (stacked below 768px). The owner confirms it as a reported fact
//   (its validity and effect stated beside the button), corrects its fields (a new revision) or dismisses it through
//   its confirm step; the reporter withdraws it before confirmation; the lot's manager may hold the lot. After
//   confirmation: the fact label, "Re-planned {N} stock decisions" and "Report a correction".
// Freshness (05-CONTEXT): loads on route entry, when the tab becomes visible again, after one's own write and when the
// workspace version moves; nothing polls; a late answer to an older load is dropped. A refetch that fails keeps the
// list under the stale banner.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, NETWORK } from "../api.ts";
import { retryable } from "../copy.ts";
import { useAction, useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { FieldInput, InlineConfirm, PreviewPanel, ReportButton, todayIso, type SheetStart } from "../report.tsx";
import {
  ALL, ANY, APPLY, BACK_TO_EVENTS, FIELD_LABEL, CASE_HINT, CHECKED_BOTH, CLEAR_FILTERS, COLUMNS, CONFIRM_FACT, CORRECT_FIELDS,
  DISMISS, DISMISS_REPORT, DISMISS_SENTENCE, EMPTY_BODY, EMPTY_TITLE, EVENTS_FAILED, EVENTS_LOADING, EVENTS_TITLE,
  FILTER, HOLD_BOX, HOLD_PLACED, KEEP_FIELDS, KEEP_REPORT, KIND_ICON, KIND_LABEL, KINDS, LOT_KINDS, NEEDS_REPORTER,
  NOTHING_MINE, NO_MATCH, PLACE_HOLD, DONT_HOLD, PREVIEWING, PREVIEW_IMPACT, REPORT_CORRECTION,
  REPORT_FAILED, REPORT_LOADING, REPORT_SUBMITTED, REVIEW_REPORT, SAVE_CORRECTIONS, SHOW_ALL, STATE_OPTIONS, STORE_HINT,
  TIMES, VIEW_FILE, VIEW_LABEL, WITHDRAW, WITHDRAW_REPORT, WITHDRAW_SENTENCE, affectsChip, confirmBlock, confirmNote,
  confirmReport, correctReport, dismissReport, fieldText, fieldsBody, fieldsOf, flaggedLine, getReport, groupOf,
  holdLot, holdReason, holdSentence, listReports, missingOf, needsMine, ownerName, owns, previewReport, replanned,
  reported, reportedBy, requiredOf, stateChip, summaryLine, titleLine, validity, withdrawReport,
  type EventsQuery, type Report, type ReportConfirmed, type ReportDetail, type ReportFields, type ReportList,
} from "../reportcopy.ts";
import { PageHead, useSession, type Route } from "../shell.tsx";
import { recordedTime } from "../copy.ts";
import {
  Button, ErrorLine, Icon, Kv, LabelledSelect, LinkButton, LoadError, Loading, Pager, Pill, Segmented, StaleBanner,
  TRY_AGAIN,
} from "../ui.tsx";

const asError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));

// --- the route's filters -----------------------------------------------------------------------------------------

type EventsRoute = { view?: string; kind?: string; store?: string; state?: string; case?: string; page?: string };

function eventsHash(q: EventsRoute): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== "") p.set(k, v);
  const text = p.toString();
  return text === "" ? "#/events" : `#/events?${text}`;
}
const filtered = (q: EventsRoute) => [q.kind, q.store, q.state, q.case].some((v) => v !== undefined && v !== "");

function queryOf(q: EventsRoute): EventsQuery {
  return {
    mine: q.view !== "all", kind: q.kind, store: q.store, state: q.state, case: q.case,
    page: q.page === undefined ? undefined : Number(q.page) || 1,
  };
}

type Load = { view: ReportList | null; error: ApiError | null; version: number | null; of: string | null };

function useReports(run: string, version: number, q: EventsQuery) {
  const [state, setState] = useState<Load>({ view: null, error: null, version: null, of: null });
  const latest = useRef(0);
  const at = useRef(version);
  at.current = version;
  const key = JSON.stringify(q);
  const load = useCallback(async () => {
    const n = ++latest.current;
    const v = at.current;
    try {
      const view = await listReports(JSON.parse(key) as EventsQuery);
      if (n === latest.current) setState({ view, error: null, version: v, of: key });
    } catch (e) {
      if (n === latest.current) setState((s) => ({ ...s, error: asError(e) }));
    }
  }, [key]);
  useEffect(() => { void load(); }, [load, run, version]);
  useRefetchOnVisible(() => void load());
  return { ...state, reload: () => void load() };
}

// --- the timeline --------------------------------------------------------------------------------------------------

function Chips({ r, tz }: { r: Report; tz: string }) {
  const chip = stateChip(r, tz);
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-1">
      <Pill tone={chip.tone} icon={chip.icon}>{chip.text}</Pill>
      {r.simulated_label !== null && <Pill tone="plan" icon="info">{r.simulated_label}</Pill>}
      {r.state === "READ" && r.needs_input.length > 0 && <Pill tone="warn" icon="alert">{NEEDS_REPORTER}</Pill>}
      {r.affects_count > 0 && <Pill tone="gray" icon="cases">{affectsChip(r.affects_count)}</Pill>}
    </span>
  );
}

function Timeline({ rows, fresh }: { rows: Report[]; fresh: string | null }) {
  const { session } = useSession();
  const tz = session.time_zone;
  const groups: { date: string; items: Report[] }[] = [];
  for (const r of rows) {
    const { date } = groupOf(r.created_at, tz);
    if (groups.length === 0 || groups[groups.length - 1].date !== date) groups.push({ date, items: [] });
    groups[groups.length - 1].items.push(r);
  }
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {groups.map((g) => (
        <section key={g.date} className="min-w-0">
          <h2 className="m-0 mb-2 text-body font-semibold text-ink-2">{g.date}</h2>
          <ol className="m-0 flex list-none flex-col p-0">
            {g.items.map((r, i) => <Row key={r.report_id} r={r} last={i === g.items.length - 1} fresh={fresh === r.report_id} />)}
          </ol>
        </section>
      ))}
    </div>
  );
}

function Row({ r, last, fresh }: { r: Report; last: boolean; fresh: boolean }) {
  const { session } = useSession();
  const mine = r.state === "READ" && owns(session, r);
  const href = `#/events/${encodeURIComponent(r.report_id)}`;
  const ref = useRef<HTMLLIElement>(null);
  const [lit, setLit] = useState(fresh);
  useEffect(() => {
    if (!fresh) return;
    ref.current?.focus();
    setLit(true);
    const t = setTimeout(() => setLit(false), 2000);
    return () => clearTimeout(t);
  }, [fresh]);
  return (
    <li ref={ref} tabIndex={fresh ? -1 : undefined} data-testid="report-row" data-report={r.report_id}
      className={`relative flex min-w-0 gap-3 rounded-lg py-2 pr-2 outline-none motion-safe:transition-colors ${lit ? "bg-navy-50" : ""} ${mine ? "shadow-[inset_4px_0_0_var(--color-navy-800)] pl-3" : "pl-1"}`}>
      <span className="num w-12 shrink-0 pt-1 text-label text-ink-2">{groupOf(r.created_at, session.time_zone).time}</span>
      <span className="relative flex shrink-0 flex-col items-center">
        <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-navy-50 text-navy-800"><Icon name={KIND_ICON[r.kind]} size={14} /></span>
        {!last && <span aria-hidden="true" className="w-0.5 flex-1 bg-rule" />}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <a href={href} className="text-body font-semibold break-words text-ink hover:underline">{titleLine(r)}</a>
        <span className="text-label break-words text-ink-2">{summaryLine(r, session.currency)}</span>
        <Chips r={r} tz={session.time_zone} />
      </span>
      {mine && <span className="shrink-0 self-center"><LinkButton href={href} primary>{REVIEW_REPORT}</LinkButton></span>}
    </li>
  );
}

function Toolbar({ q, n, go }: { q: EventsRoute; n: number; go: (next: EventsRoute) => void }) {
  const [store, setStore] = useState(q.store ?? "");
  const [stock, setStock] = useState(q.case ?? "");
  useEffect(() => { setStore(q.store ?? ""); setStock(q.case ?? ""); }, [q.store, q.case]);
  const apply = () => go({ ...q, store: store.trim(), case: stock.trim(), page: undefined });
  const text = "h-8 w-40 max-w-full rounded-md border border-line-2 bg-white px-2 text-body font-mono max-md:h-11";
  return (
    <div className="flex min-w-0 flex-wrap items-end gap-3">
      <Segmented label={VIEW_LABEL} value={q.view === "all" ? "all" : "mine"}
        options={[["mine", needsMine(n)], ["all", ALL]] as const}
        onPick={(v) => go({ ...q, view: v, page: undefined })} />
      <LabelledSelect label={FILTER.kind} value={q.kind ?? ""} options={[["", ANY], ...KINDS.map((k) => [k, KIND_LABEL[k]] as const)]}
        onPick={(v) => go({ ...q, kind: v, page: undefined })} />
      <LabelledSelect label={FILTER.state} value={q.state ?? ""} options={[["", ANY], ...STATE_OPTIONS]}
        onPick={(v) => go({ ...q, state: v, page: undefined })} />
      <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); apply(); }}>
        <label className="flex items-center gap-1.5 text-label text-tx2">{FILTER.store}
          <input value={store} placeholder={STORE_HINT} onChange={(e) => setStore(e.target.value)} className={text} /></label>
        <label className="flex items-center gap-1.5 text-label text-tx2">{FILTER.case}
          <input value={stock} placeholder={CASE_HINT} onChange={(e) => setStock(e.target.value)} className={text} /></label>
        <Button kind="plain" type="submit">{APPLY}</Button>
      </form>
      {filtered(q) && <Button kind="ghost" onClick={() => go({ view: q.view })}>{CLEAR_FILTERS}</Button>}
    </div>
  );
}

function Empty({ title, body, children }: { title: string; body?: string; children?: ReactNode }) {
  return (
    <section className="flex flex-col items-start gap-2 rounded-2xl border border-line bg-white px-6 py-12">
      <h2 className="m-0 text-title font-semibold text-ink">{title}</h2>
      {body !== undefined && <p className="m-0 max-w-prose text-body text-ink-2">{body}</p>}
      {children}
    </section>
  );
}

export function BusinessEvents({ route }: { route: Route }) {
  const { session } = useSession();
  const q = route.query as EventsRoute;
  const query = queryOf(q);
  const { view, error, version, of, reload } = useReports(session.run_id, session.version, query);
  const go = (next: EventsRoute) => { window.location.hash = eventsHash(next); };
  const [fresh, setFresh] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  // Opens on "Needs my confirmation" only while something waits for this account (07-UI-SPEC surface 4).
  useEffect(() => {
    // Only on the answer to this very query, never on an earlier page's list still on screen.
    if (q.view === undefined && view !== null && error === null && of === JSON.stringify(query)
      && view.needs_my_confirmation === 0) {
      window.location.replace(eventsHash({ ...q, view: "all" }));
    }
  }, [q, view, error, of, query]);
  const mineView = q.view !== "all";
  return (
    <div className="min-w-0">
      <PageHead title={EVENTS_TITLE} actions={(
        <ReportButton primary start={{}} onSubmitted={(r) => { setFresh(r.report_id); setSaid(REPORT_SUBMITTED); reload(); }} />
      )} />
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        <p role="status" className="sr-only">{said}</p>
        <Toolbar q={q} n={view?.needs_my_confirmation ?? 0} go={go} />
        {view !== null && error !== null && <StaleBanner kind="refresh_failed" onAct={reload} />}
        {view !== null && error === null && version !== null && version !== session.version
          && <StaleBanner kind="version_changed" onAct={reload} />}
        {view === null
          ? error !== null ? <LoadError title={EVENTS_FAILED} error={error} onRetry={reload} /> : <Loading text={EVENTS_LOADING} />
          : view.signals.length === 0
            ? filtered(q)
              ? <Empty title={NO_MATCH}><Button kind="ghost" onClick={() => go({ view: q.view })}>{CLEAR_FILTERS}</Button></Empty>
              : mineView && q.view === "mine"
                ? <Empty title={NOTHING_MINE}><a href={eventsHash({ view: "all" })} className="text-body font-semibold text-navy-800 underline underline-offset-2">{SHOW_ALL}</a></Empty>
                : mineView ? <Loading text={EVENTS_LOADING} /> : <Empty title={EMPTY_TITLE} body={EMPTY_BODY} />
            : <Timeline rows={view.signals} fresh={fresh} />}
      </div>
      {view !== null && (
        <Pager page={view.page} pageSize={view.page_size} total={view.total} onPage={(n) => go({ ...q, page: String(n) })} />
      )}
    </div>
  );
}

// --- the report detail -------------------------------------------------------------------------------------------

function useReport(id: string, version: number) {
  const [state, setState] = useState<{ d: ReportDetail | null; error: ApiError | null }>({ d: null, error: null });
  const latest = useRef(0);
  const load = useCallback(async () => {
    const n = ++latest.current;
    try {
      const d = await getReport(id);
      if (n === latest.current) setState({ d, error: null });
    } catch (e) {
      if (n === latest.current) setState((s) => ({ ...s, error: asError(e) }));
    }
  }, [id]);
  useEffect(() => { void load(); }, [load, version]);
  useRefetchOnVisible(() => void load());
  return { ...state, reload: () => void load() };
}

function Column({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex min-w-0 flex-col gap-2 rounded-2xl border border-line bg-white p-4">
      <h2 className="m-0 text-title font-semibold text-ink">{title}</h2>
      {children}
    </section>
  );
}

function Source({ r }: { r: Report }) {
  const { session } = useSession();
  const tz = session.time_zone;
  const t = (iso: string | null) => (iso === null ? null : recordedTime(iso, tz));
  const file = `/api/signals/${encodeURIComponent(r.signal_id)}/file`;
  const times: [string, string | null][] = [
    [TIMES.reported, t(r.created_at)],
    [TIMES.checked, LOT_KINDS.includes(r.kind) ? t(r.observed_at) : null],
    [TIMES.confirmed, t(r.confirmed_at)], [TIMES.dismissed, t(r.dismissed_at)], [TIMES.withdrawn, t(r.withdrawn_at)],
  ];
  return (
    <Column title={COLUMNS.source}>
      {r.text !== "" && <p className="m-0 text-body break-words whitespace-pre-wrap text-ink">{r.text}</p>}
      {r.file !== null && (
        <span className="flex flex-col gap-1">
          {r.file.type.startsWith("image/") && <img src={file} alt={`File attached to the report from ${r.branch ?? ""}`} className="h-16 w-16 rounded-md object-cover" />}
          <a href={file} target="_blank" rel="noreferrer" className="text-body font-semibold text-navy-800 underline underline-offset-2">{VIEW_FILE}</a>
        </span>
      )}
      <p className="m-0 text-label text-ink-2">{reportedBy(r, tz)}</p>
      {r.simulated_label !== null && <p className="m-0"><Pill tone="plan" icon="info">{r.simulated_label}</Pill></p>}
      <Kv items={times.filter((x): x is [string, string] => x[1] !== null)} />
    </Column>
  );
}

function Fields({ r, editing, draft, onDraft }: {
  r: Report; editing: boolean; draft: Record<string, string>; onDraft: (name: string, v: string) => void;
}) {
  const { session } = useSession();
  const names = fieldsOf(r.kind, editing ? { ...r.fields, ...draft } : r.fields);
  const needs = new Set(r.needs_input.map((x) => x.field));
  if (editing) {
    const req = requiredOf(r.kind, { ...r.fields, ...draft });
    return (
      <Column title={COLUMNS.fields}>
        {names.filter((n) => n !== "lot_id").map((n) => (
          <FieldInput key={n} name={n} value={draft[n] ?? (r.fields[n] === null || r.fields[n] === undefined ? "" : String(r.fields[n]))}
            required={req.includes(n)} readOnly={false} invalid={false} onChange={(v) => onDraft(n, v)} />
        ))}
      </Column>
    );
  }
  return (
    <Column title={COLUMNS.fields}>
      <Kv stacked items={names.map((n) => [n, needs.has(n) && r.state === "READ"
        ? <Pill tone="warn" icon="alert">{NEEDS_REPORTER}</Pill>
        : fieldText(n, r.fields[n] ?? null, session.currency)] as [string, ReactNode]).map(([n, v]) => [labelOf(n), v])} />
    </Column>
  );
}
const labelOf = (n: string) => FIELD_LABEL[n] ?? n;

type Pending = "dismiss" | "withdraw" | "hold" | null;

function ReportView({ d, reload }: { d: ReportDetail; reload: () => void }) {
  const { session, refresh } = useSession();
  const r = d.revisions.length > 0 ? d.revisions[d.revisions.length - 1] : d;
  const owner = owns(session, r);
  const reporter = reported(session, r);
  const open = r.state === "READ";
  const [pending, setPending] = useState<Pending>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [tick, setTick] = useState(false);
  const [done, setDone] = useState<ReportConfirmed | null>(null);
  const after = () => { reload(); void refresh(); };
  const confirm = useAction((_: null, key: string) => confirmReport(r.signal_id, {
    revision: r.revision, ...(r.kind === "STOCK_COUNT" ? { checked_both: tick } : {}),
  }, key));
  const dismiss = useAction((_: null, key: string) => dismissReport(r.signal_id, key));
  const withdraw = useAction((_: null, key: string) => withdrawReport(r.signal_id, key));
  const correct = useAction((fields: ReportFields, key: string) => correctReport(r.signal_id, { revision: r.revision, fields }, key));
  const look = useAction((_: null, key: string) => previewReport(r.signal_id, r.revision, key));
  const hold = useAction((_: null, key: string) => holdLot(String(r.fields.lot_id), { reason: holdReason(r), signal_id: r.signal_id }, key));
  const [shown, setShown] = useState(d.preview);
  useEffect(() => { setShown(d.preview); }, [d.preview]);

  const block = confirmBlock(r, todayIso(session) || null);
  const missing = missingOf(r.kind, r.fields);
  const lotHere = LOT_KINDS.includes(r.kind) && typeof r.fields.lot_id === "string"
    && session.role === "manager" && session.branch === r.branch;
  const holdable = open && r.hold_id === null && lotHere
    && (r.kind !== "CONDITION" || r.fields.condition === "DAMAGE" || r.fields.condition === "SAFETY");
  const current = shown !== null && shown.revision === r.revision ? shown : null;
  const range = validity(r);
  const tz = session.time_zone;
  const chip = stateChip(r, tz);
  const correction: SheetStart = LOT_KINDS.includes(r.kind)
    ? { kind: r.kind, product: r.sku, lockLot: true, fields: { lot_id: String(r.fields.lot_id ?? "") } }
    : { kind: r.kind, product: r.sku, fields: r.fields, corrects: r.report_id };
  const err = (a: { error: ApiError | null; retry: () => Promise<unknown> }) => (
    <ErrorLine error={a.error} action={a.error !== null && retryable(a.error.code) ? { label: TRY_AGAIN, onClick: () => void a.retry().then(after) } : undefined} />
  );

  const doConfirm = async () => {
    const out = await confirm.run(null);
    if (out !== undefined) { setDone(out); after(); }
  };
  const doCorrect = async () => {
    const out = await correct.run(fieldsBody(r.kind, Object.fromEntries(Object.entries({ ...r.fields, ...draft })
      .map(([k, v]) => [k, v === "" ? null : v])), Boolean(r.fields.hold)));
    if (out !== undefined) { setEditing(false); setDraft({}); after(); }
  };

  return (
    <div className="min-w-0">
      <PageHead breadcrumb={<a href="#/events" className="underline underline-offset-2">{BACK_TO_EVENTS}</a>}
        title={titleLine(r)} />
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        <div className="flex min-w-0 flex-wrap items-center gap-1">
          <Pill tone={chip.tone} icon={chip.icon}>{chip.text}</Pill>
          {r.simulated_label !== null && <Pill tone="plan" icon="info">{r.simulated_label}</Pill>}
          {open && r.needs_input.length > 0 && <Pill tone="warn" icon="alert">{NEEDS_REPORTER}</Pill>}
          {r.hold_id !== null && <Pill tone="bad" icon="hold">{HOLD_PLACED}</Pill>}
        </div>

        {(done !== null || r.state === "CONFIRMED") && (
          <section data-testid="confirmed" className="flex min-w-0 flex-col gap-1 rounded-lg bg-good-soft p-4">
            {(done?.labels.length ? done.labels : [r.fact_label ?? chip.text]).map((l) => (
              <p key={l} className="m-0 text-body font-semibold break-words text-ink">{l}</p>
            ))}
            {done !== null && (
              <p className="m-0 text-body text-ink">
                {replanned(done.cases.length)}
                {done.cases.length > 0 && <>: {done.cases.map((c, i) => (
                  <span key={c.case_id}>{i > 0 && ", "}<a href={`#/cases/${encodeURIComponent(c.key)}`} className="font-mono text-navy-800 underline underline-offset-2">{c.key}</a></span>
                ))}</>}
              </p>
            )}
            {done !== null && (done.flagged ?? []).length > 0 && <p className="m-0 text-body text-ink">{flaggedLine(done.flagged ?? [])}</p>}
            {r.state === "CONFIRMED" && r.replaced_by === null && (session.role === "hq" ? r.kind === "TRANSPORT" : session.branch === r.branch) && (
              <span className="mt-2"><ReportButton start={correction} label={REPORT_CORRECTION} onSubmitted={after} /></span>
            )}
          </section>
        )}

        {open && (owner || reporter || holdable) && (
          <section aria-label="Actions" className="flex min-w-0 flex-col gap-2 rounded-2xl border border-line bg-white p-4">
            {owner && !editing && (
              <>
                {range !== null && <p className="num m-0 text-body font-semibold text-ink">{range}</p>}
                <p className="m-0 text-label text-ink-2">{confirmNote(r.affects_count)}</p>
                {r.kind === "STOCK_COUNT" && (
                  <label className="flex min-h-11 items-center gap-2 text-body md:min-h-0">
                    <input type="checkbox" checked={tick} onChange={(e) => setTick(e.target.checked)} />{CHECKED_BOTH}
                  </label>
                )}
                <div className="flex flex-wrap items-center gap-2">
                  <Button kind="primary" busy={confirm.busy} disabled={block !== null || missing.length > 0}
                    aria-describedby={block !== null ? "confirm-why" : undefined} onClick={() => void doConfirm()}>{CONFIRM_FACT}</Button>
                  {block !== null && <span id="confirm-why" className="text-label text-ink-2">{block}</span>}
                  {block === null && missing.length > 0 && <span className="text-label text-ink-2">{NEEDS_REPORTER}</span>}
                  <Button kind="ghost" onClick={() => setEditing(true)}>{CORRECT_FIELDS}</Button>
                  {pending !== "dismiss" && <Button kind="ghost" onClick={() => setPending("dismiss")}>{DISMISS}</Button>}
                </div>
                {err(confirm)}
              </>
            )}
            {owner && editing && (
              <div className="flex flex-wrap items-center gap-2">
                <Button kind="primary" busy={correct.busy} onClick={() => void doCorrect()}>{SAVE_CORRECTIONS}</Button>
                <Button kind="ghost" onClick={() => { setEditing(false); setDraft({}); }}>{KEEP_FIELDS}</Button>
                {err(correct)}
              </div>
            )}
            {pending === "dismiss" && (
              <InlineConfirm sentence={DISMISS_SENTENCE} confirm={DISMISS_REPORT} dismiss={KEEP_REPORT} busy={dismiss.busy}
                onConfirm={() => void dismiss.run(null).then((x) => { if (x !== undefined) { setPending(null); after(); } })}
                onDismiss={() => setPending(null)}>{err(dismiss)}</InlineConfirm>
            )}
            {reporter && pending !== "withdraw" && <Button kind="ghost" className="w-fit" onClick={() => setPending("withdraw")}>{WITHDRAW}</Button>}
            {pending === "withdraw" && (
              <InlineConfirm sentence={WITHDRAW_SENTENCE} confirm={WITHDRAW_REPORT} dismiss={KEEP_REPORT} busy={withdraw.busy}
                onConfirm={() => void withdraw.run(null).then((x) => { if (x !== undefined) { setPending(null); after(); } })}
                onDismiss={() => setPending(null)}>{err(withdraw)}</InlineConfirm>
            )}
            {holdable && pending !== "hold" && <Button kind="ghost" className="w-fit" onClick={() => setPending("hold")}>{HOLD_BOX}</Button>}
            {pending === "hold" && (
              <InlineConfirm sentence={holdSentence(String(r.fields.lot_id), r.branch ?? "")} confirm={PLACE_HOLD} dismiss={DONT_HOLD}
                busy={hold.busy} onConfirm={() => void hold.run(null).then((x) => { if (x !== undefined) { setPending(null); after(); } })}
                onDismiss={() => setPending(null)}>{err(hold)}</InlineConfirm>
            )}
          </section>
        )}

        <div className="grid min-w-0 grid-cols-1 gap-4 md:grid-cols-3">
          <Source r={r} />
          <Fields r={r} editing={editing} draft={draft} onDraft={(n, v) => setDraft((x) => ({ ...x, [n]: v }))} />
          <Column title={COLUMNS.impact}>
            {r.kind === "CONDITION"
              ? <p className="m-0 text-body text-ink">{r.impact_label}</p>
              : (
                <PreviewPanel preview={current} stale={false} owner={ownerName(r.owner)} currency={session.currency}>
                  {open && (
                    <Button kind="ghost" busy={look.busy} className="w-fit border-line-2"
                      onClick={() => void look.run(null).then((p) => { if (p !== undefined) setShown(p); })}>
                      {look.busy ? PREVIEWING : PREVIEW_IMPACT}
                    </Button>
                  )}
                  {err(look)}
                </PreviewPanel>
              )}
          </Column>
        </div>
      </div>
    </div>
  );
}

export function ReportDetailPage({ id }: { id: string }) {
  const { session } = useSession();
  const { d, error, reload } = useReport(id, session.version);
  if (d === null) {
    return (
      <div className="min-w-0">
        <PageHead breadcrumb={<a href="#/events" className="underline underline-offset-2">{BACK_TO_EVENTS}</a>} title={EVENTS_TITLE} />
        <div className="p-4 md:p-6">
          {error !== null ? <LoadError title={REPORT_FAILED} error={error} onRetry={reload} /> : <Loading text={REPORT_LOADING} />}
        </div>
      </div>
    );
  }
  return (
    <>
      {error !== null && <div className="px-4 pt-4 md:px-6"><StaleBanner kind="refresh_failed" onAct={reload} /></div>}
      <ReportView key={d.report_id} d={d} reload={reload} />
    </>
  );
}

function EventsPage({ route }: { route: Route }) {
  const m = /^\/events\/(.+)$/.exec(route.path);
  return m === null ? <BusinessEvents route={route} /> : <ReportDetailPage id={decodeURIComponent(m[1])} />;
}

export const page: PageDef = {
  path: "/events",
  match: /^\/events\/.+$/,
  title: EVENTS_TITLE,
  nav: { label: EVENTS_TITLE, icon: "inbox", section: "Stock", order: 8.5 },
  Component: EventsPage,
};
