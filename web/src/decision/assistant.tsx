// Phase 09: checked assistant output supports the engine comparison. Reads and
// source controls have no business-write authority. Identity changes fence all
// callbacks; the server alone decides whether an output needs generation.
import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, get, post } from "../api.ts";
import { askAssistant, getAssistantHistory, getAssistantToolResult, getEvidenceDrafts, readBrief, sendEvidenceDraft, type AssistantBinding, type AssistantHistory, type AssistantOutput, type AssistantRef, type AssistantSource, type AssistantToolProgress, type AssistantToolResult, type AssistantUsage, type AssistantWhatIf, type CaseDetail61, type EvidenceDraft, type EvidenceDrafts, type EvidenceRequest, type SendEvidenceDraft } from "../contract.ts";
import { errorAction } from "../copy.ts";
import { expected, money, units } from "../format.ts";
import { useAction } from "../hooks.ts";
import { useSession } from "../shell.tsx";
import { Button, Drawer, ErrorLine, Icon, Kv, Provenance, Table } from "../ui.tsx";
import { recordedLine, RELOAD_DECISION } from "../vocab.ts";

export const BRIEF_TITLE = "Decision brief";
export const GENERATING = "Template summary · AI brief being written";
export const TEMPLATE = "Template summary · from the engine's figures";
export const FALLBACK = "Fallback · Gemini unavailable · figures from the engine";
export const UNAVAILABLE = "Gemini can't answer right now. Options and approvals still work.";
export const NO_SOURCES = "No workspace data used";
export const MISSING = "Missing";
const REMOVED = "⚠ number removed: not traceable to a tool result";
const WRAP = "min-w-0 [overflow-wrap:anywhere]";
const NO_LINK = "This output has no linked tool result.";
const SOURCE_REFUSAL = "This source is not available.";
const SOURCE_BUTTON = `min-h-8 max-w-full cursor-pointer py-1 text-left text-label text-navy-800 underline decoration-dotted underline-offset-4 max-md:min-h-11 ${WRAP}`;
type ResolvedRef = AssistantRef & { text?: string };
export type SourceChoice = { resultId: string; ref?: ResolvedRef };
type OpenSource = (choice: SourceChoice) => void;

const TOOL_NAMES: Readonly<Record<string, string>> = {
  get_case: "Stock decision details", get_forecast: "Forecast", get_inventory_and_inbound: "Stock and inbound",
  get_transport_options: "Transport", simulate_options: "What-if comparison", check_constraints: "Limits",
  get_fact_checks: "Fact checks", draft_evidence_request: "Evidence draft",
};
const toolName = (tool: unknown) => typeof tool === "string" ? TOOL_NAMES[tool] ?? tool : MISSING;
const sensitive = (key: string) => /thought|reasoning|credential|hidden|truth/i.test(key)
  || /^(api_key|secret|password|authorization|access_token|refresh_token|cookie|cookies|private_key|system_instruction|__proto__|prototype|constructor)$/i.test(key);

// The API is the authority. This projection is an additional rendering guard
// for legacy records; technical detail never bypasses it.
function safeDetail(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(safeDetail);
  if (value !== null && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !sensitive(key)).map(([key, child]) => [key, safeDetail(child)]));
  return value;
}
const asRecord = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value)
  ? value as Record<string, unknown> : {};
const fieldLabel = (key: string) => {
  const labels: Record<string, string> = { recorded_qty: "Recorded quantity", lot_id: "Lot", net: "Net revenue",
    expiry_day: "Expiry day", last_verified_day: "Last verified day", source_kind: "Source kind", rec_version: "Decision version", plan_key: "Plan" };
  const words = key.replace(/_/g, " ");
  return labels[key] ?? words.charAt(0).toUpperCase() + words.slice(1);
};
const metricLabel = (ref: ResolvedRef) => ref.path.split(".").some(sensitive) ? "Value"
  : ref.path.split(".").slice(-1).map(fieldLabel).join(" · ");
function lookup(value: unknown, path: string): unknown {
  let node = value;
  for (const part of path.split(".")) {
    if (sensitive(part) || node === null || typeof node !== "object" || !Object.prototype.hasOwnProperty.call(node, part)) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}
function scalar(value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return MISSING;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return Number.isFinite(value) ? new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 }).format(value) : MISSING;
  return typeof value === "string" ? value : MISSING;
}

// Human-readable values precede the optional structured audit. Deep records and
// tables expand locally, retaining every authorized row without widening a page.
function Values({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    if (!value.length) return <p className="m-0 text-body text-tx2">No rows in this result.</p>;
    const columns = [...new Set(value.flatMap(row => Object.keys(asRecord(row))))];
    if (columns.length) return <div tabIndex={0} aria-label="Source result table" className="min-w-0 max-w-full overflow-x-auto">
      <div className="min-w-max"><Table head={columns.map(fieldLabel)} rows={value.map(row => columns.map(key => <Values value={asRecord(row)[key]} />))} /></div>
    </div>;
    return <ul className="m-0 list-inside list-disc text-body">{value.map((child, i) => <li key={i} className={WRAP}><Values value={child} /></li>)}</ul>;
  }
  if (value !== null && typeof value === "object") return <div className="flex min-w-0 flex-col gap-2">
    {Object.entries(value).map(([key, child]) => child !== null && typeof child === "object"
      ? <details key={key} className={WRAP}><summary className="cursor-pointer py-2 text-body max-md:min-h-11">{fieldLabel(key)}</summary><Values value={child} /></details>
      : <Kv key={key} stacked items={[[fieldLabel(key), <span className={WRAP}>{scalar(child)}</span>]]} />)}
  </div>;
  return <span className={WRAP}>{scalar(value)}</span>;
}

type Attempt = { model_id?: unknown; assistant_version?: unknown; created_at?: unknown; status?: unknown; usage?: AssistantUsage | null };
type BriefView = { identity: string; label: string; output: AssistantOutput | null; attempt?: Attempt; error?: string; progress?: string };
const present = (value: unknown): string => typeof value === "string" && value.trim() ? value : MISSING;
const measured = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const count = (value: unknown, one: string, many = `${one}s`) => {
  const n = measured(value);
  return `${n ?? MISSING} ${n === 1 ? one : many}`;
};
const seconds = (value: unknown) => {
  const ms = measured(value);
  return ms === null ? MISSING : new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(ms / 1000);
};

export function usageLine(usage: AssistantUsage | null | undefined): string {
  return `${count(usage?.model_calls, "Gemini call")} · ${count(usage?.tool_calls, "tool call")} · ${seconds(usage?.total_ms)} s`;
}

export function GenerationDetails({ attempt, precomputed = false }: { attempt: Attempt; precomputed?: boolean }) {
  const { session } = useSession();
  const usage = attempt.usage;
  const status = usage?.status ?? attempt.status;
  const statusText = status === "DONE" || status === "CURRENT" ? "Completed"
    : status === "FALLBACK" ? "Failed / fallback" : status === "INTERRUPTED" ? "Interrupted" : present(status);
  return <details className={WRAP}>
    <summary className="cursor-pointer py-2 text-label text-navy-800 max-md:min-h-11">
      {precomputed ? "Stored generation details" : "Generation details"}
    </summary>
    <p className="num m-0 text-label text-tx2">{usageLine(usage)}</p>
    <Kv stacked items={[
      ["Model", present(attempt.model_id)], ["Assistant version", present(attempt.assistant_version)],
      ["Attempt status", statusText],
      ["Recorded", typeof attempt.created_at === "string" ? recordedLine(attempt.created_at, session.time_zone) || MISSING : MISSING],
      ["Input tokens", measured(usage?.input_tokens) ?? MISSING], ["Output tokens", measured(usage?.output_tokens) ?? MISSING],
      ["Thinking tokens", measured(usage?.thinking_tokens) ?? MISSING], ["Total tokens", measured(usage?.total_tokens) ?? MISSING],
      ["Retries", measured(usage?.retries) ?? MISSING], ["Time to first text", `${seconds(usage?.first_text_ms)} s`],
      ["Location", present(usage?.location)],
    ]} />
  </details>;
}

