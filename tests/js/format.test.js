// The web UI's pure formatting helpers (`node --test "tests/js/*.test.js"`; pytest runs them too).

import assert from "node:assert/strict";
import { test } from "node:test";

import { amount, NONE, percent, reason, relative, size } from "../../src/oiltank/web/static/js/format.js";

test("percent and amount", () => {
  assert.equal(percent(98), "98.0%");
  assert.equal(percent(null), NONE);
  assert.equal(amount(254.6, "gal"), "255 gal");
  assert.equal(amount(2.04, "gal/day", 1), "2.0 gal/day");
  assert.equal(amount(undefined, "L"), NONE);
});

test("relative times", () => {
  const now = new Date("2026-10-13T12:00:00Z");
  const at = (seconds) => new Date(now.getTime() + seconds * 1000);
  assert.equal(relative(at(-20), now), "just now");
  assert.equal(relative(at(-4 * 60), now), "4 min ago");
  assert.equal(relative(at(11 * 60), now), "in 11 min");
  assert.equal(relative(at(-3 * 3600), now), "3 h ago");
  assert.equal(relative(at(-3 * 86400), now), "3 days ago");
  assert.equal(relative("2026-10-13T11:30:00Z", now), "30 min ago");
  assert.equal(relative(null, now), NONE);
});

test("sizes use powers of 1024", () => {
  assert.equal(size(512), "512 B");
  assert.equal(size(1536), "1.5 KB");
  assert.equal(size(120 * 1024 * 1024), "120 MB");
  assert.equal(size(2000 * 1024 * 1024), "2.0 GB");
  assert.equal(size(null), NONE);
});

test("problem reasons in words", () => {
  assert.equal(reason("camera_unreachable"), "camera unreachable");
  assert.equal(reason(null), "");
});
