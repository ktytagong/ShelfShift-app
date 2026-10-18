// The shared UI primitives (05-UI-SPEC "Shared UI primitives"). Only the UI-SPEC tokens: text-label/body/title/display,
// weights 400 and 600, spacing 1/2/4/6/8/12. Status never relies on colour alone: every badge and pill carries text.
// Charts (06.1 D-13): LineChart and BarChart draw through Chart.js on a canvas ref, registering only the parts they use.
// Chart.js loads on the first chart's mount (a separate chunk), so pages without a chart never download it.
import {
  useEffect, useId, useLayoutEffect, useRef, useState, type ComponentPropsWithRef, type KeyboardEvent, type ReactNode,
} from "react";
import type { Chart, ChartConfiguration, ChartDataset } from "chart.js";
import { post, type ApiError, type CaseDetail, type Eligibility, type Session } from "./api.ts";
import {
  historyFilter, qtyParts, segmentStates, type ChartSeries, type ChartTable, type HistoryFilter, type QtyInput,
  type SegmentState,
} from "./bars.ts";
import {
  DESIGNED_EXAMPLE, DESIGNED_TIP, DESIGNED_TIP_LABEL, EVALUATING, EXCLUDED_HEADING, NEEDS_EVALUATION, PRODUCT_CODE,
  eligibilityLine, flagBody, pagerText, retryable,
} from "./copy.ts";
import { badgeChip } from "./boardview.ts";
import { pageInRange } from "./derive.ts";
import { dateLabel, units } from "./format.ts";
import { useAction } from "./hooks.ts";
import { BACK_TO_BOARD, TERMS } from "./vocab.ts";

// --- 06.1 primitive strings (06.1-UI-SPEC). "(proposed)" marks wording the user has not confirmed: one constant each,
// so a change is one line. The others are the UI-SPEC's own words. ------------------------------------------------------

export const TRY_AGAIN = "Try again";
export const BACK_TO_TODAY = "Back to Today";
export const CLOSE = "Close"; // the drawer's close button (UI-SPEC "Accessibility contract")
export const NOTHING_CHANGED = "Nothing was changed."; // (proposed) under a page that didn't load
export const STALE_REFRESH_FAILED = "Couldn't refresh. Showing what was loaded before."; // (proposed)
export const STALE_VERSION_CHANGED = "Something changed in your workspace since this page loaded."; // (proposed)
export const REFRESH = "Refresh"; // (proposed) the version-change banner's action
export const HISTORY_FILTER_LABEL = "Show"; // (proposed) the accessible name of the All · Decisions · Data control
export const HISTORY_FILTERS: HistoryFilter[] = ["All", "Decisions", "Data"];
export const ASSUMPTION_SOURCE_LABEL = "Source of this assumption"; // (proposed) the Assumption chip's info button
export const KIND_TEXT = {
  simulated: "Simulated",
  estimated: "Estimated",
  forecast: "Forecast · precomputed",
  assumption: "Assumption",
} as const;
export const QTY_TEXT = { // the quantity bar's adjacent text: "Committed 110 · Dispatched 104 · Received 104"
  committed: "Committed",
  dispatched: "Dispatched",
  received: "Received",
  damaged: "Damaged", // (proposed) shown only above zero
  missing: "Missing", // (proposed) shown only above zero
} as const;
export const SHOW_TABLE = "Show as table";
export const SHOW_CHART = "Show as chart";
export const MISSING_CELL = "—"; // (proposed) a chart table cell the series has no value for (never shown as zero)

// --- Icon: inline 24x24 line icons, stroke 1.75, round caps and joins, drawn from the mockup sprite ------------------

const PATHS = {
  report: <path d="M6 3h9l3 3v15H6z M9 17v-3 M12 17v-6 M15 17v-4" />,
  leaf: <><path d="M11 20A7 7 0 0 1 4 13c0-6 7-9 16-9 0 9-3 16-9 16z" /><path d="M4 20c4-4 7.5-6.5 11-8" /></>,
  cases: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16M15 4v16" /></>,
  inbox: <><path d="M3 13h5l2 3h4l2-3h5" /><path d="M5.5 5h13L21 13v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z" /></>,
  sliders: <><path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12" /><circle cx="16" cy="6" r="2" /><circle cx="10" cy="12" r="2" /><circle cx="18" cy="18" r="2" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></>,
  user: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  snow: <path d="M12 2v20M4.9 6.5l14.2 11M19.1 6.5 4.9 17.5" />,
  next: <><path d="m6 5 10 7-10 7z" /><path d="M19 5v14" /></>,
  reset: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" /></>,
  chev: <path d="m6 9 6 6 6-6" />,
  right: <path d="m9 6 6 6-6 6" />,
  arrow: <path d="M4 12h16M14 6l6 6-6 6" />,
  search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  board: <><rect x="3" y="4" width="5" height="16" rx="1.5" /><rect x="10" y="4" width="5" height="11" rx="1.5" /><rect x="17" y="4" width="4" height="7" rx="1.5" /></>,
  list: <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />,
  sort: <path d="M7 4v16M3 16l4 4 4-4M17 20V4M13 8l4-4 4 4" />,
  more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
  xfer: <path d="M7 4 3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7" />,
  tag: <><path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z" /><circle cx="7.5" cy="7.5" r="1.3" /></>,
  hold: <><rect x="4" y="4" width="16" height="16" rx="3" /><path d="M9 12h6" /></>,
  pause: <path d="M9 5v14M15 5v14" />,
  alert: <><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><path d="M12 9v4M12 17h.01" /></>,
  lock: <><rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></>,
  check: <><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></>,
  circle: <circle cx="12" cy="12" r="9" />,
  x: <><circle cx="12" cy="12" r="9" /><path d="m9 9 6 6M15 9l-6 6" /></>,
  redo: <><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></>,
  download: <><path d="M12 4v11M7 10l5 5 5-5" /><path d="M4 19h16" /></>,
  // 06.1 (sketches/assets/shell.js sprite; trend, menu and flag drawn in the same line style)
  today: <><rect x="3" y="4" width="18" height="17" rx="2" /><path d="M3 9h18M8 2v4M16 2v4" /><circle cx="12" cy="15" r="2" /></>,
  gantt: <><rect x="3" y="4" width="18" height="17" rx="2" /><path d="M3 9h18M8 2v4M16 2v4M7 13h6M10 17h7" /></>,
  swap: <path d="M4 8h14l-3-3M20 16H6l3 3" />,
  truck: <><path d="M3 6h11v10H3zM14 9h4l3 3v4h-7" /><circle cx="7" cy="18" r="2" /><circle cx="17" cy="18" r="2" /></>,
  box: <><path d="M3 7l9-4 9 4v10l-9 4-9-4z" /><path d="M3 7l9 4 9-4M12 11v10" /></>,
  chart: <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
  trend: <><path d="m3 17 6-6 4 4 8-8" /><path d="M14 7h7v7" /></>,
  out: <path d="M10 4H5v16h5M14 8l4 4-4 4M18 12H9" />,
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  chevl: <path d="M15 6l-6 6 6 6" />,
  chevr: <path d="M9 6l6 6-6 6" />,
  pin: <><path d="M12 22s7-7 7-12a7 7 0 0 0-14 0c0 5 7 12 7 12z" /><circle cx="12" cy="10" r="2.5" /></>,
  filter: <path d="M3 5h18l-7 8v6l-4 2v-8z" />,
  flag: <path d="M5 21V4M5 4h12l-2.5 4L17 12H5" />,
};
export type IconName = keyof typeof PATHS;