const SOURCE_LABELS: Readonly<Record<string, string[]>> = {
  get_case: ["Engine result · this version"], engine_result: ["Engine result · this version"],
  simulate_options: ["Engine result · this version"], check_constraints: ["Engine result · this version"],
  get_transport_options: ["Engine result · this version"], get_forecast: ["Forecast"],
  get_inventory_and_inbound: ["Stock records"], get_fact_checks: ["Engine result · this version", "Requests and confirmed facts"],
  draft_evidence_request: ["Requests and confirmed facts"],
};

function categories(source: AssistantSource): string[] {
  const labels = Array.isArray(source.categories) ? source.categories.filter((x): x is string => typeof x === "string")
    : SOURCE_LABELS[source.kind] ?? [present(source.kind)];
  return labels.map(label => {
    if (label !== "Forecast") return label;
    const providers = Array.isArray(source.providers) ? source.providers.filter((x): x is string => typeof x === "string") : [];
    return `Forecast · ${providers.length ? providers.join(", ") : MISSING} · ${present(source.forecast_run_id)}`;
  });
}

// Zero/one/many results keep their exact identity. IDs belong in the drawer,
// rather than an unbounded list in the prose. No unused categories are added.
export function SourceLine({ output, onOpen }: { output: AssistantOutput; onOpen: OpenSource }) {
  const groups = new Map<string, AssistantSource[]>();
  for (const source of output.sources) for (const category of categories(source)) {
    const group = groups.get(category) ?? [];
    if (!group.some(item => item.result_id === source.result_id)) group.push(source);
    groups.set(category, group);
  }
  return <div className={`flex flex-col text-label text-tx2 ${WRAP}`}>
    <span>Sources:</span>
    {groups.size === 0 ? <span>{output.refs.length || measured(output.missing_sources) ? NO_LINK : NO_SOURCES}</span>
      : [...groups].map(([category, sources]) => sources.length === 1
        ? <button key={category} type="button" className={SOURCE_BUTTON} onClick={() => onOpen({ resultId: sources[0]!.result_id })}>{category}</button>
        : <details key={category} className={WRAP}><summary className={SOURCE_BUTTON}>{category} ({sources.length})</summary>
          <ul className="m-0 list-none p-0">{sources.map((source, i) => <li key={source.result_id}>
            <button type="button" className={SOURCE_BUTTON} onClick={() => onOpen({ resultId: source.result_id })}>
              {toolName(source.tool ?? source.kind)} · result {i + 1}
            </button>
          </li>)}</ul>
        </details>)}
  </div>;
}

// The checker returns placeholders and resolved text, never HTML. Unknown/raw
// reference syntax is visibly missing; the warning keeps its words and uses the
// shared alert icon (the text alternative carries its original warning mark).
export function CheckedText({ output, onOpen }: { output: Pick<AssistantOutput, "text" | "refs" | "sources">; onOpen: OpenSource }) {
  const text = output.text.replace(/\[\[[^\]]*\]\]/g, MISSING);
  const pieces = text.split(/(\{\{\d+\}\}|⚠ number removed: not traceable to a tool result)/g);
  return <p data-testid="checked-brief" className={`m-0 whitespace-pre-wrap text-body text-tx ${WRAP}`}>
    {pieces.map((piece, i): ReactNode => {
      if (piece === REMOVED) return <span key={i}><Icon name="alert" className="mr-1 inline" /><span className="sr-only">⚠ </span>number removed: not traceable to a tool result</span>;
      const match = /^\{\{(\d+)\}\}$/.exec(piece);
      if (!match) return <Fragment key={i}>{piece}</Fragment>;
      const ref = output.refs[Number(match[1])] as ResolvedRef | undefined;
      if (!ref || !ref.result_id || !ref.path || ref.path.split(".").some(sensitive)) return <Fragment key={i}>{MISSING}</Fragment>;
      const source = output.sources.find(item => item.result_id === ref.result_id);
      const category = source ? categories(source)[0] ?? MISSING : "Linked result";
      return <button key={i} type="button" aria-label={`Source: ${metricLabel(ref)} · ${category}`}
        className={`min-h-8 max-w-full cursor-pointer text-left text-body text-navy-800 underline decoration-dotted underline-offset-4 max-md:min-h-11 ${WRAP}`}
        onClick={() => onOpen({ resultId: ref.result_id, ref })}>{present(ref.text)}</button>;
    })}
  </p>;
}

type SourceOutput = AssistantBinding & { output_id: string; kind: "BRIEF" | "ANSWER" | "DRAFT"; sources: AssistantSource[] };
export function ToolResultView({ output, choice, onClose }: { output: SourceOutput; choice: SourceChoice; onClose: () => void }) {
  const { session } = useSession();
  const [revision, setRevision] = useState(0);
  const key = JSON.stringify([session.run_id, session.role, session.branch, output.output_id, output.case_id,
    output.rec_version, output.plan_key, choice.resultId, revision]);
  const [read, setRead] = useState<{ key: string; result?: AssistantToolResult; error?: string }>({ key: "" });
  const linked = output.sources.some(source => source.result_id === choice.resultId);
  useEffect(() => {
    let active = true;
    if (linked) void getAssistantToolResult(output.case_id, choice.resultId).then(result => {
      if (!active) return;
      const expectedScope = output.kind === "ANSWER" ? "ASK" : "CASE";
      if (result.result_id !== choice.resultId || result.scope !== expectedScope || result.run_id !== session.run_id
        || result.run_id !== output.run_id || result.case_id !== output.case_id || result.rec_version !== output.rec_version
        || result.plan_key !== output.plan_key) {
        setRead({ key, error: SOURCE_REFUSAL });
        return;
      }
      setRead({ key, result: { ...result, inputs: safeDetail(result.inputs), result: safeDetail(result.result) } as AssistantToolResult });
    }).catch(error => { if (active) setRead({ key, error: error instanceof ApiError ? error.message : SOURCE_REFUSAL }); });
    const refresh = () => setRevision(previous => previous + 1);
    const visible = () => { if (document.visibilityState === "visible") refresh(); };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", visible);
    return () => { active = false; window.removeEventListener("focus", refresh); document.removeEventListener("visibilitychange", visible); };
  }, [key, linked, output.case_id, output.run_id, output.rec_version, output.plan_key, output.kind, choice.resultId, session.run_id]);
  const current = read.key === key ? read : null;
  const result = current?.result;
  const metricValue = result && choice.ref ? lookup(result.result, choice.ref.path) : undefined;
  const metricAvailable = choice.ref && typeof metricValue === "number" && Number.isFinite(metricValue) && metricValue === choice.ref.value;
  return <Drawer title="Tool result" onClose={onClose}>
    <div className={`flex flex-col gap-4 ${WRAP}`}>
      {!linked ? <p className="m-0 text-body">{NO_LINK}</p> : current?.error ? <div className="flex flex-col items-start gap-2">
        <p role="alert" className={`m-0 text-body ${WRAP}`}>Source didn't load. {current.error}</p>
        <Button kind="plain" onClick={() => setRevision(previous => previous + 1)}>Try again</Button>
      </div> : !result ? <p role="status" className="m-0 text-body">Loading source…</p> : <>
        <h3 className="m-0 text-title font-semibold">{toolName(result.tool)}</h3>
        {choice.ref && <section aria-label="Referenced metric" className="rounded-lg bg-sunken p-4">
          <p className="m-0 text-label text-tx2">{metricLabel(choice.ref)}</p>
          <p data-testid="source-metric-value" className={`num m-0 text-body ${WRAP}`}>{metricAvailable ? present(choice.ref.text) : MISSING}</p>
          {!metricAvailable && <p className="m-0 text-label text-tx2">This metric is missing from this result.</p>}
        </section>}
        <Kv stacked items={[
          ["Source kind", present(result.source_kind ?? asRecord(result.result).source_kind)],
          ["Units", present(result.units ?? asRecord(result.result).units)],
          ["Duration", `${seconds(result.duration_ms)} s`],
          ["Recorded", typeof result.created_at === "string" ? recordedLine(result.created_at, session.time_zone) || MISSING : MISSING],
          ["Result ID", <span className="font-mono">{present(result.result_id)}</span>],
          ["Stock decision", present(result.case_id)], ["Decision version", <span className="font-mono">{present(result.rec_version)}</span>],
          ["Plan", <span className="font-mono">{present(result.plan_key)}</span>],
        ]} />
        <section aria-label="Source values" className={WRAP}><h3 className="m-0 text-title font-semibold">Values</h3><Values value={result.result} /></section>
        <section aria-label="Source inputs" className={WRAP}><h3 className="m-0 text-title font-semibold">Inputs</h3><Values value={result.inputs} /></section>
        <details className={WRAP}><summary className="cursor-pointer py-2 text-label text-navy-800 max-md:min-h-11">Technical details</summary>
          <pre tabIndex={0} aria-label="Authorized technical detail" className="m-0 max-w-full overflow-x-auto rounded-lg bg-sunken p-2 font-mono text-label">{JSON.stringify({ inputs: result.inputs ?? null, result: result.result ?? null }, null, 2)}</pre>
        </details>
      </>}
      <Button kind="plain" onClick={onClose}>{output.kind === "DRAFT" ? "Return to draft" : output.kind === "BRIEF" ? "Return to brief" : "Return to answer"}</Button>
    </div>
  </Drawer>;
}

