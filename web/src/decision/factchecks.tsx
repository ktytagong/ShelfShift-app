// The Context step's fact checks (07-21; 07-UI-SPEC surface 1 "Context step: the fact-check block", "Request actions",
// "Exits from a blocked Required row"; 07-CONTEXT D-06, D-07, D-10, D-11, D-12, D-23, D-24, D-36). This module sits
// beside the step modules, not under steps/ (the page loads every steps/*.tsx as a step); context.tsx draws it.
// Rules kept here:
// - One row per fact check in the engine's order, as the server lists them (not-allowed first; the client never
//   sorts). Each row: the class chip, the title, the exposure chip, the request's compact deadline and its state
//   chip (the server's sentence), and one action. "Details" opens why it was flagged, the engine's sentence, and for a
//   Required check its limit, owner and resolution.
// - The exposure is a decision regret, never cash or savings (D-10); a not-allowed fact reads "Unverified · plan may
//   not be allowed" with no amount.
// - Count stock and Check printed expiry date open the inline request panel; Send request is one keyed write
//   (hooks.useAction, one Idempotency-Key per press, kept for Try again); the decision and the session refetch after.
//   The deadline minutes and the request limit come from the session's `requests` (settings); the requests left from
//   the server's `requests_left`. A refusal shows the server's message word for word with its mapped action.
// - The compact deadline is display only (D-06): the minutes come from the server's minutes_left, re-rendered once a
//   minute and when the tab is shown again, and one refetch when it runs out; the state chip is always the server's.
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useEffect, useRef, useState } from "react";
import {
  post, type CaseOption, type Request, type RequestSettings, type Session, type VerifiedFact, type VerifyDetailKeys,
} from "../api.ts";
import { UNVERIFIED_NOT_ALLOWED, unverifiedExposure } from "../boardview.ts";
import type { CaseDetail61, Session61 } from "../contract.ts";
import {
  ADVISORY_CHIP, ALSO_NEEDS_THIS_FACT, ASK_AGAIN, CHECK_PRINTED_EXPIRY, COUNT_STOCK, DONT_SEND_REQUEST, KEEP_CURRENT_PLAN,
  OPTION_NET, REQUEST_PANEL_NAME, REQUIRED_CHIP, SELECT_ANOTHER_PLAN, SELECT_OPTION, SELECT_THIS_PLAN, SEND_REQUEST,
  SUPERSEDE_NOTE, awaitingConfirmText, errorAction, factSentence, factTitle, factWhy, minutesLeftText, oneRequestLine,
  replacedText, requestsLeftLine, requestsUsedLine, responseMinutesLine, responsibleLine, selectConfirmLine,
  selectOptionName, statePill,
} from "../copy.ts";
import {
  checkPart, companionFact, factAction, isMe, requestBody, requestForFact, type CheckPart,
} from "../derive.ts";
import { money, units } from "../format.ts";
import { useAction } from "../hooks.ts";
import { useSession } from "../shell.tsx";
import { Button, ErrorLine, Pill, TRY_AGAIN } from "../ui.tsx";
import { useEvidenceDrafts } from "./assistant.tsx"; // 09-20
import { RELOAD_DECISION } from "../vocab.ts";
import {
  CANCEL_OPEN, REJECT_OPEN, ReasonForm, WriteError, cancelWrite, decisionWrite, rejectWrite, selectWrite, useStepWrite,
} from "./approval.tsx";

// --- strings ---------------------------------------------------------------------------------------------------------

export const FACTS_TITLE = "Fact checks";
export const NO_FACTS = "The engine flagged no suspect facts for this version.";
export const DETAILS = "Details";
export const WHY_FLAGGED = "Why flagged"; // (proposed)
export const IF_TRUE = "If true"; // (proposed)
export const LIMIT = (reasons: string[]) => `Limit: ${reasons.join("; ")}`;
export const OWNER = (store: string) => `Owner: ${store} manager`;
export const RESOLVE_BY = "Resolve by: a confirmed count or printed date";
export const LOTS = "Lots"; // (proposed) the expanded row's lot list
export const TITLE_MAX = 90; // a collapsed row's title, then an ellipsis (07-UI-SPEC E1 overflow)
// The requester's view of a confirmed count (the 06.1 D-61 words, kept).
export const COUNT_CONFIRMED = (to: string) => `Count confirmed by ${to} manager`;

