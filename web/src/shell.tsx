// The product shell (06.1-UI-SPEC "Shell"; sketch 001 E; D-06..D-09, D-63, D-66): the navy sidebar with the logo, the
// dataset mark and the nav from the page registry (pagereg.ts), the profile row, and the page header (PageHead) with
// the context line. Nothing demo-only sits in the product chrome (D-01, D-03). The sign-in card (signin.tsx) is shown
// by main.tsx while the session is locked.
import {
  Fragment, createContext, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState, type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent, type MouseEvent, type ReactNode, type RefObject,
} from "react";
import type { Session } from "./api.ts";
import { signOut } from "./contract.ts";
import { PUBLIC_DATASET, datasetMark, retryable } from "./copy.ts";
import { navItems, type NavItem } from "./pagereg.ts";
import { useAction, useRefetchOnVisible } from "./hooks.ts";
import { BACK_TO_TODAY, ErrorLine, Icon, TRY_AGAIN } from "./ui.tsx";
import { ROLE_LABEL, contextLine, markLines, profileWho } from "./vocab.ts";

// The hash router's route: "#/cases/12?tab=options" is the path "/cases/12" with the query { tab: "options" }.
export type Route = { path: string; query: Record<string, string> };

// `onSignedOut` returns the app to the sign-in card with the signed-out line (the profile menu calls it after
// POST /api/session/sign-out).
export type SessionValue = { session: Session; refresh: () => Promise<void>; onSignedOut?: () => void };

export const SessionCtx = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const value = useContext(SessionCtx);
  if (value === null) throw new Error("useSession outside SessionCtx");
  return value;
}

// The 06.1 session keys the shell reads (contract.ts Session61): the signed-in account and the nav counts. Both are
// read defensively: `nav_counts` is optional until 06.1-13 lands, and an account is derived from the role and branch
// if a session ever lacks it.
type ShellSession = Session & { account?: string; nav_counts?: Partial<Record<"today" | "board" | "tasks", number>> };

function accountOf(s: ShellSession): string {
  return s.account ?? (s.role === "hq" || s.branch === null ? "HQ" : s.branch);
}

// The dataset mark (05.2 D-03, D-04 as amended 2026-10-17; D-63): with a session it is the session's own label with
// its scenario date (copy.datasetMark: session.label filled from data_as_of by format.markDate), so no dataset label is
// typed in the web code; where no session exists yet (the sign-in card, the first load, About opened without one) it
// reads "Public sales dataset". Rendered as text, never as HTML.
export function DatasetMark({ className = "" }: { className?: string }) {
  const value = useContext(SessionCtx);
  return <span data-testid="dataset-mark" className={`text-label text-tx2 ${className}`}>
    {value === null ? PUBLIC_DATASET : datasetMark(value.session)}
  </span>;
}

// --- the page header (UI-SPEC "Page header (PageHead)") ------------------------------------------------------------

// What a page header needs from the shell: a way to say a PageHead is on screen, so the shell's own header strip
// steps aside. The mark is the shell's: the expanded sidebar's head, else the MarkStrip above the page.
type ShellValue = { claimHead: () => () => void };
const ShellCtx = createContext<ShellValue>({ claimHead: () => () => {} });

// "S042 · Scenario date: Sat 17 Oct 2026 · Store manager" (vocab.contextLine; D-08 as amended by D-63), with the
// separators drawn in line-2. The dataset's age is never shown: only the scenario date.
function ContextLine() {
  const { session } = useSession();
  const s = session as ShellSession;
  const parts = contextLine({ account: accountOf(s), role: s.role, data_as_of: s.data_as_of }).split(" · ");
  return (
    <p data-testid="context-line" className="text-body text-tx2">
      {parts.map((p, i) => (
        <Fragment key={i}>
          {i > 0 && <span className="text-line-2"> · </span>}
          <span>{p}</span>
        </Fragment>
      ))}
    </p>
  );
}