type TraceRow = { result_id: string; tool?: string; purpose?: string; status?: string; duration_ms?: number | null;
  case_id: string; rec_version: string; plan_key: string; scope: string };
function ToolTrace({ output, onOpen }: { output: AssistantOutput; onOpen: OpenSource }) {
  const [rows, setRows] = useState<TraceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setRows(null);
    setError(null);
    void get<TraceRow[]>(`/api/cases/${encodeURIComponent(output.case_id)}/trace?output_id=${encodeURIComponent(output.output_id)}`)
      .then(result => { if (active) setRows(result.filter(row => row.case_id === output.case_id
        && row.rec_version === output.rec_version && row.plan_key === output.plan_key
        && row.scope === (output.kind === "BRIEF" ? "CASE" : "ASK"))); })
      .catch(failure => { if (active) setError(failure instanceof ApiError ? failure.message : SOURCE_REFUSAL); });
    return () => { active = false; };
  }, [output.case_id, output.output_id, output.rec_version, output.plan_key, output.kind, revision]);
  if (error) return <div><p role="alert" className="m-0 text-body">Source didn't load. {error}</p><Button kind="plain" onClick={() => setRevision(previous => previous + 1)}>Try again</Button></div>;
  if (!rows) return <p role="status" className="m-0 text-label">Loading source…</p>;
  return rows.length ? <ol className="m-0 list-inside list-decimal p-0 text-label">{rows.map(row => <li key={row.result_id} className={WRAP}>
    <button type="button" className={SOURCE_BUTTON} onClick={() => onOpen({ resultId: row.result_id })}>{present(row.purpose ?? toolName(row.tool))}</button>
    <span> · {present(row.status)} · {seconds(row.duration_ms)} s</span>
  </li>)}</ol> : <p className="m-0 text-label">{NO_LINK}</p>;
}

export function DecisionBrief({ d, reload }: { d: CaseDetail61; reload: () => void }) {
  const { session } = useSession();
  const [draftRevision, setDraftRevision] = useState(0);
  const draftsReady = useCallback(() => setDraftRevision(value => value + 1), []);
  const planKey = d.selected_plan?.plan_key ?? d.result?.recommended;
  const identity = JSON.stringify([session.run_id, session.role, session.branch, d.case_id, d.rec_version, planKey]);
  // Only the brief/source reader is keyed. Later draft and Ask editors keep
  // their own original bindings and unsent inputs outside this composition.
  const personal = JSON.stringify([session.run_id, session.role, session.branch, d.case_id]);
  return <><BoundDecisionBrief key={identity} d={d} reload={reload} identity={identity} onSettled={draftsReady} />
    <EvidenceRequests key={`drafts-${personal}`} d={d} reload={reload} revision={draftRevision} />
    <AskBox key={personal} d={d} reload={reload} /></>;
}

const DRAFT_EFFECT = "You own the message. The branch gets a request only when you send it. Required checks stay unresolved until confirmed evidence settles them.";
const DRAFT_DEADLINE = "The response deadline starts when you send the request.";
const DRAFT_CHANGED = "This stock decision changed. Review the current version before sending.";
const DRAFT_OLD = "Draft belongs to an older version. Your edits are kept here for reference.";
const DRAFT_PLAN_CHANGED = "Plan changed · review current checks. Your edits are kept here for reference.";
const CHECKS_LINK = "Open fact checks";
const DRAFT_LINK = `self-start text-body font-semibold text-navy-800 underline underline-offset-2 ${WRAP}`;
const checksHref = (caseId: string) => `#/cases/${encodeURIComponent(caseId)}?step=context`;
const draftBinding = (d: CaseDetail61, run: string): AssistantBinding => ({ run_id: run, case_id: d.case_id,
  rec_version: d.rec_version ?? "", plan_key: d.selected_plan?.plan_key ?? d.result?.recommended ?? "" });

// Both surfaces read the same stored drafts. A Context link opens the one editor
// in Compare; reads never draft, send, resolve a check or spend an AI allowance.
export function useEvidenceDrafts(d: CaseDetail61, externalRevision = 0, enabled = true) {
  const { session } = useSession();
  const [data, setData] = useState<EvidenceDrafts | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    setError(null);
    void getEvidenceDrafts(d.case_id).then(value => { if (active) setData(value); }).catch(reason => {
      if (!active) return;
      const failure = reason instanceof ApiError ? reason : new ApiError(0, "network", "Could not reach ShelfShift. Check your connection and try again.");
      if ([401, 403, 404].includes(failure.status)) setData(null);
      setError(failure);
    });
    return () => { active = false; };
  }, [d, session.run_id, session.role, session.branch, session.version, revision, externalRevision, enabled]);
  useEffect(() => {
    const refresh = () => setRevision(value => value + 1);
    const visible = () => { if (document.visibilityState === "visible") refresh(); };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", visible);
    return () => { window.removeEventListener("focus", refresh); document.removeEventListener("visibilitychange", visible); };
  }, []);
  return { data, error, refresh: () => setRevision(value => value + 1) };
}

