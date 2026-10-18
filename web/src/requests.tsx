// Stock checks on Tasks (06.1-UI-SPEC "Tasks" section 1; D-39, D-50, D-58, D-61; 05-UI-SPEC "Requests (D-01, D-02)"
// with Amendments A-11, A-12, A-24 and A-26 kept): the count requests addressed to the signed-in store, newest first
// (the server's order), each with the answer form (per lot Shelf and Backroom under the lot's fieldset legend, an
// optional reply), the read-only response summary of an answered request (the stacked Kv capped at max-w-md) with
// View stock decision and Request recount (G-05-2 item 12) and the confirm block. An EXPIRED request still takes a
// late answer (spec §7 rev 6.7; 05-16 Task 3) while the server says an answer sent now could be confirmed
// (`answerable`: a live stock decision), never Confirm count for the answer it held from an earlier day. Confirm count
// shows only when the server says confirm() would take the answer (`confirmable`; 05-REVIEW CR-02, WR-10): an answer
// from an earlier day asks for a new count instead. The page (pages/tasks.tsx) loads this list on route entry, when
// the tab becomes visible again, after the visitor's own answer or confirm, and when the workspace version changes;
// signing in as another account remounts the page. Nothing polls on an interval (05-CONTEXT Freshness). A blank count
// is sent as null, never 0. No photo, no sample answers and no "Can't check" (Phase 7).
//
// Two clocks (D-58): the row line names when the stock check was assigned, "Recorded {17 Oct 2026, 02:45} Bangkok
// time", read from the stock decision's history (the request JSON carries no time of its own); the due line is the
// planned time with its scenario date; the response summary's "Recorded" row is the answer's recorded server time in
// the session's zone. A time that cannot be read leaves its part out, never a placeholder.
//
// Typed values live in one map keyed by draftKey (request id and answer revision), outside the list, so a refetch
// never clears them; when a refetch brings a new revision for a request with a kept draft, the draft stays on screen
// with "The answer changed since you looked at it" instead of being merged (research Pattern 5, Pitfall 9).
import { useCallback, useState, type ReactNode } from "react";
import {
  ApiError, form, get, post, type ActivityEntry, type AnswerSignal, type CaseDetail, type CaseState, type ConfirmResult,
  type LotCount, type Request, type Session,
} from "./api.ts";
import { confirmedLines, errorAction, lotCountText, responseNote, responseRows, statePill } from "./copy.ts";
import { canReplan, canRequestRecount, countsBody, draftKey, invalidFields, type LotDraft } from "./derive.ts";
import { hhmm } from "./format.ts";
import { useAction } from "./hooks.ts";
import { useSession } from "./shell.tsx";
import { Button, Elapsed, ErrorLine, Kv, Pill, TRY_AGAIN, type IconName } from "./ui.tsx";
import { RELOAD_DECISION, cityOf, recordedLine, scenarioDateLine, TERMS } from "./vocab.ts";

type Draft = { lots: Record<string, LotDraft>; text: string };
const EMPTY: Draft = { lots: {}, text: "" };
const BLANK_LOT: LotDraft = { shelf: "", backroom: "" };

// --- the words (06.1-UI-SPEC "Tasks"; a "(proposed)" string is Claude's or the UI-SPEC's, behind its own name) ------

// "Stock check assigned to S017 · Recorded 17 Oct 2026, 02:45 Bangkok time." (D-58, the user's form); without a
// readable recorded time, "Stock check assigned to S017."
export function assignedLine(to: string, recorded: string): string {
  return `${TERMS.STOCK_CHECK} assigned to ${to}${recorded === "" ? "" : ` · ${recorded}`}.`;
}
// "Respond by 09:45 (planned) · Scenario date: Sat 17 Oct 2026" (proposed; Phase 7 replaces it with the real
// deadline). The scenario date is the session calendar's date of the due day; a day the calendar lacks leaves it out.
export function respondByLine(min: number, dateIso: string | null): string {
  return `Respond by ${hhmm(min)} (planned)${dateIso === null ? "" : ` · ${scenarioDateLine(dateIso)}`}`;
}
// The EXPIRED line with the D-58 time form and the D-61 terms (proposed; replaces "This request expired on {Thu
// 09:45}. The case shows the fact as unverified.").
export function expiredLine61(min: number, dateIso: string | null): string {
  return `This ${TERMS.STOCK_CHECK.toLowerCase()} expired at ${hhmm(min)} (planned)`
    + `${dateIso === null ? "" : ` · ${scenarioDateLine(dateIso)}`}. The stock decision shows the fact as unverified.`;
}
// The response summary's recorded-time row: "Recorded" over "17 Oct 2026, 02:45 Bangkok time" (replaces "Submitted").
export const RECORDED_LABEL = "Recorded";
// The confirm block's note with the D-61 term (was "It recomputes the case, …").
export const CONFIRM_NOTE = "Confirming the count approves nothing. It recomputes the stock decision, which cancels any "
  + "approval in progress; send it again after checking the new plan.";