// A collapsed title within TITLE_MAX characters; the full text shows in the expanded row.
export function clipTitle(text: string): string {
  return text.length <= TITLE_MAX ? text : `${text.slice(0, TITLE_MAX - 1)}…`;
}

// The request's state as the case shows it (07-UI-SPEC "Request and answer states"): a complete answer awaiting the
// lot's manager in the requester's words, a confirmed count with its figures, a replaced request in copy.ts's words,
// otherwise the server's own sentence, else the state's pill word.
export function requestLines(r: Request): string[] {
  const part: CheckPart = checkPart(r.fact_kind) ?? "count";
  if (r.state === "CONFIRMED") {
    const counts = r.answer?.fields.counts ?? {};
    const lines = r.lot_ids.flatMap((id) => {
      const c = counts[id];
      if (c === undefined || c.shelf_units === null || c.backroom_units === null) return [];
      return [`${COUNT_CONFIRMED(r.to_branch)} · shelf ${units(c.shelf_units)} + backroom ${units(c.backroom_units)} = `
        + `${units(c.shelf_units + c.backroom_units)}`];
    });
    return lines.length === 0 ? [r.status ?? COUNT_CONFIRMED(r.to_branch)] : lines;
  }
  if (r.state === "SUPERSEDED") return [replacedText(r)];
  if (r.status_kind === "ANSWERED" || (r.state === "ANSWERED" && (r.status ?? null) === null)) {
    return [awaitingConfirmText(part, r.to_branch)];
  }
  return [r.status ?? statePill(r).text];
}

const settingsOf = (s: Session) => (s as Session & { requests?: RequestSettings }).requests ?? null;
const factsOf = (d: CaseDetail61) => d.fact_checks as VerifiedFact[];

// --- the compact deadline --------------------------------------------------------------------------------------------