function EvidenceRequests({ d, reload, revision }: { d: CaseDetail61; reload: () => void; revision: number }) {
  const { session } = useSession();
  const requested = new URLSearchParams(window.location.hash.split("?")[1]).get("draft");
  const [opened, setOpened] = useState(!!requested);
  const read = useEvidenceDrafts(d, revision, opened);
  const current = draftBinding(d, session.run_id);
  const [selected, setSelected] = useState<EvidenceDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [pending, setPending] = useState<{ run: () => void } | null>(null);
  const bypass = useRef(false);
  const region = useRef<HTMLElement>(null);
  const openedLink = useRef<string | null>(null);
  useEffect(() => {
    if (!selected && requested && requested !== openedLink.current && read.data) {
      const draft = read.data.drafts.find(row => row.draft_id === requested);
      if (draft) { openedLink.current = requested; setSelected(draft); }
    }
  }, [requested, read.data, selected]);
  const choose = (next: EvidenceDraft | null) => {
    const act = () => {
      // Keep Compare mounted after the Send refetch changes the case's current step.
      if (next) window.location.hash = `#/cases/${encodeURIComponent(d.case_id)}?step=compare`;
      setSelected(next); setDirty(false);
    };
    if (dirty) setPending({ run: act }); else act();
  };
  useEffect(() => {
    if (!dirty) return;
    const leave = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    const navigate = (event: MouseEvent) => {
      if (bypass.current) { bypass.current = false; return; }
      if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const element = event.target instanceof Element ? event.target.closest<HTMLElement>("a[href], nav[aria-label='Steps'] button") : null;
      if (!element || (element instanceof HTMLAnchorElement && element.hash === window.location.hash)) return;
      event.preventDefault(); event.stopImmediatePropagation();
      setPending({ run: () => { bypass.current = true; element.click(); } });
    };
    window.addEventListener("beforeunload", leave);
    document.addEventListener("click", navigate, true);
    return () => { window.removeEventListener("beforeunload", leave); document.removeEventListener("click", navigate, true); };
  }, [dirty]);
  const data = read.data;
  const rows = data?.drafts ?? [];
  const shown = selected && !rows.some(row => row.draft_id === selected.draft_id) ? [...rows, selected] : rows;
  const mismatch = !!data && (data.rec_version !== current.rec_version || data.plan_key !== current.plan_key);
  const denied = !!read.error && [401, 403, 404].includes(read.error.status);
  return <section ref={region} aria-label="Stock checks" className={`flex flex-col gap-2 ${WRAP}`}>
    <h3 className="m-0 text-title font-semibold">Stock checks</h3>
    <p className="m-0 text-label text-tx2">{d.selected_plan && d.selected_plan.plan_key !== d.result?.recommended ? "Manager selection" : "Engine recommendation"}</p>
    <p className="m-0 text-body text-tx2">{DRAFT_EFFECT}</p>
    <a className={DRAFT_LINK} href={checksHref(d.case_id)}>{CHECKS_LINK}</a>
    {!opened && <Button kind="plain" className="self-start max-md:min-h-11" onClick={() => setOpened(true)}>Review stock checks</Button>}
    {read.error && <ErrorLine error={read.error} action={!denied ? { label: "Try again", onClick: read.refresh } : undefined} />}
    {opened && !data && !read.error && <p role="status" className="m-0 text-label">Loading evidence drafts…</p>}
    {data && <p className="m-0 text-label text-tx2">{data.requests_left} {data.requests_left === 1 ? "request" : "requests"} left for this version.</p>}
    {mismatch && <p role="alert" className="m-0 text-body">{DRAFT_CHANGED}</p>}
    {pending && <div role="group" aria-label="Discard unsent edits" className="flex flex-col items-start gap-2 rounded-lg border border-line p-4">
      <p className="m-0 text-body">Discard these edits? Your unsent changes will be lost.</p>
      <div className="flex flex-wrap gap-2">
        <Button kind="danger" onClick={() => { const act = pending.run; setPending(null); setDirty(false); act(); }}>Discard edits</Button>
        <Button kind="ghost" onClick={() => { setPending(null); region.current?.querySelector<HTMLTextAreaElement>("textarea")?.focus(); }}>Keep editing</Button>
      </div>
    </div>}
    {data && !shown.length && <p className={`m-0 text-body text-tx2 ${WRAP}`}>{d.fact_checks.length === 0
      ? "The engine flagged no suspect facts for this version." : `No request drafted.${data.reason ? ` ${data.reason}` : ""}`}</p>}
    {!denied && shown.length > 0 && <ul data-testid="evidence-drafts-list" className="m-0 flex list-none flex-col divide-y divide-line p-0">
      {shown.map(row => {
        const opened = selected?.draft_id === row.draft_id;
        const owner = typeof row.owner === "string" ? row.owner : row.owner.branch;
        const sourceManager = session.role === "manager" && session.branch === d.source;
        return <li key={row.draft_id} data-draft-id={row.draft_id}
          className={`flex flex-col gap-2 py-4 ${WRAP} ${sourceManager && row.can_send && !mismatch ? "border-l-4 border-l-navy-800 pl-4" : ""}`}>
          <p className={`m-0 text-body font-semibold ${WRAP}`}>{row.class} · {row.fact_id}</p>
          <p className={`m-0 text-body ${WRAP}`}>{row.to_branch} · {row.sku} · lot {row.lot_ids.join(", ") || MISSING}</p>
          <p className={`m-0 text-label text-tx2 ${WRAP}`}>Sends: {owner ?? MISSING} manager · Checks: {row.to_branch} manager</p>
          <p className={`m-0 text-label text-tx2 ${WRAP}`}>Drafted by Gemini → Manager sends → Branch checks</p>
          {row.reason && <p className={`m-0 text-body text-tx2 ${WRAP}`}>{row.reason}</p>}
          {!opened && row.request_id ? <a className={DRAFT_LINK} href={`${checksHref(d.case_id)}&request=${encodeURIComponent(row.request_id)}`}>View request</a>
            : <Button kind="plain" className="self-start max-md:min-h-11" aria-expanded={opened} aria-controls={`draft-editor-${row.draft_id}`}
              onClick={() => choose(opened ? null : row)}>{opened ? "Close draft" : "Review draft"}</Button>}
          {opened && selected && <DraftEditor key={selected.draft_id} original={selected} supplied={row} current={current}
            blocked={mismatch || denied} sourceManager={sourceManager} left={data?.requests_left ?? 0}
            reload={() => { read.refresh(); reload(); }} onDirty={setDirty} />}
        </li>;
      })}
    </ul>}
  </section>;
}

