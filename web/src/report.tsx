// Report an observation (07-20; 07-CONTEXT D-13, D-14, D-16, D-20, D-22; 07-UI-SPEC surface 5): the sheet a store
// manager (or HQ, transport only) files a field report in, by hand. Kind → Details → Preview on one scrolling page:
// - the kind tiles (HQ sees Transport only); the chosen kind's fields only, each required one marked "(required)";
//   every date a scenario date (G4); "When did you check?" on a count and a printed date, with no default (D-22);
// - the store is the signed-in store, read-only; the product and lot come from the stock decision or stock record the
//   sheet was opened from;
// - "Preview impact" asks the server what the report would change if confirmed (POST /api/signals/preview, the 07-20
//   contract row), labelled "Potential impact · unconfirmed" in the dashed plan tone; an edit after it dims it with
//   "Details changed · preview again". The preview is never required to submit and changes nothing (D-16);
// - the sticky footer: the workspace's allowance from the API (D-20; never a literal and never the cross-workspace
//   upload cap), Submit report (disabled at none left) and Close without submitting. A hold the report would place
//   (a damage or safety condition with "Hold this lot for review", or mixed printed dates) goes through its confirm
//   step first. A refusal shows word for word; cap_reached has no Try again.
// A dialog: focus is trapped inside, Esc closes it (through "Discard this report?" when something was typed), and
// focus returns to the control that opened it. Full screen below 768px (06.1's phone width), 560px beside the page
// above it, always under the shell's top block (--shell-top), so the top bar stays.
// The entry points are here too, so a page that offers the sheet adds one line: Business events' header button,
// ReportFromLot (a Stock & lots row) and ReportFromDecision (the stock decision's Context step header).
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ApiError, readReport, type Reading, type Session } from "./api.ts";
import { beginSummary } from "./impactsummary.tsx";
import { NO_CHECK_TIME, REPORT_READING as R, checkTimeObserved, retryable, type CheckTimeValue } from "./copy.ts";
import { sheetPath, rowState, reviewProgress, highlightRange, fieldSources, type ReviewRow } from "./readingview.ts";
import type { CaseDetail61, LotRow } from "./contract.ts";
import type { StepProps } from "./decision/types.ts";
import { useAction } from "./hooks.ts";
import {
  ADD_FILE, CHECKED_KINDS, CHOICES, CLOSE_WITHOUT, DATE_FIELDS, DATE_HELP, DISCARD, DISCARD_SENTENCE, DISPLAY_HELP,
  DONT_HOLD, FIELD_LABEL, FILE_HELP, GO_BACK, HOLD_BOX, HOLD_HELP, INCOMPLETE, KEEP_EDITING, KIND_HINT, KIND_ICON,
  KIND_LABEL, KIND_LEGEND, MIXED, MIXED_DATES, NOT_ALLOWED_IF, NOT_CHECKED, NO_AFFECTED, NO_FILES_LEFT,
  NUMBER_FIELDS, PLACE_HOLD, PLAN_CHANGES, PREVIEWING, PREVIEW_HEAD, PREVIEW_IMPACT, PREVIEW_STALE, REMOVE,
  REPORT_AN_OBSERVATION, REQUIRED, SCENARIO_DATE, SEND_AND_HOLD, STEPS, SUBMIT, allowanceLine,
  benefitLine, draftPreview, fieldsBody, fieldsOf, fileProblem, fileReport, fileSize, holdSentence, kindsFor,
  listReports, middle, missingOf, mixedSentence, nothingChanges, ownerName, ownerOf, requiredOf,
  type Allowance, type Preview, type Report, type ReportFields, type ReportKind,
} from "./reportcopy.ts";
import { useSession } from "./shell.tsx";
import { Button, CLOSE, CheckTime, ErrorLine, Icon, Pill, TRY_AGAIN } from "./ui.tsx";

// What the sheet starts with: the stock decision or stock record it was opened from, or a report it corrects.
export type SheetStart = {
  initialFile?: File | null;
  questionText?: string;
  caseContext?: boolean;
  focusFile?: boolean;
  kind?: ReportKind;
  fields?: ReportFields;
  product?: string | null; // the product code, shown read-only (and a demand event's product)
  lockLot?: boolean; // opened from a stock record or a stock decision's own lot: the lot stays as given
  caseId?: string; // the stock decision it was opened from (the report's case_id)
  corrects?: string; // a confirmed report this one corrects (D-15; not for a count, a printed date or a condition)
};

const todayIso = (s: Pick<Session, "calendar" | "day">) => s.calendar.find((c) => c.day === s.day)?.date ?? "";
const asText = (v: unknown) => (v === null || v === undefined ? "" : String(v));
const asError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "network", String(e)));

// --- an inline second step for a consequential action (07-UI-SPEC ConfirmStep; never a modal) -----------------------
// Kept in this module: the Tasks lane (07-19) adds the shared ui.tsx primitive in parallel; merge them afterwards.
export function InlineConfirm({ sentence, confirm, dismiss, danger = true, busy = false, onConfirm, onDismiss, children }: {
  sentence: string; confirm: string; dismiss: string; danger?: boolean; busy?: boolean; onConfirm: () => void;
  onDismiss: () => void; children?: ReactNode;
}) {
  const said = useRef<HTMLParagraphElement>(null);
  useEffect(() => { said.current?.focus(); }, []);
  return (
    <div className="flex min-w-0 flex-col gap-2 rounded-lg border border-line-2 bg-sunken p-2"
      onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onDismiss(); } }}>
      <p ref={said} tabIndex={-1} className="m-0 text-label break-words text-ink outline-none">{sentence}</p>
      {children}
      <div className="flex flex-wrap items-center gap-2">
        <Button kind={danger ? "danger" : "primary"} busy={busy} onClick={onConfirm} className="max-md:min-h-11">{confirm}</Button>
        <Button kind="ghost" disabled={busy} onClick={onDismiss} className="max-md:min-h-11">{dismiss}</Button>
      </div>
    </div>
  );
}

