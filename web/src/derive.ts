// Pure helpers that derive client state from the server's answers. Erasable syntax only, and only types are imported,
// so `node --test` runs this file as it is.
import type { ApiError, CaseDetail, CaseState, FactCheck, LotCount, Request, Session } from "./api.ts";

// --- the Idempotency-Key lifecycle (contract Conventions; research Pitfall 1) -------------------------------------

// One user action: its key and the body sent with it. `settled` is true once the action succeeded.
export type Attempt<B> = { key: string; body: B; settled: boolean };

// JSON with every object's keys sorted, so two bodies that differ only in key order compare equal.
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

// The attempt for sending `body` now. An unsettled previous attempt with an equal body is the same action (a retry,
// or a second press while it is pending), so it keeps its key; a settled one or a changed body is a new action with
// a new key. `makeKey` is the key factory (api.newKey in the app), passed in so this stays pure.
export function nextAttempt<B>(prev: Attempt<B> | null, body: B, makeKey: () => string): Attempt<B> {
  if (prev !== null && !prev.settled && canonical(prev.body) === canonical(body)) return { ...prev, body };
  return { key: makeKey(), body, settled: false };
}

// --- who is signed in --------------------------------------------------------------------------------------------

// True when the signed-in account is the manager of `branch`. Exact string equality as the API sends both: no trimming,
// no case folding, no normalisation.
export function isMe(session: Pick<Session, "role" | "branch">, branch: string | null): boolean {
  return session.role === "manager" && branch !== null && session.branch === branch;
}

// --- the public dataset's board and codes (05.2 D-02, D-26) -----------------------------------------------------------

// A product as the screens name it: its code, or "Name · Code" only when the API gives a name. Names are never
// invented (D-02): the public dataset has codes only, so its products read as codes.
export function productLabel(code: string, name: string | null): string {
  return name === null || name === "" ? code : `${name} · ${code}`;
}

// The HQ risk filter's values (05.2-15: `risk=waste` keeps waste-risk items, `risk=stock` stock-at-step ones).
export type BoardRisk = "all" | "waste" | "stock";

// --- plans ------------------------------------------------------------------------------------------------------

export type PlanKind = "KEEP" | "MARKDOWN" | "TRANSFER";

// The kind of an engine plan key: "keep", "md20"/"md30", or "tr:{dest}:{run}:{qty}:{rest}". Null for no key, or a key
// shape this version does not know.
export function planKind(key: string | null): PlanKind | null {
  if (key === null) return null;
  if (key === "keep") return "KEEP";
  if (/^md\d+$/.test(key)) return "MARKDOWN";
  if (key.startsWith("tr:")) return "TRANSFER";
  return null;
}

// --- count requests (05-CONTEXT D-01, D-02; 04-API-CONTRACT "Count path") ----------------------------------------

// The fact kinds a shelf-and-backroom count settles. Mirrors api/signals.py COUNT_KINDS: the API refuses a count
// request for any other kind (400 "Only a stock count can be requested in this version").
export const COUNT_KINDS: readonly string[] = ["PHANTOM_STOCK", "STALE_COUNT"];

type RequestHolder = Pick<CaseDetail, "rec_version" | "requests">;

// The count request for this fact on the case's active version (any state), or null. With an original and its recount
// on the version, the newest: the case detail lists requests oldest first, so the last match (a SUPERSEDED original
// is read on Requests, never on the fact row). A request on an older version belongs to that version's fact checks.
export function requestForFact(detail: RequestHolder, factId: string): Request | null {
  return detail.requests.findLast((r) => r.fact_id === factId && r.rec_version === detail.rec_version) ?? null;
}

// The case states a confirmed count re-plans (mirrors api/cases.py LIVE_STATES); in any other state a count changes
// nothing, so none is asked for (G-05-2 item 11; the API refuses with 409 wrong_state).
export const REPLANNABLE: readonly CaseState[] = ["OPEN", "WAITING", "PENDING_APPROVAL"];

export function canReplan(state: CaseState): boolean {
  return REPLANNABLE.includes(state);
}

// The "Ask this branch for a count" button shows only when every D-01 condition holds: the fact changes the plan
// (this already includes a not-allowed fact, 02 D-29), its kind is one a count settles, the seat is the case's source
// manager and nothing was asked yet for this fact on this version; and the case can still be re-planned (OPEN,
// WAITING or PENDING_APPROVAL: G-05-2 item 11, user decision at UAT 2026-10-16).
export function canAskForCount(
  f: Pick<FactCheck, "fact_id" | "kind" | "changes_plan">,
  session: Pick<Session, "role" | "branch">,
  detail: RequestHolder & Pick<CaseDetail, "source" | "state">,
): boolean {
  return canReplan(detail.state) && f.changes_plan && COUNT_KINDS.includes(f.kind) && isMe(session, detail.source)
    && requestForFact(detail, f.fact_id) === null;
}