function DraftEditor({ original, supplied, current, blocked, sourceManager, left, reload, onDirty }: {
  original: EvidenceDraft; supplied: EvidenceDraft; current: AssistantBinding; blocked: boolean;
  sourceManager: boolean; left: number; reload: () => void; onDirty: (dirty: boolean) => void;
}) {
  const { session, refresh } = useSession();
  const [draft] = useState(original);
  const binding = useReviewedAssistantBinding(current);
  const [message, setMessage] = useState(draft.original_text);
  const [sent, setSent] = useState<EvidenceRequest | null>(null);
  const [source, setSource] = useState<SourceChoice | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; field.current?.focus(); return () => { mounted.current = false; }; }, []);
  useEffect(() => { onDirty(!sent && message !== draft.original_text); }, [message, sent, draft.original_text, onDirty]);
  const write = useAction((body: SendEvidenceDraft, key: string) => sendEvidenceDraft(draft.case_id, body, key));
  const stale = blocked || !sameBinding(draft, current) || supplied.state === "HISTORICAL"
    || ["stale_version", "stale_plan"].includes(write.error?.code ?? "");
  const context = asRecord(draft.request_context);
  const parts = Array.isArray(context.required_parts) ? context.required_parts.filter((part): part is string => typeof part === "string" && !!part.trim()) : [];
  const complete = !!(draft.draft_id && draft.run_id && draft.case_id && draft.rec_version && draft.plan_key && draft.fact_id
    && draft.to_branch && draft.sku && draft.lot_ids.length && parts.length && context.branch === draft.to_branch && context.sku === draft.sku
    && context.fact_id === draft.fact_id && context.class === draft.class
    && Array.isArray(context.lot_ids) && JSON.stringify(context.lot_ids) === JSON.stringify(draft.lot_ids));
  const existing = sent?.request_id ?? supplied.request_id;
  const allowed = !stale && complete && sourceManager && left > 0 && supplied.can_send === true && supplied.read_only !== true && !existing;
  const explanation = stale ? draft.rec_version !== current.rec_version || write.error?.code === "stale_version" ? DRAFT_OLD : DRAFT_PLAN_CHANGED
    : !complete ? "Required request context is missing. Review current checks before sending."
    : !sourceManager ? `Only the ${typeof draft.owner === "string" ? draft.owner : draft.owner.branch} manager can send this request.`
    : left <= 0 ? supplied.reason ?? "No request slots remain for this version. Review current checks and the available stock decision actions."
    : supplied.can_send !== true || supplied.read_only ? supplied.reason ?? "Review current checks before sending." : null;
  const submit = async () => {
    if (!allowed || !message.trim() || write.busy) return;
    const response = await write.run({ draft_id: draft.draft_id, rec_version: draft.rec_version, plan_key: draft.plan_key, message });
    if (!mounted.current || !binding.accepts(draft)) return;
    if (response) { setSent(response); onDirty(false); reload(); void refresh().catch(() => {}); }
    else field.current?.focus();
  };
  const sources = [...new Map(draft.refs.map(ref => [ref.result_id, { result_id: ref.result_id, kind: ref.kind }])).values()];
  const sourceOutput: SourceOutput = { ...draft, output_id: draft.draft_id, kind: "DRAFT", sources };
  return <div id={`draft-editor-${draft.draft_id}`} className={`flex max-h-[75vh] flex-col gap-2 overflow-y-auto overscroll-contain rounded-lg bg-sunken p-4 ${WRAP}`}>
    {explanation && <p role="status" className={`m-0 text-body ${WRAP}`}>{explanation}</p>}
    {stale && <p className={`m-0 text-label text-tx2 ${WRAP}`}>Original draft context · {draft.rec_version} · {draft.plan_key}</p>}
    <section aria-label="Request context" data-testid="draft-request-context" className={`flex flex-col gap-2 ${WRAP}`}>
      <h4 className="m-0 text-body font-semibold">Request context</h4>
      <Kv stacked items={[["Branch", draft.to_branch], ["Product", draft.sku], ["Lots", draft.lot_ids.join(", ") || MISSING]]} />
      {parts.map(part => <p key={part} className={`m-0 text-body ${WRAP}`}>{part}</p>)}
      {!existing && <p className="m-0 text-label text-tx2">{DRAFT_DEADLINE}</p>}
    </section>
    <details className={WRAP}><summary className="cursor-pointer py-2 text-body max-md:min-h-11">Original Gemini draft</summary>
      <CheckedText output={{ text: draft.original_text, refs: draft.refs, sources }} onOpen={setSource} />
      <p className={`m-0 text-label text-tx2 ${WRAP}`}>{count(draft.checks.claims, "number checked", "numbers checked")} · Gemini {draft.model_id}</p>
    </details>
    {existing ? <div className="flex flex-col items-start gap-2">
      <p role="status" className="m-0 text-body font-semibold">{sent || supplied.state === "SENT" ? "Request sent" : "A request already exists for this fact."}</p>
      {sent?.message != null && <p className={`m-0 whitespace-pre-wrap text-body ${WRAP}`}>{sent.message}</p>}
      {sent?.due_at && <p data-testid="sent-deadline" className={`m-0 text-label text-tx2 ${WRAP}`}>Respond by {recordedLine(sent.due_at, session.time_zone)}{sent.status ? ` · ${sent.status}` : ""}</p>}
      <a className={DRAFT_LINK} href={`${checksHref(draft.case_id)}&request=${encodeURIComponent(existing)}`}>View request</a>
    </div> : <form aria-busy={write.busy} className={`flex flex-col gap-2 ${WRAP}`} onSubmit={event => { event.preventDefault(); void submit(); }}>
      <label htmlFor={`message-${draft.draft_id}`} className="text-label font-semibold">Message</label>
      <textarea ref={field} id={`message-${draft.draft_id}`} rows={4} required value={message} readOnly={!allowed || write.busy}
        aria-describedby={`message-help-${draft.draft_id}`} aria-invalid={!message.trim() || !!write.error || undefined}
        onChange={event => setMessage(event.target.value)} className={`w-full max-w-full rounded-md border border-line-2 bg-white p-2 text-body read-only:bg-side ${WRAP}`} />
      <p id={`message-help-${draft.draft_id}`} className="m-0 text-label text-tx2">{message !== draft.original_text ? "Human-authored message" : "Drafted by Gemini"}{!message.trim() ? " · Enter a request message." : ""}</p>
      <div className="flex flex-wrap items-start gap-2">
        <Button kind="primary" type="submit" disabled={!allowed || !message.trim() || write.busy} className="max-md:min-h-11">Send request</Button>
        {write.busy && <p role="status" className="m-0 text-label text-tx2">Sending request…</p>}
        {write.error && errorAction(write.error.code) === "retry" && allowed && <Button disabled={write.busy} onClick={() => void submit()}>Try again</Button>}
      </div>
      <ErrorLine error={write.error} />
      {explanation && <a className={DRAFT_LINK} href={checksHref(draft.case_id)}>Review current checks</a>}
    </form>}
    {source && <ToolResultView output={sourceOutput} choice={source} onClose={() => setSource(null)} />}
  </div>;
}

function BoundDecisionBrief({ d, reload, identity, onSettled }: {
  d: CaseDetail61; reload: () => void; identity: string; onSettled: () => void;
}) {
  const { session } = useSession();
  const planKey = d.selected_plan?.plan_key ?? d.result?.recommended;
  const [view, setView] = useState<BriefView>({ identity, output: null, label: GENERATING });
  const [source, setSource] = useState<{ identity: string; outputId: string; choice: SourceChoice; sequence: number } | null>(null);
  const [trace, setTrace] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const binding = { run_id: session.run_id, case_id: d.case_id, rec_version: d.rec_version, plan_key: planKey };
    const matches = (data: Record<string, unknown>, exact = false) => Object.entries(binding)
      .every(([key, value]) => data[key] === value || (!exact && data[key] === undefined));
    const load = async () => {
      let generating = false;
      try {
        await readBrief(d.case_id, ({ event, data }) => {
          if (controller.signal.aborted || !matches(data)) return;
          if (event === "brief") {
            if (!matches(data, true)) return;
            generating = false;
            if (data.status !== "CURRENT" && data.provenance !== "FALLBACK") {
              setView({ identity, output: null, label: TEMPLATE });
              return;
            }
            setView({ identity, output: data, attempt: data, label: data.provenance === "FALLBACK" ? FALLBACK
              : data.provenance === "PRECOMPUTED" ? "Precomputed" : "Live" });
          } else if (event === "generating" || (event === "state" && data.status === "GENERATING")) {
            generating = true;
            setView({ identity, output: null, label: GENERATING });
          } else if (event === "fallback") {
            generating = false;
            setView({ identity, output: null, attempt: data, label: FALLBACK, error: typeof data.message === "string" ? data.message : undefined });
          } else if (event === "summary" && data.label === TEMPLATE) {
            setView({ identity, output: null, label: TEMPLATE });
          } else if (event === "tool_result") {
            setView(previous => ({ ...previous, identity, progress: "Checking figures against the recorded stock decision" }));
          }
        }, controller.signal);
      } catch (error) {
        if (controller.signal.aborted) return;
        generating = false;
        setView({ identity, output: null, label: FALLBACK, error: error instanceof ApiError ? error.message : UNAVAILABLE });
      }
      // A GET of the server-owned active claim is a read. A refusal/terminal
      // fallback never starts an automatic retry/generation loop.
      if (generating && !controller.signal.aborted) timer = setTimeout(() => void load(), 500);
      else if (!controller.signal.aborted) onSettled();
    };
    if (d.rec_version && planKey && d.result) void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [identity, d.case_id, d.rec_version, planKey, session.run_id, session.role, session.branch, !!d.result, onSettled]);
  const current = view.identity === identity ? view : null;
  const output = current?.output;
  const ai = output && output.provenance !== "FALLBACK";
  const label = d.result ? current?.label ?? GENERATING : TEMPLATE;
  const removed = measured(output?.checks.removed);
  const recommended = d.result?.options.find(option => option.key === d.result?.recommended)?.label;
  const openSource: OpenSource = choice => { if (output) setSource(previous => ({ identity, outputId: output.output_id,
    choice, sequence: (previous?.sequence ?? 0) + 1 })); };
  return <section aria-label="AI brief" className={`flex flex-col gap-2 ${WRAP}`}>
    <h3 id="decision-brief" tabIndex={-1} className="m-0 text-title font-semibold tracking-[-.01em]">{BRIEF_TITLE}</h3>
    <span><Provenance label={label} /></span>
    {d.result && <Kv stacked items={[
      ["Engine recommendation", recommended ?? MISSING],
      ...(d.selected_plan ? [["Manager selection", d.selected_plan.label] as [ReactNode, ReactNode]] : []),
    ]} />}
    {ai && <>
      <p className={`m-0 text-label text-tx2 ${WRAP}`}>Gemini {present(output.model_id)} · {typeof output.created_at === "string" ? recordedLine(output.created_at, session.time_zone) || MISSING : MISSING}</p>
      <CheckedText output={output} onOpen={openSource} />
      <SourceLine output={output} onOpen={openSource} />
      <p className="num m-0 text-label text-tx2">{count(output.checks.claims, "number checked", "numbers checked")}{removed !== null && removed > 0 ? ` · ${removed} removed` : ""}</p>
      <p className="num m-0 text-label text-tx2">{output.provenance === "PRECOMPUTED" ? "0 Gemini calls on page load" : usageLine(output.usage)}</p>
      <details key={output.output_id} onToggle={event => setTrace(event.currentTarget.open)} className={WRAP}>
        <summary className="cursor-pointer py-2 text-label text-navy-800 max-md:min-h-11">Show evidence</summary>
        {trace && <ToolTrace key={identity + output.output_id} output={output} onOpen={openSource} />}
      </details>
    </>}
    {(!ai || (removed !== null && removed > 0)) && <>
      {label === FALLBACK && <span><Provenance label={TEMPLATE} /></span>}
      <p data-testid="template-summary" className={`m-0 whitespace-pre-wrap text-body ${WRAP}`}>
        {d.summary || "This version has no calculated summary. Reload the stock decision."}
      </p>
    </>}
    {!d.result && <Button kind="plain" onClick={reload}>{RELOAD_DECISION}</Button>}
    {label === FALLBACK && <p className={`m-0 text-body text-tx2 ${WRAP}`}>{UNAVAILABLE}</p>}
    {current?.error && <p role="status" className={`m-0 text-body text-tx2 ${WRAP}`}>{current.error}</p>}
    {current?.progress && !output && label !== FALLBACK && <p role="status" className="m-0 text-label text-tx2">{current.progress}</p>}
    {current?.attempt && <GenerationDetails attempt={current.attempt} precomputed={output?.provenance === "PRECOMPUTED"} />}
    {output && source?.identity === identity && source.outputId === output.output_id && <ToolResultView
      key={identity + output.output_id + source.sequence} output={output} choice={source.choice} onClose={() => setSource(null)} />}
  </section>;
}

