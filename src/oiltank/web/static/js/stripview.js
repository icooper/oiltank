// A strip, drawn the way the History inspector and the Gauge tab both show it: the strip itself,
// the gauge's marks across it (dashed), where the reader put the disc (blue; amber if the reading
// failed), each frame of a burst (dots), and the reader's score for every row (the curve on the
// right, highest where the strip looks most like the disc).

import { sized } from "./canvas.js";
import { mergeLabels } from "./geometry.js";

const GUTTER = 58;
const GAP = 8;
const SCORES = 56;

/**
 * Draw on `canvas` at `scale` (1 = one pixel per strip pixel). `img` is the strip image (or
 * null), `d` is `{ rows, ticks: [{ label, row }], frames: [{ row }], disc_row, problem, level }`
 * (or null) and `curve` is `{ scores }` (or null).
 */
export function drawStrip(canvas, img, d, curve, c, scale = 1.5) {
  const rows = (d && d.rows) || 256;
  const h = Math.round(rows * scale);
  const w = Math.round(128 * scale);
  const ctx = sized(canvas, GUTTER + w + GAP + SCORES, h);
  ctx.fillStyle = c.empty;
  ctx.fillRect(GUTTER, 0, w, h);
  if (img) {
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(img, GUTTER, 0, w, h);
  }
  if (!d) return;
  const y = (row) => (row + 0.5) * scale;
  ctx.font = "11px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.setLineDash([4, 3]);
  const marks = (d.ticks || [])
    .filter((tick) => tick.row != null && tick.row >= 0 && tick.row <= rows - 1)
    .map((tick) => ({ y: y(tick.row), label: tick.label }));
  ctx.strokeStyle = c.mark;
  for (const m of marks) {
    ctx.beginPath();
    ctx.moveTo(GUTTER, m.y);
    ctx.lineTo(GUTTER + w, m.y);
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.fillStyle = c.muted;
  for (const m of mergeLabels(marks, 12)) ctx.fillText(m.label, GUTTER - 6, m.y);
  ctx.fillStyle = c.warn;
  for (const f of d.frames || []) {
    if (f.row == null) continue;
    ctx.beginPath();
    ctx.arc(GUTTER + w - 6, y(f.row), 3, 0, 2 * Math.PI);
    ctx.fill();
  }
  if (d.disc_row != null) {
    // Where the reader looked: amber if the reading failed (its best guess, not a reading).
    ctx.strokeStyle = d.problem || d.level == null ? c.warn : c.accent;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(GUTTER, y(d.disc_row));
    ctx.lineTo(GUTTER + w, y(d.disc_row));
    ctx.stroke();
    ctx.lineWidth = 1;
  }
  if (curve && curve.scores) {
    const values = curve.scores.filter((v) => v != null);
    if (values.length) {
      const lo = Math.min(...values);
      const span = Math.max(1e-6, Math.max(...values) - lo);
      const x0 = GUTTER + w + GAP;
      ctx.strokeStyle = c.fg;
      ctx.beginPath();
      let pen = false;
      curve.scores.forEach((v, row) => {
        if (v == null) {
          pen = false;
          return;
        }
        const x = x0 + ((v - lo) / span) * SCORES;
        if (pen) ctx.lineTo(x, y(row));
        else ctx.moveTo(x, y(row));
        pen = true;
      });
      ctx.stroke();
    }
  }
}
