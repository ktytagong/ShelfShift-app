import { Component, StrictMode, useCallback, useEffect, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { ApiError, NETWORK, get, whenLocked, whenPaused, whenSandboxReset, type Paused, type Session } from "./api.ts";
import { pageFor } from "./pagereg.ts";
import { AppShell, DatasetMark, NotFound, SessionCtx, type Route } from "./shell.tsx";
import { SignIn } from "./signin.tsx";
import { PausedCtx, PausedNotice, type Retry } from "./paused.tsx";
import { BACK_TO_TODAY } from "./ui.tsx";

// The hash router: "#/cases/12?tab=options" is the path "/cases/12" with the query { tab: "options" }.
// An empty hash is "/".
function parseRoute(hash: string): Route {
  const h = hash.replace(/^#/, "");
  const at = h.indexOf("?");
  const path = (at < 0 ? h : h.slice(0, at)) || "/";
  const query = Object.fromEntries(new URLSearchParams(at < 0 ? "" : h.slice(at + 1)));
  return { path, query };
}

function hashOf(path: string, query: Record<string, string>): string {
  const q = new URLSearchParams(query).toString();
  return `#${path}${q === "" ? "" : `?${q}`}`;
}

// The Phase 5 routes and their 06.1 pages (UI-SPEC "Routes"; D-50, D-53): the query is kept.
const MOVED: Readonly<Record<string, string>> = { "/requests": "/tasks", "/settings": "/policies" };

// Where a route moves to, or null when it stays. "/" is Today, which lands with its page plan; until a page claims
// "/", it goes to the Decision board. The Phase 5 list layout is the board's table view.
function redirectOf(route: Route): string | null {
  if (route.path === "/" && pageFor(route) === null) return hashOf("/board", route.query);
  if (route.path === "/list") return hashOf("/board", { ...route.query, view: "table" });
  const to = MOVED[route.path];
  return to === undefined ? null : hashOf(to, route.query);
}

// The current route, after any redirect: location.replace swaps the address, so Back never returns to the old one.
function readRoute(): Route {
  const route = parseRoute(window.location.hash);
  const to = redirectOf(route);
  if (to === null) return route;
  window.location.replace(to);
  return parseRoute(to);
}

function useRoute(): Route {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}

// A full-page message (the first load, or a session probe that failed for a reason other than a locked session),
// with the dataset mark as on every screen.
function Notice({ children }: { children: ReactNode }) {
  return (
    <main className="grid h-full place-items-center p-8">
      <div className="text-center">
        {children}
        <p className="mt-4"><DatasetMark /></p>
      </div>
    </main>
  );
}

// A render error caught by an ErrorBoundary (review WR-07): a plain message, the way back to Today and the dataset
// mark, so no failure blanks the screen or hides the mark. Back to Today also clears the boundary, so it works on the
// page Today goes to, where the hash may not change.
function Failed({ reset }: { reset: () => void }) {
  return (
    <>
      <p role="alert" className="text-body text-tx">This page could not be shown.</p>
      <a href="#/" onClick={reset} className="mt-2 inline-block text-body text-accent underline">{BACK_TO_TODAY}</a>
    </>
  );
}

// In the shell, in place of the page (the sidebar stays); a failure of the shell itself shows Failed in a Notice.
function PageFailed({ reset }: { reset: () => void }) {
  return (
    <div className="p-6">
      <Failed reset={reset} />
      <p className="mt-4"><DatasetMark /></p>
    </div>
  );
}

type BoundaryProps = { children: ReactNode; resetKey: string; fallback: (reset: () => void) => ReactNode };

// Catches a render error below it and shows `fallback` in its place; a new resetKey (another route) clears it.
class ErrorBoundary extends Component<BoundaryProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidUpdate(prev: BoundaryProps) {
    if (this.state.failed && prev.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }

  reset = () => this.setState({ failed: false });

  render() {
    return this.state.failed ? this.props.fallback(this.reset) : this.props.children;
  }
}

// The routed page inside the shell: the registry's page for the route (pagereg.ts), or "Page not found.". It is
// rendered inside an ErrorBoundary keyed by the workspace and the account, so a sign-in as someone else remounts it and
// nothing of the previous account stays on screen (T-06.1-53).
function Page({ route }: { route: Route }) {
  const page = pageFor(route);
  return page === null ? <NotFound /> : <page.Component route={route} />;
}

// The session probe: GET /api/session decides the view. 200 renders the shell; 401 `locked` renders the sign-in card
// (06.1 D-02, D-47), with the route kept, so signing in returns to it.
function App() {
  const route = useRoute();
  const [session, setSession] = useState<Session | "locked" | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [paused, setPaused] = useState<Paused | null>(null);
  // True once this page has shown a session: its workspace then exists, so a later sign-in only signs in.
  const [kept, setKept] = useState(false);
  // True after this page's own sign-out (06.1-16's profile menu calls onSignedOut): the card shows the signed-out line.
  const [signedOut, setSignedOut] = useState(false);

  // Rethrows anything but a locked session, so the sign-in card can show it under the passcode.
  const refresh = useCallback(async (recovering = false) => {
    try {
      const s = await get<Session>("/api/session");
      setFailed(null);
      setSession(s);
      setKept(true);
      setSignedOut(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 503 && e.code === "paused") {
        if (recovering) throw e;
        return; // whenPaused already selected the unavailable screen
      }
      if (e instanceof ApiError && e.status === 401) {
        setSession("locked");
        return;
      }
      throw e;
    }
  }, []);

  const probe = useCallback(() => {
    refresh().catch((e) => setFailed(e instanceof ApiError ? e.message : NETWORK));
  }, [refresh]);

  const retry = useCallback<Retry>(async () => {
    try {
      const status = await get<{ ok: boolean }>("/api/readyz");
      if (status.ok !== true) return "still";
      await refresh(true); // a second outage while re-reading the session must keep the notice
      setFailed(null);
      setPaused(null);
      requestAnimationFrame(() => document.getElementById("content")?.focus());
      return "ok";
    } catch (e) {
      return e instanceof ApiError && e.status === 0 ? "network" : "still";
    }
  }, [refresh]);

  // After the profile menu's Sign out: the sign-in card with the signed-out line, at "#/" (so a public page such as
  // About, where Sign out may be pressed, never stands in for the card; the next person starts at Today).
  const onSignedOut = useCallback(() => {
    window.location.replace("#/");
    setSignedOut(true);
    setSession("locked");
  }, []);

  useEffect(() => {
    whenLocked(() => setSession("locked"));
    whenPaused(setPaused);
    whenSandboxReset(() => {
      window.location.hash = "#/";
      probe();
    });
    probe();
  }, [probe]);

  // A public page (About, UI-01): with no session (still loading, locked, or the probe failed) it renders on its own,
  // before the session decides between the sign-in card and the shell; with a session it renders inside the shell.
  const open = pageFor(route);
  if (paused !== null && open?.public !== true) {
    const notice = <PausedNotice contact={paused.contact} retry={retry} />;
    return session !== null && session !== "locked"
      ? <SessionCtx.Provider value={{ session, refresh, onSignedOut }}>{notice}</SessionCtx.Provider>
      : notice;
  }
  if (open !== null && open.public === true && (session === null || session === "locked" || failed !== null || paused !== null))
    return (
      <PausedCtx.Provider value={{ paused, retry }}>
        <main id="content" tabIndex={-1} className="h-full overflow-auto">
          <open.Component route={route} />
        </main>
      </PausedCtx.Provider>
    );
  if (session === "locked")
    return <SignIn onSignedIn={refresh} signedOut={signedOut} workspaceKept={kept || signedOut} />;
  if (failed !== null)
    return (
      <Notice>
        <p role="alert" className="text-body text-tx">{failed}</p>
        <button type="button" onClick={probe}
          className="mt-4 h-8 rounded-md border border-line-2 bg-white px-4 text-body font-semibold text-tx">Try again</button>
      </Notice>
    );
  if (session === null)
    return (
      <Notice>
        <p className="text-body text-tx2">Loading…</p>
      </Notice>
    );
  // The page sits in its own error boundary, keyed like the page, so the shell (the sidebar) stays mounted
  // when a page fails; the outer boundary catches a failure in the shell itself (review WR-07).
  const at = `${route.path}?${new URLSearchParams(route.query).toString()}`;
  const account = (session as Session & { account?: string }).account ?? "";
  return (
    <PausedCtx.Provider value={{ paused, retry }}>
    <ErrorBoundary resetKey={at} fallback={(reset) => <Notice><Failed reset={reset} /></Notice>}>
      <SessionCtx.Provider value={{ session, refresh, onSignedOut }}>
        <AppShell route={route}>
          <ErrorBoundary key={`${session.run_id}|${account}|${session.role}|${session.branch}`} resetKey={at}
            fallback={(reset) => <PageFailed reset={reset} />}>
            <Page route={route} />
          </ErrorBoundary>
        </AppShell>
      </SessionCtx.Provider>
    </ErrorBoundary>
    </PausedCtx.Provider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
