// The disc-size scan as a grid: one cell per disc size (width across, height down), coloured by
// whether that size reads the disc where the draft's size does. Sizes that agree are green, darker
// for more confidence; sizes that read it elsewhere are amber to red, by how far off; the
// draft's size is ringed and the recommended one dotted. Clicking a cell picks that size.

import { sized } from "./canvas.js";

const CELL = 26;
const LEFT = 44;
const BOTTOM = 34;

/** Draw `scan` (from api/calibration/scan); returns a function from a click to `{ width, height }`. */
export function drawHeatmap(canvas, scan, c) {
  const cols = scan.widths.length;
  const rows = scan.heights.length;
  const width = LEFT + cols * CELL + 4;
  const height = rows * CELL + BOTTOM;
  const ctx = sized(canvas, width, height);
  const consensus = scan.consensus_t;
  for (let i = 0; i < cols; i += 1) {
    for (let j = 0; j < rows; j += 1) {
      const x = LEFT + i * CELL;
      const y = j * CELL;
      const t = scan.t[i][j];
      if (t == null) {
        ctx.fillStyle = c.empty;
      } else if (scan.stable[i][j]) {
        ctx.fillStyle = c.ok;
        ctx.globalAlpha = 0.35 + 0.6 * Math.max(0, Math.min(1, scan.confidence[i][j] ?? 0));
      } else {
        const off = consensus == null ? 1 : Math.min(1, Math.abs(t - consensus) / 0.1);
        ctx.fillStyle = off > 0.5 ? c.bad : c.warn;
        ctx.globalAlpha = 0.35 + 0.5 * off;
      }
      ctx.fillRect(x + 1, y + 1, CELL - 2, CELL - 2);
      ctx.globalAlpha = 1;
    }
  }
  const [ci, cj] = scan.current;
  ctx.strokeStyle = c.fg;
  ctx.lineWidth = 2;
  ctx.strokeRect(LEFT + ci * CELL + 1, cj * CELL + 1, CELL - 2, CELL - 2);
  if (scan.best) {
    const bi = scan.widths.indexOf(scan.best.width);
    const bj = scan.heights.indexOf(scan.best.height);
    ctx.fillStyle = c.fg;
    ctx.beginPath();
    ctx.arc(LEFT + (bi + 0.5) * CELL, (bj + 0.5) * CELL, 4, 0, 2 * Math.PI);
    ctx.fill();
  }
  ctx.lineWidth = 1;
  ctx.fillStyle = c.muted;
  ctx.font = "10px system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  scan.heights.forEach((h, j) => {
    if (j % 2 === 0) ctx.fillText(String(Math.round(h)), LEFT - 4, (j + 0.5) * CELL);
  });
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  scan.widths.forEach((w, i) => {
    if (i % 2 === 0) ctx.fillText(String(Math.round(w)), LEFT + (i + 0.5) * CELL, rows * CELL + 4);
  });
  ctx.fillText("disc width (px) →  ·  height ↓", LEFT + (cols * CELL) / 2, rows * CELL + 18);

  return ([x, y]) => {
    const i = Math.floor((x - LEFT) / CELL);
    const j = Math.floor(y / CELL);
    if (i < 0 || j < 0 || i >= cols || j >= rows) return null;
    return { width: scan.widths[i], height: scan.heights[j] };
  };
}