// The mark when the sidebar does not show it (the rail and the phone), layout (proposed): one strip outside the
// scrolling page, so the dataset line and the scenario date stay on screen while the page scrolls and while a drawer
// is open (D-07, D-59, D-63; CLAUDE.md "Honesty"). On the phone it sits under the bar on navy, as the sidebar's head
// draws it; on the rail it is a white strip above the page. It wraps within its width and never truncates.
function MarkStrip({ navy }: { navy: boolean }) {
  const { session } = useSession();
  const [dataset, date] = markLines(session);
  return (
    <p data-testid="mark-strip"
      className={`m-0 min-w-0 break-words text-label ${navy ? "bg-navy-950 px-4 pb-2 text-on-navy-2" : "border-b border-line bg-white px-4 py-1 text-tx2 md:px-6"}`}>
      <span data-testid="dataset-mark"><span>{dataset}</span>
      {date !== undefined && (
        <>
          <span aria-hidden="true"> · </span>
          <span className={`font-semibold ${navy ? "text-gold-300" : "text-tx"}`}>{date}</span>
        </>
      )}</span>
    </p>
  );
}

// The shell's fixed top block (the phone bar and the strip, or the rail's strip) as the CSS custom property
// --shell-top on the shell's root: the px of its bottom edge, 0 without one. A Drawer starts there, so neither covers
// the other (06.1-UI-SPEC "Accessibility contract": Drawers).
function useShellTop(block: RefObject<HTMLElement | null>, layout: string): CSSProperties {
  const [top, setTop] = useState(0);
  useLayoutEffect(() => {
    const el = block.current;
    if (el === null) {
      setTop(0);
      return;
    }
    const measure = () => setTop(Math.ceil(el.getBoundingClientRect().bottom));
    measure();
    const watch = new ResizeObserver(measure);
    watch.observe(el);
    return () => watch.disconnect();
  }, [block, layout]);
  return { "--shell-top": `${top}px` } as CSSProperties;
}