// --- the file control (07-UI-SPEC FilePick: "Add file", the chip, Remove; the client's precheck is a courtesy) --------
function FileField({ file, onFile, disabled, note }: {
  file: File | null; onFile: (f: File | null) => void; disabled: boolean; note: string | null;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const help = useId();
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <input ref={input} type="file" accept="image/png,image/jpeg,application/pdf" className="sr-only" tabIndex={-1}
        aria-hidden="true" disabled={disabled}
        onChange={(e) => {
          const f = e.target.files?.[0] ?? null;
          e.target.value = "";
          if (f === null) return;
          const bad = fileProblem(f);
          setProblem(bad);
          if (bad === null) onFile(f);
        }} />
      {file === null
        ? <Button data-file-pick kind="ghost" icon="download" disabled={disabled} aria-describedby={help} onClick={() => input.current?.click()}
          className="w-fit border-line-2 max-md:min-h-11">{ADD_FILE}</Button>
        : (
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="min-w-0 rounded-md bg-sunken px-2 py-1 text-label text-ink" title={file.name}>
              {middle(file.name)} · {fileSize(file.size)}
            </span>
            <Button kind="ghost" disabled={disabled} aria-label={`${REMOVE} file ${file.name}`} onClick={() => onFile(null)} className="max-md:min-h-11">{REMOVE}</Button>
          </span>
        )}
      <p id={help} className="m-0 text-label text-ink-2">{note ?? FILE_HELP}</p>
      {problem !== null && <p role="alert" className="m-0 text-label text-bad">{problem}</p>}
    </div>
  );
}

// --- one field of the chosen kind -------------------------------------------------------------------------------------

const box = "h-8 w-full min-w-0 rounded-md border border-line-2 bg-white px-2 text-body text-tx read-only:bg-sunken disabled:bg-sunken max-md:h-11";

export function FieldInput({ name, value, required, readOnly, invalid, onChange }: {
  name: string; value: string; required: boolean; readOnly: boolean; invalid: boolean; onChange: (v: string) => void;
}) {
  const id = useId();
  const helpId = `${id}-help`;
  const date = DATE_FIELDS.has(name);
  const label = (
    <label htmlFor={id} className="text-label text-ink-2">
      {FIELD_LABEL[name] ?? name}{date && <span>{SCENARIO_DATE}</span>}{required && REQUIRED}
    </label>
  );
  const common = { id, "aria-invalid": invalid || undefined, "aria-describedby": date ? helpId : undefined };
  let control: ReactNode;
  if (CHOICES[name] !== undefined) {
    control = (
      <select {...common} value={value} disabled={readOnly} onChange={(e) => onChange(e.target.value)} className={box}>
        <option value="">Choose…</option>
        {CHOICES[name].map(([v, text]) => <option key={v} value={v}>{text}</option>)}
      </select>
    );
  } else if (NUMBER_FIELDS.has(name)) {
    control = <input {...common} type="number" inputMode="decimal" min={0} step={name === "free_totes" ? 1 : "any"}
      placeholder={name === "shelf_units" || name === "backroom_units" ? NOT_CHECKED : undefined}
      value={value} readOnly={readOnly} onChange={(e) => onChange(e.target.value)} className={`num ${box}`} />;
  } else {
    control = <input {...common} type={date ? "date" : "text"} value={value} readOnly={readOnly}
      onChange={(e) => onChange(e.target.value)} className={`${date ? "num " : "font-mono "}${box}`} />;
  }
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {label}
      {control}
      {date && <p id={helpId} className="m-0 text-label text-ink-2">{DATE_HELP}</p>}
    </div>
  );
}

// --- the preview panel (07-UI-SPEC Step 3) -----------------------------------------------------------------------------

