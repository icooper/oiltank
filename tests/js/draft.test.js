// The calibration draft's bookkeeping.

import assert from "node:assert/strict";
import { test } from "node:test";

import { draft, samePoints } from "../../src/oiltank/web/static/js/draft.js";

test("point lists compare by value", () => {
  const a = [{ at: [2168, 1047.5], fraction: 0.98, note: "x" }];
  assert.ok(samePoints(a, structuredClone(a)));
  assert.ok(!samePoints(a, [{ ...a[0], fraction: 0.97 }]));
  assert.ok(!samePoints(a, []));
});

test("a draft is dirty once it differs from the saved points", () => {
  draft.calibration = { etag: "e", points: [{ t: 0.5, fraction: 0.5 }], positions: [0.5], read_only: false };
  draft.set([{ t: 0.5, fraction: 0.5 }]);
  assert.equal(draft.dirty, false);
  draft.set([{ t: 0.5, fraction: 0.6 }]);
  assert.equal(draft.dirty, true);
  draft.discard();
  assert.equal(draft.dirty, false);
});
