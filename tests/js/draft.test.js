// The calibration draft's bookkeeping.

import assert from "node:assert/strict";
import { test } from "node:test";

import { draft, geometryOf, samePoints } from "../../src/oiltank/web/static/js/draft.js";

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

test("geometry changes make a draft dirty too, and are sent with it", () => {
  const axis = { top: [1, 2], bottom: [1, 200], half_width: 50, margin: 0.25 };
  draft.calibration = {
    etag: "e", points: [], positions: [], read_only: false, axis, disc: { width: 60, height: 40 },
    anchor: { rect: [0, 0, 50, 50] },
  };
  draft.geometry = geometryOf(draft.calibration);
  draft.set([]);
  assert.equal(draft.dirty, false);
  draft.setGeometry({ axis: { ...axis, half_width: 60 } });
  assert.equal(draft.dirty, true);
  assert.equal(draft.geometryChanged, true);
  assert.equal(draft.pointsChanged, false);
  assert.equal(draft.body.axis.half_width, 60);
  assert.deepEqual(draft.body.disc, { width: 60, height: 40 });
  draft.discard();
  assert.equal(draft.dirty, false);
});

import { hasEnd, setEnd } from "../../src/oiltank/web/static/js/draft.js";

test("setting E and F points", () => {
  const owner = { at: [2168, 1047.5], fraction: 0.98, note: "owner" };
  const withF = setEnd([owner], 1, [2166.04, 1030.06], "F");
  assert.deepEqual(withF, [owner, { at: [2166, 1030.1], fraction: 1, note: "F" }]);
  assert.ok(hasEnd(withF, 1) && !hasEnd(withF, 0));
  const moved = setEnd(withF, 1, [2166, 1025], "F again");
  assert.equal(moved.length, 2);
  assert.deepEqual(moved[1].at, [2166, 1025]);
  const withE = setEnd(moved, 0, [2166, 1172], "E");
  assert.deepEqual(withE.map((p) => p.fraction), [0, 0.98, 1]); // in order up the gauge
  assert.equal(setEnd([{ t: 0.1, fraction: 0.003, note: "" }], 0, [1, 2], "E").length, 1); // the same point
});
