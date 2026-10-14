// Moving through readings.

import assert from "node:assert/strict";
import { test } from "node:test";

import { around, medianSpacing, nearestIndex, nextEvent, stepBy, stepDays, utcDay, utcDays }
  from "../../src/oiltank/web/static/js/series.js";

const T0 = Date.UTC(2026, 9, 13, 3) / 1000;
const ts = Array.from({ length: 200 }, (_, i) => T0 + i * 900); // every 15 minutes

test("nearest reading", () => {
  assert.equal(nearestIndex(ts, T0 - 1000), 0);
  assert.equal(nearestIndex(ts, T0 + 900 * 10 + 400), 10);
  assert.equal(nearestIndex(ts, T0 + 900 * 10 + 500), 11);
  assert.equal(nearestIndex(ts, T0 + 1e9), 199);
  assert.equal(nearestIndex([], T0), -1);
});

test("stepping by readings and by days", () => {
  assert.equal(stepBy(ts, 5, -10), 0);
  assert.equal(stepBy(ts, 195, 10), 199);
  assert.equal(stepDays(ts, 0, 1), 96);
  assert.equal(stepDays(ts, 96, -1), 0);
  assert.equal(stepDays(ts, 199, 1), 199);
});

test("jumping to the next event", () => {
  const n = 10;
  const series = {
    readings: {
      ts: ts.slice(0, n),
      problem: Array(n).fill(null),
      accepted: Array(n).fill(true),
    },
    fills: [{ ts: ts[7] + 30 }],
  };
  series.readings.problem[3] = "camera_moved";
  series.readings.accepted[5] = false;
  assert.equal(nextEvent(series, 0, 1, "problem"), 3);
  assert.equal(nextEvent(series, 3, 1, "problem"), -1);
  assert.equal(nextEvent(series, 9, -1, "rejected"), 5);
  assert.equal(nextEvent(series, 0, 1, "fill"), 7);
});

test("windows, spacing and UTC days", () => {
  assert.deepEqual(around(1, 10, 2), [0, 1, 2, 3]);
  assert.deepEqual(around(9, 10, 1), [8, 9]);
  assert.equal(medianSpacing(ts), 900);
  assert.equal(medianSpacing([T0]), 0);
  assert.equal(utcDay(Date.UTC(2026, 9, 13, 23, 59) / 1000), "2026-10-13");
  assert.deepEqual(utcDays(T0, T0 + 2 * 86400), ["2026-10-13", "2026-10-14", "2026-10-15"]);
});
