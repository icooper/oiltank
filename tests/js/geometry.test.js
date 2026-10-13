// Geometry the pages must work out exactly as the server does (tests/data/js-vectors.json,
// written by tests/unit/test_js_vectors.py).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { discAngle, fit, gridRow, gridT, mergeLabels, stripRow } from "../../src/oiltank/web/static/js/geometry.js";

const golden = JSON.parse(readFileSync(new URL("../data/js-vectors.json", import.meta.url)));
const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test("gauge-over-time rows match the server's grid", () => {
  for (const { row, t } of golden.grid_rows) {
    close(gridRow(golden.grid, t), row);
    close(gridT(golden.grid, row), t);
  }
});

test("strip rows match the server's strips", () => {
  const { first, last } = golden.strip;
  for (const { row, t } of golden.strip.rows) close(stripRow(t, first, last, 256), row, 1e-4);
});

test("the disc outline turns with the axis, as on the gauge image", () => {
  for (const { top, bottom, radians } of golden.disc_angles) close(discAngle(top, bottom), radians);
});

test("fitting a crop into a box", () => {
  const f = fit([100, 200, 400, 200], 200, 200);
  assert.equal(f.scale, 0.5);
  assert.deepEqual(f.toCanvas([100, 200]), [0, 50]);
  assert.deepEqual(f.toCanvas([500, 400]), [200, 150]);
});

test("labels too close together are merged", () => {
  const merged = mergeLabels([{ y: 50, label: "98%" }, { y: 120, label: "E" }, { y: 44, label: "F" }], 12);
  assert.deepEqual(merged, [{ y: 47, label: "F · 98%" }, { y: 120, label: "E" }]);
});
