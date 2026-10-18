// Stock checks on Tasks (07-19; 07-UI-SPEC "Tasks" rows, forms and confirm block; 07-CONTEXT D-05 to D-11, D-15, D-22;
// 06.1-UI-SPEC "Tasks", D-58, D-61; the Phase 5 rules kept: 05-UI-SPEC A-11, A-12, A-24, A-26, 05-REVIEW CR-02,
// WR-10, WR-12, WR-13). One row per request group, from GET /api/tasks (pages/tasks.tsx reads it; the server orders
// and filters it). A combined request is one row with a "Count" and an "Expiry" lane, each with its own chip, form
// and confirm, sent and confirmed on its own (D-22).
//
// Collapsed (R-5): the kind's title ("Count stock · lot L-1"), the compact DeadlineBar from the server's due_at and
// server_now, the server's state chip per part, and the one primary for the signed-in store ("Answer request",
// "Confirm count", "Confirm printed date"), which opens the row; then the product code, "for stock decision {key}" and
// "Simulated request" on a preloaded request. Open: the ask (server), "Why it matters", the Lifecycle, the timestamps,
// per part the form or the stored answer, the confirm block and the history; "Can't check" for the whole request.
//
// The state chip is always the server's (statePill: its sentence while a part is open). Answers carry "When did you
// check?" (no default), a count blank is null, never 0, and a photo alone never completes a count or a date. Typed
// values live in the page's draft map keyed by request and answer revision (draftKey), outside the list, so a refetch
// never clears them; a new revision under a kept draft shows "The answer changed since you looked at it".
import { useCallback, useRef, useState, type ReactNode } from "react";
import {
  ApiError, form, get, post, type AnswerSignal, type CaseDetail, type CaseState, type ConfirmResult, type LotCount,
  type Request, type Session,
} from "./api.ts";
import {
  NO_CHECK_TIME, checkTimeObserved, errorAction, lotCountText, responseNote, responseRows, stateIcon, statePill,
  type CheckTimeValue,
} from "./copy.ts";
import type { EvidenceRequest } from "./contract.ts";
import { unverifiedExposure } from "./boardview.ts";
import { canReplan, canRequestRecount, countsBody, draftKey, invalidFields, type LotDraft } from "./derive.ts";
import { hhmm, markDate, money } from "./format.ts";
import { useAction } from "./hooks.ts";
import { useSession } from "./shell.tsx";
import {
  CANT_CHECK, CANT_CHECK_SENTENCE, CHECKED_LOT, CONFIRM_COUNT, CONFIRM_DATE, CONFIRM_NOTE_P7, GO_BACK, HISTORY,
  KEEP_OPEN, MIXED_DATES, PHOTO_ALONE_COUNT, PHOTO_ALONE_DATE, PHOTO_NOT_COUNT, PRINTED_DATE, PRINTED_DATE_BOX,
  PRINTED_DATE_HELP, SEND_AND_HOLD, SIMULATED_REQUEST, WHY_IT_MATTERS, clip, countSource, dateSource, forDecision,
  cantCheckDone, mixedSentence, msLeft, partLane, unchangedDone, primaryLabel, replacedBy, requestTitle, sendPart, stampsLine, tickError,
  type TaskItem,
} from "./taskview.ts";
import {
  Button, CheckTime, ConfirmStep, DeadlineBar, Elapsed, ErrorLine, FilePick, Icon, Kv, Lifecycle, Pill, TRY_AGAIN,
} from "./ui.tsx";
import { RELOAD_DECISION, cityOf, recordedLine, scenarioDateLine, TERMS } from "./vocab.ts";
import { recordedTime } from "./copy.ts";

// --- the draft (kept by the page, outside the list) ------------------------------------------------------------------

export type DateDraft = { mode: "" | "date" | "mixed"; date: string };
export type Draft = {
  lots: Record<string, LotDraft>; dates: Record<string, DateDraft>; checkedLot: boolean; text: string;
  when: CheckTimeValue; file: File | null;
};
export const EMPTY_DRAFT: Draft = { lots: {}, dates: {}, checkedLot: false, text: "", when: NO_CHECK_TIME, file: null };
const BLANK_LOT: LotDraft = { shelf: "", backroom: "" };
const BLANK_DATE: DateDraft = { mode: "", date: "" };

// True when the visitor typed or chose anything in the draft.
export const dirty = (d: Draft) => d.text !== "" || d.when.mode !== "" || d.file !== null || d.checkedLot
  || Object.values(d.lots).some((l) => l.shelf !== "" || l.backroom !== "")
  || Object.values(d.dates).some((x) => x.mode !== "");

export type Drafts = {
  all: Record<string, Draft>;
  set: (key: string, next: Draft) => void;
  drop: (requestId: string) => void;
};

