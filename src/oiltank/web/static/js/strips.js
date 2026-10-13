// The gauge over time: each archived reading's strip as a column, on the same time axis as the
// chart, with the gauge's marks across it and the reader's position as dots.

import { image, sized } from "./canvas.js";
import { gridRow, mergeLabels } from "./geometry.js";
import { medianSpacing } from "./series.js";

/**
 * `days` maps a UTC day to its listing (`{ records, tile }` from api/archive/days/<day>);
 * `scaleInfo` is the readings' `scale` (`{ t_top, t_bottom, rows, ticks }`).
 */
export function drawStrips(canvas, width, height, scale, days, series, scaleInfo, c, redraw) {
  const ctx = sized(canvas, width, height);
  ctx.fillStyle = c.empty;
  ctx.fillRect(scale.left, 0, scale.width, height);
  if (!scaleInfo) return;
  const yOf = (t) => ((gridRow(scaleInfo, t) + 0.5) / scaleInfo.rows) * height;

  ctx.save();
  ctx.beginPath();
  ctx.rect(scale.left, 0, scale.width, height);
  ctx.clip();
  const all = [];
  for (const listing of days.values()) for (const r of listing.records) all.push(r.ts);
  all.sort((a, b) => a - b);
  const typical = Math.max(1, medianSpacing(all) * (scale.width / scale.span));
  for (const [, listing] of days) {
    if (!listing.tile || listing.records.length === 0) continue;
    const img = image(listing.tile, redraw);
    if (!img) continue;
    const recs = listing.records;
    const first = scale.x(recs[0].ts);
    const last = scale.x(recs[recs.length - 1].ts);
    if (last < scale.left - 50 || first > scale.left + scale.width + 50) continue;
    if (typical < 1.5) {  // too dense for single columns: squeeze the whole day in
      ctx.drawImage(img, first, 0, Math.max(1, last - first + typical), height);
      continue;
    }
    for (let i = 0; i < recs.length; i += 1) {
      if (!recs[i].strip) continue;
      const x = scale.x(recs[i].ts);
      const next = i + 1 < recs.length ? scale.x(recs[i + 1].ts) : x + typical;
      const w = Math.max(1, Math.min(next - x, 3 * typical));
      ctx.drawImage(img, i, 0, 1, img.height, x, 0, w + 0.5, height);
    }
  }

  if (series) {
    const r = series.readings;
    ctx.fillStyle = c.accent;
    ctx.globalAlpha = 0.85;
    for (let i = 0; i < r.ts.length; i += 1) {
      if (r.t[i] == null || r.level[i] == null) continue;
      ctx.fillRect(scale.x(r.ts[i]) - 1, yOf(r.t[i]) - 1, 2, 2);
    }
    ctx.globalAlpha = 1;
  }
  ctx.restore();

  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.setLineDash([4, 4]);
  const marks = scaleInfo.ticks
    .map((tick) => ({ y: Math.round(yOf(tick.t)) + 0.5, label: tick.label }))
    .filter((m) => m.y >= 0 && m.y <= height);
  ctx.strokeStyle = c.mark;
  for (const m of marks) {
    ctx.beginPath();
    ctx.moveTo(scale.left, m.y);
    ctx.lineTo(scale.left + scale.width, m.y);
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.fillStyle = c.muted;
  for (const m of mergeLabels(marks, 12)) ctx.fillText(m.label, scale.left - 6, m.y);
}