// Reports tabs: automatic activation, roving tabindex, linked panel and keyboard focus.
export function Tabs<T extends string>({ label, value, options, onPick, children }: {
  label: string; value: T; options: { value: T; label: string }[]; onPick: (value: T) => void; children: ReactNode;
}) {
  const id = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  return <div className="min-w-0">
    <div role="tablist" aria-label={label} className="flex border-b border-rule bg-surface px-4 md:px-6">
      {options.map((o, i) => <button key={o.value} type="button" role="tab" id={`${id}-${o.value}`}
        ref={(el) => { refs.current[i] = el; }} aria-selected={value === o.value}
        aria-controls={`${id}-panel-${o.value}`} tabIndex={value === o.value ? 0 : -1}
        className={`h-10 border-b-2 px-4 text-body font-semibold max-md:h-11 ${value === o.value
          ? "border-navy-800 text-navy-800" : "border-transparent text-ink-2"}`}
        onClick={() => onPick(o.value)} onKeyDown={(e) => {
          const next = e.key === "ArrowRight" ? (i + 1) % options.length
            : e.key === "ArrowLeft" ? (i + options.length - 1) % options.length
            : e.key === "Home" ? 0 : e.key === "End" ? options.length - 1 : null;
          if (next !== null) { e.preventDefault(); onPick(options[next].value); refs.current[next]?.focus(); }
        }}>{o.label}</button>)}
    </div>
    <div role="tabpanel" id={`${id}-panel-${value}`} aria-labelledby={`${id}-${value}`} tabIndex={0}
      className="min-w-0 p-4 md:p-6">{children}</div>
  </div>;
}

export function FigureTile({ label, count, note, icon, href, needsAction = false }: {
  label: string; count: number; note: string; icon: IconName; href?: string; needsAction?: boolean;
}) {
  return <div role="group" aria-label={`${label}: ${units(count)}, ${note}`}
    className={`flex min-w-0 flex-col gap-2 rounded-[10px] border border-rule bg-surface p-4 ${needsAction
      ? "border-l-4 border-l-navy-800" : ""}`}>
    <div className="flex min-w-0 items-start gap-2 text-label text-ink-2"><Icon name={icon} /><span>{label}</span></div>
    <p className={`num m-0 text-display font-semibold ${count === 0 ? "text-tx3" : "text-ink"}`}>{units(count)}</p>
    <p className="m-0 truncate text-label text-ink-2" title={note}>{note}</p>
    {href && count > 0 && <a href={href} aria-label={`View breakdown: ${label}`}
      className="mt-auto inline-flex min-h-11 items-center text-label font-semibold text-navy-800 underline">View breakdown</a>}
  </div>;
}

// Decorative: always aria-hidden, so the text beside it carries the meaning.
export function Icon({ name, size = 16, className = "" }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`shrink-0 ${className}`}>
      {PATHS[name]}
    </svg>
  );
}

// --- Badge / Pill: soft status badges, 20px high, radius 6px, Label 600, an optional 12px icon ----------------------

const TONE = {
  bad: "bg-bad-soft text-bad",
  warn: "bg-warn-soft text-warn",
  good: "bg-good-soft text-good",
  gray: "bg-gray-soft text-gray",
  accent: "bg-accent-soft text-accent-dark", // the 05 accent pill tone (UI-SPEC "Color")
  // 06.1 (06.1-UI-SPEC "Data tones"): waiting on someone; simulated, estimated, forecast or planned; chilled zone
  info: "bg-info-soft text-info",
  plan: "border border-dashed border-plan-line bg-sunken text-ink-2",
  chill: "bg-chill-soft text-chill",
};
export type Tone = keyof typeof TONE;

export function Pill({ tone = "gray", icon, children }: { tone?: Tone; icon?: IconName; children: ReactNode }) {
  return (
    <span className={`inline-flex h-5 items-center gap-1 rounded-md px-1 text-label font-semibold whitespace-nowrap ${TONE[tone]}`}>
      {icon && <Icon name={icon} size={12} />}
      {children}
    </span>
  );
}

// --- Elapsed: "Working · 3s", counted on the client while a write runs ---------------------------------------------