// The draft shown for a request part: the one typed against its current revision, else a kept draft typed against an
// earlier revision (shown with the changed-answer line), else an empty one.
function draftFor(drafts: Record<string, Draft>, r: Request): { key: string; draft: Draft; changed: boolean } {
  const current = draftKey(r);
  if (drafts[current] !== undefined) return { key: current, draft: drafts[current], changed: false };
  const kept = Object.keys(drafts).find((k) => k.startsWith(`${r.request_id}:`) && dirty(drafts[k]));
  return kept === undefined ? { key: current, draft: EMPTY_DRAFT, changed: false } : { key: kept, draft: drafts[kept], changed: true };
}

// --- the words kept from 06.1 (D-58, D-61) ---------------------------------------------------------------------------

// "Stock check assigned to S017 · Recorded 17 Oct 2026, 02:45 Bangkok time." (D-58, the user's form), from the
// request's own asked_at; without a readable time, "Stock check assigned to S017."
export function assignedLine(to: string, recorded: string): string {
  return `${TERMS.STOCK_CHECK} assigned to ${to}${recorded === "" ? "" : ` · ${recorded}`}.`;
}
// Legacy deadline fallback: "Respond by 09:45 (planned) · Scenario date: Sat 17 Oct 2026". A request with due_at
// uses its real server deadline instead. A day the session calendar lacks leaves the scenario date out.
export function respondByLine(min: number, dateIso: string | null): string {
  return `Respond by ${hhmm(min)} (planned)${dateIso === null ? "" : ` · ${scenarioDateLine(dateIso)}`}`;
}
export function expiredLine61(min: number, dateIso: string | null): string {
  return `This ${TERMS.STOCK_CHECK.toLowerCase()} expired at ${hhmm(min)} (planned)`
    + `${dateIso === null ? "" : ` · ${scenarioDateLine(dateIso)}`}. The stock decision shows the fact as unverified.`;
}
export const RECORDED_LABEL = "Recorded";

const MOVED_ON = new Set(["stale_version", "wrong_state"]);
const EARLIER_DAY = "This count is from an earlier day. Send today's count, then confirm it.";
const FINISHED = "Saved to history. This stock decision is closed, so this count cannot be confirmed.";
const lotGone = (r: Request) => `${r.lot_ids.length === 1 ? "The counted lot is" : "A counted lot is"} no longer on `
  + `record at ${r.to_branch}, so this count cannot be confirmed.`;

// True when the server will neither confirm the part's answer nor any answer sent now: the stock decision's state is
// read to name the reason (WR-12).
export const blocked = (r: Request) => !r.confirmable && !r.answerable
  && (r.state === "OPEN" || r.state === "ANSWERED" || r.state === "EXPIRED");

// True when the signed-in store has something to do on this part (the 4px navy rule, R-4).
export function stockCheckActs(r: Pick<Request, "state" | "answerable" | "confirmable">): boolean {
  return r.answerable || (r.state === "ANSWERED" && r.confirmable);
}

// The ISO scenario date of a calendar day, or null when the calendar does not hold it.
export function dayDate(cal: Session["calendar"], day: number): string | null {
  return cal.find((c) => c.day === day)?.date ?? null;
}

// The states of the stock decisions of blocked parts (read only for those, from each detail; a failed read leaves the
// reason out rather than guessing it).
export async function blockedCaseStates(items: TaskItem[]): Promise<Record<string, CaseState>> {
  const ids = [...new Set(items.flatMap((i) => i.parts.filter(blocked).map((r) => r.case_id)))];
  const found = await Promise.all(ids.map(async (id) => {
    try {
      return [[id, (await get<CaseDetail>(`/api/cases/${encodeURIComponent(id)}`)).state] as const];
    } catch {
      return [];
    }
  }));
  return Object.fromEntries(found.flat());
}

// The Phase 7 keys of an answer (04-API-CONTRACT Request `answer`): an expiry part's printed dates and tick, the file.
type Answer7 = AnswerSignal & {
  fields: AnswerSignal["fields"] & { dates?: Record<string, string>; checked_lot?: boolean };
  observed_at?: string | null; file?: { type: string; size: number | null } | null; simulated_label?: string | null;
};
const answerOf = (r: Request) => r.answer as Answer7 | null;
const isExpiry = (r: Request) => r.part === "expiry";

const VIEW_LINK = "text-body font-semibold text-navy-800 underline underline-offset-2";
const viewHref = (caseId: string) => `#/cases/${encodeURIComponent(caseId)}`;
const fileHref = (signalId: string) => `/api/signals/${encodeURIComponent(signalId)}/file`;

// "Thu 15 Oct 2026" for a printed date, "Mixed dates in this lot" for MIXED.
const dateText = (v: string | undefined) => (v === undefined ? "Not checked" : v === "MIXED" ? MIXED_DATES : markDate(v));

// --- one request row ------------------------------------------------------------------------------------------------