// A recount refused because the stock decision moved on (UI-SPEC stale_version, D-61 term).

// A server answer the request must be reloaded after: its answer moved on, or the request is no longer answerable.
const MOVED_ON = new Set(["stale_version", "wrong_state"]);

// A request the server will not confirm (05-REVIEW CR-02, WR-10, WR-12). EARLIER_DAY is the API's own refusal
// (api/signals.py EARLIER_DAY), shown on an ANSWERED request when an answer sent now could still be confirmed
// (`answerable`). When neither `confirmable` nor `answerable` holds, the stock decision's state says why: FINISHED
// (the user's words, 2026-10-17) on an ANSWERED request of a decision a count no longer re-plans, the late answer kept
// in history only; lotGone (proposed) on a live decision, where the only reason left is a counted lot that is no
// longer on the record at the addressed branch (api/signals.py request_view).
const EARLIER_DAY = "This count is from an earlier day. Send today's count, then confirm it.";
const FINISHED = "Saved to history. This stock decision is closed, so this count cannot be confirmed.";
const lotGone = (r: Request) => `${r.lot_ids.length === 1 ? "The counted lot is" : "A counted lot is"} no longer on `
  + `record at ${r.to_branch}, so this count cannot be confirmed.`;

// One icon per request state, so no chip is colour alone (R-6).
const STATE_ICON: Record<Request["state"], IconName> = {
  OPEN: "inbox", ANSWERED: "alert", CONFIRMED: "check", EXPIRED: "clock", SUPERSEDED: "redo",
};

// True when the server will neither confirm the request's answer nor any answer sent now: its stock decision's state
// is read to name the reason (WR-12).
const blocked = (r: Request) => !r.confirmable && !r.answerable
  && (r.state === "OPEN" || r.state === "ANSWERED" || r.state === "EXPIRED");

// True when the signed-in store has something to do on this stock check (the 4px navy rule, R-4): the server says an
// answer sent now could be confirmed, or that confirm would take the stored answer.
export function stockCheckActs(r: Pick<Request, "state" | "answerable" | "confirmable">): boolean {
  return r.answerable || (r.state === "ANSWERED" && r.confirmable);
}

// The ISO scenario date of a calendar day, or null when the calendar does not hold it.
export function dayDate(cal: Session["calendar"], day: number): string | null {
  return cal.find((c) => c.day === day)?.date ?? null;
}

// --- the read (pages/tasks.tsx calls it as part of the page's one load) ---------------------------------------------

// The stock checks, with the state of each blocked request's stock decision (read only for those, from its detail;
// the Decision board is scoped to the signed-in store and paged, so it cannot answer for another store's decision) and
// the recorded time each request was assigned (from its decision's history, `evidence_requested` with its
// request_id). `known` holds the times read before: a request's time never changes, so it is read once. A failed side
// read leaves its line out rather than guessing it.
export type StockCheckData = { list: Request[]; caseStates: Record<string, CaseState>; assignedAt: Record<string, string> };

const unique = (xs: string[]) => [...new Set(xs)];
const casePath = (id: string, tail = "") => `/api/cases/${encodeURIComponent(id)}${tail}`;