const ASK_TITLE = "Ask about this stock decision";
const DAILY_LIMIT = "Today's AI limit is reached. It resets at 00:00 Bangkok time.";
const INTERRUPTED_ASK = "Interrupted, ask again";
const EARLIER_RESULT = "This result is for an earlier version.";
type PersonalTurn = AssistantHistory["turns"][number];
type AskBody = AssistantBinding & { question: string };
const sameBinding = (a: AssistantBinding, b: AssistantBinding) => a.run_id === b.run_id && a.case_id === b.case_id
  && a.rec_version === b.rec_version && a.plan_key === b.plan_key;
// Shared with subsequent evidence editors: object refetches never replace the
// original review tuple. Async callbacks consult the latest tuple, not a closed
// over render; the parent privacy key clears all local state on identity change.
export function useReviewedAssistantBinding(current: AssistantBinding) {
  const [reviewed] = useState(() => ({ ...current }));
  const latest = useRef(current);
  latest.current = current;
  return { reviewed, stale: !sameBinding(reviewed, current), accepts: (binding: AssistantBinding) => sameBinding(latest.current, binding) };
}
const orderedTurns = (a: PersonalTurn[], b: PersonalTurn[]) => [...new Map([...a, ...b].map(turn => [turn.output_id, turn])).values()]
  .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.output_id.localeCompare(a.output_id));

function QuestionTools({ rows }: { rows: AssistantToolProgress[] }) {
  return <ol aria-label="Question tool progress" className={`m-0 flex list-inside list-decimal flex-col gap-1 p-0 text-label ${WRAP}`}>
    {rows.map(row => <li key={row.seq} className={WRAP}>
      <span>{typeof row.purpose === "string" ? row.purpose : toolName(row.tool)}</span>
      <span> · {["Waiting", "Working", "Complete", "Failed"].includes(row.state) ? row.state : MISSING} · {seconds(row.duration_ms)} s</span>
    </li>)}
  </ol>;
}

function AnswerGroup({ turn, current, d, reload }: { turn: PersonalTurn; current: AssistantBinding; d: CaseDetail61; reload: () => void }) {
  const { session } = useSession();
  const [source, setSource] = useState<{ choice: SourceChoice; sequence: number } | null>(null);
  const [trace, setTrace] = useState(false);
  const historical = !sameBinding(turn, current) || turn.status === "HISTORICAL" || turn.read_only === true;
  const removed = measured(turn.checks.removed);
  const ai = turn.provenance !== "FALLBACK";
  return <article data-testid="answer-group" id={`answer-${turn.output_id}`} tabIndex={-1}
    className={`flex flex-col gap-2 border-t border-line pt-4 ${WRAP}`}>
    <h4 className={`m-0 whitespace-pre-wrap text-body font-semibold ${WRAP}`}>{turn.question}</h4>
    {historical && <p className="m-0 text-label text-tx2">For an earlier version · {turn.rec_version} · {turn.plan_key}</p>}
    <span><Provenance label={ai ? turn.provenance === "PRECOMPUTED" ? "Precomputed" : "Live" : FALLBACK} /></span>
    {ai && <p className={`m-0 text-label text-tx2 ${WRAP}`}>Gemini {present(turn.model_id)} · {recordedLine(turn.created_at, session.time_zone) || MISSING}</p>}
    <CheckedText output={turn} onOpen={choice => setSource(previous => ({ choice, sequence: (previous?.sequence ?? 0) + 1 }))} />
    {(!ai || (removed !== null && removed > 0)) && <p className={`m-0 whitespace-pre-wrap text-body text-tx2 ${WRAP}`}>
      {historical ? "Review current options for the current engine summary." : d.summary}
    </p>}
    {!ai && <p className="m-0 text-body text-tx2">{UNAVAILABLE}</p>}
    {turn.cards.filter(card => card.result && card.result_id && card.overrides?.length && sameBinding(card, turn)
      && turn.sources.some(source => source.result_id === card.result_id)).map(card =>
      <WhatIfCard key={card.result_id} card={card} current={current} d={d} reload={reload} historical={historical}
        onSource={() => setSource(previous => ({ choice: { resultId: card.result_id }, sequence: (previous?.sequence ?? 0) + 1 }))} />)}
    <SourceLine output={turn} onOpen={choice => setSource(previous => ({ choice, sequence: (previous?.sequence ?? 0) + 1 }))} />
    {ai && <p className="num m-0 text-label text-tx2">{count(turn.checks.claims, "number checked", "numbers checked")}{removed !== null && removed > 0 ? ` · ${removed} removed` : ""}</p>}
    <p className="num m-0 text-label text-tx2">{usageLine(turn.usage)}</p>
    <details onToggle={event => setTrace(event.currentTarget.open)} className={WRAP}>
      <summary className="cursor-pointer py-2 text-label text-navy-800 max-md:min-h-11">Show evidence</summary>
      {trace && <ToolTrace output={turn} onOpen={choice => setSource(previous => ({ choice, sequence: (previous?.sequence ?? 0) + 1 }))} />}
    </details>
    <GenerationDetails attempt={turn} />
    {source && <ToolResultView key={turn.output_id + source.sequence} output={turn} choice={source.choice} onClose={() => setSource(null)} />}
  </article>;
}

