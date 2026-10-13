// The URL hash state.

import assert from "node:assert/strict";
import { test } from "node:test";

import { formatHash, parseHash, parseOptions } from "../../src/oiltank/web/static/js/store.js";

test("defaults", () => {
  assert.deepEqual(parseHash(""), { view: "now", range: "7d", end: null, at: null });
  assert.deepEqual(parseHash("#view=nonsense&range=5y"), { view: "now", range: "7d", end: null, at: null });
});

test("round trip", () => {
  const state = { view: "history", range: "30d", end: Date.UTC(2026, 9, 14) / 1000, at: Date.UTC(2026, 9, 13, 3, 15) / 1000 };
  const hash = formatHash(state);
  assert.equal(hash, "#view=history&range=30d&end=2026-10-14T00:00:00Z&at=2026-10-13T03:15:00Z");
  assert.deepEqual(parseHash(hash), state);
  assert.equal(formatHash({ view: "now", range: "7d", end: null, at: 5 }), "#view=now");
});

test("embedding options", () => {
  assert.deepEqual(parseOptions("?embed=card&theme=dark"), { card: true, theme: "dark" });
  assert.deepEqual(parseOptions("?theme=purple"), { card: false, theme: null });
});
