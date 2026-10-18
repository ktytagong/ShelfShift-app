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
import { ApiError, type Session } from "./api.ts";
import { NO_CHECK_TIME, checkTimeObserved, retryable, type CheckTimeValue } from "./copy.ts";
import type { CaseDetail61, LotRow } from "./contract.ts";
import type { StepProps } from "./decision/types.ts";
import { useAction } from "./hooks.ts";
import {
  ADD_FILE, CHECKED_KINDS, CHOICES, CLOSE_WITHOUT, DATE_FIELDS, DATE_HELP, DISCARD, DISCARD_SENTENCE, DISPLAY_HELP,
  DONT_HOLD, FIELD_LABEL, FILE_HELP, GO_BACK, HOLD_BOX, HOLD_HELP, INCOMPLETE, KEEP_EDITING, KIND_HINT, KIND_ICON,
  KIND_LABEL, KIND_LEGEND, MIXED, MIXED_DATES, NOT_ALLOWED_IF, NOT_CHECKED, NO_AFFECTED, NO_FILES_LEFT,
  NUMBER_FIELDS, PLACE_HOLD, PLAN_CHANGES, PREVIEWING, PREVIEW_HEAD, PREVIEW_IMPACT, PREVIEW_STALE, REMOVE,
  REPORT_AN_OBSERVATION, REQUIRED, SAW_LABEL, SCENARIO_DATE, SEND_AND_HOLD, STEPS, SUBMIT, TEXT_LABEL, allowanceLine,
  benefitLine, draftPreview, fieldsBody, fieldsOf, fileProblem, fileReport, fileSize, holdSentence, kindsFor,
  listReports, middle, missingOf, mixedSentence, moreAffected, nothingChanges, ownerName, ownerOf, requiredOf,
  type Allowance, type Preview, type Report, type ReportFields, type ReportKind,
} from "./reportcopy.ts";
import { useSession } from "./shell.tsx";
import { Button, CLOSE, CheckTime, ErrorLine, Icon, Pill, TRY_AGAIN } from "./ui.tsx";

// What the sheet starts with: the stock decision or stock record it was opened from, or a report it corrects.
export type SheetStart = {
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
        ? <Button kind="ghost" icon="download" disabled={disabled} aria-describedby={help} onClick={() => input.current?.click()}
          className="w-fit border-line-2 max-md:min-h-11">{ADD_FILE}</Button>
        : (
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="min-w-0 rounded-md bg-sunken px-2 py-1 text-label text-ink" title={file.name}>
              {middle(file.name)} · {fileSize(file.size)}
            </span>
            <Button kind="ghost" aria-label={`${REMOVE} file ${file.name}`} onClick={() => onFile(null)} className="max-md:min-h-11">{REMOVE}</Button>
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

export function PreviewPanel({ preview, stale, owner, currency, children }: {
  preview: Preview | null; stale: boolean; owner: string; currency: string; children?: ReactNode;
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
                <tr key={c.case_id} className="align-top">
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
      {preview !== null && preview.more > 0 && <p className="m-0 text-label text-ink-2">{moreAffected(preview.more)}</p>}
      {children}
      <p className="m-0 text-label text-ink-2">{nothingChanges(owner)}</p>
    </div>
  );
}

// --- the sheet ----------------------------------------------------------------------------------------------------

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
  const [file, setFile] = useState<File | null>(null);
  const [hold, setHold] = useState(false);
  const [pending, setPending] = useState<Pending>(null);
  const [allowance, setAllowance] = useState<Allowance | null>(null);
  const [allowanceError, setAllowanceError] = useState<ApiError | null>(null);
  const [preview, setPreview] = useState<{ body: Preview; of: string } | null>(null);
  const kindRef = useRef<HTMLDivElement>(null);
  const detailsRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const firstWhen = useRef<HTMLInputElement>(null);

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
  const submit = useAction((data: FormData, key: string) => fileReport(data, key));

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
    root.current?.querySelector<HTMLElement>("input:not([disabled]), select, button")?.focus();
    return () => { if (opener.current?.isConnected) opener.current.focus(); };
  }, []);

  const close = () => (dirty && !submit.busy ? setPending("discard") : onClose());
  const set = (name: string, v: string) => setFields((x) => ({ ...x, [name]: v }));

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
    const out = await submit.run(data);
    setPending(null);
    if (out !== undefined) onSubmitted(out);
  };

  const step = kind === null ? 0 : preview !== null ? 2 : 1;
  const goTo = (i: number) => [kindRef, detailsRef, previewRef][i].current?.focus();
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
          <h2 id={titleId} className="m-0 min-w-0 flex-1 text-title font-semibold text-ink">{REPORT_AN_OBSERVATION}</h2>
          <button type="button" aria-label={CLOSE} onClick={close}
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-ink-2 hover:bg-sunken max-md:h-11 max-md:w-11">
            <Icon name="x" />
          </button>
        </div>
        <ol aria-label="Report steps" className="m-0 flex list-none gap-1 px-6 pt-4 pb-2 max-md:px-4">
          {STEPS.map((name, i) => (
            <li key={name} className="flex min-w-0 flex-1 flex-col gap-1" aria-current={i === step ? "step" : undefined}>
              <span aria-hidden="true" className={`h-1 rounded-full ${i < step ? "bg-navy-700" : i === step ? "bg-gold-600" : "bg-rule"}`} />
              <button type="button" onClick={() => goTo(i)} disabled={i > 0 && kind === null}
                className={`text-left text-label ${i === step ? "font-semibold text-ink" : "text-ink-2"}`}>{name}</button>
            </li>
          ))}
        </ol>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-6 overflow-y-auto px-6 pb-6 max-md:px-4">
          <div ref={kindRef} tabIndex={-1} className="outline-none">
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
              <div className="flex min-w-0 flex-col gap-1">
                <label htmlFor={`${titleId}-text`} className="text-label text-ink-2">{kind === "CONDITION" ? SAW_LABEL : TEXT_LABEL}</label>
                <textarea id={`${titleId}-text`} value={text} rows={3} onChange={(e) => setText(e.target.value)}
                  className="min-w-0 rounded-md border border-line-2 bg-white p-2 text-body break-words text-tx" />
              </div>
              <FileField file={file} onFile={setFile} disabled={noFiles && file === null} note={noFiles ? NO_FILES_LEFT : null} />
            </div>
          )}

          {kind !== null && (
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
              <Button kind="primary" busy={submit.busy} disabled={kind === null || noReports} onClick={() => void send(false)}
                className="max-md:min-h-11">{SUBMIT}</Button>
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
