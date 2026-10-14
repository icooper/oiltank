// The reading under the cursor: its strip (with the gauge's marks, where the reader put the disc,
// and the reader's score for every row), the nearest full frame, and its details.

import { getJSON } from "./api.js";
import { colors, image, peek, sized } from "./canvas.js";
import { draft } from "./draft.js";
import { discAngle, fit } from "./geometry.js";
import { NONE, percent, reason } from "./format.js";
import { drawStrip } from "./stripview.js";

const STRIP_SCALE = 1.5;
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

/** Forget what's been fetched (after the calibration changes, a record's level and marks do). */
export function forget() {
  details.clear();
  scores.clear();
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
    this.form = root.querySelector("#point-form");
    this.formMessage = root.querySelector("#point-message");
    this.showing = null;
    this.restTimer = null;
    this.reading = null;
    this.form.addEventListener("submit", (e) => this.addPoint(e));
    draft.addEventListener("change", () => this.showForm());
  }

  /** "This reading was …": offered for a reading that found the disc, if changes are allowed. */
  showForm() {
    const r = this.reading;
    this.form.hidden = !(r && r.t != null && r.level != null && draft.calibration && !draft.readOnly);
  }

  async addPoint(event) {
    event.preventDefault();
    const level = Number(this.form.querySelector("#point-level").value);
    const note = this.form.querySelector("#point-note").value;
    try {
      const result = await draft.fromReading(this.reading.ts, level / 100, note);
      const replaced = result.replaced.length ? ` (it replaces the ${percent(result.replaced[0].fraction * 100)} point)` : "";
      this.formMessage.textContent = `Added ${percent(level)} to your changes${replaced}. Check them under Calibration, then save.`;
    } catch (error) {
      this.formMessage.textContent = error.message;
    }
  }

  /** Show reading `reading` (`{ ts, level, t, confidence, problem, accepted }`), with its
   * archive record (`{ day, key }`) if it has one. */
  show(reading, record) {
    const id = record ? `${record.day}/${record.key}` : `ts:${reading.ts}`;
    if (id === this.showing) return;
    this.showing = id;
    this.reading = reading;
    this.formMessage.textContent = "";
    this.form.querySelector("#point-level").value = reading.level == null ? "" : reading.level.toFixed(1);
    this.showForm();
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
    drawStrip(this.strip, img, d, curve, colors(), STRIP_SCALE);
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
