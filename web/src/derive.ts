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