export async function loadStockChecks(known: Record<string, string>): Promise<StockCheckData> {
  const list = await get<Request[]>("/api/requests");
  const stuck = unique(list.filter(blocked).map((r) => r.case_id));
  const untimed = unique(list.filter((r) => known[r.request_id] === undefined).map((r) => r.case_id));
  const [states, times] = await Promise.all([
    Promise.all(stuck.map(async (id) => {
      try {
        return [[id, (await get<CaseDetail>(casePath(id))).state] as const];
      } catch {
        return [];
      }
    })),
    Promise.all(untimed.map(async (id) => {
      try {
        const entries = await get<ActivityEntry[]>(casePath(id, "/activity"));
        return entries.flatMap((e) => (e.type === "evidence_requested" && typeof e.payload.request_id === "string"
          ? [[e.payload.request_id, e.at] as const] : []));
      } catch {
        return [];
      }
    })),
  ]);
  return {
    list,
    caseStates: Object.fromEntries(states.flat()),
    assignedAt: { ...known, ...Object.fromEntries(times.flat()) },
  };
}

// --- the section body ---------------------------------------------------------------------------------------------

// The list of stock check cards. `after` reloads the page's data and the session (its counts) after the visitor's own
// answer, confirm or recount.
export function StockChecks({ data, after }: { data: StockCheckData; after: () => Promise<void> }) {
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});

  // The draft shown for a request: the one typed against its current revision, else a kept draft typed against an
  // earlier revision (shown with the changed-answer line), else an empty one.
  const draftFor = (r: Request): { key: string; draft: Draft; changed: boolean } => {
    const current = draftKey(r);
    if (drafts[current] !== undefined) return { key: current, draft: drafts[current], changed: false };
    const kept = Object.keys(drafts).find((k) => k.startsWith(`${r.request_id}:`) && dirty(drafts[k]));
    return kept === undefined ? { key: current, draft: EMPTY, changed: false } : { key: kept, draft: drafts[kept], changed: true };
  };
  const setDraft = useCallback((key: string, next: Draft) => setDrafts((all) => ({ ...all, [key]: next })), []);
  const dropDrafts = useCallback((requestId: string) => setDrafts((all) => Object.fromEntries(
    Object.entries(all).filter(([k]) => !k.startsWith(`${requestId}:`)))), []);

  return (
    <ul className="m-0 flex list-none flex-col gap-4 p-0">
      {data.list.map((r) => {
        const { key, draft, changed } = draftFor(r);
        return (
          <li key={r.request_id} className="min-w-0">
            <RequestCard r={r} caseState={data.caseStates[r.case_id]} assignedAt={data.assignedAt[r.request_id]}
              draft={draft} changed={changed} after={after}
              onDraft={(next) => setDraft(key, next)} onSent={() => dropDrafts(r.request_id)} />
          </li>
        );
      })}
    </ul>
  );
}

// True when the visitor typed anything into the draft.
const dirty = (d: Draft) => d.text !== "" || Object.values(d.lots).some((l) => l.shelf !== "" || l.backroom !== "");

const VIEW_LINK = "text-body font-semibold text-navy-800 underline underline-offset-2";
const viewHref = (caseId: string) => `#/cases/${encodeURIComponent(caseId)}`;

// The stored answer of a request that shows no summary (an OPEN one with a partial reply, an EXPIRED one): per lot
// through lotCountText ("Shelf 4 · Backroom 2 · Total 6 units", or "Shelf: not checked" with no total), then the reply.
function StoredAnswer({ a, lotIds }: { a: AnswerSignal; lotIds: string[] }) {
  const counts = a.fields.counts;
  const rows: [string, string][] = lotIds.map((id) => {
    const c: LotCount = counts[id] ?? { shelf_units: null, backroom_units: null };
    return [`Lot ${id}`, lotCountText(c)];
  });
  if (a.text !== "") rows.push(["Reply", a.text]);
  return <div className="max-w-md"><Kv stacked items={rows} /></div>;
}

// The states that read back as a response (G-05-2 item 12): the answer is shown read-only, never as a form.
const ANSWERED_STATES: readonly Request["state"][] = ["ANSWERED", "CONFIRMED", "SUPERSEDED"];

// copy.responseRows with its recorded-time row in the D-58 form: "Recorded" over "17 Oct 2026, 02:45 Bangkok time".
function summaryRows(r: Request, timeZone: string): [string, string][] {
  return responseRows(r, timeZone).map(([k, v]) => (k === "Submitted" ? [RECORDED_LABEL, `${v} ${cityOf(timeZone)} time`] : [k, v]));
}