// Request recount shows only for an ANSWERED request the server calls `recountable` (it already holds the state, the
// active version, the live case and the per-version limit) and only to the manager of the request's case source (the
// role rule; the route still answers 403 for any other seat, D-16). A recount keeps the original response (05-17).
export function canRequestRecount(
  r: Pick<Request, "state" | "recountable" | "case_source">,
  session: Pick<Session, "role" | "branch">,
): boolean {
  return r.state === "ANSWERED" && r.recountable && isMe(session, r.case_source);
}

// What the visitor typed for one lot: the raw text of the Shelf and Backroom inputs.
export type LotDraft = { shelf: string; backroom: string };

// A typed count: a blank (or unreadable) field is null, never 0, so an unchecked shelf is never recorded as empty
// stock. The API validates the number itself (zero or more).
export function parseCount(text: string): number | null {
  if (text.trim() === "") return null;
  const n = Number(text);
  return Number.isFinite(n) ? n : null;
}

// The answer's `counts`: every requested lot with both parts, a blank part null (contract `answer` row).
export function countsBody(lotIds: string[], lots: Record<string, LotDraft>): Record<string, LotCount> {
  const out: Record<string, LotCount> = {};
  for (const id of lotIds) {
    const d = lots[id];
    out[id] = { shelf_units: d ? parseCount(d.shelf) : null, backroom_units: d ? parseCount(d.backroom) : null };
  }
  return out;
}

// The count fields a 409 needs_input names, as "lotId:shelf_units" / "lotId:backroom_units"; empty for anything else.
export function invalidFields(error: Pick<ApiError, "code" | "extra"> | null): Set<string> {
  const out = new Set<string>();
  const fields = error?.code === "needs_input" ? error.extra.fields : null;
  if (!Array.isArray(fields)) return out;
  for (const f of fields) {
    if (f !== null && typeof f === "object" && typeof f.lot_id === "string" && typeof f.field === "string") {
      out.add(`${f.lot_id}:${f.field}`);
    }
  }
  return out;
}

// The key a Requests draft is kept under: the request and the answer revision it was typed against (0 before any
// answer), so a refetch never clears it and a new revision is noticed rather than merged (research Pattern 5).
export function draftKey(r: Pick<Request, "request_id" | "answer">): string {
  return `${r.request_id}:${r.answer?.revision ?? 0}`;
}

// --- the day a stored result describes (review CR-01) ---------------------------------------------------------------

// True when the case's stored result was computed on `today` (its engine_input.today is the session day). The API
// re-evaluates only OPEN, WAITING, PENDING_APPROVAL and INVALIDATED cases each morning, and none after the last demo
// day; any other case keeps the result, markdown step and wait of the day it was last evaluated, so the lines that
// count from that day ("Price drops 20% tomorrow", the wait sentence) are shown only while this holds. False without
// a stored input.
export function resultIsToday(d: Pick<CaseDetail, "engine_input">, today: number): boolean {
  return d.engine_input !== null && d.engine_input.today === today;
}

// --- the plan's own deadline and the wait sentence (G-05-2 item 4, user decision at UAT 2026-10-16) ----------------

// True when the Options Wait row may carry the wait sentence: the case has a wait, can still be decided and its
// result is today's. A REJECTED (or any other finished) case states no wait.
export function waitShown(d: Pick<CaseDetail, "wait" | "state" | "engine_input">, today: number): boolean {
  return d.wait !== null && canReplan(d.state) && resultIsToday(d, today);
}

// --- a list that shrank past the page being read (06.1 D-17; quick 261017-wzs finding 5) ------------------------------

// The page to ask the server for when `page` is past the last page that holds records (the list shrank since it was
// read): that last page, or 1 when nothing is left. Null while the page is in range or the page size is unknown, so a
// page in range is never asked for again. The server still orders and pages; this only names a page.
export function pageInRange(page: number, pageSize: number, total: number): number | null {
  if (pageSize <= 0) return null;
  const last = Math.max(1, Math.ceil(total / pageSize));
  return page <= last ? null : last;
}

// --- 07-21: the fact checks' actions, the commitment's blockers and the final-approval rule (07-CONTEXT D-07, D-11,
// D-12; 07-UI-SPEC surface 1). Own block, additive. Everything reads the server's keys (class, requests_left,
// askable_again, commitment.blocks); nothing here decides what the server enforces, it only chooses what is shown.
import type { Commitment, VerifiedFact } from "./api.ts";

export type CheckPart = "count" | "expiry";
// The check a fact kind takes: a count on PHANTOM_STOCK and STALE_COUNT, a printed date on ESTIMATED_EXPIRY (D-11).
export function checkPart(kind: string): CheckPart | null {
  return COUNT_KINDS.includes(kind) ? "count" : kind === "ESTIMATED_EXPIRY" ? "expiry" : null;
}

