// node --test (no dependency): money from the session currency, the rounding helpers, units, times and the calendar
// labels. The currency codes below are test inputs; the formatter itself holds no currency.
import { test } from "node:test";
import assert from "node:assert/strict";
import { centsDown, centsUp, dateLabel, expected, factor, hhmm, markDate, money, signed, units, weekday } from "./format.ts";

const CAL = [
  { day: 3, date: "2026-10-15", weekday: "Thu" },
  { day: 4, date: "2026-10-16", weekday: "Fri" },
  { day: 5, date: "2026-10-17", weekday: "Sat" },
];

test("money: cents by default, a true minus (U+2212), whole units on request, an em dash for null", () => {
  assert.equal(money(-5, "USD"), "−$5.00");
  assert.equal(money(1234.5, "USD", true), "$1,235");
  assert.equal(money(-1234.5, "USD", true), "−$1,235");
  assert.equal(money(23.333, "USD"), "$23.33");
  assert.equal(money(null, "USD"), "—");
  assert.equal(money(undefined, "USD"), "—");
  assert.equal(money(0, "USD"), "$0.00");
  assert.equal(money(-0.001, "USD"), "$0.00"); // a value that rounds to zero carries no minus
});

test("money: another ISO currency renders its own symbol with no code change", () => {
  assert.equal(money(10, "EUR"), "€10.00");
  assert.equal(money(-10, "EUR"), "−€10.00");
  assert.equal(money(1500, "JPY", true), "¥1,500");
  assert.notEqual(money(10, "GBP"), money(10, "USD"));
});

test("signed: a plus on positive values, none on zero, a true minus on negative ones", () => {
  assert.equal(signed(12.3, "USD"), "+$12.30");
  assert.equal(signed(0, "USD"), "$0.00");
  assert.equal(signed(-5, "USD"), "−$5.00");
  assert.equal(signed(42.4, "USD", true), "+$42");
  assert.equal(signed(null, "USD"), "—");
});

test("centsUp and centsDown round to the cent in their direction", () => {
  assert.equal(centsUp(0.4201, "USD"), "$0.43");
  assert.equal(centsDown(0.4299, "USD"), "$0.42");
  assert.equal(centsDown(5e-324, "USD"), "$0.00");
});

test("units: grouped whole numbers, an em dash for null", () => {
  assert.equal(units(1234), "1,234");
  assert.equal(units(0), "0");
  assert.equal(units(null), "—");
});

test("expected: the engine's expected unit counts in whole units, nearest, half away from zero (G-05-2 item 10)", () => {
  assert.equal(expected(250.66), "251");
  assert.equal(expected(49.84), "50");
  assert.equal(expected(13.34), "13");
  assert.equal(expected(0.4), "0");
  assert.equal(expected(0.5), "1");
  assert.equal(expected(1234.5), "1,235");
  assert.equal(expected(null), "—");
  assert.equal(expected(undefined), "—");
});

test("hhmm: two-digit hours and minutes from minutes after midnight", () => {
  assert.equal(hhmm(600), "10:00");
  assert.equal(hhmm(5), "00:05");
  assert.equal(hhmm(1439), "23:59");
});

test("weekday and dateLabel read the calendar; an unknown day gives an empty string", () => {
  assert.equal(weekday(CAL, 3), "Thu");
  assert.equal(weekday(CAL, 5), "Sat");
  assert.equal(dateLabel(CAL, 4), "Fri 16 Oct");
  assert.equal(dateLabel(CAL, 3), "Thu 15 Oct");
  assert.equal(weekday(CAL, 9), "");
  assert.equal(dateLabel(CAL, 9), "");
});

test("factor: a markdown multiplier with at most two fraction digits", () => {
  assert.equal(factor(1.4321), "1.43");
  assert.equal(factor(1.3), "1.3");
  assert.equal(factor(1.5), "1.5");
  assert.equal(factor(2), "2");
  assert.equal(factor(1.4625), "1.46");
});

test("centsUp and centsDown at the cent boundary: on a cent stays, just past it moves in the rounding direction", () => {
  assert.equal(centsUp(0.42, "USD"), "$0.42");
  assert.equal(centsUp(0.4200000001, "USD"), "$0.43");
  assert.equal(centsDown(0.42, "USD"), "$0.42");
  assert.equal(centsDown(0.4199999999, "USD"), "$0.41");
});

test("markDate: the dataset mark's date from an ISO date, en-US parts on the UTC calendar, no time (05.2 D-04)", () => {
  assert.equal(markDate("2026-10-17"), "Sat 17 Oct 2026");
  assert.equal(markDate("2026-10-14"), "Wed 14 Oct 2026");
  assert.equal(markDate("2027-01-03"), "Sun 3 Jan 2027");
});
