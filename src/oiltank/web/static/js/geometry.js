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

// ---- the geometry editor ------------------------------------------------------------------------

/** How a big image is shown: screen = (image - origin) * scale. Pure; transforms return copies. */
export class View {
  constructor(scale, x0, y0) {
    this.scale = scale;
    this.x0 = x0;
    this.y0 = y0;
  }

  toScreen([x, y]) {
    return [(x - this.x0) * this.scale, (y - this.y0) * this.scale];
  }

  toImage([sx, sy]) {
    return [sx / this.scale + this.x0, sy / this.scale + this.y0];
  }

  /** Zoomed by `factor` about screen point `at`, which stays over the same image point. */
  zoomAt(factor, at, min = 0.02, max = 16) {
    const scale = Math.min(max, Math.max(min, this.scale * factor));
    const [ix, iy] = this.toImage(at);
    return new View(scale, ix - at[0] / scale, iy - at[1] / scale);
  }

  /** Moved by a screen distance. */
  panBy(dx, dy) {
    return new View(this.scale, this.x0 - dx / this.scale, this.y0 - dy / this.scale);
  }

  /** Showing image rectangle `[x, y, w, h]` whole and centred in a `width` x `height` screen. */
  static fit([x, y, w, h], width, height) {
    const scale = Math.min(width / w, height / h);
    return new View(scale, x + w / 2 - width / 2 / scale, y + h / 2 - height / 2 / scale);
  }
}

const add = ([x, y], [dx, dy]) => [x + dx, y + dy];
const round1 = (v) => Math.round(v * 10) / 10;

/** The unit vector across the axis (to the right on screen for a vertical axis). */
export function across(axis) {
  const dx = axis.bottom[0] - axis.top[0];
  const dy = axis.bottom[1] - axis.top[1];
  const length = Math.hypot(dx, dy) || 1;
  let n = [-dy / length, dx / length];
  if (n[0] < 0) n = [-n[0], -n[1]];
  return n.map((v) => v + 0); // no -0
}

/** The strip's outline: the axis extended by its margin, half_width either side (image px). */
export function stripOutline(axis) {
  const [nx, ny] = across(axis);
  const u = [axis.bottom[0] - axis.top[0], axis.bottom[1] - axis.top[1]];
  const m = axis.margin ?? 0.25;
  const start = [axis.top[0] - m * u[0], axis.top[1] - m * u[1]];
  const end = [axis.bottom[0] + m * u[0], axis.bottom[1] + m * u[1]];
  const w = axis.half_width;
  return [
    [start[0] - w * nx, start[1] - w * ny],
    [start[0] + w * nx, start[1] + w * ny],
    [end[0] + w * nx, end[1] + w * ny],
    [end[0] - w * nx, end[1] - w * ny],
  ];
}

/** The handles for a geometry (`{ axis, anchor }`), in image px. */
export function handles({ axis, anchor }) {
  const mid = [(axis.top[0] + axis.bottom[0]) / 2, (axis.top[1] + axis.bottom[1]) / 2];
  const [nx, ny] = across(axis);
  const [x, y, w, h] = anchor.rect;
  return [
    { id: "top", at: axis.top },
    { id: "bottom", at: axis.bottom },
    { id: "half", at: [mid[0] + axis.half_width * nx, mid[1] + axis.half_width * ny] },
    { id: "anchor-nw", at: [x, y] },
    { id: "anchor-se", at: [x + w, y + h] },
    { id: "anchor", at: [x + w / 2, y + h / 2] },
  ];
}

/** The handle within `radius` screen px of screen point `at`, nearest first (null if none). */
export function hit(list, view, at, radius = 12) {
  let best = null;
  let bestDistance = radius;
  for (const handle of list) {
    const [sx, sy] = view.toScreen(handle.at);
    const d = Math.hypot(sx - at[0], sy - at[1]);
    if (d <= bestDistance) {
      best = handle.id;
      bestDistance = d;
    }
  }
  return best;
}

/**
 * The geometry after dragging handle `id` by `delta` (image px) from where it was in `start`.
 * Axis ends move; the half-width is the new distance across the axis; the anchor moves or
 * resizes (whole pixels, at least 16 px a side).
 */
export function drag(start, id, delta) {
  const axis = { ...start.axis };
  const anchor = { ...start.anchor };
  const [x, y, w, h] = start.anchor.rect;
  const [dx, dy] = delta;
  switch (id) {
    case "top":
      axis.top = add(start.axis.top, delta).map(round1);
      break;
    case "bottom":
      axis.bottom = add(start.axis.bottom, delta).map(round1);
      break;
    case "half": {
      const [nx, ny] = across(start.axis);
      axis.half_width = Math.max(4, round1(start.axis.half_width + dx * nx + dy * ny));
      break;
    }
    case "anchor":
      anchor.rect = [Math.round(x + dx), Math.round(y + dy), w, h];
      break;
    case "anchor-nw": {
      const nx0 = Math.round(Math.min(x + dx, x + w - 16));
      const ny0 = Math.round(Math.min(y + dy, y + h - 16));
      anchor.rect = [nx0, ny0, x + w - nx0, y + h - ny0];
      break;
    }
    case "anchor-se":
      anchor.rect = [x, y, Math.max(16, Math.round(w + dx)), Math.max(16, Math.round(h + dy))];
      break;
    default:
      return start;
  }
  return { ...start, axis, anchor };
}
