// Reports (08 D-01/D-03/D-04/D-15/D-16/D-18). The server owns scope and currentness.
// This first slice displays workspace activity and published benchmark status only.
import { useEffect, useRef, useState, type FormEvent } from "react";
import type { ApiError, Session } from "../api.ts";
import { getReportsBenchmark, getReportsWorkspace, type BenchmarkView, type ReportFigure,
  type ReportsWorkspaceView } from "../contract.ts";
import { REPORT_FIGURES } from "../copy.ts";
import { dateLabel, units } from "../format.ts";
import { useRefetchOnVisible } from "../hooks.ts";
import type { PageDef } from "../pagereg.ts";
import { PageHead, useSession, type Route } from "../shell.tsx";
import { Banner, Button, ErrorLine, FigureTile, Icon, KindChip, Kv, LoadError, Loading, Pill, Section, StaleBanner, Tabs,
  type IconName } from "../ui.tsx";

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
  branch: "Branch", showBranch: "Show branch", allBranches: "All branches", // (proposed) actions
} as const;

type Tab = "workspace" | "benchmark";
export function reportsHash(tab: Tab, branch: string): string {
  const p = new URLSearchParams({ tab });
  if (branch) p.set("branch", branch);
  return `#/reports?${p}`;
}

// Invalidate on every identity, scope or selector transition, including departure/unmount. The render key
// masks old-identity content immediately, before the effect runs. A failed scope lookup may retain the prior
// scope's labelled figures only for the SAME identity. No client version comparison decides benchmark status.
function useReportRead<T>(active: boolean, identity: string, selector: string, load: () => Promise<T>) {
  const [state, setState] = useState<{ identity: string; selector: string; shown: string; data: T | null; error: ApiError | null }>({
    identity, selector, shown: selector, data: null, error: null,
  });
  const [attempt, setAttempt] = useState(0);
  const latest = useRef(0);
  const key = JSON.stringify([identity, selector, active]);
  const current = useRef(key);
  current.current = key;
  useEffect(() => {
    const generation = ++latest.current;
    if (!active) return;
    let alive = true;
    setState((s) => ({ identity, selector, shown: s.shown, data: s.identity === identity ? s.data : null, error: null }));
    const applies = () => alive && latest.current === generation && current.current === key;
    load().then((data) => {
      if (applies()) setState({ identity, selector, shown: selector, data, error: null });
    }).catch((error: ApiError) => {
      if (applies()) setState((s) => ({ ...s, error }));
    });
    return () => { alive = false; ++latest.current; };
  // load is represented by identity and the complete selector, not a new closure on every render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, identity, selector, attempt, key]);
  const same = state.identity === identity;
  return { data: same && (state.shown === selector || (state.selector === selector && state.error?.code === "not_found"))
    ? state.data : null, error: same && state.selector === selector ? state.error : null,
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

function BenchmarkStatus({ view, session }: { view: BenchmarkView; session: Session }) {
  if (view.status === "none") return <Section className="py-12 text-center">
    <span className="mx-auto mb-4 flex h-11 w-11 items-center justify-center rounded-full bg-sunken"><Icon name="chart" size={20} /></span>
    <h2 className="text-title font-semibold">{REPORTS_TEXT.none}</h2>
    <p className="m-0 text-body text-ink-2">{REPORTS_TEXT.noneBody}</p>
  </Section>;
  if (view.status === "historical") return <Banner tone="warn" icon="alert"
    title={REPORTS_TEXT.historical} meta={REPORTS_TEXT.historicalBody} />;
  const result = view.current;
  if (result === null) return null; // malformed data is never turned into invented figures
  const label = (day: number) => dateLabel(session.calendar, day);
  const first = label(result.window.first_day), last = label(result.window.last_day);
  const end = session.calendar.find((d) => d.day === result.window.last_day)?.date;
  const range = `${first} – ${last}${end ? ` ${end.slice(0, 4)}` : ""}`;
  return <Section title={REPORTS_TEXT.evaluation} right={<div className="flex flex-wrap gap-2">
    <KindChip kind="simulated" /><Pill>{REPORTS_TEXT.current}</Pill></div>}>
    <p className="mb-4 text-body text-ink-2">{REPORTS_TEXT.period(range)}</p>
    <Kv stacked items={Object.entries(REPORTS_TEXT.versions).map(([key, text]) => {
      const value = result.versions[key as keyof typeof result.versions];
      return [text, <span className="break-all" key={key}>{Array.isArray(value)
        ? value.join(", ") || REPORTS_TEXT.noForecast : key === "settings" ? value.slice(0, 12) : value}</span>];
    })} />
  </Section>;
}

function ReportsPage({ route }: { route: Route }) {
  const { session } = useSession();
  const hq = session.role === "hq";
  const branch = hq ? (route.query.branch ?? "").trim() : "";
  const tab: Tab = route.query.tab === "benchmark" ? "benchmark" : "workspace";
  const identity = JSON.stringify([(session as Session & { account?: string }).account, session.run_id,
    session.role, session.branch]);
  const selector = JSON.stringify([branch, tab, route.query.breakdown, route.query.chart, route.query.page,
    route.query.benchmark_id, route.path]);
  const workspace = useReportRead(tab === "workspace", identity, selector,
    () => getReportsWorkspace({ branch: branch || undefined }));
  const benchmark = useReportRead(tab === "benchmark", identity, selector,
    () => getReportsBenchmark({ branch: branch || undefined }));
  useRefetchOnVisible(() => { if (tab === "workspace") workspace.retry(); });
  const read = tab === "workspace" ? workspace : benchmark;
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
          aria-invalid={read.error?.code === "not_found" || undefined}
          aria-describedby={read.error?.code === "not_found" ? "reports-branch-error" : undefined}
          className="h-8 w-28 rounded-lg border border-rule-2 bg-surface px-2 font-mono text-body text-ink max-md:h-11" />
      </label>
      <Button kind="plain" type="submit" className="max-md:min-h-11">{REPORTS_TEXT.showBranch}</Button>
      <Button kind="ghost" className="max-md:min-h-11" onClick={() => { setBranchText(""); window.location.hash = reportsHash(tab, ""); }}>
        {REPORTS_TEXT.allBranches}</Button>
    </form>
    {read.error?.code === "not_found" && <div id="reports-branch-error"><ErrorLine error={read.error} /></div>}
  </div> : undefined;
  const again = () => read.retry();
  let body;
  if (read.error && !read.data) body = <LoadError title={tab === "workspace" ? REPORTS_TEXT.failed : REPORTS_TEXT.benchmarkFailed}
    error={read.error} onRetry={again} />;
  else if (!read.data) body = <Loading text={tab === "workspace" ? REPORTS_TEXT.loading : REPORTS_TEXT.benchmarkLoading} />;
  else body = <>{read.error && <StaleBanner kind="refresh_failed" onAct={again} />}
    {tab === "workspace" ? <Activity view={workspace.data!} hq={hq} />
      : <BenchmarkStatus view={benchmark.data!} session={session} />}</>;
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
