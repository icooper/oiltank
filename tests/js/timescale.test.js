// The shared time axis (run with TZ=UTC, as tests/unit/test_js.py does).

import assert from "node:assert/strict";
import { test } from "node:test";

import { DAY, HOUR, TimeScale, ticks } from "../../src/oiltank/web/static/js/timescale.js";

const T0 = Date.UTC(2026, 9, 13) / 1000; // 13 Oct 2026, 00:00 UTC

test("x and t are inverses", () => {
  const s = new TimeScale(T0, T0 + DAY, 40, 800);
  assert.equal(s.x(T0), 40);
  assert.equal(s.x(T0 + DAY), 840);
  assert.equal(s.t(440), T0 + DAY / 2);
});

test("zooming keeps the point under the pointer still", () => {
  const s = new TimeScale(T0, T0 + DAY, 0, 1000);
  const at = T0 + 6 * HOUR;
  const zoomed = s.zoom(0.5, at);
  assert.equal(zoomed.span, DAY / 2);
  assert.ok(Math.abs(zoomed.x(at) - s.x(at)) < 1e-6);
  assert.equal(s.pan(HOUR).min, T0 + HOUR);
});

test("clamped keeps a sensible width and doesn't run into the future", () => {
  const s = new TimeScale(T0, T0 + 10, 0, 100).clamped(HOUR, 365 * DAY, null);
  assert.equal(s.span, HOUR);
  const late = new TimeScale(T0, T0 + DAY, 0, 100).clamped(HOUR, 365 * DAY, T0 + HOUR);
  assert.equal(late.max, T0 + HOUR);
  assert.equal(late.span, DAY);
});

test("ticks over a day are hours, with midnight as a date", () => {
  const s = new TimeScale(T0 - HOUR, T0 + DAY, 0, 960);
  const t = ticks(s, 80);
  assert.ok(t.length >= 6 && t.length <= 13, `${t.length} ticks`);
  const midnight = t.find((k) => k.t === T0);
  assert.deepEqual(midnight, { t: T0, major: true, label: "13 Oct" });
  assert.ok(t.some((k) => k.label === "06:00"));
  for (let i = 1; i < t.length; i += 1) assert.ok(s.x(t[i].t) - s.x(t[i - 1].t) >= 80);
});

test("ticks over a week are days; over a year, months", () => {
  const week = ticks(new TimeScale(T0, T0 + 7 * DAY, 0, 700), 80);
  assert.deepEqual(week.map((k) => k.label).slice(0, 3), ["13 Oct", "14 Oct", "15 Oct"]);
  const year = ticks(new TimeScale(T0, T0 + 365 * DAY, 0, 800), 80);
  assert.ok(year.some((k) => k.label === "Jan 2027" && k.major));
  assert.ok(year.every((k) => !k.label.includes(":")));
});
