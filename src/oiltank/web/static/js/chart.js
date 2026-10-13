// The level chart: readings (opacity by confidence, hollow if rejected), the filtered level,
// refills, and problems along the bottom.

import { sized } from "./canvas.js";
import { ticks } from "./timescale.js";

export const PLOT = { left: 46, right: 10 };
const TOP = 10;
const BOTTOM = 24;

export function drawChart(canvas, width, height, scale, series, c) {
  const ctx = sized(canvas, width, height);
  const top = TOP;
  const bottom = height - BOTTOM;
  const y = (level) => bottom - (level / 100) * (bottom - top);
  const right = scale.left + scale.width;
  ctx.font = "12px system-ui, sans-serif";

  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (const level of [0, 25, 50, 75, 100]) {
    ctx.strokeStyle = c.grid;
    ctx.beginPath();
    ctx.moveTo(scale.left, Math.round(y(level)) + 0.5);
    ctx.lineTo(right, Math.round(y(level)) + 0.5);
    ctx.stroke();
    ctx.fillStyle = c.muted;
    ctx.fillText(`${level}%`, scale.left - 6, y(level));
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (const tick of ticks(scale, 90)) {
    const x = Math.round(scale.x(tick.t)) + 0.5;
    ctx.strokeStyle = c.grid;
    ctx.globalAlpha = tick.major ? 1 : 0.5;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = tick.major ? c.fg : c.muted;
    ctx.fillText(tick.label, x, bottom + 6);
  }
  if (!series) return;

  const r = series.readings;
  ctx.save();
  ctx.beginPath();
  ctx.rect(scale.left, 0, scale.width, height);
  ctx.clip();

  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  for (const fill of series.fills) {
    const x = scale.x(fill.ts);
    ctx.fillStyle = c.ok;
    ctx.globalAlpha = 0.25;
    ctx.fillRect(x - 2, top, 4, bottom - top);
    ctx.globalAlpha = 1;
    const label = `+${Math.round(fill.volume)} ${series.unit}`;
    const w = ctx.measureText(label).width;
    ctx.fillText(label, x + 5 + w > right ? x - 5 - w : x + 5, bottom - 18);
  }

  ctx.strokeStyle = c.accent;
  ctx.lineWidth = 2;
  ctx.beginPath();
  let pen = false;
  for (let i = 0; i < r.ts.length; i += 1) {
    const level = r.smoothed[i];
    if (level == null) {
      pen = false;
      continue;
    }
    const x = scale.x(r.ts[i]);
    if (pen) ctx.lineTo(x, y(level));
    else ctx.moveTo(x, y(level));
    pen = true;
  }
  ctx.stroke();

  const dense = r.ts.length > 1 && scale.width / r.ts.length < 3;
  const radius = dense ? 1.5 : 2.5;
  ctx.lineWidth = 1;
  for (let i = 0; i < r.ts.length; i += 1) {
    const x = scale.x(r.ts[i]);
    if (r.problem[i] != null) {
      ctx.fillStyle = c.bad;
      ctx.globalAlpha = 1;
      ctx.fillRect(x - 1, bottom - 8, 2, 8);
    }
    const level = r.level[i];
    if (level == null) continue;
    ctx.globalAlpha = 0.25 + 0.75 * Math.max(0, Math.min(1, r.confidence[i]));
    ctx.beginPath();
    ctx.arc(x, y(level), radius, 0, 2 * Math.PI);
    if (r.accepted[i]) {
      ctx.fillStyle = c.fg;
      ctx.fill();
    } else {
      ctx.strokeStyle = c.warn;
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  ctx.restore();
}

/** The chart's vertical extent, for drawing the cursor's value. */
export function levelY(height, level) {
  const bottom = height - BOTTOM;
  return bottom - (level / 100) * (bottom - TOP);
}