// The read-only response summary (user wording 2026-10-16, with D-58's recorded row): the lots, the reply, who
// submitted it (the recorded seat), when it was recorded (the recorded server time in the session's zone), its Status,
// then "Answered after deadline" as a warn chip on a line of its own when the answer came after its due day, and the
// actions: View stock decision, and Request recount (`actions`) when permitted; the actions' error shows under them.
function ResponseSummary({ r, timeZone, actions, error }: { r: Request; timeZone: string; actions?: ReactNode; error?: ReactNode }) {
  const note = responseNote(r);
  return (
    <div className="flex flex-col gap-2">
      <div data-testid="response-summary" className="max-w-md"><Kv stacked items={summaryRows(r, timeZone)} /></div>
      {note !== null && <p data-testid="response-note" className="m-0"><Pill tone="warn" icon="clock">{note}</Pill></p>}
      <div className="flex flex-wrap items-center gap-4">
        <a href={viewHref(r.case_id)} className={VIEW_LINK}>{TERMS.VIEW_STOCK_DECISION}</a>
        {actions}
      </div>
      {error}
    </div>
  );
}

type CardProps = {
  r: Request;
  caseState: CaseState | undefined; // read only for a blocked request
  assignedAt: string | undefined; // the recorded time the request was made, when its history could be read
  draft: Draft;
  changed: boolean;
  after: () => Promise<void>;
  onDraft: (next: Draft) => void;
  onSent: () => void;
};

