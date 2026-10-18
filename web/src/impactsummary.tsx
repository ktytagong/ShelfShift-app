import { Fragment, useEffect, useRef, useState } from "react";
import { ApiError, startSummary } from "./api.ts";
import { recordedTime } from "./copy.ts";
import { getReport, type Preview, type Report } from "./reportcopy.ts";
import { checkLine, refTarget, summaryState, type ImpactSummaryData, type SummaryEvent, type SummaryProgress, type SummaryRef } from "./summaryview.ts";
import { Pill, Provenance } from "./ui.tsx";

const UNAVAILABLE = "AI summary unavailable.";
const DAILY = "Today's AI limit is reached. It resets at 00:00 Bangkok time.";
// The per-minute preview limit (07-20 review; cap_reached with kind "preview") is not the daily AI limit.
const PREVIEW_LIMIT = "Too many previews right now. Try again in a minute.";
const PREVIEW_REFUSED = "cap_reached:preview";
type Job = { promise: Promise<ImpactSummaryData>; listeners: Set<(event: SummaryEvent) => void>; events: SummaryEvent[]; settled: boolean };
const jobs = new Map<string, Job>();
const eligible = (r: Pick<Report, "kind" | "state">) => r.state === "READ" && r.kind !== "NOT_RELEVANT" && r.kind !== "CONDITION";
const identity = (workspace: string, r: Pick<Report, "signal_id" | "revision">) => `${workspace}:${r.signal_id}:${r.revision}`;
const sameRevision = (a: Report, b: Report) => a.signal_id === b.signal_id && a.revision === b.revision && a.state === b.state;
class ReportChanged extends Error {}

// A submitted sheet may close while its generation continues. A detail in the
// same workspace subscribes to that one request; other tabs observe GET state.
export function beginSummary(workspace: string, report: Report, generation = "submit"): Job | null {
  if (!eligible(report)) return null;
  const base = identity(workspace, report);
  const active = [...jobs.entries()].find(([key, job]) => key.startsWith(`${base}:`) && !job.settled)?.[1];
  if (active) return active;
  const key = `${base}:${generation}`;
  if (jobs.has(key)) return jobs.get(key)!;
  const listeners = new Set<(event: SummaryEvent) => void>();
  const events: SummaryEvent[] = [];
  const job: Job = { promise: Promise.resolve(null as never), listeners, events, settled: false };
  jobs.set(key, job);
  const generate = () => startSummary(report.signal_id, report.revision, crypto.randomUUID(), event => {
    events.push(event); for (const listener of listeners) listener(event);
  });
  // A second document can read before the first POST's admission transaction
  // commits. The same-origin Web Lock closes that gap without a second POST.
  const observe = async (): Promise<ImpactSummaryData> => {
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 500));
      const detail = await getReport(report.signal_id);
      const latest = detail.revisions.at(-1) ?? detail;
      if (!sameRevision(report, latest)) throw new ReportChanged();
      const next = latest.impact_summary;
      if (next && next.status !== "GENERATING" && next.status !== "OUT_OF_DATE") return next;
      // GET intentionally hides GENERATING outputs; its same-revision preview
      // is the observer's evidence that deterministic rows are ready to display.
      const previewKey = detail.preview?.revision === report.revision ? detail.preview.preview_key : undefined;
      const item = { event: "status", data: { state: "being_written", preview_key: previewKey } };
      for (const listener of listeners) listener(item);
    }
    throw new Error("Summary observation timed out");
  };
  job.promise = (async () => navigator.locks ? await navigator.locks.request(`impact-summary:${base}`, { ifAvailable: true },
    lock => lock ? generate() : observe()) : await generate())().finally(() => { job.settled = true; });
  // Background failure is rendered only on the detail; never an unhandled rejection. A per-minute preview refusal
  // (07-20 review) is transient: forget it, so reopening the report asks again (release C).
  void job.promise.catch((error: unknown) => {
    if (error instanceof ApiError && error.code === "cap_reached" && error.extra.kind === "preview" && jobs.get(key) === job) jobs.delete(key);
  });
  if (jobs.size > 100) for (const [old, value] of jobs) { if (old !== key && value.settled) { jobs.delete(old); break; } }
  return job;
}

const metric = (path: string) => path.endsWith("if_confirmed.net") || path.endsWith("benefit_if") ? "Net benefit if confirmed"
  : path.endsWith("now.net") || path.endsWith("benefit_now") ? "Net benefit now" : path.split(".").at(-1)?.replaceAll("_", " ") ?? "Value";

