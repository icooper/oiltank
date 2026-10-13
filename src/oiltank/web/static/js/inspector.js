// The reading under the cursor: its strip (with the gauge's marks, where the reader put the disc,
// and the reader's score for every row), the nearest full frame, and its details.

import { getJSON } from "./api.js";
import { colors, image, peek, sized } from "./canvas.js";
import { discAngle, fit, mergeLabels } from "./geometry.js";
import { NONE, percent, reason } from "./format.js";

const STRIP_SCALE = 1.5;
const GUTTER = 58;
const SCORES = 56;
const FRAME_SIZE = 300;

const details = new Map();
const scores = new Map();

function remember(map, key, value, limit = 200) {
  map.set(key, value);
  if (map.size > limit) map.delete(map.keys().next().value);
  return value;
}

/** A record's details (cached; the archive never changes a record). */
export async function detail(day, key) {
  const id = `${day}/${key}`;
  if (!details.has(id)) {
    remember(details, id, getJSON(`api/archive/records/${day}/${key}`).then((r) => r.body));
  }
  try {
    return await details.get(id);
  } catch (error) {
    details.delete(id);
    throw error;
  }
}

/** Fetch a record's details and strip ahead of time, for smooth stepping. */
export function prefetch(day, key) {
  image(`archive/${day}/${key}-strip.png`);
  detail(day, key).catch(() => {});
}

const when = (iso) =>
  new Date(iso).toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });

export class Inspector {
  constructor(root) {
    this.root = root;
    this.strip = root.querySelector("#strip");
    this.frame = root.querySelector("#frame");
    this.frameCaption = root.querySelector("#frame-caption");
    this.facts = root.querySelector("#reading-facts");
    this.showing = null;
    this.restTimer = null;
  }

  /** Show reading `reading` (`{ ts, level, t, confidence, problem, accepted }`), with its
   * archive record (`{ day, key }`) if it has one. */
  show(reading, record) {
    const id = record ? `${record.day}/${record.key}` : `ts:${reading.ts}`;
    if (id === this.showing) return;
    this.showing = id;
    this.current = null;
    this.curve = null;
    this.stripUrl = record ? `archive/${record.day}/${record.key}-strip.png` : null;
    clearTimeout(this.restTimer);
    this.renderFacts(reading, null);
    this.redraw(id);
    if (!record) {
      this.frameCaption.textContent = "No archive record for this reading.";
      return;
    }
    const cached = details.has(id);
    detail(record.day, record.key)
      .then((d) => {
        if (this.showing !== id) return;
        this.current = d;
        this.renderFacts(reading, d);
        this.redraw(id);
        // The full frame and the scores wait until the cursor rests here.
        this.restTimer = setTimeout(() => this.settle(id, record), cached ? 150 : 250);
      })
      .catch((error) => {
        if (this.showing === id) this.frameCaption.textContent = `Couldn't load the record: ${error.message}`;
      });
  }

  settle(id, record) {
    if (this.showing !== id) return;
    if (!scores.has(id)) {
      const url = `api/archive/records/${record.day}/${record.key}/scores`;
      remember(scores, id, getJSON(url).then((r) => r.body));
    }
    scores
      .get(id)
      .then((curve) => {
        if (this.showing !== id) return;
        this.curve = curve;
        this.redraw(id);
      })
      .catch(() => scores.delete(id));
    const frame = this.current && this.current.frame;
    if (frame) image(frame.url, () => this.redraw(id), "frames");
    this.redraw(id);
  }

  redraw(id) {
    if (this.showing !== id) return;
    const img = this.stripUrl ? image(this.stripUrl, () => this.redraw(id)) : null;
    this.drawStrip(img, this.current, this.curve);
    this.drawFrame(this.current ? this.current.frame : null, Boolean(this.current));
  }