function AskBox({ d, reload }: { d: CaseDetail61; reload: () => void }) {
  const { session } = useSession();
  const current: AssistantBinding = { run_id: session.run_id, case_id: d.case_id, rec_version: d.rec_version ?? "",
    plan_key: d.selected_plan?.plan_key ?? d.result?.recommended ?? "" };
  const binding = useReviewedAssistantBinding(current);
  const bindingKey = JSON.stringify(current);
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<PersonalTurn[]>([]);
  const [rows, setRows] = useState<AssistantToolProgress[]>([]);
  const [stage, setStage] = useState("Checking this stock decision…");
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [ready, setReady] = useState<string | null>(null);
  const [historyRequested, setHistoryRequested] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const historySequence = useRef(0);
  const mounted = useRef(true);
  const controller = useRef<AbortController | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; historySequence.current++; controller.current?.abort(); };
  }, []);
  useEffect(() => {
    if (field.current) { field.current.style.height = "auto"; field.current.style.height = `${field.current.scrollHeight}px`; }
  }, [question]);
  const loadHistory = async (older?: string) => {
    const sequence = ++historySequence.current;
    setHistoryRequested(true);
    setHistoryBusy(true);
    setHistoryError(null);
    try {
      const result = await getAssistantHistory(d.case_id, older);
      if (!mounted.current || sequence !== historySequence.current || !binding.accepts(current)) return;
      const authorized = result.turns.filter(turn => turn.kind === "ANSWER" && turn.run_id === current.run_id && turn.case_id === current.case_id);
      setTurns(previous => orderedTurns(previous, authorized));
      setCursor(result.next_cursor);
      setTotal(measured(result.total));
      if (older) setExpanded(true);
    } catch (error) {
      if (!mounted.current || sequence !== historySequence.current || !binding.accepts(current)) return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) setTurns([]);
      setHistoryError(error instanceof ApiError ? error.message : "Please try again.");
    } finally {
      if (mounted.current && sequence === historySequence.current) setHistoryBusy(false);
    }
  };
  const ask = useAction(async (body: AskBody, key: string) => {
    const stream = new AbortController();
    controller.current = stream;
    let output: AssistantOutput | null = null;
    try {
      await askAssistant(body.case_id, body.question, key, event => {
        if (!mounted.current || stream.signal.aborted || !binding.accepts(body)) return;
        if (event.event === "tool" || event.event === "tool_result") {
          const row = event.data;
          if (typeof row.seq === "number" && Number.isFinite(row.seq)) setRows(previous =>
            previous.some(old => old.seq === row.seq) ? previous : [...previous, row].sort((a, b) => a.seq - b.seq));
        } else if (event.event === "state" && event.data.state === "Checking numbers") setStage("Checking numbers…");
        else if (event.event === "answer") {
          if (!sameBinding(event.data, body) || event.data.status === "HISTORICAL") {
            throw new ApiError(409, "stale_plan", EARLIER_RESULT);
          }
          output = event.data;
        }
      }, stream.signal);
      if (!output) throw new ApiError(0, "interrupted", INTERRUPTED_ASK);
      return { output: output as AssistantOutput, interrupted: null };
    } catch (error) {
      // A dropped attempt is terminal. Ask again is a new, explicit human Ask,
      // never a stream replay. Unacknowledged HTTP retries keep useAction's key.
      if (error instanceof ApiError && error.code === "interrupted") return { output: null, interrupted: error };
      throw error;
    }
  });
  useEffect(() => {
    controller.current?.abort();
    historySequence.current++;
    setHistoryBusy(false);
    setReady(null);
    setRows([]);
  }, [bindingKey]);
  const error = failure ?? ask.error;
  const capped = error?.code === "cap_reached";
  const serverStale = ["stale_version", "stale_plan", "wrong_state"].includes(error?.code ?? "");
  const authorized = !!d.result && !!current.rec_version && !!current.plan_key && !binding.stale && !serverStale;
  const submit = async () => {
    if (!question.trim() || !authorized || ask.busy || capped) return;
    const body = { ...binding.reviewed, question };
    setFailure(null);
    setReady(null);
    setRows([]);
    setStage("Checking this stock decision…");
    const result = await ask.run(body);
    if (!mounted.current || !result || !binding.accepts(body)) return;
    if (result.interrupted) { setFailure(result.interrupted); return; }
    if (result.output) {
      const turn = { ...result.output, question: body.question };
      setTurns(previous => orderedTurns(previous, [turn]));
      setReady(turn.output_id);
      void loadHistory();
    }
  };
  const visible = expanded ? turns : turns.slice(0, 3);
  const hasEarlier = !!cursor || (!expanded && turns.length > 3);
  const earlier = total === null ? null : Math.max(0, total - visible.length);
  const retryable = error && !capped && (error.code === "interrupted" || ["network", "engine_busy", "engine_timeout", "db_busy"].includes(error.code));
  return <section aria-label={ASK_TITLE} className={`mt-4 flex flex-col gap-4 border-t border-line pt-4 ${WRAP}`}>
    <h3 className="m-0 text-title font-semibold">{ASK_TITLE}</h3>
    <p className="m-0 text-label text-tx2">Gemini drafts. Managers send requests and authorize business actions.</p>
    <form noValidate onSubmit={event => { event.preventDefault(); void submit(); }} className={`flex flex-col items-start gap-2 ${WRAP}`}>
      <div className="flex flex-wrap gap-2">
        {["Compare these options", "What needs checking?", ...(d.result?.options.some(option => option.run_code) ? ["What if this run is unavailable?"] : [])].map(prompt =>
          <Button key={prompt} kind="ghost" disabled={ask.busy || !authorized} onClick={() => { setQuestion(prompt); if (!historyRequested) void loadHistory(); }}>{prompt}</Button>)}
      </div>
      <label htmlFor={`ask-${d.case_id}`} className="text-label font-semibold">Your question</label>
      <textarea id={`ask-${d.case_id}`} ref={field} rows={3} value={question} readOnly={ask.busy || !authorized}
        aria-describedby={`ask-help-${d.case_id}`} onFocus={() => { if (!historyRequested) void loadHistory(); }}
        onChange={event => setQuestion(event.target.value)} onKeyDown={event => {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void submit(); }
        }} className={`w-full resize-y rounded-md border border-line-2 bg-white p-2 text-body text-tx read-only:bg-side ${WRAP}`} />
      <p id={`ask-help-${d.case_id}`} className="m-0 text-label text-tx2">{question.trim() ? "Ask about this decision's stock, forecasts, options or checks." : "Enter a question."}</p>
      {binding.stale || serverStale ? <div className={`flex flex-col gap-1 ${WRAP}`}>
        <p className="m-0 text-body">{binding.reviewed.rec_version === current.rec_version && error?.code !== "stale_version"
          ? "Plan changed · review current checks. Your edits are kept here for reference."
          : "This result is for an earlier version. Your edits are kept here for reference."}</p>
        <p className={`m-0 text-label text-tx2 ${WRAP}`}>Original question context · {binding.reviewed.rec_version} · {binding.reviewed.plan_key}</p>
        <a className="py-2 text-label text-navy-800 underline" href={`#/cases/${encodeURIComponent(d.case_id)}?step=context`}>Review current checks</a>
      </div> : !authorized && <p className="m-0 text-body text-tx2">This stock decision needs current calculated options before you can ask. Reload the stock decision.</p>}
      <Button kind="primary" type="submit" busy={ask.busy} disabled={!question.trim() || !authorized || capped}>Ask question</Button>
    </form>
    {[...visible].reverse().map(turn => <AnswerGroup key={turn.output_id} turn={turn} current={current} d={d} reload={reload} />)}
    {error && <p role="alert" className={`m-0 text-body ${WRAP}`}>{capped && error.extra.scope === "daily" ? DAILY_LIMIT : error.message}</p>}
    {retryable && <Button kind="plain" disabled={ask.busy || !authorized} onClick={() => void submit()}>{error.code === "interrupted" ? "Ask again" : "Try again"}</Button>}
    {ask.busy && <div className={`flex flex-col gap-2 ${WRAP}`}>
      <p role="status" className="m-0 text-label text-tx2">{stage}</p>
      <QuestionTools rows={rows} />
    </div>}
    {ready && <div role="status" className="flex flex-wrap items-center gap-2 text-label">
      <span>Answer ready</span><Button kind="ghost" onClick={() => document.getElementById(`answer-${ready}`)?.focus()}>Go to answer</Button>
    </div>}
    {!historyRequested && <Button kind="plain" onClick={() => void loadHistory()}>Show your questions</Button>}
    {historyBusy && <p role="status" className="m-0 text-label text-tx2">Loading your questions…</p>}
    {historyError && <div className={WRAP}><p role="alert" className="m-0 text-body">Your questions didn't load. {historyError}</p>
      <Button kind="plain" disabled={historyBusy} onClick={() => void loadHistory()}>Try again</Button></div>}
    {historyRequested && !historyBusy && !historyError && turns.length === 0 && <div>
      <h4 className="m-0 text-body font-semibold">No questions yet</h4>
      <p className="m-0 text-body text-tx2">Ask about the options or what needs checking. Your questions are visible only to you.</p>
    </div>}
    {hasEarlier && <Button kind="plain" busy={historyBusy} onClick={() => { setExpanded(true); if (cursor) void loadHistory(cursor); }}>
      Show earlier questions{earlier !== null && earlier > 0 ? ` (${earlier})` : ""}</Button>}
  </section>;
}