export function PageHead({ breadcrumb, title, actions }: { breadcrumb?: ReactNode; title: ReactNode; actions?: ReactNode }) {
  const { claimHead } = useContext(ShellCtx);
  useLayoutEffect(() => claimHead(), [claimHead]);
  return (
    <header className="flex flex-wrap items-end gap-x-4 gap-y-2 border-b border-line bg-white px-4 py-4 md:px-6">
      <div className="min-w-0">
        {breadcrumb !== undefined && <p className="text-label text-tx3">{breadcrumb}</p>}
        <h1 className="text-display font-semibold break-words">{title}</h1>
        <ContextLine />
      </div>
      {actions !== undefined && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

// The shell's own strip for a page that does not draw PageHead yet (the Phase 5 pages each later plan rebuilds): the
// context line, so no signed-in screen lacks it (the mark is the shell's own, above).
function HeadStrip() {
  return (
    <div className="border-b border-line bg-white px-4 py-2 md:px-6">
      <ContextLine />
    </div>
  );
}

// An unknown route (UI-SPEC "Routes"): "Page not found." and the way back to Today ("#/", which goes to the first page
// while Today has no page of its own).
export function NotFound() {
  return (
    <div className="p-4 md:p-6">
      <p className="text-body text-tx">Page not found.</p>
      <a href="#/" className="mt-2 inline-block text-body text-accent underline">{BACK_TO_TODAY}</a>
    </div>
  );
}

// --- the shell's layout state (UI-SPEC "Sidebar": the edge chevron; "Phone top bar and nav sheet") ------------------

// The collapse choice: localStorage "ss-shell" holds "1" (collapsed) or "0" (expanded) once the person chose, and that
// choice wins on reload; nothing else is ever stored under it (T-06.1-55). With no choice the width decides: expanded
// at 1024px and wider, the rail at 768-1023px. Storage that is blocked keeps the choice for this page only.
const SHELL_KEY = "ss-shell";

function storedCollapse(): boolean | null {
  try {
    const v = localStorage.getItem(SHELL_KEY);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}

function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const change = () => setOn(m.matches);
    change();
    m.addEventListener("change", change);
    return () => m.removeEventListener("change", change);
  }, [query]);
  return on;
}

// Keeps an absolutely placed panel (a fixed tooltip or menu) 8px inside the viewport (UI-SPEC "Responsive and overflow
// contract"): measured before paint and on every resize while it is shown.
const EDGE = 8;
function useClamp(el: RefObject<HTMLElement | null>, shown: boolean, deps: unknown[]): void {
  useLayoutEffect(() => {
    const node = el.current;
    if (!shown || node === null) return;
    const place = () => {
      node.style.translate = "0 0";
      const box = node.getBoundingClientRect();
      const w = document.documentElement.clientWidth;
      const h = window.innerHeight;
      let dx = 0;
      let dy = 0;
      if (box.right > w - EDGE) dx = w - EDGE - box.right;
      if (box.left + dx < EDGE) dx = EDGE - box.left;
      if (box.bottom > h - EDGE) dy = h - EDGE - box.bottom;
      if (box.top + dy < EDGE) dy = EDGE - box.top;
      node.style.translate = `${dx}px ${dy}px`;
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [shown, ...deps]);
}

// --- the nav (UI-SPEC "Sidebar": nav table, rail) ---------------------------------------------------------------------

const COUNT_WORDS: Record<string, string> = { today: "actions waiting", board: "open stock decisions", tasks: "open tasks" };

type Tip = { label: string; top: number; height: number; left: number };

function NavLink({ item, current, rail, onTip, onNavigate }: {
  item: NavItem; current: boolean; rail: boolean; onTip: (tip: Tip | null) => void; onNavigate?: () => void;
}) {
  const hot = item.countKey === "today";
  const words = COUNT_WORDS[item.countKey ?? ""] ?? "";
  // The rail hides the label and the count, so the link's name carries both ("Today, 3 actions waiting").
  const railName = item.count > 0 ? `${item.label}, ${item.count} ${words}` : item.label;
  const showTip = (e: { currentTarget: HTMLAnchorElement }) => {
    if (!rail) return;
    const box = e.currentTarget.getBoundingClientRect();
    onTip({ label: item.label, top: box.top, height: box.height, left: box.right + 8 });
  };
  return (
    <a href={item.href} aria-current={current ? "page" : undefined} aria-label={rail ? railName : undefined}
      onClick={onNavigate} onFocus={showTip} onBlur={() => onTip(null)} onMouseEnter={showTip}
      onMouseLeave={() => onTip(null)}
      className={`relative flex h-11 items-center gap-2 rounded-lg text-body text-on-navy hover:bg-white/[.07] hover:text-white aria-[current=page]:bg-navy-800 aria-[current=page]:font-semibold aria-[current=page]:text-white md:h-9 ${rail ? "justify-center" : "px-2"}`}>
      {current && <span aria-hidden="true" className="absolute top-2 bottom-2 -left-4 w-1 rounded-r bg-gold-500" />}
      <span className="relative">
        <Icon name={item.icon} />
        {rail && hot && item.count > 0 && (
          <span aria-hidden="true" className="absolute -top-1 -right-1 h-2 w-2 rounded-full bg-gold-500" />
        )}
      </span>
      <span className={rail ? "sr-only" : "min-w-0 flex-1 truncate"}>{item.label}</span>
      {!rail && item.count > 0 && (
        <span className={`num text-label ${hot ? "rounded-full bg-gold-500 px-2 font-semibold text-navy-950" : "text-on-navy-2"}`}>
          {item.count}<span className="sr-only"> {words}</span>
        </span>
      )}
    </a>
  );
}

// The rail's label to the right of an item, on hover and on keyboard focus (navy-950, white Label, radius 8px). It is
// fixed to the viewport, so the sidebar's own scroll box never clips it, and kept 8px inside the viewport.
function RailTip({ tip }: { tip: Tip }) {
  const ref = useRef<HTMLSpanElement>(null);
  useClamp(ref, true, [tip.top, tip.left]);
  return (
    <span ref={ref} style={{ top: tip.top, height: tip.height, left: tip.left }}
      className="pointer-events-none fixed z-50 flex items-center">
      <span role="tooltip" className="rounded-lg bg-navy-950 px-2 py-1 text-label whitespace-nowrap text-white shadow-raise">
        {tip.label}
      </span>
    </span>
  );
}

// The nav from the registry (D-09): a link only for a page that works, by section in the nav table's order.
function Nav({ route, rail = false, onNavigate }: { route: Route; rail?: boolean; onNavigate?: () => void }) {
  const { session } = useSession();
  const [tip, setTip] = useState<Tip | null>(null);
  useEffect(() => setTip(null), [rail, route.path]);
  return (
    <nav aria-label="Main" className="mt-4 flex flex-col gap-1">
      {navItems(session as ShellSession).map((g) => (
        <div key={g.section} className="flex flex-col gap-1">
          {g.section !== "" && !rail && (
            <p className="px-2 pt-2 text-label font-semibold uppercase tracking-[.06em] text-on-navy-2">{g.section}</p>
          )}
          {g.section !== "" && rail && <span aria-hidden="true" className="mx-2 my-1 border-t border-white/10" />}
          {g.items.map((item) => (
            <NavLink key={item.path} item={item} current={route.path === item.path} rail={rail} onTip={setTip}
              onNavigate={onNavigate} />
          ))}
        </div>
      ))}
      {tip !== null && <RailTip tip={tip} />}
    </nav>
  );
}

// The head: the logo and "ShelfShift"; with the label, the two mark lines (dataset in on-navy-2, scenario date in
// gold-300). The rail shows the logo only; the mark then moves to the page header.
function SideHead({ rail = false }: { rail?: boolean }) {
  const { session } = useSession();
  const [dataset, date] = markLines(session);
  return (
    <div>
      <div className={`flex items-center gap-2 ${rail ? "justify-center" : ""}`}>
        <img src="/logo.png" alt="" width={32} height={32} className="h-8 w-8 shrink-0 rounded-lg" />
        {!rail && <span className="text-title font-semibold text-white">ShelfShift</span>}
      </div>
      {!rail && <div data-testid="dataset-mark">
        <p className="mt-2 text-label text-on-navy-2">{dataset}</p>
        {date !== undefined && <p className="text-label font-semibold text-gold-300">{date}</p>}
      </div>}
    </div>
  );
}

// --- the profile menu (UI-SPEC "Profile row", "Profile menu"; D-01, D-02) ------------------------------------------

export const ABOUT_ITEM = "About ShelfShift and the data";
export const SIGN_OUT = "Sign out";

// The avatar's initials: "HQ", or the store code's last two characters.
function initialsOf(account: string): string {
  return account === "HQ" ? "HQ" : account.replace(/\s+/g, "").slice(-2).toUpperCase();
}

function Avatar({ account }: { account: string }) {
  return (
    <span aria-hidden="true" className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-navy-700 text-label font-semibold text-white">
      {initialsOf(account)}
    </span>
  );
}

type Place = { left?: number; right?: number; top?: number; bottom?: number };

// `look` "row": the sidebar's profile row (avatar, account, role, chevron; the avatar only in the rail), whose menu
// opens to the right of the sidebar, upward from the row. "avatar": the phone top bar's 44px avatar button, whose
// menu opens below the bar, right-aligned. The menu: the who block, a separator, then exactly two items. Enter or
// Space opens it on its first item; ArrowDown and ArrowUp move (wrapping), Home and End jump; Esc closes it and
// returns focus to the button; a press outside or focus leaving closes it.
function ProfileMenu({ look, rail = false, onSignOut }: { look: "row" | "avatar"; rail?: boolean; onSignOut: () => void }) {
  const { session } = useSession();
  const s = session as ShellSession;
  const account = accountOf(s);
  const role = ROLE_LABEL[s.role];
  const [name, who] = profileWho({ account, role: s.role });
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<Place>({});
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  useClamp(panel, open, [place]);

  const show = () => {
    const box = trigger.current?.getBoundingClientRect();
    if (box === undefined) return;
    setPlace(look === "row"
      ? { left: box.right + 16 + 8, bottom: window.innerHeight - box.bottom } // past the sidebar's 16px padding
      : { right: document.documentElement.clientWidth - box.right, top: box.bottom + 4 });
    setOpen(true);
  };
  const close = (focus = true) => {
    setOpen(false);
    if (focus) trigger.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    items.current[0]?.focus();
    const outside = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!panel.current?.contains(t) && !trigger.current?.contains(t)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);

  const move = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const list = items.current.filter((x): x is HTMLButtonElement => x !== null);
    const at = list.indexOf(document.activeElement as HTMLButtonElement);
    const to = e.key === "ArrowDown" ? (at + 1) % list.length
      : e.key === "ArrowUp" ? (at - 1 + list.length) % list.length
      : e.key === "Home" ? 0
      : e.key === "End" ? list.length - 1
      : null;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (to !== null) {
      e.preventDefault();
      list[to].focus();
    }
  };
  const choose = (run: () => void) => {
    close();
    run();
  };

  return (
    <>
      <button ref={trigger} type="button" aria-haspopup="menu" aria-expanded={open} aria-label={`Account: ${account}, ${role}`}
        onClick={() => (open ? close() : show())}
        onKeyDown={(e) => { if (e.key === "ArrowDown" && !open) { e.preventDefault(); show(); } }}
        className={look === "avatar"
          ? "grid h-11 w-11 place-items-center rounded-lg"
          : `flex w-full items-center gap-2 rounded-lg p-1 text-left hover:bg-white/[.06] ${rail ? "justify-center" : ""}`}>
        <Avatar account={account} />
        {look === "row" && !rail && (
          <>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-body font-semibold text-white">{account}</span>
              <span className="block text-label text-on-navy-2">{role}</span>
            </span>
            <Icon name="chev" size={14} className="text-on-navy-2" />
          </>
        )}
      </button>
      {open && (
        <div ref={panel} style={place}
          onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false); }}
          className="fixed z-50 max-w-[calc(100vw-32px)] min-w-60 rounded-xl border border-line bg-white p-1 text-tx shadow-float">
          <div className="p-2">
            <p className="text-body font-semibold text-tx">{name}</p>
            <p className="text-label text-tx2">{who}</p>
          </div>
          <hr className="my-1 border-line" />
          <div role="menu" aria-label={`Account: ${account}`} onKeyDown={move} className="flex flex-col">
            <button ref={(el) => { items.current[0] = el; }} type="button" role="menuitem" tabIndex={-1}
              onClick={() => choose(() => { window.location.hash = "#/about"; })}
              className="flex min-h-11 items-center gap-2 rounded-lg px-2 text-left text-body text-tx hover:bg-navy-50 focus-visible:bg-navy-50 md:min-h-9">
              <Icon name="info" className="text-tx2" />{ABOUT_ITEM}
            </button>
            <button ref={(el) => { items.current[1] = el; }} type="button" role="menuitem" tabIndex={-1}
              onClick={() => choose(onSignOut)}
              className="flex min-h-11 items-center gap-2 rounded-lg px-2 text-left text-body text-tx hover:bg-navy-50 focus-visible:bg-navy-50 md:min-h-9">
              <Icon name="out" className="text-tx2" />{SIGN_OUT}
            </button>
          </div>
        </div>
      )}
    </>
  );
}