  drawStrip(img, d, curve) {
    const h = Math.round(256 * STRIP_SCALE);
    const w = Math.round(128 * STRIP_SCALE);
    const ctx = sized(this.strip, GUTTER + w + 8 + SCORES, h);
    const c = colors();
    ctx.fillStyle = c.empty;
    ctx.fillRect(GUTTER, 0, w, h);
    if (img) {
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(img, GUTTER, 0, w, h);
    }
    if (!d) return;
    const y = (row) => (row + 0.5) * STRIP_SCALE;
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.setLineDash([4, 3]);
    const marks = d.ticks
      .filter((tick) => tick.row != null && tick.row >= 0 && tick.row <= d.rows - 1)
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
    for (const f of d.frames) {
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
        const x0 = GUTTER + w + 8;
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

  drawFrame(frame, known) {
    const ctx = sized(this.frame, FRAME_SIZE, FRAME_SIZE);
    const c = colors();
    ctx.fillStyle = c.empty;
    ctx.fillRect(0, 0, FRAME_SIZE, FRAME_SIZE);
    if (!frame) {
      if (known) this.frameCaption.textContent = "No full frame within two days.";
      return;
    }
    const minutes = Math.round(Math.abs(frame.age_s) / 60);
    const away = minutes < 1 ? "" : minutes < 120 ? `${minutes} min` : `${Math.round(minutes / 60)} h`;
    this.frameCaption.textContent = !away
      ? "The full frame of this reading."
      : `The nearest full frame, ${away} ${frame.age_s < 0 ? "earlier" : "later"}.`;
    const img = peek(frame.url, "frames");
    if (!img) {
      ctx.fillStyle = c.muted;
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Loading the frame…", FRAME_SIZE / 2, FRAME_SIZE / 2);
      return;
    }
    const [x, y, w, h] = frame.crop;
    const view = fit(frame.crop, FRAME_SIZE, FRAME_SIZE);
    const [dx, dy] = view.toCanvas([x, y]);
    ctx.drawImage(img, x, y, w, h, dx, dy, w * view.scale, h * view.scale);
    const [tx, ty] = view.toCanvas(frame.axis.top);
    const [bx, by] = view.toCanvas(frame.axis.bottom);
    ctx.strokeStyle = c.mark;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(tx, ty);
    ctx.lineTo(bx, by);
    ctx.stroke();
    ctx.setLineDash([]);
    if (frame.disc) {
      const [cx, cy] = view.toCanvas(frame.disc);
      ctx.strokeStyle = c.accent;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.ellipse(
        cx, cy,
        (frame.disc_size[0] / 2) * view.scale, (frame.disc_size[1] / 2) * view.scale,
        discAngle(frame.axis.top, frame.axis.bottom), 0, 2 * Math.PI,
      );
      ctx.stroke();
      ctx.lineWidth = 1;
    }
  }

  renderFacts(reading, d) {
    const rows = [
      ["Time", when(new Date(reading.ts * 1000).toISOString())],
      ["Level", percent(reading.level)],
      ["Gauge position", reading.t == null ? NONE : reading.t.toFixed(3)],
      ["Confidence", reading.confidence == null ? NONE : reading.confidence.toFixed(2)],
    ];
    if (reading.problem) rows.push(["Problem", reason(reading.problem)]);
    else if (!reading.accepted) rows.push(["Filter", "rejected (low confidence)"]);
    if (d) {
      const strip = d.strip_frame != null ? d.frames[d.strip_frame] : null;
      const a = strip && strip.alignment;
      if (a) rows.push(["Camera shift", `${a.dx.toFixed(1)}, ${a.dy.toFixed(1)} px (match ${a.score.toFixed(2)})`]);
      rows.push(["Frames", String(d.frames.length)]);
      if (d.notices.length) rows.push(["Notices", d.notices.map(reason).join(", ")]);
    }
    this.facts.replaceChildren(
      ...rows.map(([term, value]) => {
        const div = document.createElement("div");
        const dt = document.createElement("dt");
        const dd = document.createElement("dd");
        dt.textContent = term;
        dd.textContent = value;
        div.append(dt, dd);
        return div;
      }),
    );
  }
}
