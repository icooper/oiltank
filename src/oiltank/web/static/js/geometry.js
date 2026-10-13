// Small bits of geometry the pages share with the server, which they mirror exactly
// (tests/js/geometry.test.js checks them against values Python works out). Pure: no DOM.

/**
 * The (fractional) row of gauge position `t` in a gauge-over-time tile, row centres at whole
 * numbers: mirrors `oiltank.web.kymograph.Grid`. `grid` is `{ t_top, t_bottom, rows }`.
 */
export function gridRow(grid, t) {
  return ((grid.t_top - t) / (grid.t_top - grid.t_bottom)) * grid.rows - 0.5;
}

/** The inverse of `gridRow`. */
export function gridT(grid, row) {
  return grid.t_top - ((row + 0.5) / grid.rows) * (grid.t_top - grid.t_bottom);
}

/** The row of `t` in a strip whose top row is at `first` and bottom row (`rows - 1`) at `last`. */
export function stripRow(t, first, last, rows) {
  return ((t - first) / (last - first)) * (rows - 1);
}

/**
 * The disc outline's rotation, in radians clockwise on screen, for an axis from `bottom` to
 * `top` (`[x, y]`, y down): 0 when the axis is vertical. Mirrors `oiltank.annotate._disc_box`.
 */
export function discAngle(top, bottom) {
  return Math.atan2(top[0] - bottom[0], bottom[1] - top[1]);
}

/** How to draw `crop` (`[x, y, w, h]` in image pixels) into a `width` x `height` box, centred. */
export function fit(crop, width, height) {
  const [x, y, w, h] = crop;
  const scale = Math.min(width / w, height / h);
  const dx = (width - w * scale) / 2 - x * scale;
  const dy = (height - h * scale) / 2 - y * scale;
  return { scale, toCanvas: ([px, py]) => [px * scale + dx, py * scale + dy] };
}

/**
 * Labels at heights `y` (`[{ y, label }]`), with any closer than `gap` px merged into one
 * ("F · 98%") at their mean height, so they don't overprint.
 */
export function mergeLabels(items, gap) {
  const sorted = [...items].sort((a, b) => a.y - b.y);
  const out = [];
  for (const item of sorted) {
    const last = out[out.length - 1];
    if (last && item.y - last.ys[last.ys.length - 1] < gap) {
      last.ys.push(item.y);
      last.labels.push(item.label);
    } else {
      out.push({ ys: [item.y], labels: [item.label] });
    }
  }
  return out.map((g) => ({ y: g.ys.reduce((a, b) => a + b, 0) / g.ys.length, label: g.labels.join(" · ") }));
}
