// Display formats (05-UI-SPEC "Money", "Deadline labels"): every amount, number, time and date label on screen goes
// through here. Money is Intl.NumberFormat with the session's currency, so another ISO currency needs no code change;
// no currency sign is typed in this file. Day labels read the session calendar; nothing here knows what day 0 is.
// Pure, erasable syntax only, so `node --test` runs it.
import type { Session } from "./api.ts";

export const LOCALE = "en-US"; // the display locale for numbers and dates; not retailer data
export type Cal = Session["calendar"];

const MINUS = "−"; // a true minus sign, never a hyphen

// Formats `v` as money in `currency`. Negative values get a true minus, and a value that rounds to zero gets no sign;
// `plus` adds "+" to values above zero.
function currencyText(v: number, currency: string, options: Intl.NumberFormatOptions, plus = false): string {
  const nf = new Intl.NumberFormat(LOCALE, {
    style: "currency", currency, ...options, signDisplay: plus ? "exceptZero" : "negative",
  });
  return nf.formatToParts(v).map((p) => (p.type === "minusSign" ? MINUS : p.value)).join("");
}

const WHOLE: Intl.NumberFormatOptions = { minimumFractionDigits: 0, maximumFractionDigits: 0 };

// Cents everywhere, whole units on card values and KPI tiles (`whole`); "—" when there is no value.
export function money(v: number | null | undefined, currency: string, whole = false): string {
  return v == null ? "—" : currencyText(v, currency, whole ? WHOLE : {});
}

// A signed amount: "+" above zero, a true minus below it, no sign at zero.
export function signed(v: number | null | undefined, currency: string, whole = false): string {
  return v == null ? "—" : currencyText(v, currency, whole ? WHOLE : {}, true);
}

// A rising waste flip, rounded up to the cent, and a falling one, rounded down.
export function centsUp(v: number, currency: string): string {
  return currencyText(v, currency, { roundingMode: "ceil" });
}
export function centsDown(v: number, currency: string): string {
  return currencyText(v, currency, { roundingMode: "floor" });
}

// A markdown sales multiplier with at most two fraction digits ("1.43", "1.5"); the "×" sign is the caller's.
export function factor(v: number): string {
  return new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 2 }).format(v);
}

// A count of units, grouped; "—" when there is none. For recorded quantities, counts, plan counts and resources.
export function units(n: number | null | undefined): string {
  return n == null ? "—" : new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 1 }).format(n);
}

// A unit count the engine computes as an expectation (sold, wasted, at risk, unserved, forecast), in whole units:
// nearest, half away from zero (Intl's default), grouped; "—" when there is none. One rule everywhere, so the same
// expectation reads the same number on the card, the KPI tile and the Options table (G-05-2 item 10).
export function expected(n: number | null | undefined): string {
  return n == null ? "—" : new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 }).format(n);
}

// Minutes after midnight as 24-hour "HH:MM".
export function hhmm(min: number): string {
  return `${String(Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;
}

// The calendar's weekday for `day` ("Thu"); "" for a day the calendar does not hold.
export function weekday(cal: Cal, day: number): string {
  return cal.find((c) => c.day === day)?.weekday ?? "";
}

const MONTH = new Intl.DateTimeFormat(LOCALE, { month: "short", timeZone: "UTC" });

// "Thu 15 Oct" from the calendar entry for `day` (the shape the API's own date labels use); "" for an unknown day.
export function dateLabel(cal: Cal, day: number): string {
  const c = cal.find((x) => x.day === day);
  if (c === undefined) return "";
  const date = new Date(`${c.date}T00:00:00Z`);
  return `${c.weekday} ${date.getUTCDate()} ${MONTH.format(date)}`;
}

const MARK_PARTS = new Intl.DateTimeFormat(LOCALE, {
  weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
});

// The dataset mark's date (05.2 D-04): "Sat 17 Oct 2026" from the ISO date the session's data_as_of carries, read on
// the UTC calendar so the browser's own zone and clock never move it. Date parts only, no time.
export function markDate(iso: string): string {
  const parts = Object.fromEntries(
    MARK_PARTS.formatToParts(new Date(`${iso}T00:00:00Z`)).map((p) => [p.type, p.value]),
  ) as Record<string, string>;
  return `${parts.weekday} ${parts.day} ${parts.month} ${parts.year}`;
}

// "Sat 17 Oct": the compact scenario date, markDate without its year (the page header already states the year; UI-SPEC
// "Times and dates"). Defined once here for Today, Stock & lots and Analytics (06.1-30).
export function shortDate(iso: string): string {
  return markDate(iso).replace(/ \d{4}$/, "");
}

// --- "When did you check?" (07-CONTEXT D-22; 07-UI-SPEC `CheckTime` and the time-zone backstop) ----------------------
// A typed check time is wall-clock time in the session's zone (`caps.time_zone`, Asia/Bangkok: fixed +07:00, no daylight
// saving), whatever the device's own zone, and it is sent as UTC. Both helpers read the zone through Intl, never Date's
// local getters, so the browser's zone and its daylight saving never move the result. `realTime` is not added here:
// copy.recordedTime already gives "16 Oct 2026, 20:30" (07-SCREENS-MAP section 1).

const WALL = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

// The zone's offset from UTC at the instant `utcMs`, in milliseconds (positive east of Greenwich).
function zoneOffsetMs(utcMs: number, timeZone: string): number {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(utcMs)).map((x) => [x.type, x.value])) as Record<string, string>;
  const wall = Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day), Number(p.hour), Number(p.minute), Number(p.second));
  return wall - Math.floor(utcMs / 1000) * 1000;
}

// A `datetime-local` value ("2026-10-16T20:40") read as wall-clock time in `timeZone`, as an ISO UTC string
// ("2026-10-16T13:40:00.000Z" for Asia/Bangkok); null for a value it cannot read or a zone Intl does not know.
export function zonedToUtc(local: string, timeZone: string): string | null {
  const m = WALL.exec(local.trim());
  if (m === null) return null;
  const wall = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6] ?? 0));
  if (Number.isNaN(wall)) return null;
  try {
    const first = wall - zoneOffsetMs(wall, timeZone);
    return new Date(wall - zoneOffsetMs(first, timeZone)).toISOString();
  } catch {
    return null;
  }
}

// The wall-clock time in `timeZone` at the instant `nowMs`, as a `datetime-local` value ("2026-10-16T20:40"): the
// input's `max`. "" for a zone Intl does not know (no max then; the server still refuses a future time).
export function zonedNow(timeZone: string, nowMs: number): string {
  try {
    return new Date(Math.floor(nowMs / 60000) * 60000 + zoneOffsetMs(nowMs, timeZone)).toISOString().slice(0, 16);
  } catch {
    return "";
  }
}