export function ImpactSummary({ report, preview, workspace, zone, rowPrefix, onRefresh }: {
  report: Report; preview: Preview | null; workspace: string; zone: string; rowPrefix: string; onRefresh: () => void;
}) {
  const saved = report.impact_summary ?? null;
  const [summary, setSummary] = useState<ImpactSummaryData | null>(saved);
  const [progress, setProgress] = useState<SummaryProgress>(null);
  const [tools, setTools] = useState<SummaryEvent[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [announced, setAnnounced] = useState("");
  const [storedOpen, setStoredOpen] = useState(false);
  const attempts = useRef(new Set<string>());
  const previewRefreshes = useRef(new Set<string>());
  const refresh = useRef(onRefresh); refresh.current = onRefresh;
  const tintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savedKey = `${saved?.output_id}:${saved?.status}:${saved?.reason}`;
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let subscription: Job | null = null;
    const apply = (next: ImpactSummaryData | null) => {
      if (!live) return;
      // Prior rows are only accepted when this report's own server projection
      // explicitly supplied that OUT_OF_DATE output. Other epochs are discarded.
      if (next && (next.signal_id !== report.signal_id || next.revision !== report.revision)
        && !(next.status === "OUT_OF_DATE" && next.output_id === saved?.output_id)) {
        setProgress(null); refresh.current(); return;
      }
      if (next?.status === "GENERATING") { setProgress("being_written"); return; }
      if (next?.provenance === "FALLBACK") {
        setFailure(next.reason ?? "error");
        setSummary(old => old?.status === "OUT_OF_DATE" && old.text ? old : next);
      } else { setSummary(next); setFailure(null); }
      setProgress(null);
      if (next && next.provenance !== "FALLBACK") setAnnounced("Impact summary ready");
    };
    const event = (item: SummaryEvent) => {
      if (!live) return;
      if (item.event === "status") setProgress(item.data.state === "checking" ? "checking" : "being_written");
      // Admission prepares and persists the deterministic preview before this
      // first status frame. Fetch those rows while model work is still running.
      const prepared = typeof item.data.preview_key === "string" && item.data.preview_key
        ? `preview:${item.data.preview_key}` : typeof item.data.output_id === "string" && item.data.output_id
          ? `output:${item.data.output_id}` : null;
      if (item.event === "status" && prepared && !previewRefreshes.current.has(prepared)) {
        previewRefreshes.current.add(prepared); refresh.current();
      }
      if (item.event === "tool") setTools(old => [...old.filter(x => x.data.n !== item.data.n), item]);
    };
    const poll = async () => {
      try {
        const detail = await getReport(report.signal_id);
        if (!live) return;
        const latest = detail.revisions.at(-1) ?? detail;
        const next = latest.impact_summary ?? null;
        if (!sameRevision(report, latest)) { setProgress(null); refresh.current(); return; }
        if (next?.status === "GENERATING") timer = setTimeout(() => void poll(), 750);
        else { apply(next); refresh.current(); }
      } catch { if (live) { setFailure("error"); setProgress(null); } }
    };
    setSummary(old => (saved?.status === "GENERATING" || saved?.provenance === "FALLBACK")
      && old?.status === "OUT_OF_DATE" && old.text ? old : saved);
    if (saved?.provenance === "FALLBACK") setFailure(saved.reason ?? "error");
    if (!eligible(report)) { setProgress(null); return () => { live = false; }; }
    const active = [...jobs.entries()].find(([key, job]) => key.startsWith(`${identity(workspace, report)}:`) && !job.settled)?.[1];
    if (saved?.status === "GENERATING" && !active) {
      setProgress("being_written"); timer = setTimeout(() => void poll(), 400);
    } else if (!saved || saved.status === "OUT_OF_DATE" || active) {
      const attempt = `${identity(workspace, report)}:${savedKey}:${preview?.preview_key ?? ""}`;
      if (!attempts.current.has(attempt)) {
        attempts.current.add(attempt); setProgress("being_written"); setFailure(null); setTools([]);
        subscription = active ?? beginSummary(workspace, report, saved ? `refresh:${preview?.preview_key ?? saved.preview_key}` : "submit");
        if (subscription) {
          subscription.events.forEach(event); subscription.listeners.add(event);
          void subscription.promise.then(next => {
            if (!live) return;
            if (next.status === "GENERATING") timer = setTimeout(() => void poll(), 400);
            else { apply(next); refresh.current(); }
          }).catch(error => {
            if (!live) return;
            setProgress(null);
            if (error instanceof ReportChanged) { refresh.current(); return; }
            setFailure(!(error instanceof ApiError) ? "error"
              : error.code === "cap_reached" && error.extra.kind === "preview" ? PREVIEW_REFUSED : error.code);
          });
        }
      }
    } else setProgress(null);
    return () => { live = false; if (timer) clearTimeout(timer); subscription?.listeners.delete(event); };
  }, [workspace, report.signal_id, report.revision, report.state, savedKey]);
  useEffect(() => () => { if (tintTimer.current) clearTimeout(tintTimer.current); }, []);
  if (report.kind === "NOT_RELEVANT" || report.kind === "CONDITION") return null;
  const state = summaryState(summary, progress);
  if (state === "absent" && !failure) return null;
  const historical = state === "historical";
  const outdated = state === "out_of_date";
  const hasText = summary && summary.text && summary.provenance !== "FALLBACK" && summary.status !== "GENERATING";
  const stamp = summary?.created_at ? recordedTime(summary.created_at, zone) : "time not recorded";
  const focusRef = (ref: SummaryRef) => {
    if (!summary) return;
    const target = refTarget(summary, ref, preview);
    if (!target) return;
    const id = target.scope === "panel" ? `${rowPrefix}-${target.caseId}` : `${rowPrefix}-stored-${target.caseId ?? "aggregate"}`;
    if (target.scope === "stored") setStoredOpen(true);
    requestAnimationFrame(() => {
      const node = document.getElementById(id);
      if (!node) return;
      node.focus({ preventScroll: true });
      node.scrollIntoView({ block: "nearest", behavior: "instant" });
      node.classList.add("bg-navy-50");
      if (tintTimer.current) clearTimeout(tintTimer.current);
      tintTimer.current = setTimeout(() => node.classList.remove("bg-navy-50"), 2000);
    });
  };
  return <section data-testid="impact-summary" className="mt-4 flex min-w-0 flex-col gap-2 [overflow-wrap:anywhere]">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="m-0 text-title font-semibold">Impact summary</h3>
      {outdated && <Pill tone="warn" icon="redo">Out of date</Pill>}
      {hasText && <Provenance reading={{provenance: summary.provenance, model_id: summary.model_id ?? "Model not recorded"}}
        recorded={`Recorded ${summary.created_at ? recordedTime(summary.created_at, zone) : "time not recorded"} Bangkok time`} />}
    </div>
    <span role="status" className="sr-only">{announced}</span>
    {hasText && <>
      <p className={`m-0 whitespace-pre-wrap text-body ${outdated ? "text-ink-2" : "text-ink"}`}>
        {summary.text.split(/(\{\{\d+\}\})/).map((piece, i) => {
          const match = /^\{\{(\d+)\}\}$/.exec(piece);
          if (!match) return <Fragment key={i}>{piece}</Fragment>;
          const ref = summary.refs[Number(match[1])];
          const target = ref && refTarget(summary, ref, preview);
          if (!target) return <Fragment key={i}>Missing</Fragment>;
          const sourceCase = target.caseId ? summary.rows.cases.find(c => c.case_id === target.caseId)?.key : "Engine preview";
          return <button key={i} type="button" className="min-h-8 cursor-pointer text-left text-navy-800 underline decoration-dotted underline-offset-4 max-md:min-h-11"
            aria-label={`Source: ${metric(ref.path)}, ${sourceCase}`} onClick={() => focusRef(ref)}>{ref.text ?? String(ref.value)}</button>;
        })}
      </p>
      {summary.capped_text && <p className="m-0 text-label text-ink-2">{summary.capped_text}</p>}
      <p className="m-0 text-label text-ink-2">Sources: Engine preview · this revision</p>
      <p className="m-0 text-label text-ink-2">{checkLine(summary)}</p>
      {summary.provenance === "PRECOMPUTED" && <details><summary className="cursor-pointer text-label">Stored generation details</summary><p>{checkLine(summary, { stored: true })}</p></details>}
      {historical && <p className="m-0 text-label text-ink-2">Written for the unconfirmed report · Recorded {stamp} Bangkok time</p>}
      <details open={storedOpen} onToggle={e => setStoredOpen(e.currentTarget.open)}>
        <summary className="cursor-pointer py-2 text-label text-navy-800">Rows this summary used · Recorded {stamp} Bangkok time</summary>
        <p id={`${rowPrefix}-stored-aggregate`} tabIndex={-1} className="m-0 text-label">Previewed: {summary.rows.previewed ?? "Missing"} · Affected: {summary.rows.affected ?? "Missing"}</p>
        {summary.rows.cases?.map(row => <div key={row.case_id} id={`${rowPrefix}-stored-${row.case_id}`} data-stored-case={row.case_id} tabIndex={-1} className="rounded border border-line p-2 text-label">
          <strong>{row.key}</strong><p className="m-0">{row.plan_now} → {row.plan_if}</p>
          <p className="m-0">Net benefit: {row.benefit_now ?? "Missing"} → {row.benefit_if ?? "Missing"}</p>
        </div>)}
      </details>
    </>}
    {(progress || state === "being_written") && <div role="status" className="text-label text-ink-2">
      <p className="m-0">{progress === "checking" ? "Checking numbers…" : "AI summary being written"}</p>
      <ol className="m-0 list-inside list-decimal p-0">{tools.map((tool, i) => <li key={i}>{String(tool.data.purpose ?? "Reading the impact preview")}</li>)}</ol>
    </div>}
    {!progress && tools.length > 0 && <details><summary className="cursor-pointer text-label">Generation details</summary>
      <ol className="list-inside list-decimal p-0">{tools.map((tool, i) => <li key={i}>{String(tool.data.purpose ?? "Reading the impact preview")}</li>)}</ol>
    </details>}
    {(failure || state === "unavailable") && <p className="m-0 text-label text-ink-2">{UNAVAILABLE}{failure === PREVIEW_REFUSED ? ` ${PREVIEW_LIMIT}`
      : (failure ?? summary?.reason) === "cap_reached" ? ` ${DAILY}` : ""}</p>}
  </section>;
}
