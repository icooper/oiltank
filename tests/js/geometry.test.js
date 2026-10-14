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

import { across, drag, handles, hit, stripOutline, View } from "../../src/oiltank/web/static/js/geometry.js";

const geometry = {
  axis: { top: [2166, 1020], bottom: [2166, 1172], half_width: 50, margin: 0.25 },
  disc: { width: 64, height: 38 },
  anchor: { rect: [1950, 1185, 290, 165], search: 48, min_score: 0.6, recalibrate_shift: 20 },
};

test("the view maps image and screen both ways, and zooms about the pointer", () => {
  const v = new View(0.5, 1000, 800);
  assert.deepEqual(v.toScreen([1100, 900]), [50, 50]);
  assert.deepEqual(v.toImage([50, 50]), [1100, 900]);
  const z = v.zoomAt(2, [50, 50]);
  assert.equal(z.scale, 1);
  assert.deepEqual(z.toScreen([1100, 900]), [50, 50]);
  assert.deepEqual(v.panBy(10, 0).toImage([0, 0]), [980, 800]);
  const fit = View.fit([2000, 1000, 400, 200], 800, 800);
  assert.equal(fit.scale, 2);
  assert.deepEqual(fit.toScreen([2200, 1100]), [400, 400]);
});

test("the strip outline and the handles", () => {
  assert.deepEqual(across(geometry.axis), [1, 0]);
  const outline = stripOutline(geometry.axis);
  assert.deepEqual(outline[0], [2116, 982]); // above the top, by the margin; left of the axis
  assert.deepEqual(outline[2], [2216, 1210]);
  const h = Object.fromEntries(handles(geometry).map((x) => [x.id, x.at]));
  assert.deepEqual(h.half, [2216, 1096]);
  assert.deepEqual(h.anchor, [2095, 1267.5]);
  const v = new View(1, 2100, 1000);
  assert.equal(hit(handles(geometry), v, [66, 21]), "top");
  assert.equal(hit(handles(geometry), v, [300, 300]), null);
});

test("dragging handles", () => {
  assert.deepEqual(drag(geometry, "top", [0, -30]).axis.top, [2166, 990]);
  assert.equal(drag(geometry, "half", [10, 50]).axis.half_width, 60); // only the across part counts
  assert.deepEqual(drag(geometry, "anchor", [5.4, -2.6]).anchor.rect, [1955, 1182, 290, 165]);
  assert.deepEqual(drag(geometry, "anchor-se", [-400, 10]).anchor.rect, [1950, 1185, 16, 175]);
  assert.deepEqual(drag(geometry, "anchor-nw", [10, 10]).anchor.rect, [1960, 1195, 280, 155]);
  assert.deepEqual(drag(geometry, "anchor", [1, 1]).axis, geometry.axis); // the rest unchanged
  assert.equal(drag(geometry, "nothing", [1, 1]), geometry);
});

import { alongAxis, onAxis } from "../../src/oiltank/web/static/js/geometry.js";

test("projecting onto the axis, and back", () => {
  const axis = { top: [2166, 1020], bottom: [2166, 1172] };
  const { t, at } = onAxis(axis, [2180, 1096]);
  assert.equal(t, 0.5);
  assert.deepEqual(at, [2166, 1096]);
  assert.equal(onAxis(axis, [2166, 1172]).t, 0);
  assert.ok(onAxis(axis, [2166, 1000]).t > 1);
  assert.deepEqual(alongAxis(axis, 0.5), [2166, 1096]);
  assert.deepEqual(alongAxis(axis, 1.25), [2166, 982]); // past the top, as F can be
  const tilted = { top: [2200, 1020], bottom: [2160, 1172] };
  assert.ok(Math.abs(onAxis(tilted, alongAxis(tilted, 0.37)).t - 0.37) < 1e-12);
});
