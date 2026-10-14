// Moving through a series of readings: nearest reading, stepping, jumping to events. Pure: no DOM.
// `ts` is a sorted array of Unix seconds.

import { DAY } from "./timescale.js";

/** The index of the reading closest to time `t` (-1 if there are none). */
export function nearestIndex(ts, t) {
  if (ts.length === 0) return -1;
  let lo = 0;
  let hi = ts.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) lo = mid;
    else hi = mid;
  }
  return Math.abs(ts[hi] - t) < Math.abs(ts[lo] - t) ? hi : lo;
}

/** `delta` readings on from `i`, kept within the series. */
export function stepBy(ts, i, delta) {
  if (ts.length === 0) return -1;
  return Math.max(0, Math.min(ts.length - 1, i + delta));
}

/** About `days` days on from reading `i` (backwards if negative): the nearest reading to then. */
export function stepDays(ts, i, days) {
  if (ts.length === 0) return -1;
  const j = nearestIndex(ts, ts[i] + days * DAY);
  if (j === i) return stepBy(ts, i, Math.sign(days));
  return j;
}

/** Whether reading `i` is an event of `kind`: "problem", "rejected" or "fill". */
function isEvent(series, i, kind, fillIndexes) {
  const r = series.readings;
  switch (kind) {
    case "problem":
      return r.problem[i] != null;
    case "rejected":
      return !r.accepted[i];
    case "fill":
      return fillIndexes.has(i);
    default:
      return false;
  }
}

/** The next reading after `i` (or before it, for `dir` -1) that is an event of `kind`; -1 if none. */
export function nextEvent(series, i, dir, kind) {
  const ts = series.readings.ts;
  const fills = new Set((series.fills || []).map((f) => nearestIndex(ts, f.ts)));
  for (let j = i + dir; j >= 0 && j < ts.length; j += dir) {
    if (isEvent(series, j, kind, fills)) return j;
  }
  return -1;
}

/** The indexes from `i - radius` to `i + radius`, within `[0, n)`. */
export function around(i, n, radius) {
  const out = [];
  for (let j = Math.max(0, i - radius); j <= Math.min(n - 1, i + radius); j += 1) out.push(j);
  return out;
}

/** The median gap between readings, in seconds (0 for fewer than two). */
export function medianSpacing(ts) {
  if (ts.length < 2) return 0;
  const gaps = [];
  for (let i = 1; i < ts.length; i += 1) gaps.push(ts[i] - ts[i - 1]);
  gaps.sort((a, b) => a - b);
  return gaps[gaps.length >> 1];
}

/** The UTC day (YYYY-MM-DD) a time falls on: archive records are filed by it. */
export function utcDay(t) {
  return new Date(t * 1000).toISOString().slice(0, 10);
}

/** The UTC days from `min` to `max`, inclusive. */
export function utcDays(min, max) {
  const out = [];
  for (let t = Math.floor(min / DAY) * DAY; t <= max; t += DAY) out.push(utcDay(t));
  return out;
}