// The request's deadline drawn to scale from asked_at to due_at, with the minutes copy.minutesLeftText words (D-06). Only while the request is open. One refetch once the minutes run out (the server applies the deadline on
// read); nothing else is fetched.
function CompactDeadline({ r, onDue }: { r: Request; onDue: () => void }) {
  const [mounted] = useState(() => Date.now());
  const [, setTick] = useState(0);
  const fired = useRef(false);
  useEffect(() => {
    const tick = () => setTick((x) => x + 1);
    const timer = setInterval(tick, 60_000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, []);
  const ok = r.state === "OPEN" && r.asked_at != null && r.due_at != null && r.server_now != null && r.minutes_left != null;
  const since = Date.now() - mounted;
  const asked = ok ? Date.parse(r.asked_at as string) : 0;
  const due = ok ? Date.parse(r.due_at as string) : 0;
  const now = ok ? Date.parse(r.server_now as string) + since : 0;
  const left = ok ? (r.minutes_left as number) - Math.floor(since / 60_000) : 0;
  const overdue = ok && (r.overdue === true || now >= due);
  const runOut = ok && r.overdue !== true && now >= due;
  useEffect(() => {
    if (runOut && !fired.current) {
      fired.current = true;
      onDue();
    }
  }, [runOut, onDue]);
  if (!ok) return null;
  const span = Math.max(1, due - asked);
  const width = overdue ? 1 : Math.min(1, Math.max(0, (now - asked) / span));
  return (
    <span data-testid="deadline-bar" className="flex items-center gap-2">
      <span aria-hidden="true" className="relative block h-2 w-24 shrink-0 overflow-hidden rounded-full bg-sunken">
        <span className={`block h-2 rounded-full ${overdue ? "bg-warn" : "bg-navy-700"}`} style={{ width: `${width * 100}%` }} />
      </span>
      <span className="text-label text-tx2">{minutesLeftText(Math.max(0, left), overdue)}</span>
    </span>
  );
}

// --- the request panel -----------------------------------------------------------------------------------------------

const PART_LABEL: Readonly<Record<CheckPart, string>> = { count: COUNT_STOCK, expiry: CHECK_PRINTED_EXPIRY };

// The inline panel behind Count stock, Check printed expiry date and Ask again: the parts on offer for the lot (the
// pressed one ticked; the other check of the same lot when the server would pair them), the lots in mono, the
// once-per-version line when both are ticked, the minutes the branch has, Send request and Don't send request.
function RequestPanel({ d, f, again, onClose, reload }: {
  d: CaseDetail61; f: VerifiedFact; again: Request | null; onClose: () => void; reload: () => void;
}) {
  const { session, refresh } = useSession();
  const settings = settingsOf(session);
  const other = again === null ? companionFact({ ...d, fact_checks: factsOf(d) }, f) : null;
  const offered = other === null ? [f] : [f, other];
  const [ticked, setTicked] = useState<string[]>([f.fact_id]);
  const first = useRef<HTMLInputElement>(null);
  useEffect(() => first.current?.focus(), []);
  const send = useAction(async (body: Record<string, unknown>, key: string) => {
    const out = await post<Request>(`/api/cases/${encodeURIComponent(d.case_id)}/requests`, body, key);
    onClose();
    reload();
    await refresh().catch(() => {}); // the next page load or write shows a session failure
    return out;
  });
  const parts = offered.filter((x) => ticked.includes(x.fact_id));
  const version = d.rec_version;
  const next = send.error === null ? "none" : errorAction(send.error.code);
  const action = next === "reload_case" ? { label: RELOAD_DECISION, onClick: () => { send.clear(); reload(); } }
    : next === "retry" ? { label: TRY_AGAIN, onClick: () => void send.retry() } : undefined;
  return (
    <div role="group" aria-label={REQUEST_PANEL_NAME} data-testid="request-panel"
      className="flex min-w-0 flex-col items-start gap-2 rounded-lg border border-line bg-sunken p-4">
      {offered.map((x, i) => {
        const part = checkPart(x.kind) ?? "count";
        const id = `ask-${d.case_id}-${x.fact_id}`;
        return (
          <label key={x.fact_id} htmlFor={id} className="flex min-h-11 max-w-full flex-wrap items-center gap-2 text-body md:min-h-0">
            <input ref={i === 0 ? first : undefined} id={id} type="checkbox" checked={ticked.includes(x.fact_id)}
              disabled={send.busy} aria-label={PART_LABEL[part]}
              onChange={(e) => setTicked((t) => (e.target.checked ? [...t, x.fact_id] : t.filter((y) => y !== x.fact_id)))} />
            <span>{PART_LABEL[part]}</span>
            <span className="font-mono text-label break-all text-tx2">{x.lot_ids.join(", ")}</span>
          </label>
        );
      })}
      {again !== null && <p className="m-0 text-label text-tx2">{SUPERSEDE_NOTE}</p>}
      {parts.length > 1 && settings !== null && <p className="m-0 text-label text-tx2">{oneRequestLine(settings.max_per_version)}</p>}
      {settings !== null && <p className="m-0 text-label text-tx2">{responseMinutesLine(settings.response_min)}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="primary" busy={send.busy} disabled={parts.length === 0 || version === null}
          onClick={() => void send.run(requestBody(version as string, parts, again ?? undefined))}>{SEND_REQUEST}</Button>
        <Button kind="ghost" disabled={send.busy} onClick={onClose}>{DONT_SEND_REQUEST}</Button>
      </div>
      <ErrorLine error={send.error} action={action} />
    </div>
  );
}

// --- one row ---------------------------------------------------------------------------------------------------------

function FactRow({ d, f, reload, draft }: {
  d: CaseDetail61; f: VerifiedFact; reload: () => void; draft?: { draft_id: string; request_id?: string | null };
}) {
  const { session } = useSession();
  const cal = session.calendar;
  const settings = settingsOf(session);
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (wasOpen.current && !open) opener.current?.focus();
    wasOpen.current = open;
  }, [open]);
  const r = requestForFact(d, f.fact_id);
  const act = factAction(f, session, d);
  const required = f.class === "REQUIRED";
  const title = factTitle(f, cal);
  const why = factWhy(f, d.engine_input, session.day, cal);
  const price = (d.engine_input as { policy?: { waste_price?: number } } | null)?.policy?.waste_price ?? 0;
  const notAllowed = f.pick_feasible_if_alt === false;
  return (
    <li data-fact-id={f.fact_id} className="flex min-w-0 flex-col gap-2 rounded-lg border border-line p-4">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {required && <Pill tone="bad" icon="lock">{REQUIRED_CHIP}</Pill>}
        {f.class === "ADVISORY" && <Pill tone="warn" icon="info">{ADVISORY_CHIP}</Pill>}
        <span data-testid="fact-title" title={title} className="num min-w-0 text-body font-semibold break-words">{clipTitle(title)}</span>
        {notAllowed ? <Pill tone="bad" icon="lock">{UNVERIFIED_NOT_ALLOWED}</Pill>
          : <Pill tone="warn" icon="alert">{unverifiedExposure(money(f.decision_regret, session.currency))}</Pill>}
      </div>
      {r !== null && (
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <CompactDeadline key={r.request_id} r={r} onDue={reload} />
          {requestLines(r).map((line, i) => (
            <span key={i} data-testid="stock-check-status" className="num min-w-0 text-label text-tx2 break-words">{line}</span>
          ))}
        </div>
      )}
      {f.blocked_label != null && (
        <p data-testid="blocked-label" className="m-0 flex min-w-0 flex-wrap items-center gap-2 text-label text-tx">
          <Pill tone="bad" icon="lock">{f.blocked_label}</Pill><span>{responsibleLine(f.owner_branch ?? f.branch)}</span>
        </p>
      )}
      {act.kind !== "none" && !open && (
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Button ref={opener} kind={required ? "primary" : "plain"} disabled={act.disabled} aria-expanded={false}
            onClick={() => setOpen(true)}>
            {act.kind === "again" ? ASK_AGAIN : act.part === "count" ? COUNT_STOCK : CHECK_PRINTED_EXPIRY}
          </Button>
          {act.disabled && settings !== null && (
            <span className="text-label text-tx3">{requestsUsedLine(settings.max_per_version)}</span>
          )}
        </div>
      )}
      {act.kind !== "none" && open && (
        <RequestPanel d={d} f={f} again={act.kind === "again" ? act.request : null} onClose={() => setOpen(false)} reload={reload} />
      )}
      {required && <WaysForward d={d} f={f} reload={reload} />}
      <details className="min-w-0">
        <summary className="cursor-pointer text-label font-semibold text-navy-800">{DETAILS}</summary>
        <div className="mt-2 flex min-w-0 flex-col gap-1">
          <p className="num m-0 text-body break-words">{title}</p>
          {why !== null && <p className="num m-0 text-body text-tx2 break-words">{WHY_FLAGGED}: {why}</p>}
          <p data-testid="fact-outcome" className="num m-0 text-body text-tx break-words">
            {IF_TRUE}: {factSentence(f, session.currency, price, cal)}
          </p>
          {required && (
            <>
              {(f.reasons ?? []).length > 0 && <p className="m-0 text-body break-words">{LIMIT(f.reasons ?? [])}</p>}
              <p className="m-0 text-body">{OWNER(f.owner_branch ?? f.branch)}</p>
              <p className="m-0 text-body">{RESOLVE_BY}</p>
            </>
          )}
          <p className="m-0 text-label text-tx2">{LOTS}: <span className="font-mono break-all">{f.lot_ids.join(", ")}</span></p>
        </div>
      </details>
      {draft && <a className="self-start py-2 text-body font-semibold text-navy-800 underline underline-offset-2 [overflow-wrap:anywhere] max-md:min-h-11"
        href={`#/cases/${encodeURIComponent(d.case_id)}?step=compare&draft=${encodeURIComponent(draft.draft_id)}`}>
        {draft.request_id ? "View request" : "Review draft"}
      </a>}{/* 09-20: the evidence draft for this fact, once drafts are under review */}
    </li>
  );
}

// --- the summary's verification strip (07-UI-SPEC surface 1 "Summary") -------------------------------------------------

export const VERIFICATION_STRIP_NAME = "Verification"; // (proposed) the strip's accessible name

// One line under the facts grid while a commit is still ahead and a fact check matters (Required or Advisory, in the
// server's order): each fact's request state with its compact deadline, beside its exposure chip. Every word is the
// server's sentence or the D-10 exposure form; nothing here says verified.
export function VerificationStrip({ d, reload }: { d: CaseDetail61; reload: () => void }) {
  const { session } = useSession();
  const facts = factsOf(d).filter((f) => f.class === "REQUIRED" || f.class === "ADVISORY");
  if (facts.length === 0 || !["OPEN", "WAITING", "PENDING_APPROVAL", "APPROVED"].includes(d.state)) return null;
  return (
    <div data-testid="verification-strip" role="group" aria-label={VERIFICATION_STRIP_NAME}
      className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1">
      {facts.map((f) => {
        const r = requestForFact(d, f.fact_id);
        return (
          <span key={f.fact_id} data-strip-fact={f.fact_id} className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="num text-label text-tx2">{f.branch} · lot <span className="font-mono">{f.lot_ids.join(", ")}</span></span>
            {r !== null && <CompactDeadline key={r.request_id} r={r} onDue={reload} />}
            {r !== null && <span className="num text-label text-tx break-words">{requestLines(r)[0]}</span>}
            {f.pick_feasible_if_alt === false ? <Pill tone="bad" icon="lock">{UNVERIFIED_NOT_ALLOWED}</Pill>
              : <Pill tone="warn" icon="alert">{unverifiedExposure(money(f.decision_regret, session.currency))}</Pill>}
          </span>
        );
      })}
    </div>
  );
}

// --- the ways forward from a block (07-UI-SPEC "Exits from a blocked Required row"; D-12, D-23, D-36) -------------------

// A Required row's other exits, each only while the server lists it (commitment.next_actions) and only for the account
// that may take it: Select another plan and Reject stock decision… for the source store's manager before approval;
// Cancel approved plan… for a manager who approves the plan (the source, or the destination of a transfer) after it.
// Ask again is the row's own button. The server checks the seat and the state again on every write.
function WaysForward({ d, f, reload }: { d: CaseDetail61; f: VerifiedFact; reload: () => void }) {
  const { session: plain } = useSession();
  const session = plain as unknown as Session61;
  const [open, setOpen] = useState<"select" | "reject" | "cancel" | null>(null);
  const next = d.commitment?.next_actions ?? [];
  const source = isMe(plain, d.source);
  const select = next.includes("select_plan") && source;
  const reject = next.includes("close_proposal") && source;
  const cancel = next.includes("cancel_approved") && (source || isMe(plain, d.dest));
  if (!select && !reject && !cancel) return null;
  const toggle = (which: "select" | "reject" | "cancel") => setOpen((o) => (o === which ? null : which));
  return (
    <div data-testid="ways-forward" className="flex min-w-0 flex-col items-start gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        {select && (
          <Button kind="ghost" aria-expanded={open === "select"} onClick={() => toggle("select")}>{SELECT_ANOTHER_PLAN}</Button>
        )}
        {reject && <Button kind="plain" aria-expanded={open === "reject"} onClick={() => toggle("reject")}>{REJECT_OPEN}</Button>}
        {cancel && <Button kind="plain" aria-expanded={open === "cancel"} onClick={() => toggle("cancel")}>{CANCEL_OPEN}</Button>}
      </div>
      {open === "select" && select && <SelectPlan d={d} f={f} session={session} reload={reload} />}
      {open === "reject" && reject && (
        <ReasonForm kind="reject" d={d} session={session} reload={reload} onClose={() => setOpen(null)}
          write={(reason) => (d.state === "PENDING_APPROVAL" ? decisionWrite(d, "reject", reason) : rejectWrite(d, reason))} />
      )}
      {open === "cancel" && cancel && (
        <ReasonForm kind="cancel" d={d} session={session} reload={reload} onClose={() => setOpen(null)}
          write={(reason) => cancelWrite(d, reason)} />
      )}
    </div>
  );
}