const ADOPTABLE_ASSUMPTIONS = new Set(["transport_quote", "handling_cost", "markdown_response", "demand_multiplier", "disposal_cost"]);
const NOT_PROVIDED = "Not provided";
const ADOPTION_EFFECT = "This creates a new recommendation version and cancels pending approvals. Facts stay unverified and lot holds stay in place.";
type ReviewedAdoption = { overrides: AssistantWhatIf["overrides"]; note: string; rec_version: string; plan_key: string };
const finite = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;

function WhatIfCard({ card: supplied, current, d, reload, historical, onSource }: {
  card: AssistantWhatIf; current: AssistantBinding; d: CaseDetail61; reload: () => void; historical: boolean; onSource: () => void;
}) {
  const { session } = useSession();
  const [card] = useState(supplied);
  const binding = useReviewedAssistantBinding(current);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [discarded, setDiscarded] = useState(false);
  const [review, setReview] = useState(false);
  const [note, setNote] = useState("");
  const [applied, setApplied] = useState<string | null>(null);
  const [reviewAfter, setReviewAfter] = useState<CaseDetail61 | null>(null);
  const noteField = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (review) noteField.current?.focus(); }, [review]);
  const write = useAction((body: ReviewedAdoption, key: string) => post<{ rec_version: string }>(
    `/api/cases/${encodeURIComponent(card.case_id)}/adopt`, body, key));
  useEffect(() => {
    if (reviewAfter && d !== reviewAfter) {
      if (sameBinding(card, current)) write.clear();
      setReviewAfter(null);
    }
  }, [d, reviewAfter, card, current, write.clear]);
  const supported = supplied.adoptable !== false && card.overrides.length > 0
    && card.overrides.every(override => ADOPTABLE_ASSUMPTIONS.has(override.kind));
  const stale = historical || supplied.read_only === true || !sameBinding(card, current)
    || ["stale_version", "stale_plan"].includes(write.error?.code ?? "");
  const sourceManager = session.role === "manager" && session.branch === d.source;
  const live = ["OPEN", "WAITING", "PENDING_APPROVAL"].includes(d.state);
  const allowed = supported && !stale && sourceManager && live && supplied.can_adopt !== false && !applied && !reviewAfter;
  const result = asRecord(card.result);
  const options: Record<string, unknown>[] = Array.isArray(result.options) ? result.options.map(asRecord)
    : Object.entries(asRecord(result.options)).map(([key, value]) => ({ ...asRecord(value), key }));
  const option = options.find(option => option.key === result.recommended) ?? {};
  const net = finite(asRecord(option.net).base);
  const waste = finite(option.wasted_kg);
  const unserved = finite(option.unserved);
  const feasibility = option.feasible ?? result.feasible;
  const failures = [result.reason, ...(Array.isArray(result.failures) ? result.failures : []),
    ...(Array.isArray(option.failures) ? option.failures : [])].filter((value): value is string => typeof value === "string" && !!value);
  const apply = async () => {
    if (!allowed || !note.trim() || write.busy) return;
    const response = await write.run({ overrides: card.overrides, note, rec_version: card.rec_version, plan_key: card.plan_key });
    if (response && mounted.current && binding.accepts(card)) { setApplied(response.rec_version); reload(); }
  };
  if (discarded) return null;
  return <section data-testid="whatif-card" aria-label="What-if comparison" className={`flex flex-col gap-2 rounded-lg border border-dashed border-line-2 bg-sunken p-4 ${WRAP}`}>
    <h5 className="m-0 text-body font-semibold">{applied ? "Assumptions applied" : "What-if · not applied"}</h5>
    <p className={`m-0 text-label text-tx2 ${WRAP}`}>Baseline version · {card.rec_version}<br />Plan · {card.plan_key}<br />Result · {card.result_id}</p>
    <div className={`flex flex-col gap-2 ${WRAP}`} aria-label="Supplied assumptions">
      {card.overrides.map((override, i) => <div key={i} className={WRAP}>
        <h6 className="m-0 text-label font-semibold">{fieldLabel(override.kind)}</h6>
        <Values value={safeDetail(Object.fromEntries(Object.entries(override).filter(([key]) => key !== "kind")))} />
      </div>)}
    </div>
    <Kv stacked items={[
      ["Engine comparison", typeof option.label === "string" ? option.label : NOT_PROVIDED],
      ["Net revenue, base", net === null ? NOT_PROVIDED : money(net, session.currency)],
      ["Waste", waste === null ? NOT_PROVIDED : `${units(waste)} kg`],
      ["Unserved", unserved === null ? NOT_PROVIDED : `${expected(unserved)} units`],
      ["Feasibility", typeof feasibility === "boolean" ? feasibility ? "Passes" : "Does not pass" : typeof feasibility === "string" ? feasibility : NOT_PROVIDED],
    ]} />
    {failures.map((failure, i) => <p key={i} className={`m-0 text-body ${WRAP}`}>{failure}</p>)}
    {options.length > 0 && <details className={WRAP}><summary className="cursor-pointer py-2 text-label text-navy-800 max-md:min-h-11">All compared options</summary>
      <div tabIndex={0} aria-label="What-if engine options" className="min-w-0 max-w-full overflow-x-auto">
        <div className="min-w-max"><Table head={["Plan", "Net revenue, base", "Waste"]} rows={options.map(option => [present(option.label),
          finite(asRecord(option.net).base) === null ? NOT_PROVIDED : money(finite(asRecord(option.net).base), session.currency),
          finite(option.wasted_kg) === null ? NOT_PROVIDED : `${units(finite(option.wasted_kg))} kg`])} /></div>
      </div>
    </details>}
    <button type="button" className={SOURCE_BUTTON} onClick={onSource}>Source: What-if comparison</button>
    {!supported && <p className={`m-0 text-body ${WRAP}`}>These changes are for comparison only.</p>}
    {stale && !applied && <p className={`m-0 text-body ${WRAP}`}>{EARLIER_RESULT}</p>}
    {!sourceManager && <p className={`m-0 text-body ${WRAP}`}>Only the {d.source} manager can apply these assumptions.</p>}
    {!live && !applied && <p className="m-0 text-body">This stock decision no longer accepts assumptions. Review current options.</p>}
    {!supported && <a className="py-2 text-label text-navy-800 underline" href={`#/cases/${encodeURIComponent(d.case_id)}?step=context`}>Review current checks</a>}
    {(stale || !live) && !applied && <Button kind="plain" onClick={() => { setReviewAfter(d); reload(); }}>Review current options</Button>}
    <div className="flex flex-wrap gap-2">
      <Button kind="plain" disabled={!allowed || write.busy} onClick={() => setReview(true)}>Use these assumptions…</Button>
      <Button kind="ghost" disabled={write.busy} onClick={() => setDiscarded(true)}>Discard</Button>
    </div>
    {review && <form noValidate onSubmit={event => { event.preventDefault(); void apply(); }} className={`flex flex-col items-start gap-2 border-t border-line pt-2 ${WRAP}`}>
      <p className={`m-0 text-body ${WRAP}`}>{ADOPTION_EFFECT}</p>
      <label className="text-label font-semibold" htmlFor={`source-${card.result_id}`}>Source of this assumption (required)</label>
      <textarea id={`source-${card.result_id}`} ref={noteField} rows={3} value={note} readOnly={write.busy || stale || !!applied}
        onChange={event => setNote(event.target.value)} className={`w-full resize-y rounded-md border border-line-2 bg-white p-2 text-body read-only:bg-side ${WRAP}`} />
      {!note.trim() && <p className="m-0 text-label text-tx2">Add the source of this assumption.</p>}
      <Button kind="primary" type="submit" busy={write.busy} disabled={!allowed || !note.trim()}>Apply assumptions</Button>
      {write.error && <p role="alert" className={`m-0 text-body ${WRAP}`}>{write.error.message}</p>}
      {write.error && allowed && ["network", "engine_busy", "engine_timeout", "db_busy"].includes(write.error.code)
        && <Button kind="plain" disabled={write.busy} onClick={() => void apply()}>Try again</Button>}
    </form>}
    {applied && <p role="status" className={`m-0 text-body font-semibold ${WRAP}`}><span>Assumptions applied · new version</span><span className="block font-mono text-label">{applied}</span></p>}
  </section>;
}