// One stock check. Confirm errors (needs_input, scope_tick, count_mismatch, stale_version) show the server's message
// word for word next to Confirm count; a needs_input also marks each listed field with a bad border and aria-invalid.
function RequestCard({ r, caseState, assignedAt, draft, changed, after, onDraft, onSent }: CardProps) {
  const { session } = useSession();
  const [tickedRevision, setTickedRevision] = useState<number | null>(null);
  const [result, setResult] = useState<ConfirmResult | null>(null);

  // A write the server refused because the answer or the request moved on reloads the list; the message stays.
  const reloadOn = async <T,>(write: () => Promise<T>): Promise<T> => {
    try {
      return await write();
    } catch (e) {
      if (e instanceof ApiError && MOVED_ON.has(e.code)) void after();
      throw e;
    }
  };
  const confirm = useAction(async (body: { signal: string; revision: number; checked_both: true }, key: string) => {
    const out = await reloadOn(() => post<ConfirmResult>(`/api/signals/${encodeURIComponent(body.signal)}/confirm`,
      { revision: body.revision, checked_both: body.checked_both }, key));
    setResult(out);
    await after();
    return out;
  });

  const answer = useAction(async (body: { text: string; counts: Record<string, LotCount> }, key: string) => {
    const data = new FormData();
    data.set("text", body.text);
    data.set("counts", JSON.stringify(body.counts));
    const out = await reloadOn(() => form<AnswerSignal>(
      `/api/requests/${encodeURIComponent(r.request_id)}/answer`, data, key));
    onSent();
    confirm.clear(); // a refused confirm was about the earlier answer, never this one (05-REVIEW WR-13)
    await after();
    return out;
  });
  // Request recount (the stock decision's source manager's; the server's `recountable` and the seat): one keyed POST
  // that makes a new request for the fact and keeps this response as it is (05-17); afterwards the page refetches.
  const recount = useAction(async (body: { rec_version: string; fact_id: string; recount_of: string }, key: string) => {
    const out = await post<Request>(`/api/cases/${encodeURIComponent(r.case_id)}/requests`, body, key);
    await after();
    return out;
  });
  const open = r.state === "OPEN" || r.state === "ANSWERED";
  const a = r.answer;
  const missing = a !== null && a.fields.needs_input.length > 0;
  const complete = a !== null && a.state === "READ" && !missing;
  const ticked = a !== null && tickedRevision === a.revision;
  const busy = answer.busy || confirm.busy || recount.busy;
  const invalid = invalidFields(confirm.error);
  const pill = statePill(r);
  // An answered request reads back as a response, never as an empty form beside the stored count (G-05-2 item 12).
  const summary = a !== null && ANSWERED_STATES.includes(r.state);
  // An ANSWERED request whose answer the server will not confirm while an answer sent now could be: for an ANSWERED
  // request that only means the answer is from an earlier day (send a new count). The server's flags alone decide it,
  // never the client's own day (05-REVIEW CR-02, WR-10, IN-15).
  const earlierDay = r.state === "ANSWERED" && a !== null && !r.confirmable && r.answerable;
  // Neither flag holds: the decision is finished (FINISHED on its late answer) or, on a live one, a lot left the record.
  const stuck = blocked(r) && caseState !== undefined;
  const finished = stuck && caseState !== undefined && !canReplan(caseState) && r.state === "ANSWERED" && a !== null;
  const lotLeft = stuck && caseState !== undefined && canReplan(caseState);
  // The form shows only while the server calls the request `answerable` (an answer sent now could be confirmed now;
  // 05-REVIEW WR-10, WR-12): an OPEN request; an EXPIRED one, for a late answer (spec §7 rev 6.7, 05-16 Task 3), below
  // its expired line and any earlier answer, never with Confirm count; an ANSWERED one to send a new count over an
  // earlier day's answer, to show a kept draft typed against an earlier revision (05-CONTEXT Freshness: typed input is
  // never wiped) or to fill the counts a confirm was refused for (409 needs_input marks them).
  const showForm = r.answerable && (r.state === "OPEN" || r.state === "EXPIRED"
    || (r.state === "ANSWERED" && (changed || invalid.size > 0 || earlierDay)));
  const confirmedText = result?.labels ?? (r.state === "CONFIRMED" ? confirmedLines(r) : []);
  // The confirm's other re-planned decisions (this card's own link already opens this one).
  const others = (result?.cases ?? []).filter((c) => c.case_id !== r.case_id);
  const setLot = (id: string, field: keyof LotDraft, value: string) => onDraft({
    ...draft, lots: { ...draft.lots, [id]: { ...(draft.lots[id] ?? BLANK_LOT), [field]: value } },
  });
  const retryOf = (act: { error: ApiError | null; retry: () => Promise<unknown> }) =>
    act.error !== null && errorAction(act.error.code) === "retry"
      ? { label: TRY_AGAIN, onClick: () => void act.retry() } : undefined;
  // A recount the server refused because the decision moved on offers Reload the stock decision (the page refetches);
  // a busy server offers Try again with the same key; any other refusal shows its message alone.
  const recountAction = recount.error === null ? undefined
    : errorAction(recount.error.code) === "reload_case" ? { label: RELOAD_DECISION, onClick: () => { recount.clear(); void after(); } }
    : retryOf(recount);
  const due = dayDate(session.calendar, r.due_day);
  const acts = stockCheckActs(r);
  const titleId = `stock-check-${r.request_id}`;

  return (
    <article aria-labelledby={titleId} data-request-id={r.request_id} data-acts={acts || undefined}
      className={`min-w-0 rounded-2xl border border-line bg-white p-4 shadow-sm ${acts ? "border-l-4 border-l-navy-800" : ""}`}>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 id={titleId} className="m-0 min-w-0 text-title font-semibold break-words">
          {r.case_key} · lot {r.lot_ids.join(", ")}
        </h3>
        <span className="flex flex-wrap gap-1">
          <Pill tone={pill.tone} icon={STATE_ICON[r.state]}>{pill.text}</Pill>
          {r.partial_note !== null && <Pill tone="warn" icon="alert">{r.partial_note}</Pill>}
        </span>
      </div>
      <div className="flex flex-col gap-2">
        <p data-testid="assigned-line" className="num m-0 text-label text-tx2 break-words">
          {assignedLine(r.to_branch, assignedAt === undefined ? "" : recordedLine(assignedAt, session.time_zone))}
        </p>
        <p className="m-0 text-body break-words">{r.ask_text}</p>
        <p data-testid="due-line" className="num m-0 text-label text-tx2 break-words">{respondByLine(r.due_min, due)}</p>
        {summary ? (
          <ResponseSummary r={r} timeZone={session.time_zone}
            actions={canRequestRecount(r, session) && (
              <Button kind="plain" icon="redo" busy={recount.busy} disabled={answer.busy || confirm.busy}
                className="min-h-11 md:min-h-8"
                onClick={() => void recount.run({ rec_version: r.rec_version, fact_id: r.fact_id, recount_of: r.request_id })}>
                Request recount
              </Button>
            )}
            error={<ErrorLine error={recount.error} action={recountAction} />} />
        ) : (
          <>
            <a href={viewHref(r.case_id)} className={`${VIEW_LINK} self-start`}>{TERMS.VIEW_STOCK_DECISION}</a>
            {a !== null && <StoredAnswer a={a} lotIds={r.lot_ids} />}
          </>
        )}
        {open && missing && (
          <p className="m-0 rounded-md bg-warn-soft px-2 py-1 text-body text-warn">
            Shelf or backroom not checked, so the total can't be confirmed.
          </p>
        )}
        {r.state === "EXPIRED" && (
          <p data-testid="expired-line" className="num m-0 text-label text-tx2 break-words">{expiredLine61(r.due_min, due)}</p>
        )}
        {earlierDay && (
          <p data-testid="earlier-day" className="m-0 rounded-md bg-warn-soft px-2 py-1 text-body text-warn">{EARLIER_DAY}</p>
        )}
        {finished && <p data-testid="not-confirmable" className="m-0 text-label text-tx2">{FINISHED}</p>}
        {lotLeft && <p data-testid="lot-left" className="m-0 text-label text-tx2">{lotGone(r)}</p>}
        {showForm && (
          <form className="flex flex-col gap-2" onSubmit={(e) => {
            e.preventDefault();
            void answer.run({ text: draft.text, counts: countsBody(r.lot_ids, draft.lots) });
          }}>
            {changed && (
              <p role="status" className="m-0 rounded-md bg-warn-soft px-2 py-1 text-body text-warn">
                The answer changed since you looked at it
              </p>
            )}
            <fieldset disabled={busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
              {r.lot_ids.map((id) => {
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
                            <label htmlFor={inputId} className="text-label text-tx3">
                              {field === "shelf" ? "Shelf" : "Backroom"}
                            </label>
                            <input id={inputId} type="number" inputMode="numeric" min="0" placeholder="Not checked"
                              value={v[field]} onChange={(e) => setLot(id, field, e.target.value)}
                              aria-invalid={bad || undefined}
                              className={`num h-11 w-40 max-w-full rounded-md border bg-white px-2 text-body md:h-8 ${bad ? "border-bad" : "border-line-2"}`} />
                          </span>
                        );
                      })}
                    </div>
                  </fieldset>
                );
              })}
              <span className="flex max-w-md flex-col gap-1">
                <label htmlFor={`${r.request_id}-reply`} className="text-label text-tx3">Reply</label>
                <textarea id={`${r.request_id}-reply`} rows={2} value={draft.text}
                  onChange={(e) => onDraft({ ...draft, text: e.target.value })}
                  className="rounded-md border border-line-2 bg-white px-2 py-1 text-body" />
              </span>
              <span><Button kind="primary" type="submit" busy={answer.busy} className="min-h-11 md:min-h-8">Send answer</Button></span>
            </fieldset>
            <ErrorLine error={answer.error} action={retryOf(answer)} />
          </form>
        )}
        {r.state === "ANSWERED" && r.confirmable && complete && a !== null && (
          <div className="flex flex-col gap-2 border-t border-line pt-2">
            <label className="flex min-h-11 items-center gap-2 text-body md:min-h-0">
              <input type="checkbox" checked={ticked} disabled={busy}
                onChange={(e) => setTickedRevision(e.target.checked ? a.revision : null)} />
              I checked the shelf and the backroom for this lot
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <Button kind="primary" disabled={!ticked || busy} aria-busy={confirm.busy || undefined}
                className="min-h-11 md:min-h-8"
                onClick={() => void confirm.run({ signal: a.signal_id, revision: a.revision, checked_both: true })}>
                Confirm count
              </Button>
              {confirm.busy && <span className="text-label text-tx2"><Elapsed label="Recomputing the plan" /></span>}
              <ErrorLine error={confirm.error} action={retryOf(confirm)} />
            </div>
            {!ticked && !confirm.busy && <p className="m-0 text-label text-tx3">Tick the box to confirm</p>}
            <p className="m-0 max-w-md text-label text-tx3">{CONFIRM_NOTE}</p>
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
      </div>
    </article>
  );
}