// The engine's other options on this version (the server's `options`, in the result's order), each with its outcome:
// enabled when the option is allowed today and does not itself need this fact verified; otherwise disabled with
// "Also needs this fact verified" or the engine's reasons word for word. Selecting goes through the confirm step and
// the server's recheck (POST /select); a refusal shows word for word.
function SelectPlan({ d, f, session, reload }: { d: CaseDetail61; f: VerifiedFact; session: Session61; reload: () => void }) {
  const options = (d as CaseDetail61 & VerifyDetailKeys).options ?? [];
  const others = options.filter((o) => o.key !== d.suggested_key);
  const [picked, setPicked] = useState<CaseOption | null>(null);
  const write = useStepWrite(d, reload);
  const sentence = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (picked !== null) sentence.current?.focus();
  }, [picked]);
  const net = (key: string) => d.result?.options.find((o) => o.key === key)?.net.base ?? null;
  return (
    <div data-testid="plan-options" role="group" aria-label={SELECT_ANOTHER_PLAN}
      className="flex w-full min-w-0 flex-col gap-2 rounded-lg border border-line bg-white p-4">
      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {others.map((o) => {
          const needs = o.required_fact_ids.includes(f.fact_id);
          const allowed = o.feasible && !needs;
          const amount = net(o.key);
          return (
            <li key={o.key} data-option={o.key} className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="min-w-0 text-body break-words">{o.label}</span>
              {amount !== null && <span className="num text-label text-tx2">{OPTION_NET(money(amount, session.currency))}</span>}
              <Button kind="plain" aria-label={selectOptionName(o.label)} disabled={!allowed || write.busy}
                onClick={() => { write.clear(); setPicked(o); }}>{SELECT_OPTION}</Button>
              {needs && <span className="text-label text-tx3">{ALSO_NEEDS_THIS_FACT}</span>}
              {!needs && !o.feasible && <span className="text-label text-tx3 break-words">{o.reasons.join("; ")}</span>}
            </li>
          );
        })}
      </ul>
      {picked !== null && (
        <div data-testid="select-confirm" className="flex min-w-0 flex-col items-start gap-2 rounded-lg bg-sunken p-2"
          onKeyDown={(e) => { if (e.key === "Escape") setPicked(null); }}>
          <p ref={sentence} tabIndex={-1} className="m-0 text-label text-tx break-words">{selectConfirmLine(picked.label)}</p>
          <div className="flex flex-wrap gap-2">
            <Button kind="primary" busy={write.busy} onClick={() => void write.run(selectWrite(d, picked.key))}>{SELECT_THIS_PLAN}</Button>
            <Button kind="ghost" disabled={write.busy} onClick={() => setPicked(null)}>{KEEP_CURRENT_PLAN}</Button>
          </div>
        </div>
      )}
      <WriteError write={write} d={d} session={session} reload={reload} />
    </div>
  );
}