// --- the phone nav sheet (UI-SPEC "Phone top bar and nav sheet"; D-59) -----------------------------------------------

const FOCUSABLE = "a[href], button:not([disabled]), [tabindex]:not([tabindex='-1'])";

// A modal left sheet with the expanded sidebar's head, mark and nav. Focus moves to its first link and stays inside
// (Tab and Shift+Tab wrap); Esc, the scrim and any navigation close it, and focus returns to the menu button.
function NavSheet({ route, onClose }: { route: Route; onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    box.current?.querySelector<HTMLElement>("nav a[href]")?.focus();
  }, []);
  const keys = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key !== "Tab" || box.current === null) return;
    const list = Array.from(box.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (list.length === 0) return;
    const first = list[0];
    const last = list[list.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };
  return (
    <div className="fixed inset-0 z-40">
      <div aria-hidden="true" onClick={onClose} className="absolute inset-0 bg-[rgba(2,13,81,.4)]" />
      <div ref={box} role="dialog" aria-modal="true" aria-label="Main menu" onKeyDown={keys}
        className="on-navy absolute top-0 bottom-0 left-0 flex w-[280px] max-w-[85vw] flex-col overflow-y-auto bg-navy-950 p-4">
        <SideHead />
        <Nav route={route} onNavigate={onClose} />
      </div>
    </div>
  );
}

