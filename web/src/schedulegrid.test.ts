// node --test (no dependency): the Planned schedule's pure geometry (06.1-UI-SPEC "Planned schedule"; D-18, D-29):
// the time columns of each window, where a planned time sits on the grid (never a time the server did not send), the
// dependency connectors, and the date stepper's arithmetic on ISO dates (no clock is read).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DAY_FIRST_MIN, HOUR_W, MONTH_W, WEEK_W, barGeometry, columnsFor, connectorPath, dateOfDay, rangeLabel, shortDay,
  stepDate,
} from "./schedulegrid.ts";

const D = 3; // a window's first day index in the dataset calendar
const WEEK = { from: "2026-10-12", to: "2026-10-18" };
const MONTH = { from: "2026-10-01", to: "2026-10-31" };

test("columnsFor: Day is 13 hour columns 06:00..18:00 of 64px", () => {
  const cols = columnsFor("day");
  assert.equal(cols.length, 13);
  assert.equal(cols[0].label, "06:00");
  assert.equal(cols[12].label, "18:00");
  assert.ok(cols.every((c) => c.width === HOUR_W && c.date === null));
  assert.equal(HOUR_W, 64);
  assert.equal(DAY_FIRST_MIN, 360);
});

test("columnsFor: Week is 7 day columns of 128px, Monday first, each with its date", () => {
  const cols = columnsFor("week", WEEK);
  assert.equal(cols.length, 7);
  assert.ok(cols.every((c) => c.width === WEEK_W));
  assert.deepEqual(cols.map((c) => c.date), [
    "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]);
  assert.equal(cols[0].label, "Mon · 12");
  assert.equal(cols[6].label, "Sun · 18");
});

test("columnsFor: Month is one 40px column per day of the month", () => {
  const cols = columnsFor("month", MONTH);
  assert.equal(cols.length, 31);
  assert.ok(cols.every((c) => c.width === MONTH_W));
  assert.equal(cols[0].label, "1");
  assert.equal(cols[0].sub, "T"); // Thu 1 Oct 2026
  assert.equal(columnsFor("month", { from: "2026-02-01", to: "2026-02-28" }).length, 28);
});

test("barGeometry: a 10:00-12:00 bar on the Day grid starts 4 hours in and is 2 hours wide", () => {
  const g = barGeometry({ start: { day: D, min: 600 }, end: { day: D, min: 720 } }, "day", D);
  assert.ok(g !== null);
  assert.equal(g.left, 4 * 64);
  assert.equal(g.width, 2 * 64);
  assert.equal(g.shape, "bar");
  assert.equal(g.clipStart, false);
  assert.equal(g.clipEnd, false);
});

test("barGeometry: no planned start is no bar (Unscheduled), never an invented time", () => {
  assert.equal(barGeometry({ start: null, end: null }, "day", D), null);
  assert.equal(barGeometry({ start: null, end: { day: D, min: 720 } }, "week", D), null);
});

test("barGeometry: a zero-length time is a diamond at its minute", () => {
  const g = barGeometry({ start: { day: D, min: 720 }, end: { day: D, min: 720 } }, "day", D);
  assert.deepEqual(g, { left: 6 * 64, width: 0, shape: "diamond", clipStart: false, clipEnd: false });
});

test("barGeometry: a markdown (a day, no clock time) has no bar in Day and sits on its day in Week and Month", () => {
  const md = { start: { day: D + 2, min: null }, end: { day: D + 2, min: null } };
  assert.equal(barGeometry(md, "day", D + 2), null);
  const w = barGeometry(md, "week", D, 7);
  assert.ok(w !== null);
  assert.equal(w.shape, "diamond");
  assert.equal(w.left, 2 * WEEK_W + 12);
  const m = barGeometry(md, "month", D, 31);
  assert.ok(m !== null);
  assert.equal(m.left, 2 * MONTH_W + MONTH_W / 2);
});

test("barGeometry: a bar past the Day grid's edge is cut there and says so; one wholly outside is not drawn", () => {
  const late = barGeometry({ start: { day: D, min: 1080 }, end: { day: D + 1, min: 60 } }, "day", D);
  assert.ok(late !== null);
  assert.equal(late.left, 12 * 64);
  assert.equal(late.width, 64); // to 19:00, the grid's end
  assert.equal(late.clipEnd, true);
  assert.equal(barGeometry({ start: { day: D, min: 120 }, end: { day: D, min: 300 } }, "day", D), null);
  assert.equal(barGeometry({ start: { day: D, min: 300 }, end: { day: D, min: 300 } }, "day", D), null);
  assert.equal(barGeometry({ start: { day: D + 1, min: 600 }, end: { day: D + 1, min: 720 } }, "day", D), null);
});

test("barGeometry: in Week a timed bar fills its day cell, a two-day bar two cells", () => {
  const one = barGeometry({ start: { day: D + 1, min: 750 }, end: { day: D + 1, min: 800 } }, "week", D, 7);
  assert.deepEqual(one, { left: WEEK_W + 3, width: WEEK_W - 6, shape: "bar", clipStart: false, clipEnd: false });
  const two = barGeometry({ start: { day: D + 5, min: 1300 }, end: { day: D + 6, min: 60 } }, "week", D, 7);
  assert.ok(two !== null);
  assert.equal(two.width, 2 * WEEK_W - 6);
  assert.equal(barGeometry({ start: { day: D + 7, min: 600 }, end: { day: D + 7, min: 700 } }, "week", D, 7), null);
});

test("connectorPath: an elbow from the end of one bar to the start of the next, with an arrowhead, Day only", () => {
  const p = connectorPath({ x: 100, y: 22 }, { x: 200, y: 66 }, "day");
  assert.ok(p !== null);
  assert.equal(p.line, "M100,22 H192 V66 H200");
  assert.equal(p.head, "M194,63 L200,66 L194,69 Z");
  assert.equal(connectorPath({ x: 100, y: 22 }, { x: 200, y: 66 }, "week"), null);
  assert.equal(connectorPath({ x: 100, y: 22 }, { x: 200, y: 66 }, "month"), null);
});

test("connectorPath: when the next bar starts at or before the end, the line steps out and back round", () => {
  const p = connectorPath({ x: 200, y: 22 }, { x: 205, y: 66 }, "day");
  assert.ok(p !== null);
  assert.equal(p.line, "M200,22 h8 V44 H195 V66 H205");
});

test("stepDate: a day, a week, or a month at a time, on the ISO calendar", () => {
  assert.equal(stepDate("2026-10-17", "day", 1), "2026-10-18");
  assert.equal(stepDate("2026-10-01", "day", -1), "2026-09-30");
  assert.equal(stepDate("2026-10-17", "week", -1), "2026-10-10");
  assert.equal(stepDate("2026-10-31", "month", 1), "2026-11-01");
  assert.equal(stepDate("2026-01-15", "month", -1), "2025-12-01");
});

test("dateOfDay and shortDay: a day index in the window to its date and label", () => {
  const win = { from: "2026-10-12", to: "2026-10-18", from_day: 10, to_day: 16 };
  assert.equal(dateOfDay(win, 15), "2026-10-17");
  assert.equal(dateOfDay(win, 17), "2026-10-19");
  assert.equal(shortDay("2026-10-17"), "Sat 17 Oct");
});

test("rangeLabel: the stepper's words for the window", () => {
  assert.equal(rangeLabel("day", { from: "2026-10-17", to: "2026-10-17" }), "Sat 17 Oct 2026");
  assert.equal(rangeLabel("week", WEEK), "Mon 12 – Sun 18 Oct 2026");
  assert.equal(rangeLabel("week", { from: "2026-09-28", to: "2026-10-04" }), "Mon 28 Sep – Sun 4 Oct 2026");
  assert.equal(rangeLabel("month", MONTH), "October 2026");
});
