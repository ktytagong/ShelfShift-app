// React hooks shared by the shell and the pages: visibility, refetch on refocus, and keyed writes.
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, NETWORK, get, newKey, type ActivityEntry, type CaseDetail, type Session } from "./api.ts";
import { nextAttempt, type Attempt } from "./derive.ts";

// True while the tab is visible (research Pattern 4).
export function useVisible(): boolean {
  const [visible, setVisible] = useState(document.visibilityState === "visible");
  useEffect(() => {
    const on = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}

// Calls `load` once each time the document becomes visible again, so a change made in another tab shows up then
// (05-CONTEXT Freshness). Nothing runs on an interval.
export function useRefetchOnVisible(load: () => void): void {
  const latest = useRef(load);
  latest.current = load;
  useEffect(() => {
    const on = () => {
      if (document.visibilityState === "visible") latest.current();
    };
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
}

// The board's poll (research Pattern 4; UI-SPEC "Polling"): `load` runs once on mount and once each time the tab
// becomes visible again, then every `ms` while it stays visible. While the tab is hidden no timer exists at all. A tick
// that comes while the previous load is still in flight is skipped, never queued. `load` must be stable (useCallback),
// or the effect restarts on every render.
export function useBoardPoll(load: () => Promise<void>, ms = 2000): void {
  const visible = useVisible();
  const inFlight = useRef(false);
  useEffect(() => {
    if (!visible) return;
    const tick = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        await load();
      } finally {
        inFlight.current = false;
      }
    };
    void tick();
    const timer = setInterval(() => void tick(), ms);
    return () => clearInterval(timer);
  }, [visible, load, ms]);
}

// The case page's data (05-CONTEXT Freshness; research Pattern 5): GET /api/cases/{id} on route entry, each time the
// tab becomes visible again, when the session's run_id, role, branch or version changes (a seat switch, a reset, Next
// day), and on reload(). It never polls on an interval. A response that arrives after a newer load started (another
// case opened, a quicker refetch) is dropped, so the page never shows an older answer over a newer one. A failed
// refetch keeps the last detail on screen next to the error.
export type CaseLoad = { detail: CaseDetail | null; error: ApiError | null; loading: boolean; reload: () => void };

export function useCase(caseId: string, session: Pick<Session, "run_id" | "role" | "branch" | "version">): CaseLoad {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const latest = useRef(0);

  const load = useCallback(async () => {
    const n = ++latest.current;
    setLoading(true);
    try {
      const d = await get<CaseDetail>("/api/cases/" + encodeURIComponent(caseId));
      if (n !== latest.current) return;
      setDetail(d);
      setError(null);
    } catch (e) {
      if (n !== latest.current) return;
      setError(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    } finally {
      if (n === latest.current) setLoading(false);
    }
  }, [caseId]);

  // Another case: nothing from the previous one stays on screen while the new one loads.
  useEffect(() => {
    setDetail(null);
    setError(null);
  }, [caseId]);

  const { run_id, role, branch, version } = session;
  useEffect(() => {
    void load();
  }, [load, run_id, role, branch, version]);
  useRefetchOnVisible(() => void load());

  const reload = useCallback(() => void load(), [load]);
  return { detail, error, loading, reload };
}

// The case's activity log (GET /api/cases/{id}/activity, oldest first) with the same refetch triggers as useCase:
// route entry, the tab becoming visible again, a change of the session's run_id, role, branch or version, and
// reload() (the stock decision page calls it with its own reload, after the visitor's writes and on Reload the stock
// decision). It never polls. An older response is dropped; a failed refetch keeps the last entries next to the error.
export type ActivityLoad = { entries: ActivityEntry[] | null; error: ApiError | null; reload: () => void };

export function useActivity(
  caseId: string, session: Pick<Session, "run_id" | "role" | "branch" | "version">,
): ActivityLoad {
  const [entries, setEntries] = useState<ActivityEntry[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const latest = useRef(0);

  const load = useCallback(async () => {
    const n = ++latest.current;
    try {
      const e = await get<ActivityEntry[]>(`/api/cases/${encodeURIComponent(caseId)}/activity`);
      if (n !== latest.current) return;
      setEntries(e);
      setError(null);
    } catch (e) {
      if (n !== latest.current) return;
      setError(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    }
  }, [caseId]);

  useEffect(() => {
    setEntries(null);
    setError(null);
  }, [caseId]);

  const { run_id, role, branch, version } = session;
  useEffect(() => {
    void load();
  }, [load, run_id, role, branch, version]);
  useRefetchOnVisible(() => void load());

  const reload = useCallback(() => void load(), [load]);
  return { entries, error, reload };
}

export type Action<B, R> = {
  busy: boolean;
  error: ApiError | null;
  run: (body: B) => Promise<R | undefined>; // a user action: a new key unless it repeats an unsettled one
  retry: () => Promise<R | undefined>; // Try again: the same key and the same body
  clear: () => void; // forget the attempt and its error (the next run gets a new key)
};

// One keyed write per user action (contract Idempotency-Key). The attempt ({key, body}) is kept while it is pending
// and after a failure, so Try again resends exactly what was sent; a success settles it, so the next action, an edited
// body or a remounted page (a reload) gets a new key. A press while the write is pending sends nothing. Nothing here
// ever resends on its own: only `run` and `retry`, called by the visitor's press, send.
export function useAction<B, R>(fn: (body: B, key: string) => Promise<R>): Action<B, R> {
  const attempt = useRef<Attempt<B> | null>(null);
  const pending = useRef(false);
  const latest = useRef(fn);
  latest.current = fn;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const send = useCallback(async (a: Attempt<B>): Promise<R | undefined> => {
    if (pending.current) return undefined;
    pending.current = true;
    attempt.current = a;
    setBusy(true);
    setError(null);
    try {
      const out = await latest.current(a.body, a.key);
      attempt.current = { ...a, settled: true };
      return out;
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "error", NETWORK));
      return undefined;
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }, []);

  const run = useCallback((body: B) => send(nextAttempt(attempt.current, body, newKey)), [send]);
  const retry = useCallback(() => {
    const a = attempt.current;
    return a === null || a.settled ? Promise.resolve(undefined) : send(a);
  }, [send]);
  const clear = useCallback(() => {
    attempt.current = null;
    setError(null);
  }, []);

  return { busy, error, run, retry, clear };
}

// The booking hold's refresh at its end (06.1 D-57; RESEARCH Pitfall 6): one timer, scheduled from the server's clock,
// never the client's. `until` is the hold's end and `serverNow` the server's time in the same response (both ISO). When
// both are set and until − serverNow is above zero, one setTimeout at that difference + 500 ms calls the latest
// `reload`, so the read that applies the expiry (the server is the truth) comes just after it. A skewed laptop clock
// cannot fire it early or late, and nothing counts down on screen (no minutes left). The timer is cleared when either
// value changes and on unmount; a reload that returns the same pair arms it again from the new server time.
export function useRefreshAt(until: string | null, serverNow: string | null, reload: () => void): void {
  const latest = useRef(reload);
  latest.current = reload;
  useEffect(() => {
    if (until === null || serverNow === null) return;
    const ms = Date.parse(until) - Date.parse(serverNow);
    if (!(ms > 0)) return; // already over, or unreadable: the server's read shows the state, nothing to schedule
    const timer = setTimeout(() => latest.current(), ms + 500);
    return () => clearTimeout(timer);
  }, [until, serverNow]);
}