// --- the block -------------------------------------------------------------------------------------------------------

// "Fact checks" with "{N} of {max} requests left on this version" (the server's requests_left, the session's limit),
// then the rows; "The engine flagged no suspect facts for this version." with none, and no request button.
export function FactChecks({ d, reload }: { d: CaseDetail61; reload: () => void }) {
  const { session } = useSession();
  const settings = settingsOf(session);
  const facts = factsOf(d);
  const left = facts.find((f) => f.requests_left !== undefined)?.requests_left;
  // 09-20: Gemini's evidence drafts are read only after an explicit "Review evidence drafts" (b90e10f).
  const [review, setReview] = useState(false);
  const draftRead = useEvidenceDrafts(d, 0, review);
  const drafts = draftRead.data;
  const draftOf = (f: VerifiedFact) => drafts?.drafts.find((row) => row.fact_id === f.fact_id && row.state !== "HISTORICAL"
    && row.rec_version === d.rec_version && row.plan_key === (d.selected_plan?.plan_key ?? d.result?.recommended));
  return (
    <section data-testid="fact-checks" aria-label={FACTS_TITLE} className="flex min-w-0 flex-col gap-2 rounded-xl border border-line p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{FACTS_TITLE}</h3>
        {facts.length > 0 && left !== undefined && settings !== null && (
          <span data-testid="requests-left" className="ml-auto text-label text-tx2">{requestsLeftLine(left, settings.max_per_version)}</span>
        )}
      </div>
      {!review && <Button kind="plain" className="self-start max-md:min-h-11" onClick={() => setReview(true)}>Review evidence drafts</Button>}
      {review && !drafts && !draftRead.error && <p role="status" className="m-0 text-label">Loading evidence drafts…</p>}
      <ErrorLine error={draftRead.error} action={draftRead.error ? { label: TRY_AGAIN, onClick: draftRead.refresh } : undefined} />
      {facts.length === 0 ? <p className="m-0 text-body text-tx2">{NO_FACTS}</p> : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {facts.map((f) => <FactRow key={f.fact_id} d={d} f={f} reload={reload} draft={draftOf(f)} />)}
        </ul>
      )}
    </section>
  );
}