export function RequestTask({ item, caseStates, expanded, onToggle, drafts, after, onZero, serverNow, receivedAt, onAnnounce }: {
  item: TaskItem; caseStates: Record<string, CaseState>; expanded: boolean; onToggle: (open: boolean) => void;
  drafts: Drafts; after: () => Promise<void>; onZero: () => void; serverNow: string; receivedAt: number;
  onAnnounce: (lines: string[]) => void;
}) {
  const { session } = useSession();
  const parts = item.parts;
  const head = parts[0];
  const gid = item.key.slice("request:".length);
  const title = requestTitle(parts);
  const acts = item.open && parts.some(stockCheckActs);
  const primary = primaryLabel(item);
  const region = `task-${gid}-body`;
  const titleId = `task-${gid}-title`;
  const ms = head.due_at != null && head.server_now != null ? msLeft(head.due_at, serverNow, receivedAt, Date.now()) : null;
  const confirmedAt = Object.fromEntries(Object.entries(item.confirmations).map(([k, v]) => [k, v.at]));
  const fmt = (iso: string) => recordedTime(iso, session.time_zone);
  const allAnswered = parts.every((p) => p.answered_at != null && p.status_kind !== "PHOTO_PARTIAL" && p.status_kind !== "TEXT_PARTIAL");
  const closedAll = parts.every((p) => !["OPEN", "ANSWERED"].includes(p.state));
  const cantCheck = parts.some((p) => p.state === "OPEN" && p.answerable);
  const cant = useAction(async (_: Record<string, never>, key: string) => {
    const out = await post<Request>(`/api/requests/${encodeURIComponent(head.request_id)}/cant-check`, {}, key);
    drafts.drop(head.request_id);
    onAnnounce([cantCheckDone(head.lot_ids.join(", "))]);
    await after();
    return out;
  });
  const latest = parts.map((p) => p.answered_at).filter((x): x is string => x != null).sort().pop() ?? null;

  return (
    <article aria-labelledby={titleId} data-request-id={gid} data-acts={acts || undefined}
      className={`min-w-0 rounded-2xl border border-line bg-white p-4 shadow-sm ${acts ? "border-l-4 border-l-navy-800" : ""}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
        <h3 id={titleId} className="m-0 min-w-0 flex-1 basis-56 text-title font-semibold">
          <button type="button" aria-expanded={expanded} aria-controls={region} title={title}
            onClick={() => onToggle(!expanded)}
            className="inline-flex max-w-full items-center gap-2 text-left break-words">
            <Icon name={isExpiry(head) && parts.length === 1 ? "today" : "box"} />
            <span>{clip(title)}</span>
          </button>
        </h3>
        {head.asked_at != null && head.due_at != null && (
          <DeadlineBar compact askedAt={head.asked_at} dueAt={head.due_at} serverNow={serverNow} receivedAt={receivedAt}
            answeredAt={allAnswered ? latest : null} late={parts.some((p) => p.late === true)} closed={closedAll && !allAnswered}
            onZero={onZero} />
        )}
        <span className="flex flex-wrap items-center gap-1">
          {parts.map((p) => {
            const pill = statePill(p);
            return (
              <span key={p.request_id} className="inline-flex items-center gap-1">
                {parts.length > 1 && <span className="text-label text-tx3">{partLane(p.part)}</span>}
                <Pill tone={pill.tone} icon={stateIcon(p)}>{pill.text}</Pill>
                {p.partial_note !== null && p.status_kind !== "TEXT_PARTIAL" && p.status_kind !== "PHOTO_PARTIAL"
                  && <Pill tone="warn" icon="alert">{p.partial_note}</Pill>}
              </span>
            );
          })}
        </span>
        {primary !== null && !expanded && ( // opens the row; once open, the form or the confirm block acts (R-4)
          <Button kind="primary" className="min-h-11 md:min-h-8" onClick={() => onToggle(true)}>{primary}</Button>
        )}
      </div>
      <p className="num m-0 mt-1 flex flex-wrap items-center gap-x-2 text-label text-tx2">
        {item.sku !== null && <span>{item.sku}</span>}
        <a href={viewHref(head.case_id)} className="text-navy-800 underline underline-offset-2">{forDecision(head.case_key)}</a>
        {head.simulated_label != null && <Pill tone="plan" icon="info">{SIMULATED_REQUEST}</Pill>}
      </p>
      {expanded && (
        <div id={region} className="mt-4 flex flex-col gap-4 border-t border-line pt-4">
          <RequestMessage r={head as EvidenceRequest} />{/* 09-20: the sent message, its context and the original draft */}
          {parts.map((p) => {
            const x = item.exposure[p.request_id];
            return x == null || p.state === "CONFIRMED" ? null : (
              <p key={`why-${p.request_id}`} className="m-0 text-body">
                {WHY_IT_MATTERS} <strong>{unverifiedExposure(money(x, session.currency))}</strong>
                {parts.length > 1 ? ` (${partLane(p.part)})` : ""}
              </p>
            );
          })}
          {head.asked_at != null && head.due_at != null && (
            <Lifecycle parts={parts} confirmedAt={confirmedAt} timeZone={session.time_zone} msLeftNow={ms} fmt={fmt} />
          )}
          {parts.map((p) => (
            <PartBody key={p.request_id} r={p} combined={parts.length > 1} caseState={caseStates[p.case_id]}
              labels={item.confirmations[p.request_id]?.labels ?? []} drafts={drafts} after={after}
              confirmedAt={confirmedAt[p.request_id] ?? null} onAnnounce={onAnnounce} />
          ))}
          {cantCheck && (
            <div className="flex flex-col gap-1">
              <ConfirmStep trigger={CANT_CHECK} triggerKind="ghost" sentence={CANT_CHECK_SENTENCE} confirm={CANT_CHECK}
                dismiss={KEEP_OPEN} busy={cant.busy} onConfirm={() => void cant.run({})} />
              <ErrorLine error={cant.error} />
            </div>
          )}
        </div>
      )}
    </article>
  );
}

// --- one part of a request: its lines, form or stored answer, confirm block and history -------------------------------

const ANSWERED_STATES: readonly Request["state"][] = ["ANSWERED", "CONFIRMED", "SUPERSEDED"];

function summaryRows(r: Request, timeZone: string): [string, string][] {
  const a = answerOf(r);
  if (a === null) return [];
  if (!isExpiry(r)) {
    return responseRows(r, timeZone).map(([k, v]) => (k === "Submitted" ? [RECORDED_LABEL, `${v} ${cityOf(timeZone)} time`] : [k, v]));
  }
  const rows: [string, string][] = r.lot_ids.map((id) => [`Lot ${id}`, dateText(a.fields.dates?.[id])]);
  if (a.text !== "") rows.push(["Reply", a.text]);
  const at = recordedTime(a.created_at, timeZone);
  if (at !== "") rows.push([RECORDED_LABEL, `${at} ${cityOf(timeZone)} time`]);
  rows.push(["Status", r.state === "ANSWERED" ? "Awaiting confirmation" : r.state === "CONFIRMED" ? "Confirmed" : statePill(r).text]);
  return rows;
}

function StoredAnswer({ r }: { r: Request }) {
  const a = answerOf(r);
  if (a === null) return null;
  const rows: [string, string][] = r.lot_ids.map((id) => {
    if (isExpiry(r)) return [`Lot ${id}`, dateText(a.fields.dates?.[id])];
    const c: LotCount = a.fields.counts?.[id] ?? { shelf_units: null, backroom_units: null };
    return [`Lot ${id}`, lotCountText(c)];
  });
  if (a.text !== "") rows.push(["Reply", a.text]);
  return <div className="max-w-md"><Kv stacked items={rows} /></div>;
}

// The answer's photo: a thumbnail (images) or a link (PDF), labelled so it never reads as a count (VER-04).
function Photo({ r }: { r: Request }) {
  const a = answerOf(r);
  if (a === null || a.file == null) return null;
  const href = fileHref(a.signal_id);
  const image = a.file.type === "image/png" || a.file.type === "image/jpeg";
  return (
    <span data-testid="answer-photo" className="flex flex-wrap items-center gap-2">
      {image && <img src={href} alt={`Photo attached to the answer from ${r.to_branch}`} className="h-16 w-16 rounded-md object-cover" />}
      <Pill tone="gray" icon="info">{isExpiry(r) ? "Photo" : PHOTO_NOT_COUNT}</Pill>
      <a href={href} target="_blank" rel="noreferrer" className={VIEW_LINK}>{image ? "View photo" : "View file"}</a>
    </span>
  );
}

// Human wording is rendered as text, independently of the server's immutable
// instructions. Neither edits nor generated prose replace a lot or check binding.
function RequestMessage({ r }: { r: EvidenceRequest }) {
  const context = r.request_context;
  const edited = !r.draft_id || r.message !== r.original_text;
  const lots = Array.isArray(context?.lot_ids) ? context.lot_ids.filter((v): v is string => typeof v === "string") : [];
  const parts = Array.isArray(context?.required_parts) ? context.required_parts.filter((v): v is string => typeof v === "string") : [];
  const items: [string, string][] = context ? [
    ["Branch", context.branch], ["Product", context.sku], ["Fact", context.fact_id], ["Class", context.class],
  ].filter((entry): entry is [string, string] => typeof entry[1] === "string" && entry[1] !== "") : [];
  if (lots.length) items.push(["Lots", lots.join(", ")]);
  return <>
    {typeof r.message === "string" ? <section aria-label={`Message from ${r.case_source} manager`} className="flex min-w-0 flex-col gap-2">
      <h4 className="m-0 text-body font-semibold">Message from {r.case_source} manager</h4>
      <p data-testid="sent-message" className="m-0 min-w-0 whitespace-pre-wrap text-body [overflow-wrap:anywhere]">{r.message}</p>
      <p className="m-0 text-label text-tx2">{edited ? "Human-authored message" : "Drafted by Gemini · Sent by manager"}</p>
    </section> : <p className="m-0 whitespace-pre-wrap text-body [overflow-wrap:anywhere]">{r.ask_text}</p>}
    {context && <section aria-label="Request context" className="flex min-w-0 flex-col gap-2 border-t border-line pt-2 [overflow-wrap:anywhere]">
      <h4 className="m-0 text-body font-semibold">Request context</h4>
      <Kv stacked items={items} />
      {parts.map(part => <p key={part} className="m-0 text-body">{part}</p>)}
    </section>}
    {r.draft_id && typeof r.original_text === "string" && <details className="min-w-0 [overflow-wrap:anywhere]">
      <summary className="cursor-pointer py-2 text-body max-md:min-h-11">Original Gemini draft</summary>
      <p data-testid="sent-original-draft" className="m-0 whitespace-pre-wrap text-body">{r.original_text}</p>
      <p className="m-0 text-label text-tx2">Read-only original</p>
    </details>}
  </>;
}

function PartBody({ r, combined, caseState, labels, drafts, after, confirmedAt, onAnnounce }: {
  r: Request; combined: boolean; caseState: CaseState | undefined; labels: string[]; drafts: Drafts;
  after: () => Promise<void>; confirmedAt: string | null; onAnnounce: (lines: string[]) => void;
}) {
  const { session } = useSession();
  const { key, draft, changed } = draftFor(drafts.all, r);
  const onDraft = (next: Draft) => drafts.set(key, next);
  const [tickedRevision, setTickedRevision] = useState<number | null>(null);
  const [result, setResult] = useState<ConfirmResult | null>(null);
  const [timeError, setTimeError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const firstRadio = useRef<HTMLInputElement>(null);

  const reloadOn = async <T,>(write: () => Promise<T>): Promise<T> => {
    try {
      return await write();
    } catch (e) {
      if (e instanceof ApiError && MOVED_ON.has(e.code)) void after();
      throw e;
    }
  };
  const confirm = useAction(async (body: { signal: string; revision: number; checked_both?: true }, k: string) => {
    const payload = body.checked_both ? { revision: body.revision, checked_both: true } : { revision: body.revision };
    const out = await reloadOn(() => post<ConfirmResult>(`/api/signals/${encodeURIComponent(body.signal)}/confirm`, payload, k));
    setResult(out);
    onAnnounce(out.labels.length > 0 ? out.labels : [unchangedDone(r.lot_ids.join(", "))]);
    await after();
    return out;
  });
  const answer = useAction(async (body: { text: string; counts?: Record<string, LotCount>; dates?: Record<string, string>;
    checked_lot?: boolean; observed: string; file: File | null }, k: string) => {
    const data = new FormData();
    data.set("text", body.text);
    if (body.counts !== undefined) data.set("counts", JSON.stringify(body.counts));
    if (body.dates !== undefined) data.set("dates", JSON.stringify(body.dates));
    if (body.checked_lot !== undefined) data.set("checked_lot", body.checked_lot ? "true" : "false");
    data.set("observed", body.observed); // "When did you check?" (07-CONTEXT D-22)
    if (body.file !== null) data.set("file", body.file);
    const out = await reloadOn(() => form<AnswerSignal>(`/api/requests/${encodeURIComponent(r.request_id)}/answer`, data, k));
    drafts.drop(r.request_id);
    confirm.clear(); // a refused confirm was about the earlier answer, never this one (05-REVIEW WR-13)
    await after();
    return out;
  });
  const recount = useAction(async (body: { rec_version: string; fact_id: string; recount_of: string }, k: string) => {
    const out = await post<Request>(`/api/cases/${encodeURIComponent(r.case_id)}/requests`, body, k);
    await after();
    return out;
  });

  const open = r.state === "OPEN" || r.state === "ANSWERED";
  const a = answerOf(r);
  const missing = a !== null && (a.fields.needs_input?.length ?? 0) > 0;
  const complete = a !== null && a.state === "READ" && !missing;
  const ticked = a !== null && tickedRevision === a.revision;
  const busy = answer.busy || confirm.busy || recount.busy;
  const invalid = invalidFields(confirm.error ?? answer.error);
  const summary = a !== null && ANSWERED_STATES.includes(r.state);
  const earlierDay = r.state === "ANSWERED" && a !== null && !r.confirmable && r.answerable && !isExpiry(r);
  const stuck = blocked(r) && caseState !== undefined;
  const finished = stuck && caseState !== undefined && !canReplan(caseState) && r.state === "ANSWERED" && a !== null;
  const lotLeft = stuck && caseState !== undefined && canReplan(caseState);
  const showForm = r.answerable && (r.state === "OPEN" || r.state === "EXPIRED"
    || (r.state === "ANSWERED" && (changed || invalid.size > 0 || earlierDay)));
  const confirmedText = result?.labels ?? labels;
  const others = (result?.cases ?? []).filter((c) => c.case_id !== r.case_id);
  const mixed = isExpiry(r) && r.lot_ids.some((id) => draft.dates[id]?.mode === "mixed");
  const setLot = (id: string, field: keyof LotDraft, value: string) => onDraft({
    ...draft, lots: { ...draft.lots, [id]: { ...(draft.lots[id] ?? BLANK_LOT), [field]: value } },
  });
  const setDate = (id: string, next: Partial<DateDraft>) => onDraft({
    ...draft, dates: { ...draft.dates, [id]: { ...(draft.dates[id] ?? BLANK_DATE), ...next } },
  });
  const retryOf = (act: { error: ApiError | null; retry: () => Promise<unknown> }) =>
    act.error !== null && errorAction(act.error.code) === "retry" ? { label: TRY_AGAIN, onClick: () => void act.retry() } : undefined;
  const reloadAction = (act: { error: ApiError | null; clear: () => void; retry: () => Promise<unknown> }) =>
    act.error === null ? undefined
      : errorAction(act.error.code) === "reload_case" ? { label: RELOAD_DECISION, onClick: () => { act.clear(); void after(); } }
      : retryOf(act);
  const due = dayDate(session.calendar, r.due_day);
  const partId = `${r.request_id}`;

  // Validates and sends the part's answer; a reason the client can name (check time, the tick) shows in place.
  const send = () => {
    const when = checkTimeObserved(draft.when, session.time_zone, Date.now());
    if ("error" in when) {
      setTimeError(when.error);
      firstRadio.current?.focus();
      return;
    }
    setTimeError(null);
    if (isExpiry(r)) {
      const dates: Record<string, string> = {};
      for (const id of r.lot_ids) {
        const d = draft.dates[id] ?? BLANK_DATE;
        if (d.mode === "mixed") dates[id] = "MIXED";
        else if (d.mode === "date" && d.date !== "") dates[id] = d.date;
      }
      if (Object.keys(dates).length > 0 && !draft.checkedLot) {
        setFormError(tickError(r.lot_ids.join(", ")));
        return;
      }
      setFormError(null);
      void answer.run({ text: draft.text, dates, checked_lot: draft.checkedLot, observed: when.observed, file: draft.file });
    } else {
      setFormError(null);
      void answer.run({ text: draft.text, counts: countsBody(r.lot_ids, draft.lots), observed: when.observed, file: draft.file });
    }
  };
  const sendLabel = mixed ? SEND_AND_HOLD : combined ? sendPart(r.part) : "Send answer";
  const answerSource = a === null ? "" : recordedTime(a.observed_at ?? a.created_at, session.time_zone);

  return (
    <section aria-label={combined ? partLane(r.part) : undefined} data-part={r.part ?? "count"}
      className="flex min-w-0 flex-col gap-2">
      {combined && <h4 className="m-0 text-body font-semibold">{partLane(r.part)}</h4>}
      <p data-testid="assigned-line" className="num m-0 text-label text-tx2 break-words">
        {assignedLine(r.to_branch, r.asked_at == null ? "" : recordedLine(r.asked_at, session.time_zone))}
      </p>
      {r.asked_at != null && (
        <p data-testid="timestamps" className="num m-0 text-label text-tx2 break-words">
          {stampsLine({ asked: r.asked_at, due: r.due_at ?? null, checked: r.observed_at ?? null,
            answered: r.answered_at ?? null, confirmed: confirmedAt }, session.time_zone)}
        </p>
      )}
      {r.due_at == null && (
        <p data-testid="due-line" className="num m-0 text-label text-tx2 break-words">{respondByLine(r.due_min, due)}</p>
      )}
      {summary ? (
        <div className="flex flex-col gap-2">
          <div data-testid="response-summary" className="max-w-md"><Kv stacked items={summaryRows(r, session.time_zone)} /></div>
          {a?.simulated_label != null && <span><Pill tone="plan" icon="info">{a.simulated_label}</Pill></span>}
          {responseNote(r) !== null && <p data-testid="response-note" className="m-0"><Pill tone="warn" icon="clock">{responseNote(r)}</Pill></p>}
          <div className="flex flex-wrap items-center gap-4">
            <a href={viewHref(r.case_id)} className={VIEW_LINK}>{TERMS.VIEW_STOCK_DECISION}</a>
            {canRequestRecount(r, session) && (
              <Button kind="plain" icon="redo" busy={recount.busy} disabled={answer.busy || confirm.busy} className="min-h-11 md:min-h-8"
                onClick={() => void recount.run({ rec_version: r.rec_version, fact_id: r.fact_id, recount_of: r.request_id })}>
                Request recount
              </Button>
            )}
          </div>
          <ErrorLine error={recount.error} action={reloadAction(recount)} />
        </div>
      ) : (
        <>
          <a href={viewHref(r.case_id)} className={`${VIEW_LINK} self-start`}>{TERMS.VIEW_STOCK_DECISION}</a>
          {a !== null && <StoredAnswer r={r} />}
          {a?.simulated_label != null && <span><Pill tone="plan" icon="info">{a.simulated_label}</Pill></span>}
        </>
      )}
      <Photo r={r} />
      {open && missing && (
        <p className="m-0 rounded-md bg-warn-soft px-2 py-1 text-body text-warn">
          {isExpiry(r) ? "A printed date is missing, so the date can't be confirmed." : "Shelf or backroom not checked, so the total can't be confirmed."}
        </p>
      )}
      {r.state === "EXPIRED" && (
        <p data-testid="expired-line" className="num m-0 text-label text-tx2 break-words">{expiredLine61(r.due_min, due)}</p>
      )}
      {earlierDay && <p data-testid="earlier-day" className="m-0 rounded-md bg-warn-soft px-2 py-1 text-body text-warn">{EARLIER_DAY}</p>}
      {finished && <p data-testid="not-confirmable" className="m-0 text-label text-tx2">{FINISHED}</p>}
      {lotLeft && <p data-testid="lot-left" className="m-0 text-label text-tx2">{lotGone(r)}</p>}
      {showForm && (
        <form className="flex flex-col gap-2" onSubmit={(e) => { e.preventDefault(); if (!mixed) send(); }}>
          {changed && (
            <p role="status" className="m-0 rounded-md bg-warn-soft px-2 py-1 text-body text-warn">The answer changed since you looked at it</p>
          )}
          <fieldset disabled={busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
            {isExpiry(r)
              ? r.lot_ids.map((id) => <DateFields key={id} r={r} lot={id} value={draft.dates[id] ?? BLANK_DATE} onChange={(n) => setDate(id, n)} />)
              : r.lot_ids.map((id) => {
                const v = draft.lots[id] ?? BLANK_LOT;
                return (
                  <fieldset key={id} className="m-0 min-w-0 border-0 p-0">
                    <legend className="mb-1 p-0 text-body font-semibold">Lot {id}</legend>
                    <div className="flex flex-wrap gap-4">
                      {(["shelf", "backroom"] as const).map((field) => {
                        const inputId = `${r.request_id}-${id}-${field}`;
                        const bad = invalid.has(`${id}:${field}_units`);
                        return (
                          <span key={field} className="flex min-w-0 flex-col gap-1">
                            <label htmlFor={inputId} className="text-label text-tx3">{field === "shelf" ? "Shelf" : "Backroom"}</label>
                            <input id={inputId} type="number" inputMode="numeric" min="0" placeholder="Not checked"
                              value={v[field]} onChange={(e) => setLot(id, field, e.target.value)} aria-invalid={bad || undefined}
                              className={`num h-11 w-40 max-w-full rounded-md border bg-white px-2 text-body md:h-8 ${bad ? "border-bad" : "border-line-2"}`} />
                          </span>
                        );
                      })}
                    </div>
                  </fieldset>
                );
              })}
            {isExpiry(r) && (
              <label className="flex min-h-11 items-center gap-2 text-body md:min-h-0">
                <input type="checkbox" checked={draft.checkedLot} onChange={(e) => onDraft({ ...draft, checkedLot: e.target.checked })} />
                {CHECKED_LOT}
              </label>
            )}
            <span className="flex max-w-md flex-col gap-1">
              <label htmlFor={`${partId}-reply`} className="text-label text-tx3">Reply</label>
              <textarea id={`${partId}-reply`} rows={2} value={draft.text} onChange={(e) => onDraft({ ...draft, text: e.target.value })}
                className="rounded-md border border-line-2 bg-white px-2 py-1 text-body break-words" />
            </span>
            <FilePick id={`${partId}-file`} file={draft.file} onFile={(file) => onDraft({ ...draft, file })} disabled={busy}
              note={isExpiry(r) ? PHOTO_ALONE_DATE : PHOTO_ALONE_COUNT} />
            <CheckTime id={`${partId}-checked`} value={draft.when} timeZone={session.time_zone} error={timeError}
              firstRef={firstRadio} onInvalid={(reason) => setTimeError(reason)}
              onChange={(when) => { setTimeError(null); onDraft({ ...draft, when }); }} />
            {formError !== null && <p role="alert" className="m-0 text-label text-bad">{formError}</p>}
            <span>
              {mixed ? (
                <ConfirmStep trigger={SEND_AND_HOLD} triggerKind="primary" sentence={mixedSentence(r.lot_ids.join(", "), r.to_branch)}
                  confirm={SEND_AND_HOLD} dismiss={GO_BACK} busy={answer.busy} onConfirm={send} />
              ) : (
                <Button kind="primary" type="submit" busy={answer.busy} className="min-h-11 md:min-h-8">{sendLabel}</Button>
              )}
            </span>
          </fieldset>
          <ErrorLine error={answer.error} action={retryOf(answer)} />
        </form>
      )}
      {r.state === "ANSWERED" && r.confirmable && complete && a !== null && (
        <div data-testid="confirm-block" className="flex flex-col gap-2 border-t border-line pt-2">
          <p className="num m-0 text-label text-tx2">
            {(isExpiry(r) ? dateSource : countSource)(a.reporter_branch ?? r.to_branch, answerSource)}
          </p>
          {!isExpiry(r) && (
            <label className="flex min-h-11 items-center gap-2 text-body md:min-h-0">
              <input type="checkbox" checked={ticked} disabled={busy} onChange={(e) => setTickedRevision(e.target.checked ? a.revision : null)} />
              I checked the shelf and the backroom for this lot
            </label>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button kind="primary" disabled={(!isExpiry(r) && !ticked) || busy} aria-busy={confirm.busy || undefined}
              className="min-h-11 md:min-h-8"
              onClick={() => void confirm.run(isExpiry(r) ? { signal: a.signal_id, revision: a.revision }
                : { signal: a.signal_id, revision: a.revision, checked_both: true })}>
              {isExpiry(r) ? CONFIRM_DATE : CONFIRM_COUNT}
            </Button>
            {confirm.busy && <span className="text-label text-tx2"><Elapsed label="Recomputing the plan" /></span>}
            <ErrorLine error={confirm.error} action={reloadAction(confirm)} />
          </div>
          {!isExpiry(r) && !ticked && !confirm.busy && <p className="m-0 text-label text-tx3">Tick the box to confirm</p>}
          <p className="m-0 max-w-md text-label text-tx3">{CONFIRM_NOTE_P7}</p>
        </div>
      )}
      {confirmedText.length > 0 && (
        <div className="flex flex-col gap-1">
          {confirmedText.map((label, i) => (
            <p key={i} className="num m-0 rounded-md bg-good-soft px-2 py-1 text-body text-good break-words">{label}</p>
          ))}
          {others.map((c) => (
            <a key={c.case_id} href={viewHref(c.case_id)} aria-label={`${TERMS.VIEW_STOCK_DECISION} ${c.key}`}
              className={`${VIEW_LINK} self-start`}>{TERMS.VIEW_STOCK_DECISION}</a>
          ))}
        </div>
      )}
      <History r={r} />
    </section>
  );
}

// An expiry part's per-lot choice: "Printed date" (a business date) or "Mixed dates in this lot".
function DateFields({ r, lot, value, onChange }: { r: Request; lot: string; value: DateDraft; onChange: (n: Partial<DateDraft>) => void }) {
  const name = `${r.request_id}-${lot}-date-mode`;
  const box = `${r.request_id}-${lot}-date`;
  return (
    <fieldset className="m-0 min-w-0 border-0 p-0">
      <legend className="mb-1 p-0 text-body font-semibold">Printed date or mixed dates for lot {lot}</legend>
      <label className="flex min-h-11 items-center gap-2 text-body md:min-h-0">
        <input type="radio" name={name} checked={value.mode === "date"} onChange={() => onChange({ mode: "date" })} />
        {PRINTED_DATE}
      </label>
      {value.mode === "date" && (
        <span className="flex flex-col gap-1 pl-6">
          <label htmlFor={box} className="text-label text-tx3">{PRINTED_DATE_BOX}</label>
          <input id={box} type="date" value={value.date} onChange={(e) => onChange({ date: e.target.value })}
            aria-describedby={`${box}-help`}
            className="num h-11 w-48 max-w-full rounded-md border border-line-2 bg-white px-2 text-body md:h-8" />
          <span id={`${box}-help`} className="text-label text-tx3">{PRINTED_DATE_HELP}</span>
        </span>
      )}
      <label className="flex min-h-11 items-center gap-2 text-body md:min-h-0">
        <input type="radio" name={name} checked={value.mode === "mixed"} onChange={() => onChange({ mode: "mixed" })} />
        {MIXED_DATES}
      </label>
    </fieldset>
  );
}

// The part's history: a replacement, a close without an answer, a late answer (kept, with its label).
function History({ r }: { r: Request }) {
  const { session } = useSession();
  const lines: ReactNode[] = [];
  if (r.replaced_by) lines.push(replacedBy(r.replaced_by));
  if (r.state === "CANT_CHECK" && r.closed_at) lines.push(`${CANT_CHECK} · ${recordedTime(r.closed_at, session.time_zone)}`);
  if (r.state === "HELD" && r.closed_at) lines.push(`${MIXED_DATES} · lot held · ${recordedTime(r.closed_at, session.time_zone)}`);
  if (r.late === true && r.answered_at) lines.push(`Answered after deadline · ${recordedTime(r.answered_at, session.time_zone)}`);
  if (lines.length === 0) return null;
  return (
    <div data-testid="request-history" className="flex flex-col gap-1">
      <h5 className="m-0 text-label font-semibold text-tx2">{HISTORY}</h5>
      <ul className="num m-0 list-disc pl-6 text-label text-tx2">
        {lines.map((l, i) => <li key={i} className="break-words">{l}</li>)}
      </ul>
    </div>
  );
}

// The page's draft map, kept outside the list so a refetch never wipes it.
export function useDrafts(): Drafts {
  const [all, setAll] = useState<Record<string, Draft>>({});
  const set = useCallback((key: string, next: Draft) => setAll((d) => ({ ...d, [key]: next })), []);
  const drop = useCallback((requestId: string) => setAll((d) => Object.fromEntries(
    Object.entries(d).filter(([k]) => !k.startsWith(`${requestId}:`)))), []);
  return { all, set, drop };
}