// `label` null shows only the seconds (the busy Button appends it after its own label). It is not a live region and
// the seconds are hidden from assistive tech, so a screen reader is never told each tick (review WR-06): the label
// stays readable, and the busy control carries aria-busy.
export function Elapsed({ label = "Working" }: { label?: string | null }) {
  const [s, setS] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setS((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const seconds = <span aria-hidden="true">{s}s</span>;
  return <span className="num">{label === null ? seconds : <>{label} · {seconds}</>}</span>;
}

// --- Button: primary, plain, ghost, danger; 32px, Body 600, px-4, radius 6px -----------------------------------------

const KIND = {
  primary: "border border-accent bg-accent text-white hover:bg-accent-dark",
  plain: "border border-line-2 bg-white text-tx hover:bg-side",
  ghost: "border border-transparent text-tx2 hover:bg-gray-soft hover:text-tx",
  danger: "border border-line-2 bg-white text-bad hover:bg-bad-soft", // destructive confirms only
};
export type ButtonKind = keyof typeof KIND;

// A busy button keeps its label, appends the Elapsed counter and is disabled, so a second press sends nothing.
export function Button({ kind = "plain", busy = false, icon, children, className = "", disabled, type = "button", ...rest }:
  ComponentPropsWithRef<"button"> & { kind?: ButtonKind; busy?: boolean; icon?: IconName }) {
  return (
    <button {...rest} type={type} disabled={disabled || busy} aria-busy={busy || undefined}
      className={`inline-flex min-h-8 items-center justify-center gap-1 rounded-md px-4 py-1 text-center text-body font-semibold disabled:cursor-not-allowed disabled:opacity-80 ${KIND[kind]} ${className}`}>
      {icon && <Icon name={icon} />}
      <span>{children}</span>
      {busy && <span aria-hidden="true">· <Elapsed label={null} /></span>}
    </button>
  );
}

// --- Section, Table, Kv ------------------------------------------------------------------------------------------------

export function Section({ title, headingId, right, children, className = "" }:
  { title?: ReactNode; headingId?: string; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-[10px] border border-line bg-white p-4 ${className}`}>
      {(title || right) && (
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 id={headingId} tabIndex={headingId ? -1 : undefined} className="text-title font-semibold">{title}</h2>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

// Dense table: header Label 600 tx3, cells Body, 8px x 16px; `numeric[i]` right-aligns column i.
export function Table({ head, rows, numeric = [] }: { head: ReactNode[]; rows: ReactNode[][]; numeric?: boolean[] }) {
  const align = (i: number) => (numeric[i] ? "text-right" : "text-left");
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-body">
        <thead className="bg-side">
          <tr>{head.map((h, i) => <th key={i} scope="col" className={`border-b border-line px-4 py-2 text-label font-semibold text-tx2 ${align(i)}`}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{r.map((c, j) => <td key={j} className={`num border-b border-line px-4 py-2 ${align(j)}`}>{c}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Label/value rows: label Label tx3, value Body tx, right-aligned and tabular. `stacked` (the board card's body,
// G-05-2 item 10) puts each label above its value, left-aligned and wrapping between words, so a narrow column never
// squeezes the value into a few characters per line; the rows keep the caller's order either way.
export function Kv({ items, stacked = false }: { items: [ReactNode, ReactNode][]; stacked?: boolean }) {
  if (stacked) {
    return (
      <dl className="flex flex-col gap-y-1">
        {items.map(([k, v], i) => (
          <div key={i}>
            <dt className="text-label text-tx3">{k}</dt>
            <dd className="num m-0 text-left text-body text-tx break-words">{v}</dd>
          </div>
        ))}
      </dl>
    );
  }
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1">
      {items.map(([k, v], i) => [
        <dt key={`k${i}`} className="text-label text-tx3">{k}</dt>,
        <dd key={`v${i}`} className="num m-0 text-right text-body text-tx">{v}</dd>,
      ])}
    </dl>
  );
}

// --- ErrorLine: the API message word for word next to the control that caused it ------------------------------------

export function ErrorLine({ error, action, id, className = "" }: {
  error: ApiError | string | null;
  action?: { label: string; onClick: () => void };
  id?: string;
  className?: string;
}) {
  if (error === null) return null;
  const message = typeof error === "string" ? error : error.message;
  return (
    <div id={id} role="alert" className={`flex flex-wrap items-center gap-2 rounded-md bg-bad-soft px-2 py-1 text-body text-tx ${className}`}>
      <span className="min-w-0 break-words">{message}</span>
      {action && <Button kind="plain" onClick={action.onClick}>{action.label}</Button>}
    </div>
  );
}

// --- Provenance: in Phase 5 only the template summary pill (05-CONTEXT D-14) ----------------------------------------

export function Provenance() {
  return <Pill tone="gray">Template summary · from the engine's figures</Pill>;
}

// --- Tooltip: an info button with a short note (the waste-flip lines; UI-SPEC "Accessibility Contract") ----------

// Never hover-only. The button is focusable, named by `label`, and its aria-describedby names the role=tooltip element,
// so a screen reader announces the note on focus. The note opens on keyboard focus and on mouse hover, and closes on
// Escape, blur and mouse leave; a tap or a keyboard press toggles it (touch has no hover). A press with a pointer does
// not open it on focus, so the click that follows toggles it once, and a mouse click keeps it open (hover owns it).
// The button and the note are one hover area (WCAG 1.4.13 "hoverable"; 05-REVIEW WR-11): the pointer handlers sit on
// the wrapper, and the note's 4px gap under the button is its own transparent padding, so moving onto the note keeps it
// open. While it is open a document-level Escape closes it wherever focus is (1.4.13 "dismissible"); the listener goes
// on close and on unmount. A blur while the pointer is still over the button or the note (a press inside the note to
// select its text) leaves it open (05-REVIEW IN-16). On open, and on every window resize while it is open (IN-17), the
// note is moved left or right just enough to stay inside the viewport, 8px from either edge (05-UI-REVIEW item 2: it
// was cut off at the window edge).
// While closed it carries the `hidden` attribute, so it adds nothing to the line's visible text. The panel: the menu
// surface (white, 1px line border, radius 8px, the menu shadow), Label size, tx2, 8px padding, at most 240px wide,
// under the button; no transition. The global focus ring applies.
const EDGE = 8; // px kept between the open note and the viewport edge

export function Tooltip({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const pressed = useRef<string | null>(null); // the pointer type of a press in progress
  const panel = useRef<HTMLSpanElement>(null);
  const hovered = useRef(false); // the mouse is over the button or the note

  useEffect(() => {
    if (!open) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  // Measured before paint, from the note's own place under the button (left 0), so it never shows cut off; measured
  // again on each resize while it is open.
  useLayoutEffect(() => {
    const el = panel.current;
    if (!open || el === null) return;
    const place = () => {
      const width = document.documentElement.clientWidth;
      el.style.left = "0px";
      const box = el.getBoundingClientRect();
      let shift = 0;
      if (box.right > width - EDGE) shift = width - EDGE - box.right;
      if (box.left + shift < EDGE) shift = EDGE - box.left;
      el.style.left = `${shift}px`;
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open, text]);

  return (
    <span className="relative ml-1 inline-flex align-middle"
      onPointerEnter={(e) => { if (e.pointerType === "mouse") { hovered.current = true; setOpen(true); } }}
      onPointerLeave={(e) => { if (e.pointerType === "mouse") { hovered.current = false; setOpen(false); } }}>
      <button type="button" aria-label={label} aria-describedby={id}
        onPointerDown={(e) => { pressed.current = e.pointerType; }}
        onFocus={() => { if (pressed.current === null) setOpen(true); }}
        onBlur={() => { pressed.current = null; if (!hovered.current) setOpen(false); }}
        onClick={() => {
          const by = pressed.current;
          pressed.current = null;
          setOpen((x) => by === "mouse" || !x);
        }}
        className="inline-flex h-5 w-5 items-center justify-center rounded text-tx3 hover:text-tx2">
        <Icon name="info" />
      </button>
      <span ref={panel} role="tooltip" id={id} hidden={!open} className="absolute left-0 top-full z-10 w-max pt-1">
        <span className="block max-w-60 rounded-lg border border-line bg-white p-2 text-label font-normal text-tx2 shadow-[0_8px_24px_rgba(16,24,40,.12)]">
          {text}
        </span>
      </span>
    </span>
  );
}

// --- Menu: a small disclosure menu behind an icon button (the case header's ⋯; UI-SPEC "Shared UI primitives") ------

export type MenuItem = { label: string; icon?: IconName; onSelect: () => void };

// The trigger is an icon-only button with its aria-label, aria-haspopup="menu" and aria-expanded. Enter or Space (the
// button's own click) opens the panel and focuses its first item; ArrowDown and ArrowUp move between the items
// (wrapping), Home and End jump; Esc closes the panel and returns focus to the trigger. Choosing an item closes the
// panel, returns focus to the trigger, then runs the item. A press outside the menu, or focus leaving it, closes it.
// The panel: white, 1px line border, radius 8px, the menu shadow (UI-SPEC "Radii", "Shadows").
export function Menu({ label, icon = "more", items }: { label: string; icon?: IconName; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const entries = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!open) return;
    entries.current[0]?.focus();
    const outside = (e: PointerEvent) => {
      if (root.current !== null && !root.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);

  const close = () => {
    setOpen(false);
    trigger.current?.focus();
  };
  const move = (e: KeyboardEvent<HTMLDivElement>) => {
    const list = entries.current.filter((x): x is HTMLButtonElement => x !== null);
    const at = list.indexOf(document.activeElement as HTMLButtonElement);
    const to = e.key === "ArrowDown" ? (at + 1) % list.length
      : e.key === "ArrowUp" ? (at - 1 + list.length) % list.length
      : e.key === "Home" ? 0
      : e.key === "End" ? list.length - 1
      : null;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (to !== null && list.length > 0) {
      e.preventDefault();
      list[to].focus();
    }
  };

  return (
    <div ref={root} className="relative"
      onBlur={(e) => { if (open && !e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false); }}>
      <button ref={trigger} type="button" aria-label={label} aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen((x) => !x)}
        onKeyDown={(e) => { if (e.key === "ArrowDown" && !open) { e.preventDefault(); setOpen(true); } }}
        className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-line-2 bg-white text-tx2 hover:bg-side hover:text-tx">
        <Icon name={icon} />
      </button>
      {open && (
        <div role="menu" aria-label={label} onKeyDown={move}
          className="absolute right-0 top-full z-10 mt-1 flex min-w-48 flex-col rounded-lg border border-line bg-white py-1 shadow-[0_8px_24px_rgba(16,24,40,.12)]">
          {items.map((x, i) => (
            <button key={x.label} ref={(el) => { entries.current[i] = el; }} type="button" role="menuitem" tabIndex={-1}
              onClick={() => { close(); x.onSelect(); }}
              className="flex h-8 items-center gap-2 px-4 text-left text-body text-tx hover:bg-side focus-visible:bg-side">
              {x.icon && <Icon name={x.icon} className="text-tx3" />}
              {x.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// =====================================================================================================================
// 06.1 primitives (06.1-UI-SPEC "New 06.1 primitives"), shared by two or more pages. They use only the 06.1 tokens
// (index.css, the `@theme static` block), so nothing a Phase 5 screen draws changes until 06.1-16 switches the shell.
// Single-page components stay in their page's own module.
// =====================================================================================================================

// --- StepBar: nine 4px segments and the text "Step {n} of 9 · {Name}" ------------------------------------------------

export const STEP_COUNT = 9;

const SEGMENT: Record<SegmentState, string> = {
  done: "bg-navy-700",
  current: "bg-gold-600", // 2.60:1 on white, so the step text beside the bar always carries the meaning
  later: "bg-rule",
};

// `current` is the server's step_number (1..9), 0 for a flag before evaluation (no step text then); `label` is the
// step's name. The bar is aria-hidden: the visible text says the same thing.
export function StepBar({ current, optionsDone = false, label }: { current: number; optionsDone?: boolean; label: string }) {
  const states = segmentStates(current, STEP_COUNT, optionsDone);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
      <div aria-hidden="true" className="flex min-w-24 flex-1 gap-1">
        {states.map((s, i) => <span key={i} className={`h-1 flex-1 rounded-full ${SEGMENT[s]}`} />)}
      </div>
      {current > 0 && <span className="text-label whitespace-nowrap text-ink-2">Step {current} of {STEP_COUNT} · {label}</span>}
    </div>
  );
}

// --- KindChip: what kind of value sits beside it (06.1-UI-SPEC "Data kinds", D-15; R-8) -------------------------------

export type ValueKind = keyof typeof KIND_TEXT;

// Simulated, Estimated and Forecast are drawn in the dashed plan tone (never observed); Assumption is gray and names its
// source in a tooltip. Public values carry no chip.
const KIND_LOOK: Record<ValueKind, { tone: Tone; icon: IconName }> = {
  simulated: { tone: "plan", icon: "info" },
  estimated: { tone: "plan", icon: "info" },
  forecast: { tone: "plan", icon: "trend" },
  assumption: { tone: "gray", icon: "tag" },
};

export function KindChip({ kind, source }: { kind: ValueKind; source?: string }) {
  const look = KIND_LOOK[kind];
  return (
    <span className="inline-flex items-center">
      <Pill tone={look.tone} icon={look.icon}>{KIND_TEXT[kind]}</Pill>
      {kind === "assumption" && source && <Tooltip label={ASSUMPTION_SOURCE_LABEL} text={source} />}
    </span>
  );
}

// --- Banner: a full-width notice with tone, icon, title, meta line and actions -----------------------------------------

export type BannerTone = "bad" | "warn" | "info" | "good" | "gray";

const BANNER: Record<BannerTone, { box: string; icon: string }> = {
  bad: { box: "bg-bad-soft", icon: "text-bad" },
  warn: { box: "bg-warn-soft", icon: "text-warn" },
  info: { box: "bg-info-soft", icon: "text-info" },
  good: { box: "bg-good-soft", icon: "text-good" },
  gray: { box: "bg-sunken", icon: "text-ink-2" },
};

// A bad banner is an alert (it blocks something); every other tone is a polite status. Title and meta are text nodes
// only (T-06.1-03); `children` holds the actions.
export function Banner({ tone, icon, title, meta, children }:
  { tone: BannerTone; icon: IconName; title: ReactNode; meta?: ReactNode; children?: ReactNode }) {
  const look = BANNER[tone];
  return (
    <div role={tone === "bad" ? "alert" : "status"}
      className={`flex w-full min-w-0 flex-wrap items-start gap-x-2 gap-y-2 rounded-lg p-4 ${look.box}`}>
      <span className={`inline-flex h-5 items-center ${look.icon}`}><Icon name={icon} /></span>
      <div className="min-w-0 flex-1">
        <p className="m-0 text-body font-semibold break-words text-ink">{title}</p>
        {meta && <p className="m-0 text-label break-words text-ink-2">{meta}</p>}
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

// --- Drawer: a right-side, non-modal panel, 384px (full width below 768px) ---------------------------------------------

// role="complementary", named by its title. It starts below the shell's fixed top block (--shell-top: the phone bar
// and the mark strip, or the rail's strip; 0 with the expanded sidebar), so neither covers the other and the mark stays
// on screen while it is open. On open, focus moves to Close and the element that had focus is kept;
// Escape inside the drawer (or Close) calls onClose, and when the drawer goes away focus returns to that element if it
// is still on the page (06.1-UI-SPEC "Accessibility contract": Drawers). A key a menu inside already handled (default
// prevented) does not close the drawer.
export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  const titleId = useId();
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    close.current?.focus();
    return () => {
      if (opener !== null && opener.isConnected) opener.focus();
    };
  }, []);
  return (
    <aside role="complementary" aria-labelledby={titleId}
      onKeyDown={(e) => { if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); onClose(); } }}
      className="fixed top-[var(--shell-top,0px)] right-0 bottom-0 z-20 flex w-full flex-col border-l border-rule bg-surface shadow-float md:w-96">
      <div className="flex items-center gap-2 border-b border-rule p-4">
        <h2 id={titleId} className="m-0 min-w-0 flex-1 text-title font-semibold break-words text-ink">{title}</h2>
        <button ref={close} type="button" aria-label={CLOSE} onClick={onClose}
          className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-2 hover:bg-sunken hover:text-ink max-md:h-11 max-md:w-11">
          <Icon name="x" />
        </button>
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-2 overflow-y-auto p-4">{children}</div>
    </aside>
  );
}

// --- QtyBar: committed track, dispatched and received fills, damaged and missing parts -------------------------------

const HATCH = "bg-[repeating-linear-gradient(45deg,var(--color-bad)_0_2px,var(--color-bad-soft)_2px_5px)]";

// The bar is aria-hidden; the numbers sit in the visible text beside it (R-6): "Committed 110 · Dispatched 104 ·
// Received 104", plus "Damaged" and "Missing" above zero. Before a receipt the dispatched part is navy-800; after it,
// received is good, damaged warn and missing bad hatched. The rest of the track is what was never dispatched.
export function QtyBar({ committed, dispatched, received, damaged, className = "w-30" }: QtyInput & { className?: string }) {
  const { units, widths } = qtyParts({ committed, dispatched, received, damaged });
  const pct = (w: number) => ({ width: `${w * 100}%` });
  const text = [`${QTY_TEXT.committed} ${units.committed}`];
  if (units.dispatched !== null) text.push(`${QTY_TEXT.dispatched} ${units.dispatched}`);
  if (units.received !== null) text.push(`${QTY_TEXT.received} ${units.received}`);
  if (units.damaged) text.push(`${QTY_TEXT.damaged} ${units.damaged}`);
  if (units.missing) text.push(`${QTY_TEXT.missing} ${units.missing}`);
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div aria-hidden="true" className={`flex h-2 overflow-hidden rounded-full bg-sunken ${className}`}>
        {units.received === null
          ? <span className="h-full bg-navy-800" style={pct(widths.dispatched)} />
          : <>
            <span className="h-full bg-good" style={pct(widths.received)} />
            <span className="h-full bg-warn" style={pct(widths.damaged)} />
            <span className={`h-full ${HATCH}`} style={pct(widths.missing)} />
          </>}
      </div>
      <span className="num text-label text-ink-2">{text.join(" · ")}</span>
    </div>
  );
}

// --- HistoryList: two lines per entry and the All · Decisions · Data filter --------------------------------------------

// Entries come already formatted by the caller (vocab.historyLine, 06.1-03) and sorted by recorded time (D-58): line 1
// the event and who, line 2 both clocks, an optional detail line. Text nodes only (T-06.1-03).
export type HistoryEntry = { title: ReactNode; meta: ReactNode; detail?: ReactNode; group: string };

export function HistoryList({ entries, filter, onFilter, emptyText }: {
  entries: HistoryEntry[]; filter: HistoryFilter; onFilter: (f: HistoryFilter) => void; emptyText: string;
}) {
  const shown = historyFilter(entries, filter);
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div role="group" aria-label={HISTORY_FILTER_LABEL} className="inline-flex w-fit gap-1 rounded-lg bg-sunken p-1">
        {HISTORY_FILTERS.map((f) => (
          <button key={f} type="button" aria-pressed={filter === f} onClick={() => onFilter(f)}
            className={`h-7 rounded-md px-2 text-label font-semibold max-md:h-11 ${filter === f ? "bg-surface text-ink shadow-card" : "text-ink-2 hover:text-ink"}`}>
            {f}
          </button>
        ))}
      </div>
      {shown.length === 0
        ? <p className="m-0 text-body text-ink-2">{emptyText}</p>
        : (
          <ol className="m-0 flex list-none flex-col gap-2 p-0">
            {shown.map((e, i) => (
              <li key={i} className="min-w-0">
                <p className="m-0 text-body font-semibold break-words text-ink">{e.title}</p>
                <p className="num m-0 text-label break-words text-ink-2">{e.meta}</p>
                {e.detail && <p className="m-0 text-label break-words text-ink-2">{e.detail}</p>}
              </li>
            ))}
          </ol>
        )}
    </div>
  );
}

// --- toolbar controls and link buttons more than one page uses (06.1-30 hoist from Schedule, Branch transfers, the
// Decision board, Today, Stock & lots and Tasks) ---------------------------------------------------------------------

// A segmented control: one pressed button per option (aria-pressed), on the sunken track; an option may carry an icon.
export function Segmented<T extends string>({ label, value, options, onPick }: {
  label: string; value: T; options: readonly (readonly [T, string, IconName?])[]; onPick: (v: T) => void;
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex gap-1 rounded-lg bg-sunken p-1">
      {options.map(([v, text, icon]) => (
        <button key={v} type="button" aria-pressed={value === v} onClick={() => onPick(v)}
          className={`inline-flex h-7 items-center gap-1 rounded-md px-3 text-label font-semibold whitespace-nowrap max-md:h-11 ${value === v ? "bg-white text-tx shadow-card" : "text-tx2 hover:text-tx"}`}>
          {icon !== undefined && <Icon name={icon} size={12} />}{text}
        </button>
      ))}
    </div>
  );
}

// A labelled select: the visible label beside the control names it (a label beside it, not around it, so the selected
// option's text never joins the name).
export function LabelledSelect({ label, value, options, onPick, disabled = false, className = "" }: {
  label: string; value: string; options: readonly (readonly [string, string])[]; onPick: (v: string) => void;
  disabled?: boolean; className?: string;
}) {
  const id = useId();
  return (
    <span className={`flex items-center gap-1.5 ${className}`}>
      <label htmlFor={id} className="text-label text-tx2">{label}</label>
      <span className="relative inline-flex">
        <select id={id} value={value} disabled={disabled} onChange={(e) => onPick(e.target.value)}
          className="h-8 max-w-40 appearance-none rounded-md border border-line-2 bg-white pr-8 pl-2 text-body text-tx disabled:bg-sunken disabled:text-tx2 max-md:h-11">
          {options.map(([v, text]) => <option key={v} value={v}>{text}</option>)}
        </select>
        <span className="pointer-events-none absolute top-2 right-2 text-tx3 max-md:top-3.5"><Icon name="chev" /></span>
      </span>
    </span>
  );
}

// An anchor that looks like a Button, for an action that is a route (a stock decision's step, a page): primary (navy)
// only where the signed-in account acts (R-4), plain otherwise; `compact` inside a card.
export function LinkButton({ primary = false, compact = false, className = "", ...rest }: ComponentPropsWithRef<"a"> & {
  primary?: boolean; compact?: boolean;
}) {
  const size = compact ? "min-h-7 px-2" : "min-h-8 px-4 py-1 whitespace-nowrap";
  const look = primary ? "border border-accent bg-accent text-white hover:bg-accent-dark"
    : "border border-line-2 bg-white text-tx hover:bg-side";
  return <a {...rest} className={`inline-flex items-center justify-center rounded-md text-body font-semibold max-md:min-h-11 ${size} ${look} ${className}`} />;
}

// --- Page states: loading, a page that didn't load, stale data (06.1-UI-SPEC "Interaction states and feedback") -------

// The page's own loading line ("Loading the Decision board…"), no spinner; refetches keep the old content instead.
export function Loading({ text }: { text: string }) {
  return <p role="status" className="m-0 text-body text-ink-2">{text}</p>;
}

// In place of the page: the title ("The Decision board didn't load"), the API message word for word, "Nothing was
// changed.", Try again and Back to Today.
export function LoadError({ title, error, onRetry }: { title: string; error: ApiError | string; onRetry: () => void }) {
  const message = typeof error === "string" ? error : error.message;
  return (
    <section role="alert" className="flex min-w-0 flex-col items-start gap-2 rounded-2xl border border-rule bg-surface p-4 shadow-card">
      <span className="inline-flex h-11 w-11 items-center justify-center rounded-full bg-bad-soft text-bad">
        <Icon name="alert" size={20} />
      </span>
      <h2 className="m-0 text-title font-semibold text-ink">{title}</h2>
      <p className="m-0 text-body break-words text-ink">{message}</p>
      <p className="m-0 text-body text-ink-2">{NOTHING_CHANGED}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="plain" onClick={onRetry}>{TRY_AGAIN}</Button>
        <a href="#/" className="text-body font-semibold text-navy-800 underline underline-offset-2">{BACK_TO_TODAY}</a>
      </div>
    </section>
  );
}

// Stale means the last refetch failed or the workspace version moved since the page loaded; never the dataset's age
// (D-12). The content stays under the banner.
export function StaleBanner({ kind, onAct }: { kind: "refresh_failed" | "version_changed"; onAct: () => void }) {
  const failed = kind === "refresh_failed";
  return (
    <Banner tone="warn" icon="alert" title={failed ? STALE_REFRESH_FAILED : STALE_VERSION_CHANGED}>
      <Button kind="plain" onClick={onAct}>{failed ? TRY_AGAIN : REFRESH}</Button>
    </Banner>
  );
}

// --- Charts: LineChart and BarChart through a Chart.js ref, inside a ChartCard with Show as table ---------------------

// Loaded once, on the first chart; a failed load is forgotten so the next chart tries again (the card's summary and
// table still say what the chart shows).
let chartModule: Promise<typeof Chart> | null = null;
function loadChart(): Promise<typeof Chart> {
  chartModule ??= import("chart.js").then((m) => {
    m.Chart.register(
      m.BarController, m.BarElement, m.CategoryScale, m.Filler, m.Legend, m.LinearScale, m.LineController, m.LineElement,
      m.PointElement, m.Tooltip,
    );
    return m.Chart;
  }).catch((e: unknown) => {
    chartModule = null;
    throw e;
  });
  return chartModule;
}

// What a series shows, by the 06.1-UI-SPEC chart rows: public sales navy-800; the forecast navy-700 dashed with a
// navy-100 band; hours out of stock and simulated stock plan-line; estimated waste warn (#b54708; the lighter orange
// fails 3:1 on white). Colours are read from the CSS variables in index.css at draw time, never typed here.
export type ChartTone = "public" | "forecast" | "plan" | "waste";
const TONE_VAR: Record<ChartTone, string> = {
  public: "--color-navy-800",
  forecast: "--color-navy-700",
  plan: "--color-plan-line",
  waste: "--color-warn",
};
const BAND_VAR = "--color-navy-100";

// A chart series: the table's series plus how it is drawn. A band (low/high) fills between its two lines; `axis`
// "right" puts a series on the second axis (hours out of stock beside daily units).
export type PlotSeries = ChartSeries & { tone: ChartTone; type?: "line" | "bar"; axis?: "left" | "right" };

const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const BAND_HIGH = " high"; // the band's upper line is not a legend entry of its own

function datasets(series: PlotSeries[], chartType: "line" | "bar"): ChartDataset[] {
  return series.flatMap((s): ChartDataset[] => {
    const yAxisID = s.axis === "right" ? "y2" : "y";
    const colour = cssVar(TONE_VAR[s.tone]);
    if ("low" in s) {
      const band = cssVar(BAND_VAR);
      const edge = { type: "line" as const, yAxisID, borderWidth: 0, pointRadius: 0, backgroundColor: band, borderColor: band, order: 3 };
      return [
        { ...edge, label: s.name, data: s.low, fill: false },
        { ...edge, label: s.name + BAND_HIGH, data: s.high, fill: "-1" },
      ];
    }
    const type = s.type ?? chartType;
    return [type === "line"
      ? { type, label: s.name, data: s.values, yAxisID, borderColor: colour, backgroundColor: colour, borderWidth: 2,
          borderDash: s.tone === "forecast" ? [6, 4] : [], pointRadius: 0, tension: 0, order: 1 }
      : { type, label: s.name, data: s.values, yAxisID, backgroundColor: colour, borderWidth: 0, order: 2 }];
  });
}

// Creates the chart on the canvas when the data changes (once Chart.js has loaded) and destroys it on change and on
// unmount. The canvas is
// aria-hidden: the ChartCard's summary line and its table say what it shows.
function ChartCanvas({ type, labels, series, horizontal = false, rightAxisTitle }: {
  type: "line" | "bar"; labels: string[]; series: PlotSeries[]; horizontal?: boolean; rightAxisTitle?: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const data = JSON.stringify({ type, labels, series, horizontal, rightAxisTitle }); // a stable dependency for new arrays
  useEffect(() => {
    if (canvas.current === null) return;
    const { type: t, labels: ls, series: ss, horizontal: h, rightAxisTitle: right } =
      JSON.parse(data) as { type: "line" | "bar"; labels: string[]; series: PlotSeries[]; horizontal: boolean; rightAxisTitle?: string };
    const ink2 = cssVar("--color-ink-2");
    const rule = cssVar("--color-rule");
    const axis = { ticks: { color: ink2 }, grid: { color: rule }, border: { color: rule } };
    const config = {
      type: t,
      data: { labels: ls, datasets: datasets(ss, t) },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: reducedMotion() ? false : undefined,
        indexAxis: h ? "y" : "x",
        plugins: {
          legend: {
            display: ss.length > 1,
            labels: { color: ink2, filter: (item: { text: string }) => !item.text.endsWith(BAND_HIGH) },
          },
        },
        scales: {
          x: axis,
          y: { ...axis, beginAtZero: true },
          ...(right ? {
            y2: { ...axis, beginAtZero: true, position: "right", grid: { drawOnChartArea: false },
                  title: { display: true, text: right, color: ink2 } },
          } : {}),
        },
      },
    } as ChartConfiguration;
    let chart: Chart | null = null;
    let gone = false;
    loadChart().then((ChartJs) => {
      if (gone || canvas.current === null) return;
      ChartJs.defaults.font.family = cssVar("--font-sans");
      chart = new ChartJs(canvas.current, config);
    }).catch(() => {});
    return () => {
      gone = true;
      chart?.destroy();
    };
  }, [data]);
  return (
    <div className="relative h-64 min-w-0">
      <canvas ref={canvas} aria-hidden="true" />
    </div>
  );
}

// Daily lines (public sales, the dashed forecast with its band) with optional bars on a second axis.
export function LineChart({ labels, series, rightAxisTitle }: { labels: string[]; series: PlotSeries[]; rightAxisTitle?: string }) {
  return <ChartCanvas type="line" labels={labels} series={series} rightAxisTitle={rightAxisTitle} />;
}

// Bars; `horizontal` for ranked lists (top products), labels on the left.
export function BarChart({ labels, series, horizontal = false }: { labels: string[]; series: PlotSeries[]; horizontal?: boolean }) {
  return <ChartCanvas type="bar" labels={labels} series={series} horizontal={horizontal} />;
}

// Title, the kind chip, the period, the chart (or its table) and a one-line summary the card is described by. The
// toggle "Show as table" / "Show as chart" (aria-pressed) swaps the chart for `table` (bars.tableRows), which scrolls
// inside its own box; a value the series lacks shows as missing, never zero.
// `actions` (optional) sits beside the title: a control that drives this card only (Analytics' Product code).
export function ChartCard({ title, kind, period, summary, table, children, actions }: {
  title: ReactNode; kind?: ValueKind; period?: ReactNode; summary: ReactNode; table: ChartTable; children: ReactNode;
  actions?: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  const titleId = useId();
  const summaryId = useId();
  const rows = table.rows.map((r) => r.map((c) => (c === null ? MISSING_CELL : c)));
  return (
    <section aria-labelledby={titleId} aria-describedby={summaryId}
      className="flex min-w-0 flex-col gap-2 rounded-2xl border border-rule bg-surface p-4 shadow-card">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={titleId} className="m-0 text-title font-semibold text-ink">{title}</h2>
        {kind && <KindChip kind={kind} />}
        {actions}
        <button type="button" aria-pressed={asTable} onClick={() => setAsTable((x) => !x)}
          className="ml-auto inline-flex h-7 items-center rounded-lg border border-rule-2 bg-surface px-2 text-body font-semibold text-ink hover:bg-navy-50 max-md:h-11">
          {asTable ? SHOW_CHART : SHOW_TABLE}
        </button>
      </div>
      {period && <p className="num m-0 text-label text-ink-3">{period}</p>}
      {asTable
        ? <div className="min-w-0"><Table head={table.head} rows={rows} numeric={table.head.map((_, i) => i > 0)} /></div>
        : children}
      <p id={summaryId} className="m-0 text-label text-ink-2">{summary}</p>
    </section>
  );
}

// --- 05.2-21's shared pieces, moved here unchanged from board.tsx (Pager) and case.tsx (NeedsEvaluation, DesignedLabel,
// EligibilityLine) so the 06.1 page plans import them without editing those two files (06.1-15) -------------------

// The public board's pager (05.2 D-26; UI-SPEC A-36): Previous, the server's "Page N of M · T items", Next. Hidden
// when everything fits on one page. Each press asks the server for that page; nothing is sorted or cut here. A page
// past the last (the list shrank since it was read) asks the server once for the last page with records, or page 1
// when none is left, so a shrinking list never strands the reader on an empty page (D-17).
export function Pager({ page, pageSize, total, onPage }: {
  page: number; pageSize: number; total: number; onPage: (page: number) => void;
}) {
  const ask = useRef(onPage);
  ask.current = onPage;
  const target = pageInRange(page, pageSize, total);
  useEffect(() => {
    if (target !== null) ask.current(target);
  }, [target, page, pageSize, total]);
  const text = pagerText(page, pageSize, total);
  if (text === null) return null;
  const last = Math.ceil(total / pageSize);
  return (
    <nav aria-label="Pages" className="flex flex-wrap items-center gap-2 px-6 pb-6">
      <Button kind="plain" aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</Button>
      <span data-testid="pager" className="num text-body text-tx2">{text}</span>
      <Button kind="plain" aria-label="Next page" disabled={page >= last} onClick={() => onPage(page + 1)}>Next</Button>
    </nav>
  );
}

// A flagged item the engine has not evaluated (05.2 D-24, D-25; UI-SPEC A-33, A-34): the header names it with the
// Needs evaluation pill, the body shows the screen's figures only (no plan, amount or deadline), and on mount the page
// posts /evaluate once, one Idempotency-Key for this attempt, while an indeterminate progress bar and "Evaluating this
// item…" show. The answer is the evaluated case's detail, handed to the page. A busy, timed-out or unreachable server
// gets Try again with the same key; any other refusal shows the API message with the way back to the board. A refetch of
// the page (visibility, a session change) never posts again: one mount, one evaluation.
export function NeedsEvaluation({ d, session, onEvaluated }: {
  d: CaseDetail; session: Session; onEvaluated: (out: CaseDetail) => void;
}) {
  const act = useAction((_: Record<string, never>, key: string) =>
    post<CaseDetail>(`/api/cases/${encodeURIComponent(d.key)}/evaluate`, {}, key));
  const done = useRef(onEvaluated);
  done.current = onEvaluated;
  const started = useRef(false);
  const { run, retry } = act;
  useEffect(() => {
    if (started.current) return; // React's development double-invoke and every refetch: one POST per mount
    started.current = true;
    void run({}).then((out) => { if (out !== undefined) done.current(out); });
  }, [run]);
  const again = () => void retry().then((out) => { if (out !== undefined) done.current(out); });
  const lot = d.lot;
  const details: string[] = [`${PRODUCT_CODE} ${d.sku}`];
  if (lot !== undefined && lot !== null) {
    details.push(`Lot ${lot.lot_id} · ${units(lot.qty)} units on record`);
    if (lot.expiry_day !== null) details.push(`Expires ${dateLabel(session.calendar, lot.expiry_day)}`);
  }
  if (d.zone === "chilled") details.push("Chilled");
  const err = act.error;
  return (
    <div data-testid="needs-evaluation">
      <header className="border-b border-line bg-white px-6 py-4">
        <nav aria-label="Breadcrumb" className="text-label text-tx3">
          <a href="#/board" className="hover:underline">{TERMS.DECISION_BOARD}</a> / <span className="font-mono">{d.key}</span>
        </nav>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <h1 className="min-w-0 text-display font-semibold tracking-[-0.02em] break-words">{d.product ?? d.sku} · {d.source}</h1>
          <div className="flex flex-wrap items-center gap-1">
            <Pill tone="warn" icon="alert">{NEEDS_EVALUATION}</Pill>
            {lot !== undefined && lot !== null && <DesignedLabel lot={lot} session={session} />}
            {(d.badges ?? []).map((b) => {
              const v = badgeChip(b, session.currency);
              return <Pill key={b.kind} tone={v.tone} icon={v.icon ?? undefined}>{v.text}</Pill>;
            })}
          </div>
        </div>
        {details.length > 0 && (
          <ul className="num mt-2 flex flex-wrap gap-x-4 gap-y-1 text-body text-tx2">
            {details.map((x) => <li key={x} className="break-words">{x}</li>)}
          </ul>
        )}
      </header>
      <div className="flex flex-col gap-4 p-6">
        <Section>
          <Kv items={flagBody(d, session.calendar).flatMap((r) => (r.kind === "kv" ? [[r.label, r.value] as [string, string]] : []))} />
          {err === null ? (
            <div data-testid="evaluating" className="mt-4 flex flex-col gap-2" aria-live="polite">
              <p className="text-body text-tx2">{EVALUATING}</p>
              <div role="progressbar" aria-label={EVALUATING} aria-busy="true"
                className="h-1 w-full overflow-hidden rounded-full bg-gray-soft">
                <div className="h-full w-1/3 animate-pulse rounded-full bg-accent motion-reduce:animate-none" />
              </div>
            </div>
          ) : (
            <div className="mt-4 flex flex-col items-start gap-2">
              <ErrorLine error={err} action={retryable(err.code) ? { label: TRY_AGAIN, onClick: again } : undefined} />
              <a href="#/board" className="text-body text-accent underline">{BACK_TO_BOARD}</a>
            </div>
          )}
        </Section>
      </div>
    </div>
  );
}

// The designed-example label next to the lot id (05.2 D-18, D-40; UI-SPEC A-39): a grey pill with the tooltip that
// says what the lot is. Only for a lot whose origin is "designed" on the public dataset; the synthetic test set's
// story lots keep their look (their designed situations are not record errors).
export function DesignedLabel({ lot, session }: { lot: Pick<CaseDetail["lot"], "origin">; session: Session }) {
  if (session.kind !== "public" || lot.origin !== "designed") return null;
  return (
    <span data-testid="designed-example" className="ml-2 inline-flex items-center gap-1 align-middle">
      <Pill tone="gray">{DESIGNED_EXAMPLE}</Pill>
      <Tooltip label={DESIGNED_TIP_LABEL} text={DESIGNED_TIP} />
    </span>
  );
}

// Stage 1's outcome under the Options table (05.2 D-27, D-29..D-31; UI-SPEC A-35): every destination excluded before
// the search with the server's reason, in the server's order, under "Excluded before the search". Shown for the
// "available" and "none_eligible" statuses whenever something was excluded; the unmapped status has its row in the
// table instead (it has no destinations to list). Nothing on the synthetic set (`applies` false) or before the API
// sends the key. "Best among the plans evaluated." is the footnote's, once.
export function EligibilityLine({ e }: { e: Eligibility | undefined }) {
  if (e === undefined || !e.applies || e.status === "unavailable_unmapped") return null;
  const lines = eligibilityLine(e);
  if (lines.length === 0) return null;
  return (
    <div data-testid="eligibility" className="mt-4">
      <p className="text-label text-tx3">{EXCLUDED_HEADING}</p>
      <ul className="num list-disc pl-6 text-body text-tx2">
        {lines.map((x, i) => <li key={i} className="break-words">{x}</li>)}
      </ul>
    </div>
  );
}