type VerifyHolder = RequestHolder & Pick<CaseDetail, "source" | "state">;

// The row's one request action for the source store's manager (04-CONTEXT D-20) on a decision that can still be
// re-planned: "ask" with no request on this version yet, "again" when the latest one may be asked again (the
// server's `askable_again`: Unanswered, Can't check, Answered or Held, within the limit), and a disabled "again" once
// the limit is used on a request that no longer stands. "none" otherwise: another account, a request still standing,
// a kind with no check, a fact that changes nothing (no class).
export type FactAction =
  | { kind: "ask"; part: CheckPart; disabled: boolean }
  | { kind: "again"; part: CheckPart; request: Request; disabled: boolean }
  | { kind: "none" };
export function factAction(
  f: Pick<VerifiedFact, "fact_id" | "kind" | "class" | "changes_plan" | "requests_left">,
  session: Pick<Session, "role" | "branch">, d: VerifyHolder,
): FactAction {
  const part = checkPart(f.kind);
  if (part === null || !canReplan(d.state) || !isMe(session, d.source)) return { kind: "none" };
  if (f.class === null || (f.class === undefined && !f.changes_plan)) return { kind: "none" };
  const left = f.requests_left ?? 1; // the server refuses a request past the limit in any case
  const r = requestForFact(d, f.fact_id);
  if (r === null) return { kind: "ask", part, disabled: left <= 0 };
  if (r.askable_again === true) return { kind: "again", part, request: r, disabled: false };
  if (left <= 0 && (r.state === "CANT_CHECK" || r.overdue === true)) return { kind: "again", part, request: r, disabled: true };
  return { kind: "none" };
}

// The other check offered in the same request panel (07-UI-SPEC "Request actions"; D-11): a fact of the other part at
// the same branch with no request on this version, the expiry lots inside the count fact's lots (the server's pairing
// rule). Null when there is none.
export function companionFact<F extends Pick<VerifiedFact, "fact_id" | "kind" | "branch" | "lot_ids">>(
  d: RequestHolder & { fact_checks: F[] }, f: F,
): F | null {
  const part = checkPart(f.kind);
  if (part === null) return null;
  for (const x of d.fact_checks) {
    const p = checkPart(x.kind);
    if (x.fact_id === f.fact_id || x.branch !== f.branch || p === null || p === part) continue;
    if (requestForFact(d, x.fact_id) !== null) continue;
    const [count, expiry] = part === "count" ? [f, x] : [x, f];
    if (expiry.lot_ids.every((l) => count.lot_ids.includes(l))) return x;
  }
  return null;
}

// The body of POST /api/cases/{id}/requests: one fact (`fact_id`) or a count and an expiry together (`fact_ids`,
// count first), and `recount_of` when asking again.
export function requestBody(
  recVersion: string, parts: Pick<VerifiedFact, "fact_id" | "kind">[], again?: Pick<Request, "request_id">,
): Record<string, unknown> {
  const body: Record<string, unknown> = { rec_version: recVersion };
  if (parts.length === 1) body.fact_id = parts[0].fact_id;
  else body.fact_ids = [...parts].sort((a, b) => (checkPart(a.kind) === "count" ? -1 : checkPart(b.kind) === "count" ? 1 : 0))
    .map((p) => p.fact_id);
  if (again !== undefined) body.recount_of = again.request_id;
  return body;
}

// What blocks committing, in the server's order (commitment.blocks: lot holds, then Required checks), as the summary
// and the approve step name them. `titleOf` words a fact check (copy.factTitle; derive never imports copy).
export type Blocker =
  | { kind: "required_check"; fact_id: string; title: string; owner: string; blocked: string | null }
  | { kind: "lot_hold"; lot: string; owner: string };
export function blockersOf<F extends Pick<VerifiedFact, "fact_id" | "blocked_label">>(
  d: { commitment?: Commitment; fact_checks: F[] }, titleOf: (f: F) => string,
): Blocker[] {
  return (d.commitment?.blocks ?? []).map((b): Blocker => {
    if (b.kind === "lot_hold") return { kind: "lot_hold", lot: b.lot_ids.join(", "), owner: b.owner_branch };
    const f = d.fact_checks.find((x) => x.fact_id === b.fact_id);
    return {
      kind: "required_check", fact_id: b.fact_id ?? "", title: f === undefined ? b.lot_ids.join(", ") : titleOf(f),
      owner: b.owner_branch, blocked: f?.blocked_label ?? null,
    };
  });
}

// True when this side's approving decision would be the final one of the round: every other side the plan needs has
// decided (a Keep or a markdown needs the source only). The server checks the blocks at that commit (D-12).
export function isFinalDecision(
  d: { approvals: Pick<CaseDetail["approvals"], "required" | "decisions"> }, side: "source" | "destination",
): boolean {
  return d.approvals.required.filter((s) => s !== side).every((s) => d.approvals.decisions[s] !== undefined);
}