export function PreviewPanel({ preview, stale, owner, currency, children, rowPrefix }: {
  preview: Preview | null; stale: boolean; owner: string; currency: string; children?: ReactNode; rowPrefix?: string;
}) {
  const shown = preview?.cases ?? [];
  return (
    <div data-testid="preview" className={`flex min-w-0 flex-col gap-2 rounded-lg border border-dashed border-plan-line bg-sunken p-4 ${stale ? "opacity-60" : ""}`}>
      {preview !== null && <p className="m-0"><Pill tone="plan" icon="info">{preview.label}</Pill></p>}
      {stale && <p className="m-0 text-label font-semibold text-ink">{PREVIEW_STALE}</p>}
      {preview !== null && preview.message !== null && <p className="m-0 text-body text-ink">{preview.message}</p>}
      {preview !== null && preview.message === null && shown.length === 0 && <p className="m-0 text-body text-ink">{NO_AFFECTED}</p>}
      {shown.length > 0 && (
        <div className="min-w-0 overflow-x-auto">
          <table className="w-full border-collapse text-label">
            <thead><tr>{PREVIEW_HEAD.map((h) => <th key={h} scope="col" className="px-2 py-1 text-left font-semibold text-tx3">{h}</th>)}</tr></thead>
            <tbody>
              {shown.map((c) => (
                <tr key={c.case_id} id={rowPrefix ? `${rowPrefix}-${c.case_id}` : undefined} data-engine-case={c.case_id} tabIndex={rowPrefix ? -1 : undefined} className="align-top">
                  <td className="px-2 py-1"><a href={`#/cases/${encodeURIComponent(c.key)}`} className="font-mono text-navy-800 underline underline-offset-2">{c.key}</a></td>
                  <td className="px-2 py-1 break-words">{c.plan_now ?? "—"}</td>
                  <td className="px-2 py-1 break-words">
                    {c.error ?? c.plan_if ?? "—"}
                    {c.changed && <span className="ml-1"><Pill tone="warn" icon="alert">{PLAN_CHANGES}</Pill></span>}
                    {c.allowed === false && <span className="ml-1"><Pill tone="bad" icon="lock">{NOT_ALLOWED_IF}</Pill></span>}
                  </td>
                  <td className="num px-2 py-1 whitespace-nowrap">{benefitLine(c.benefit_now, c.benefit_if, currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {preview !== null && preview.more > 0 && <p className="m-0 text-label text-ink-2">{preview.more_text}</p>}
      {children}
      <p className="m-0 text-label text-ink-2">{nothingChanges(owner)}</p>
    </div>
  );
}

// --- the sheet ----------------------------------------------------------------------------------------------------

export function SourceExcerpt({source, text, onActive}: {source: string | null | undefined; text: string | null | undefined; onActive: (text: string) => void}) {
  const [expanded, setExpanded] = useState(false);
  if (!text) return null;
  const prefix = source === "photo" ? "Read from photo: " : source === "file" ? "From the file: " : source === "calculated" ? "Calculated from: " : "From your message: ";
  return <button type="button" aria-expanded={expanded} title={text} onFocus={() => onActive(text)} onMouseEnter={() => onActive(text)}
    onClick={() => {setExpanded(!expanded); onActive(text);}}
    className={`block w-full min-w-0 text-left text-label text-ink-2 ${expanded ? "[overflow-wrap:anywhere]" : "truncate"}`}>{prefix}{text}</button>;
}

export function EvidenceStep({children}: {children: ReactNode}) {
  return <section data-evidence className="flex min-w-0 flex-col gap-4">{children}</section>;
}

export function SuggestionRow({row, children, onChoice, onActive, required = false, invalid = false}: {
  row: ReviewRow; children: ReactNode; onChoice: (choice: ReviewRow["choice"]) => void; onActive: (text: string) => void; required?: boolean; invalid?: boolean;
}) {
  const state = rowState(row), name = row.field === "kind" ? "Report kind" : FIELD_LABEL[row.field] ?? row.field;
  const ref = useRef<HTMLLIElement>(null);
  const [editing, setEditing] = useState(false);
  const labels: Record<string, string> = {suggested: "Suggested by Gemini", conflict: "Differs from what you typed", accepted: "Accepted", edited: "Edited by you", typed: "Typed by you", needs_input: "Needs your input", cleared: "Cleared"};
  const control = editing || state.state === "needs_input" || state.state === "cleared";
  const choose = (choice: ReviewRow["choice"]) => {
    if (choice === "edit") {setEditing(true); requestAnimationFrame(() => ref.current?.querySelector<HTMLElement>("input,select,textarea")?.focus());}
    else {setEditing(false); onChoice(choice); requestAnimationFrame(() => ref.current?.querySelector<HTMLElement>("button")?.focus());}
  };
  return <li ref={ref} tabIndex={-1} data-field={row.field} aria-label={`${name}: ${asText(state.value)} · ${labels[state.state]}`}
    onFocus={event => {onActive(row.suggestion?.excerpt ?? ""); if (event.target instanceof HTMLElement && event.target.matches("input,select,textarea")) setEditing(true);}} onMouseEnter={() => onActive(row.suggestion?.excerpt ?? "")}
    className={`min-w-0 border-b border-line p-4 [overflow-wrap:anywhere] ${state.needsReview && !state.reviewed ? "border-l-4 border-l-navy-800" : ""}`}>
    <div className="flex flex-wrap justify-between gap-2"><span className="font-semibold">{name}{required && REQUIRED}</span><span className="text-label">{labels[state.state]}</span></div>
    {control ? children : <p className="my-2 font-mono">{state.state === "conflict" ? `You typed: ${asText(row.typed)} · Gemini read: ${asText(row.suggestion?.value)}` : row.field === "kind" ? KIND_LABEL[state.value as ReportKind] : asText(state.value)}</p>}
    {required && state.value == null && <p className="text-label text-ink-2">Needs your input</p>}
    {invalid && <p role="alert" className="text-label text-bad">{R.validation}</p>}
    <div className="flex flex-wrap gap-2">
      {state.state === "conflict" ? <><Button kind="plain" aria-label={`Keep typed value ${name}`} onClick={() => choose("keep")}>Keep typed value</Button><Button kind="ghost" aria-label={`Use suggested value ${name}`} onClick={() => choose("accept")}>Use suggested value</Button></>
      : state.state === "suggested" ? <Button kind="plain" aria-label={`Accept ${name}`} onClick={() => choose("accept")}>Accept</Button>
      : state.state === "cleared" && row.suggestion?.state === "suggested" ? <Button kind="ghost" aria-label={`Use suggested value ${name}`} onClick={() => choose("accept")}>Use suggested value</Button> : null}
      {!control && <Button kind="ghost" aria-label={`${state.reviewed ? "Change" : "Edit"} ${name}`} onClick={() => choose("edit")}>{state.reviewed ? "Change" : "Edit"}</Button>}
      {state.state === "suggested" && <Button kind="ghost" aria-label={`Clear ${name}`} onClick={() => choose("clear")}>Clear</Button>}
    </div>
    <SourceExcerpt source={row.suggestion?.source} text={row.suggestion?.excerpt} onActive={onActive} />
  </li>;
}

export function NotRelevantChoice({choice, onChange}: {choice: string | null; onChange: (choice: "keep" | "choose") => void}) {
  return <div><p>{R.irrelevant}</p><fieldset><legend>Report kind</legend>
    <label className="flex min-h-11 items-center gap-2"><input type="radio" name="not-relevant" checked={choice === "keep"} onChange={() => onChange("keep")} />{R.keep}</label>
    <label className="flex min-h-11 items-center gap-2"><input type="radio" name="not-relevant" checked={choice === "choose"} onChange={() => onChange("choose")} />{R.choose}</label>
  </fieldset>{choice === "keep" && <p>{R.noImpact}</p>}</div>;
}

function EvidencePanel({text, file, note, excerpt}: {text: string; file: File | null; note: boolean; excerpt: string}) {
  const [full, setFull] = useState(false), [url, setUrl] = useState<string | null>(null);
  useEffect(() => {if (!file) {setUrl(null); return;} const next = URL.createObjectURL(file); setUrl(next); return () => URL.revokeObjectURL(next);}, [file]);
  const range = highlightRange(text, excerpt);
  return <div className="min-w-0 rounded-lg bg-sunken p-4">
    {note && <p className="text-label text-ink-2">{R.injection}</p>}
    <p data-testid="reading-message" className={`whitespace-pre-wrap [overflow-wrap:anywhere] ${full ? "" : "line-clamp-6"}`}>{range ? <>{text.slice(0, range[0])}<mark>{text.slice(...range)}</mark>{text.slice(range[1])}</> : text}</p>
    {text.length > 200 && <Button kind="ghost" onClick={() => setFull(!full)}>{full ? "Show less" : "Show full message"}</Button>}
    {file && url && <div className="min-w-0">{file.type.startsWith("image/") && <img src={url} alt="Attached photo" className="max-h-[240px] max-w-full object-contain" />}
      <p title={file.name} className="[overflow-wrap:anywhere]">{middle(file.name)}</p><a href={url} target="_blank" rel="noreferrer" className="underline">View file</a></div>}
  </div>;
}

type Pending = "discard" | "hold" | "mixed" | null;

export function ReportSheet({ start = {}, onClose, onSubmitted }: {
  start?: SheetStart; onClose: () => void; onSubmitted: (r: Report) => void;
}) {
  const { session } = useSession();
  const titleId = useId();
  const root = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const kinds = kindsFor(session.role);
  const [kind, setKind] = useState<ReportKind | null>(start.kind ?? (kinds.length === 1 ? kinds[0] : null));
  const [fields, setFields] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(start.fields ?? {}).map(([k, v]) => [k, asText(v)])));
  const [when, setWhen] = useState<CheckTimeValue>(NO_CHECK_TIME);
  const [whenError, setWhenError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(start.initialFile ?? null);
  const [stage, setStage] = useState<"evidence" | "manual" | "review" | "preview">("evidence");
  const [path, setPath] = useState<"ai" | "manual">("manual");
  const [reading, setReading] = useState<Reading | null>(null);
  const [rows, setRows] = useState<ReviewRow[]>([]);
  const [readingBusy, setReadingBusy] = useState(false);
  const [readingNote, setReadingNote] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [activeExcerpt, setActiveExcerpt] = useState("");
  const [irrelevant, setIrrelevant] = useState<"keep" | "choose" | null>(null);
  const readingCache = useRef(new Map<string, {reading: Reading | null; error: string | null}>());
  const readingLock = useRef(false);
  const identity = `${session.run_id}:${session.role}:${session.branch}:${session.day}`;
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const previousIdentity = useRef(identity);
  useEffect(() => {
    readingCache.current.clear(); setReading(null); setRows([]); setReadingNote(null); setStage("evidence");
    if (previousIdentity.current !== identity) {setFields({}); setKind(null); setText(""); setFile(null); setWhen(NO_CHECK_TIME); setPreview(null);}
    previousIdentity.current = identity;
  }, [identity]);
  useEffect(() => {if (stage === "review") root.current?.querySelector<HTMLElement>("[data-review-heading]")?.focus();}, [stage]);
  const [hold, setHold] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [allowance, setAllowance] = useState<Allowance | null>(null);
  const [allowanceError, setAllowanceError] = useState<ApiError | null>(null);
  const [preview, setPreview] = useState<{ body: Preview; of: string } | null>(null);
  const kindRef = useRef<HTMLDivElement>(null);
  const detailsRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const firstWhen = useRef<HTMLInputElement>(null);
  // Manual Next becomes a disabled Submit until a kind is chosen. Keep the
  // keyboard inside the dialog so Escape and the focus trap still receive it.
  useEffect(() => {if (stage === "manual") kindRef.current?.focus();}, [stage]);

  const store = session.role === "manager" ? session.branch : null;
  const f: ReportFields = kind === null ? {} : Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, v === "" ? null : v]));
  const body = kind === null ? null : fieldsBody(kind, f, hold);
  const ofNow = kind === null ? "" : JSON.stringify({ kind, body });
  const missing = kind === null ? [] : missingOf(kind, f);
  const dirty = kind !== start.kind && kind !== null || text.trim() !== "" || file !== null
    || Object.entries(fields).some(([k, v]) => v !== asText(start.fields?.[k]));
  const owner = kind === null ? "" : ownerName(ownerOf(kind, store));
  const holdable = kind === "CONDITION" && (fields.condition === "DAMAGE" || fields.condition === "SAFETY");
  const mixed = kind === "EXPIRY" && fields.printed_date === MIXED;
  const lotFixed = start.lockLot === true && asText(start.fields?.lot_id) !== "";

  const look = useAction((b: { kind: ReportKind; fields: ReportFields; corrects?: string }, key: string) => draftPreview(b, key));
  const submit = useAction(async (data: FormData, key: string) => {
    const report = await fileReport(data, key); beginSummary(session.run_id, report); return report;
  });

  // The allowance, read when the sheet opens (GET /api/signals carries it; one row is enough).
  useEffect(() => {
    let live = true;
    listReports({ pageSize: 1 }).then((v) => { if (live) setAllowance(v.allowance); })
      .catch((e: unknown) => { if (live) setAllowanceError(asError(e)); });
    return () => { live = false; };
  }, []);

  // Opening: keep the opener, focus the first control; closing returns focus to it.
  useEffect(() => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    root.current?.querySelector<HTMLElement>(start.focusFile ? "[data-file-pick]" : "input:not([disabled]), select, button")?.focus();
    return () => { if (opener.current?.isConnected) opener.current.focus(); };
  }, []);

  const close = () => (dirty && !submit.busy ? setPending("discard") : onClose());
  const set = (name: string, v: string) => setFields((x) => ({ ...x, [name]: v }));

  const chooseRow = (name: string, choice: ReviewRow["choice"], edited?: unknown) => {
    const old = rows.find(row => row.field === name) ?? {field: name};
    const next = {...old, choice, edited};
    setRows(all => [...all.filter(row => row.field !== name), next]);
    const value = rowState(next).value;
    if (name === "kind") setKind(value as ReportKind | null); else set(name, asText(value));
    setReviewError(null);
  };

  const analyse = async () => {
    if (readingLock.current) return;
    if (sheetPath(text, file) === "manual") {setPath("manual"); setStage("manual"); setReading(null); setReadingNote(null); return;}
    readingLock.current = true;
    const requestIdentity = identity;
    try {
      const bytes = file ? new Uint8Array(await file.arrayBuffer()) : new Uint8Array();
      const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(b => b.toString(16).padStart(2, "0")).join("");
      const key = JSON.stringify([identity, start.caseId, text, digest]);
      let cached = readingCache.current.get(key);
      if (!cached) {
        setReadingBusy(true);
        const data = new FormData(); data.set("text", text); if (file) data.set("file", file); if (start.caseId) data.set("case_id", start.caseId);
        try {const result = await readReport(data); cached = {reading: result, error: result.reason === "cap_reached" ? R.limit : result.status !== "DONE" ? R.failed : null};}
        catch (error) {const e = asError(error); cached = {reading: null, error: e.code === "cap_reached" ? R.limit : e.status >= 400 && e.status < 500 ? e.message : R.failed};}
        readingCache.current.set(key, cached);
      }
      if (currentIdentity.current !== requestIdentity) return;
      const result = cached.reading;
      if (cached.error || !result || (result.kind !== "NOT_RELEVANT" && !result.suggestions.some(s => s.value != null && s.state !== "needs_input"))) {
        setReading(null); setReadingNote(cached.error ?? R.empty); setPath("manual"); setStage("manual"); return;
      }
      if (reading?.reading_id !== result.reading_id) {
        // Only actual typed or reviewed values survive as the reporter's values.
        // Merely displaying an earlier suggestion never turns it into a typed fact.
        const preserved = (name: string, value: unknown) => {
          const prior = rows.find(row => row.field === name);
          return prior && !rowState(prior).reviewed ? undefined : value;
        };
        const nextRows: ReviewRow[] = result.suggestions.map(s => ({field: s.field, suggestion: {...s, state: s.state === "calculated" ? "suggested" : s.state},
          typed: preserved(s.field, s.field === "kind" ? kind ?? undefined : fields[s.field] || undefined)}));
        if (!nextRows.some(row => row.field === "kind")) nextRows.unshift({field: "kind", suggestion: {value: result.kind, state: "suggested"}, typed: kind ?? undefined});
        setRows(nextRows); setIrrelevant(null);
        if (result.kind !== "NOT_RELEVANT") {
          const k = rowState(nextRows.find(row => row.field === "kind")!).value as ReportKind;
          setKind(k);
          setFields(old => ({...old, ...Object.fromEntries(nextRows.filter(row => row.field !== "kind").map(row => [row.field, asText(rowState(row).value)]))}));
        } else setKind(null);
      }
      setReading(result); setReadingNote(null); setPath("ai"); setStage("review"); setReviewError(null);
    } finally {readingLock.current = false; setReadingBusy(false);}
  };

  const activeRows = kind === null ? rows.filter(row => row.field === "kind") : ["kind", ...fieldsOf(kind, f)].map(name =>
    ({...(rows.find(row => row.field === name) ?? {field: name, typed: name === "kind" ? kind : fields[name]}), required: name === "kind" || requiredOf(kind, f).includes(name)}));
  const validReview = () => {
    const unreviewed = activeRows.find(row => !rowState(row).reviewed);
    if (kind === null || unreviewed || (session.role === "hq" && kind !== "TRANSPORT" && kind !== "NOT_RELEVANT")) {
      setReviewError(R.validation);
      root.current?.querySelector<HTMLElement>(unreviewed ? `[data-field="${unreviewed.field}"]` : "[data-review-heading]")?.focus(); return false;
    }
    const missingInput = missing.filter(name => !(name === "printed_date" && mixed));
    if (missingInput.length > 0) {setReviewError(INCOMPLETE); root.current?.querySelector<HTMLElement>(`[data-field="${missingInput[0]}"] input, [data-field="${missingInput[0]}"] select`)?.focus(); return false;}
    if (CHECKED_KINDS.includes(kind)) {const got = checkTimeObserved(when, session.time_zone, Date.now()); if ("error" in got) {setWhenError(got.error); firstWhen.current?.focus(); return false;}}
    return true;
  };

  const trap = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape" && !e.defaultPrevented) {
      e.preventDefault();
      if (pending !== null) setPending(null); else close();
      return;
    }
    if (e.key !== "Tab" || root.current === null) return;
    const all = [...root.current.querySelectorAll<HTMLElement>(
      "a[href], button:not([disabled]), input:not([disabled]):not([tabindex='-1']), select:not([disabled]), textarea:not([disabled]), [tabindex='0']")];
    if (all.length === 0) return;
    const first = all[0], last = all[all.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };

  const runPreview = async () => {
    if (kind === null || body === null) return;
    const of = ofNow;
    const out = await look.run({ kind, fields: body, ...(start.corrects ? { corrects: start.corrects } : {}) });
    if (out !== undefined) setPreview({ body: out, of });
  };

  const send = async (confirmed: boolean) => {
    if (kind === null || body === null) return;
    if (path === "ai" && !validReview()) return;
    let observed: string | null = null;
    if (CHECKED_KINDS.includes(kind)) {
      const got = checkTimeObserved(when, session.time_zone, Date.now());
      if ("error" in got) {
        setWhenError(got.error);
        firstWhen.current?.focus();
        return;
      }
      observed = got.observed;
    }
    setWhenError(null);
    if (!confirmed && holdable && hold) { setPending("hold"); return; }
    if (!confirmed && mixed) { setPending("mixed"); return; }
    const data = new FormData();
    data.set("kind", kind);
    data.set("fields", JSON.stringify(body));
    data.set("text", text);
    if (observed !== null) data.set("observed", observed);
    if (start.caseId) data.set("case_id", start.caseId);
    if (start.corrects) data.set("corrects", start.corrects);
    if (file !== null) data.set("file", file);
    if (reading !== null) {
      data.set("reading_id", reading.reading_id);
      data.set("field_sources", JSON.stringify(kind === "CONDITION" ? Object.fromEntries(["kind", ...Object.keys(body)].map(name => [name, "typed"])) : fieldSources(activeRows)));
    }
    const out = await submit.run(data);
    setPending(null);
    if (out !== undefined) onSubmitted(out);
  };

  const selectedPath = stage === "evidence" ? sheetPath(text, file) : path;
  const step = stage === "evidence" ? 0 : path === "ai" ? stage === "review" ? 1 : 3 : kind === null ? 1 : preview !== null ? 3 : 2;
  const steps = [R.evidence, ...(selectedPath === "ai" ? [R.review, R.preview, R.submit] : STEPS)];
  const goTo = (i: number) => {if (i === 0) setStage("evidence"); else if (path === "ai" && i === 1) setStage("review"); else [kindRef, detailsRef, previewRef][Math.min(i - 1, 2)].current?.focus();};
  const noReports = allowance !== null && allowance.reports_left <= 0;
  const noFiles = allowance !== null && allowance.files_left <= 0;
  const stale = preview !== null && preview.of !== ofNow;
  const shownFields = kind === null ? [] : fieldsOf(kind, f);
  const required = kind === null ? [] : requiredOf(kind, f);
  const badField = submit.error?.extra.field;

  return (
    <div className="fixed top-[var(--shell-top,0px)] right-0 bottom-0 left-0 z-30 flex justify-end bg-[rgba(16,24,40,.24)]"
      onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div ref={root} role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={trap}
        className="flex h-full w-full min-w-0 flex-col border-l border-rule bg-surface shadow-float md:w-[560px]">
        <div className="flex items-center gap-2 border-b border-rule px-6 py-4 max-md:px-4">
          <h2 id={titleId} className="m-0 min-w-0 flex-1 text-title font-semibold text-ink">{start.caseContext ? R.caseTitle : REPORT_AN_OBSERVATION}</h2>
          <button type="button" aria-label={CLOSE} onClick={close}
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-ink-2 hover:bg-sunken max-md:h-11 max-md:w-11">
            <Icon name="x" />
          </button>
        </div>
        <ol aria-label="Report steps" className="m-0 flex list-none gap-1 px-6 pt-4 pb-2 max-md:px-4">
          {steps.map((name, i) => (
            <li key={name} className="flex min-w-0 flex-1 flex-col gap-1" aria-current={i === step ? "step" : undefined}>
              <span aria-hidden="true" className={`h-1 rounded-full ${i < step ? "bg-navy-700" : i === step ? "bg-gold-600" : "bg-rule"}`} />
              <button type="button" onClick={() => goTo(i)} disabled={i > step || readingBusy}
                className={`text-left text-label ${i === step ? "font-semibold text-ink" : "text-ink-2"}`}>{name}</button>
              {kind === "NOT_RELEVANT" && i === 2 && <span aria-label="Preview impact, not run for a Not relevant report" className="text-label text-ink-2">Not run</span>}
            </li>
          ))}
        </ol>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-6 overflow-y-auto px-6 pb-6 max-md:px-4">
          {readingBusy && <p role="status">{R.reading}</p>}
          {readingNote && <p className="text-label text-ink-2">{readingNote}</p>}
          {stage === "evidence" && <EvidenceStep>
            <h3 className="text-body font-semibold">{R.evidence}</h3><p className="text-label text-ink-2">{R.helper}</p>
            {store && <div><FieldInput name="store" value={store} readOnly required={false} invalid={false} onChange={() => {}} />
              {start.caseContext && <p className="text-label text-ink-2">Typed by you</p>}</div>}
            {start.product && <div><FieldInput name="product" value={start.product} readOnly required={false} invalid={false} onChange={() => {}} />
              {start.caseContext && <p className="text-label text-ink-2">Typed by you</p>}</div>}
            {start.fields?.lot_id && <div><FieldInput name="lot_id" value={asText(start.fields.lot_id)} readOnly required={false} invalid={false} onChange={() => {}} />
              {start.caseContext && <p className="text-label text-ink-2">Typed by you</p>}</div>}
            {start.questionText && <Button kind="ghost" disabled={readingBusy} onClick={() => setText(start.questionText!)}>{R.copyQuestion}</Button>}
            {rows.length > 0 && <p className="text-label">{R.kept}</p>}
            <label htmlFor={`${titleId}-text`} className="text-label">{R.message}</label>
            <textarea id={`${titleId}-text`} value={text} readOnly={readingBusy} onChange={e => {setText(e.target.value); e.target.style.height = "96px"; e.target.style.height = `${e.target.scrollHeight}px`;}}
              className="min-h-[96px] w-full min-w-0 rounded-md border border-line-2 p-2 [overflow-wrap:anywhere]" />
            <FileField file={file} onFile={setFile} disabled={readingBusy || noFiles} note={noFiles ? NO_FILES_LEFT : null} />
            {file && <EvidencePanel text="" file={file} note={false} excerpt="" />}
            {selectedPath === "manual" && <p className="text-label text-ink-2">{R.noRequest}</p>}
          </EvidenceStep>}
          {stage === "review" && reading && <section className="min-w-0">
            <h3 data-review-heading tabIndex={-1} className="text-body font-semibold">{R.review}</h3>
            <p role="status" className="sr-only">{reviewProgress(activeRows).total} fields suggested. Review each one.</p>
            <p className="text-label">{reviewProgress(activeRows).reviewed} of {reviewProgress(activeRows).total} suggestions reviewed</p>
            <EvidencePanel text={text} file={file} note={reading.note} excerpt={activeExcerpt} />
            {reading.kind === "NOT_RELEVANT" && <NotRelevantChoice choice={irrelevant} onChange={choice => {setIrrelevant(choice); if (choice === "keep") chooseRow("kind", "accept"); else {setKind(null); setRows(all => all.map(row => row.field === "kind" ? {...row, choice: undefined} : row));}}} />}
            {session.role === "hq" && kind !== "TRANSPORT" && kind !== "NOT_RELEVANT" && <div><p>{R.hq}</p><Button kind="plain" onClick={() => chooseRow("kind", "edit", "TRANSPORT")}>Choose Transport</Button></div>}
            {reading.kind === "NOT_RELEVANT" && irrelevant === "choose" && <fieldset><legend>{KIND_LEGEND}</legend>{kinds.map(k => <label key={k} className="flex min-h-11 items-center gap-2"><input type="radio" name="choose-kind" onChange={() => chooseRow("kind", "edit", k)} checked={kind === k} />{KIND_LABEL[k]}</label>)}</fieldset>}
            <ul aria-label="Suggested fields" className="m-0 min-w-0 list-none p-0">
              {activeRows.filter(row => !(row.field === "kind" && reading.kind === "NOT_RELEVANT")).map(row => <SuggestionRow key={row.field} row={row} onChoice={choice => chooseRow(row.field, choice)} onActive={setActiveExcerpt}
                required={row.field === "kind" || required.includes(row.field)} invalid={reviewError === R.validation && !rowState(row).reviewed}>
                {row.field === "kind" ? <fieldset><legend>{KIND_LEGEND}</legend>{kinds.map(k => <label key={k} className="flex min-h-11 items-center gap-2"><input type="radio" name="review-kind" checked={row.choice === "edit" && kind === k} onChange={() => chooseRow("kind", "edit", k)} />{KIND_LABEL[k]}</label>)}</fieldset>
                  : <><FieldInput name={row.field} value={row.field === "printed_date" && mixed ? "" : fields[row.field] ?? ""} required={required.includes(row.field)} readOnly={(row.field === "lot_id" && lotFixed) || (row.field === "printed_date" && mixed)} invalid={badField === row.field} onChange={value => chooseRow(row.field, "edit", value)} />
                    {row.field === "printed_date" && <label className="flex min-h-11 items-center gap-2"><input type="checkbox" checked={mixed} onChange={e => chooseRow("printed_date", "edit", e.target.checked ? MIXED : "")} />{MIXED_DATES}</label>}</>}
              </SuggestionRow>)}
            </ul>
            {kind !== null && CHECKED_KINDS.includes(kind) && <CheckTime id={`${titleId}-when`} value={when} onChange={v => {setWhen(v); setWhenError(null);}} timeZone={session.time_zone} error={whenError} firstRef={firstWhen} onInvalid={setWhenError} />}
            {holdable && <div><label className="flex min-h-11 items-center gap-2"><input type="checkbox" checked={hold} onChange={e => setHold(e.target.checked)} />{HOLD_BOX}</label><p className="text-label text-ink-2">{HOLD_HELP}</p></div>}
            {kind === "CONDITION" && fields.condition === "DISPLAY" && <p className="text-label">{DISPLAY_HELP}</p>}
            {reviewError && <p role="alert" className="text-label text-bad">{reviewError}</p>}
          </section>}
          {stage === "manual" && <><div ref={kindRef} tabIndex={-1} className="outline-none">
            <fieldset className="m-0 min-w-0 border-0 p-0">
              <legend className="mb-2 p-0 text-body font-semibold text-ink">{KIND_LEGEND}</legend>
              <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
                {kinds.map((k) => {
                  const id = `${titleId}-${k}`;
                  return (
                    <div key={k} className={`relative flex min-h-11 flex-col gap-1 rounded-lg border p-2 ${kind === k ? "border-navy-700 bg-navy-50" : "border-line-2 bg-white"}`}>
                      <span className="flex items-center gap-2">
                        <input id={id} type="radio" name={`${titleId}-kind`} checked={kind === k} aria-describedby={`${id}-hint`} className="relative z-10"
                          onChange={() => { setKind(k); setPreview(null); }} />
                        <Icon name={KIND_ICON[k]} className="text-ink-2" />
                        <label htmlFor={id} className="text-body font-semibold text-ink after:absolute after:inset-0">{KIND_LABEL[k]}</label>
                      </span>
                      <span id={`${id}-hint`} className="text-label text-ink-2">{KIND_HINT[k]}</span>
                    </div>
                  );
                })}
              </div>
            </fieldset>
          </div>

          {kind !== null && (
            <div ref={detailsRef} tabIndex={-1} className="flex min-w-0 flex-col gap-4 outline-none">
              <h3 className="m-0 text-body font-semibold text-ink">{STEPS[1]}</h3>
              <div className="grid min-w-0 grid-cols-1 gap-4 md:grid-cols-2">
                {store !== null && <FieldInput name="store" value={store} required={false} readOnly invalid={false} onChange={() => {}} />}
                {start.product && kind !== "DEMAND_EVENT" && (
                  <FieldInput name="product" value={start.product} required={false} readOnly invalid={false} onChange={() => {}} />
                )}
                {shownFields.filter((n) => n !== "printed_date").map((n) => (
                  <FieldInput key={n} name={n} value={fields[n] ?? ""} required={required.includes(n)}
                    readOnly={(n === "lot_id" && lotFixed) || (n === "sku" && start.product !== undefined && start.product !== null && start.caseId !== undefined)}
                    invalid={badField === n || (submit.error !== null && submit.error.code === "needs_input" && missing.includes(n))}
                    onChange={(v) => set(n, v)} />
                ))}
                {kind === "EXPIRY" && (
                  <div className="flex min-w-0 flex-col gap-2">
                    <FieldInput name="printed_date" value={mixed ? "" : fields.printed_date ?? ""} required={!mixed}
                      readOnly={mixed} invalid={badField === "printed_date"} onChange={(v) => set("printed_date", v)} />
                    <label className="flex min-h-11 items-center gap-2 text-body md:min-h-0">
                      <input type="checkbox" checked={mixed} onChange={(e) => set("printed_date", e.target.checked ? MIXED : "")} />
                      {MIXED_DATES}
                    </label>
                  </div>
                )}
              </div>
              {CHECKED_KINDS.includes(kind) && (
                <CheckTime id={`${titleId}-when`} value={when} onChange={(v) => { setWhen(v); setWhenError(null); }}
                  timeZone={session.time_zone} error={whenError} firstRef={firstWhen}
                  onInvalid={(reason) => setWhenError(reason)} />
              )}
              {holdable && (
                <div className="flex min-w-0 flex-col gap-1">
                  <label className="flex min-h-11 items-center gap-2 text-body font-semibold md:min-h-0">
                    <input type="checkbox" checked={hold} onChange={(e) => setHold(e.target.checked)} />{HOLD_BOX}
                  </label>
                  <p className="m-0 text-label text-ink-2">{HOLD_HELP}</p>
                </div>
              )}
              {kind === "CONDITION" && fields.condition === "DISPLAY" && <p className="m-0 text-label text-ink-2">{DISPLAY_HELP}</p>}
              <p className="text-label text-ink-2">{R.noRequest}</p>
            </div>
          )}</>}

          {kind !== null && kind !== "NOT_RELEVANT" && (stage === "manual" || stage === "preview") && (
            <div ref={previewRef} tabIndex={-1} className="flex min-w-0 flex-col gap-2 outline-none">
              <h3 className="m-0 text-body font-semibold text-ink">{STEPS[2]}</h3>
              {kind === "CONDITION"
                ? <PreviewPanel preview={{ ...emptyPreview(kind), message: REVIEW_ONLY_TEXT }} stale={false} owner={owner} currency={session.currency} />
                : missing.length > 0 && preview === null
                  ? (
                    <PreviewPanel preview={null} stale={false} owner={owner} currency={session.currency}>
                      <p className="m-0 text-body text-ink">{INCOMPLETE}</p>
                      <Button kind="ghost" disabled className="w-fit border-line-2">{PREVIEW_IMPACT}</Button>
                    </PreviewPanel>
                  )
                  : (
                    <PreviewPanel preview={preview?.body ?? null} stale={stale} owner={owner} currency={session.currency}>
                      {missing.length > 0 && <p className="m-0 text-body text-ink">{INCOMPLETE}</p>}
                      <Button kind="ghost" busy={look.busy} disabled={missing.length > 0} onClick={() => void runPreview()}
                        className="w-fit border-line-2 max-md:min-h-11">{look.busy ? PREVIEWING : PREVIEW_IMPACT}</Button>
                      <ErrorLine error={look.error} />
                    </PreviewPanel>
                  )}
            </div>
          )}
        </div>

        <div className="flex min-w-0 flex-col gap-2 border-t border-rule bg-surface px-6 py-4 max-md:px-4">
          {allowance !== null && <p data-testid="allowance" className="num m-0 text-label text-ink-2">{allowanceLine(allowance, session.time_zone)}</p>}
          {allowanceError !== null && <ErrorLine error={allowanceError} />}
          {submit.error !== null && (
            <ErrorLine error={submit.error}
              action={retryable(submit.error.code) ? { label: TRY_AGAIN, onClick: () => void submit.retry().then((r) => { if (r !== undefined) onSubmitted(r); }) } : undefined} />
          )}
          {pending === "discard" && (
            <InlineConfirm sentence={DISCARD_SENTENCE} confirm={DISCARD} dismiss={KEEP_EDITING} onConfirm={onClose} onDismiss={() => setPending(null)} />
          )}
          {pending === "hold" && (
            <InlineConfirm sentence={holdSentence(fields.lot_id ?? "", store ?? "")} confirm={PLACE_HOLD} dismiss={DONT_HOLD}
              busy={submit.busy} onConfirm={() => void send(true)} onDismiss={() => { setHold(false); setPending(null); }} />
          )}
          {pending === "mixed" && (
            <InlineConfirm sentence={mixedSentence(fields.lot_id ?? "", store ?? "")} confirm={SEND_AND_HOLD} dismiss={GO_BACK}
              busy={submit.busy} onConfirm={() => void send(true)} onDismiss={() => setPending(null)} />
          )}
          {pending === null && (
            <div className="flex flex-wrap items-center gap-2">
              <Button kind="primary" busy={readingBusy || submit.busy || look.busy} disabled={(noReports && stage !== "evidence") || (stage === "manual" && kind === null)} onClick={() => {
                if (stage === "evidence") void analyse();
                else if (stage === "review" && kind !== "NOT_RELEVANT") {if (validReview()) {setStage("preview"); if (kind !== "CONDITION" && missing.length === 0) void runPreview();}}
                else void send(false);
              }} className="max-md:min-h-11">{stage === "evidence" ? selectedPath === "ai" ? R.analyse : R.manual : stage === "review" && kind !== "NOT_RELEVANT" ? R.next : SUBMIT}</Button>
              {stage !== "evidence" && <Button kind="ghost" onClick={() => setStage("evidence")}>{R.back}</Button>}
              <Button kind="ghost" onClick={close} className="max-md:min-h-11">{CLOSE_WITHOUT}</Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

const REVIEW_ONLY_TEXT = "Requires human review · engine impact not calculated"; // D-13, word for word
const emptyPreview = (kind: ReportKind): Preview => ({
  signal_id: null, report_id: null, revision: null, kind, label: "Potential impact · unconfirmed", complete: true,
  message: null, cases: [], more: 0, more_text: null,
});

// --- the entry points ---------------------------------------------------------------------------------------------

// A button that opens the sheet with `start`; `onSubmitted` runs after the sheet closes on success.
export function ReportButton({ start, primary = false, label = REPORT_AN_OBSERVATION, onSubmitted }: {
  start: SheetStart; primary?: boolean; label?: string; onSubmitted?: (r: Report) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button kind={primary ? "primary" : "ghost"} onClick={() => setOpen(true)}
        className={primary ? "max-md:min-h-11" : "border-line-2 max-md:min-h-11"}>{label}</Button>
      {open && <ReportSheet start={start} onClose={() => setOpen(false)}
        onSubmitted={(r) => { setOpen(false); onSubmitted?.(r); }} />}
    </>
  );
}

// A Stock & lots row (07-UI-SPEC surface 6): the store's own lot, with its product and lot pre-filled. A lot of another
// store (HQ's list) offers nothing here: a manager reports for their own store only, and HQ reports transport.
export function ReportFromLot({ lot }: { lot: Pick<LotRow, "lot_id" | "branch" | "sku"> }) {
  const { session } = useSession();
  if (session.role !== "manager" || session.branch !== lot.branch) return null;
  return <ReportButton start={{ product: lot.sku, lockLot: true, fields: { lot_id: lot.lot_id } }} />;
}

// The stock decision's Context step header (07-SCREENS-MAP 07-20 T2: the store app has no ⋯ menu and no assumptions
// table): the store and product pre-filled, and the decision's own lot when it is the signed-in store's.
export function ReportFromDecision({ d, reload }: StepProps) {
  const { session } = useSession();
  return <ReportButton start={decisionStart(d, session)} onSubmitted={() => reload()} />;
}

export function decisionStart(d: Pick<CaseDetail61, "case_id" | "sku" | "lot_id" | "source">, s: Pick<Session, "role" | "branch">): SheetStart {
  const own = s.role === "manager" && s.branch === d.source;
  return {
    caseId: d.case_id, product: d.sku, lockLot: own,
    fields: own ? { lot_id: d.lot_id } : s.role === "manager" ? { sku: d.sku } : {},
  };
}

export { todayIso };