// --- the shell -------------------------------------------------------------------------------------------------------

// A visually hidden "Skip to content" link comes first in tab order. After a route change to another page focus moves
// to main#content without scrolling (G-05-2 item 14). The ref starts at the current page, so the first load and React's
// development double-invoke move nothing. 768px and wider: the sidebar (or its rail) beside the page; below: the 48px
// top bar and the nav sheet. Sign out is one keyed write (contract.signOut); the app then shows the sign-in card with
// the signed-out line, and the pages remount, so nothing of the previous account stays on screen (T-06.1-53).
export function AppShell({ route, children }: { route: Route; children: ReactNode }) {
  const { refresh, onSignedOut } = useSession();
  useRefetchOnVisible(() => void refresh().catch(() => {})); // the next write or page load shows any failure
  const shownPage = useRef(route.path); // another path is another page; the query (a tab, a view) is the same page
  useEffect(() => {
    if (route.path === shownPage.current) return;
    shownPage.current = route.path;
    document.getElementById("content")?.focus({ preventScroll: true });
  }, [route.path]);
  const skip = (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault(); // a plain #content would change the hash route
    document.getElementById("content")?.focus();
  };

  const [heads, setHeads] = useState(0);
  const claimHead = useCallback(() => {
    setHeads((n) => n + 1);
    return () => setHeads((n) => n - 1);
  }, []);

  const phone = useMedia("(max-width: 767px)");
  const wide = useMedia("(min-width: 1024px)");
  const [chosen, setChosen] = useState(storedCollapse);
  const collapsed = chosen ?? !wide;
  const toggle = () => {
    const next = !collapsed;
    setChosen(next);
    try {
      localStorage.setItem(SHELL_KEY, next ? "1" : "0");
    } catch {
      // storage blocked: the choice holds for this page only
    }
  };

  const [sheet, setSheet] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const topBlock = useRef<HTMLDivElement>(null);
  const shellTop = useShellTop(topBlock, phone ? "phone" : collapsed ? "rail" : "expanded");
  const closeSheet = useCallback(() => {
    setSheet(false);
    menuButton.current?.focus();
  }, []);
  useEffect(() => {
    if (!phone) setSheet(false);
  }, [phone]);

  const out = useAction(async (_body: Record<string, never>, key: string) => {
    const done = await signOut(key);
    onSignedOut?.();
    return done;
  });
  const signOutAction = out.error !== null && retryable(out.error.code)
    ? { label: TRY_AGAIN, onClick: () => void out.retry() }
    : undefined;

  const content = (
    <main id="content" tabIndex={-1} className="min-w-0 flex-1 overflow-auto" data-route={route.path}>
      <ErrorLine error={out.error} action={signOutAction} className="m-4" />
      {heads === 0 && <HeadStrip />}
      {children}
    </main>
  );
  // The page's column, the same element in every layout so the page keeps its state when a phone turns or the window
  // crosses the 768px breakpoint; on the rail the mark strip sits at its top, outside the scrolling main.
  const column = (strip: ReactNode) => (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {strip}
      {content}
    </div>
  );
  const skipLink = (
    <a href="#content" onClick={skip}
      className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-lg focus:bg-white focus:px-4 focus:py-2 focus:text-body">
      Skip to content
    </a>
  );

  if (phone)
    return (
      <ShellCtx.Provider value={{ claimHead }}>
        <div className="flex h-full flex-col" style={shellTop}>
          {skipLink}
          <div ref={topBlock} className="on-navy sticky top-0 z-30 shrink-0 bg-navy-950">
            <header className="flex h-12 items-center gap-2 px-1">
              <button ref={menuButton} type="button" aria-label="Open menu" aria-expanded={sheet} onClick={() => setSheet(true)}
                className="grid h-11 w-11 shrink-0 place-items-center rounded-lg text-white">
                <Icon name="menu" size={20} />
              </button>
              <img src="/logo.png" alt="" width={24} height={24} className="h-6 w-6 shrink-0 rounded-md" />
              <span className="min-w-0 truncate text-title font-semibold text-white">ShelfShift</span>
              <span className="ml-auto">
                <ProfileMenu look="avatar" onSignOut={() => void out.run({})} />
              </span>
            </header>
            <MarkStrip navy />
          </div>
          {column(null)}
          {sheet && <NavSheet route={route} onClose={closeSheet} />}
        </div>
      </ShellCtx.Provider>
    );

  return (
    <ShellCtx.Provider value={{ claimHead }}>
      <div className="flex h-full" style={shellTop}>
        {skipLink}
        <div className="relative shrink-0">
          <aside className={`on-navy flex h-full flex-col overflow-x-hidden overflow-y-auto bg-navy-950 p-4 transition-[width] duration-200 ${collapsed ? "w-[72px]" : "w-[236px]"}`}>
            <SideHead rail={collapsed} />
            <Nav route={route} rail={collapsed} />
            <div className="mt-auto border-t border-white/10 pt-2">
              <ProfileMenu look="row" rail={collapsed} onSignOut={() => void out.run({})} />
            </div>
          </aside>
          <button type="button" onClick={toggle} aria-expanded={!collapsed}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className="absolute top-6 -right-3 z-20 grid h-6 w-6 place-items-center rounded-full border border-line bg-white text-navy-950 shadow-raise">
            <Icon name="chevl" size={14} className={collapsed ? "rotate-180" : ""} />
          </button>
        </div>
        {column(collapsed ? <div ref={topBlock} className="shrink-0"><MarkStrip navy={false} /></div> : null)}
      </div>
    </ShellCtx.Provider>
  );
}
