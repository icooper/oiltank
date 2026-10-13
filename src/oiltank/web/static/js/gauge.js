// The Gauge view: where the gauge is in the frame (the axis the disc travels, the strip's width,
// the disc's size, the anchor), checked against a frame as you change it; and rebasing after the
// camera moved. Changes go into the shared draft, saved from the bar like the points.

import { getJSON, postJSON } from "./api.js";
import { colors } from "./canvas.js";
import { draft, geometryOf } from "./draft.js";
import { reason } from "./format.js";
import { drawHeatmap } from "./heatmap.js";
import { drawStrip } from "./stripview.js";
import { Viewer } from "./viewer.js";

const CHECK_MS = 250;
const STRIP_SCALE = 1.25; // fits the side panel
const REFERENCE = { kind: "reference" };

const replaced = (list, i, value) => list.map((v, j) => (j === i ? value : v));

/** The geometry inputs: id → [read from the geometry, the change for a new value]. */
const INPUTS = {
  "g-top-x": [(g) => g.axis.top[0], (g, v) => ({ axis: { ...g.axis, top: [v, g.axis.top[1]] } })],
  "g-top-y": [(g) => g.axis.top[1], (g, v) => ({ axis: { ...g.axis, top: [g.axis.top[0], v] } })],
  "g-bottom-x": [(g) => g.axis.bottom[0], (g, v) => ({ axis: { ...g.axis, bottom: [v, g.axis.bottom[1]] } })],
  "g-bottom-y": [(g) => g.axis.bottom[1], (g, v) => ({ axis: { ...g.axis, bottom: [g.axis.bottom[0], v] } })],
  "g-half": [(g) => g.axis.half_width, (g, v) => ({ axis: { ...g.axis, half_width: v } })],
  "g-margin": [(g) => g.axis.margin, (g, v) => ({ axis: { ...g.axis, margin: v } })],
  "g-disc-w": [(g) => g.disc.width, (g, v) => ({ disc: { ...g.disc, width: v } })],
  "g-disc-h": [(g) => g.disc.height, (g, v) => ({ disc: { ...g.disc, height: v } })],
  "g-anchor-x": [(g) => g.anchor.rect[0], (g, v) => ({ anchor: { ...g.anchor, rect: replaced(g.anchor.rect, 0, Math.round(v)) } })],
  "g-anchor-y": [(g) => g.anchor.rect[1], (g, v) => ({ anchor: { ...g.anchor, rect: replaced(g.anchor.rect, 1, Math.round(v)) } })],
  "g-anchor-w": [(g) => g.anchor.rect[2], (g, v) => ({ anchor: { ...g.anchor, rect: replaced(g.anchor.rect, 2, Math.round(v)) } })],
  "g-anchor-h": [(g) => g.anchor.rect[3], (g, v) => ({ anchor: { ...g.anchor, rect: replaced(g.anchor.rect, 3, Math.round(v)) } })],
  "g-search": [(g) => g.anchor.search, (g, v) => ({ anchor: { ...g.anchor, search: Math.round(v) } })],
  "g-min-score": [(g) => g.anchor.min_score, (g, v) => ({ anchor: { ...g.anchor, min_score: v } })],
};

/** A reading, in words: "reads 98.2% (position 0.820, confidence 1.00)". */
function readingText(r) {
  if (!r.ok) return `no reading (${reason(r.problem)}).`;
  return `reads ${r.level.toFixed(1)}% (position ${r.t.toFixed(3)}, confidence ${r.confidence.toFixed(2)}).`;
}

const when = (iso) =>
  new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

function line(text, tone = "") {
  const p = document.createElement("p");
  p.textContent = text;
  if (tone) p.dataset.tone = tone;
  return p;
}

export class GaugeView {
  constructor(root) {
    this.root = root;
    this.q = (sel) => root.querySelector(sel);
    this.viewer = new Viewer(this.q("#g-canvas"));
    this.options = [];
    this.grabs = [];
    this.frame = REFERENCE;
    this.generation = 0;
    this.fitted = false;
    this.rebaseResult = null;
    this.savedChecks = new Map(); // frame + saved calibration -> its check, to compare against
    this.wire();
  }

  wire() {
    this.viewer.addEventListener("drag", (e) => {
      this.dragging = true;
      draft.setGeometry(e.detail);
    });
    this.viewer.addEventListener("change", (e) => {
      this.dragging = false;
      draft.setGeometry(e.detail);
    });
    this.viewer.addEventListener("fit", () => this.fit());
    this.q("#g-fit").addEventListener("click", () => this.fit());
    this.q("#g-frame").addEventListener("change", (e) => this.select(this.options[Number(e.target.value)].ref));
    this.q("#g-grab").addEventListener("click", () => this.grab());
    this.q("#g-scan").addEventListener("click", () => this.scan());
    this.q("#g-rebase-find").addEventListener("click", () => this.findShift());
    this.q("#g-rebase-save").addEventListener("click", () => this.saveRebase());
    this.q("#g-reset").addEventListener("click", () => draft.setGeometry(geometryOf(draft.calibration)));
    this.q("#g-form").addEventListener("submit", (e) => e.preventDefault()); // Enter applies a field
    for (const [id, [, change]] of Object.entries(INPUTS)) {
      this.q(`#${id}`).addEventListener("change", (e) => {
        const value = Number(e.target.value);
        if (Number.isFinite(value)) draft.setGeometry(change(draft.geometry, value));
      });
    }
    draft.addEventListener("change", () => {
      if (this.root.hidden || !draft.geometry) return;
      if (!this.dragging) this.viewer.setGeometry(draft.geometry);
      this.showState();
      this.fillInputs();
      this.scheduleCheck();
    });
    draft.addEventListener("saved", () => {
      if (!this.root.hidden && !this.rebasing) this.loadFrames(); // the reference may be new
    });
    new ResizeObserver(() => this.layout()).observe(this.q("#g-stage"));
  }

  async open() {
    try {
      if (!draft.calibration) await draft.load();
    } catch (error) {
      this.q("#g-result").replaceChildren(line(`Couldn't load the calibration: ${error.message}`, "bad"));
      return;
    }
    const readOnly = draft.readOnly;
    this.viewer.editable = !readOnly;
    for (const input of this.root.querySelectorAll("#g-form input")) input.disabled = readOnly;
    this.q("#g-rebase-save").hidden = true;
    this.viewer.setGeometry(draft.geometry);
    this.showState();
    this.fillInputs();
    this.layout();
    await this.loadFrames();
  }

  layout() {
    const stage = this.q("#g-stage");
    if (this.root.hidden || stage.clientWidth === 0) return;
    this.viewer.resize(stage.clientWidth, stage.clientHeight);
    if (!this.fitted && draft.geometry) {
      this.fitted = true;
      this.fit();
    }
  }

  fit() {
    if (draft.geometry) this.viewer.fit(this.viewer.gaugeRect());
  }

  /** Say whether this is the saved geometry or a try-out, and draw the saved one faintly under
   * changes to compare. */
  showState() {
    const changed = draft.geometryChanged;
    let text;
    if (draft.readOnly) text = "This web UI is read-only: it shows the saved calibration.";
    else if (changed) {
      text = "Trying out changes: nothing is saved until you press Save at the bottom. Pick other frames to test them; the saved calibration's reading is shown alongside.";
    } else {
      text = "Showing the saved calibration. Drag the handles or type numbers to try changes; nothing is saved until you press Save.";
    }
    this.q("#g-state-text").textContent = text;
    this.q("#g-state").dataset.changed = String(changed);
    this.q("#g-reset").hidden = !changed;
    this.viewer.setSaved(changed ? geometryOf(draft.calibration) : null);
  }

  fillInputs() {
    const g = draft.geometry;
    if (!g) return;
    for (const [id, [read]] of Object.entries(INPUTS)) {
      const input = this.q(`#${id}`);
      if (document.activeElement !== input) input.value = String(read(g));
    }
  }

  // ---- frames -----------------------------------------------------------------------------------

  async loadFrames() {
    let frames;
    try {
      frames = (await getJSON("api/frames")).body;
    } catch (error) {
      this.q("#g-result").replaceChildren(line(`Couldn't list the frames: ${error.message}`, "bad"));
      return;
    }
    this.options = [];
    if (frames.reference) {
      this.options.push({
        ref: REFERENCE,
        label: `The reference frame (${frames.reference.name})`,
        url: `${frames.reference.url}?name=${encodeURIComponent(frames.reference.name)}`,
      });
    }
    for (const g of this.grabs) this.options.push(g);
    for (const f of frames.archive) {
      this.options.push({
        ref: { kind: "archive", day: f.day, key: f.key },
        label: `${when(f.ts)}${f.problem ? ` (${reason(f.problem)})` : ""}`,
        url: f.url,
      });
    }
    this.q("#g-grab").hidden = !frames.camera;
    const select = this.q("#g-frame");
    select.replaceChildren(
      ...this.options.map((o, i) => Object.assign(document.createElement("option"), { value: String(i), textContent: o.label })),
    );
    const index = this.options.findIndex((o) => JSON.stringify(o.ref) === JSON.stringify(this.frame));
    this.select(index >= 0 ? this.options[index].ref : REFERENCE);
  }

  select(ref) {
    this.frame = ref;
    const i = this.options.findIndex((o) => JSON.stringify(o.ref) === JSON.stringify(ref));
    if (i < 0) return;
    this.q("#g-frame").value = String(i);
    this.viewer.setResult(null);
    this.viewer.setImage(this.options[i].url);
    this.rebaseResult = null;
    this.q("#g-rebase").hidden = ref.kind === "reference";
    this.q("#g-rebase-text").textContent = "";
    this.q("#g-rebase-save").hidden = true;
    this.check();
  }

  async grab() {
    const button = this.q("#g-grab");
    button.disabled = true;
    button.textContent = "Grabbing…";
    try {
      const body = await postJSON("api/frames/grab", { frames: 3 });
      const fresh = body.frames.map((f, i) => ({
        ref: f.frame, label: `From the camera, ${when(f.ts)} (${i + 1} of ${body.frames.length})`, url: f.url,
      }));
      this.grabs = [...fresh, ...this.grabs].slice(0, 9);
      this.frame = fresh[0].ref;
      await this.loadFrames();
    } catch (error) {
      this.q("#g-result").replaceChildren(line(error.message, "bad"));
    } finally {
      button.disabled = false;
      button.textContent = "Grab from the camera";
    }
  }

  // ---- checking ---------------------------------------------------------------------------------

  scheduleCheck() {
    clearTimeout(this.checkTimer);
    this.checkTimer = setTimeout(() => this.check(), CHECK_MS);
  }

  async check() {
    if (!draft.geometry) return;
    const generation = (this.generation += 1);
    try {
      const [result, saved] = await Promise.all([
        draft.check([this.frame]),
        draft.geometryChanged ? this.savedCheck(this.frame) : null,
      ]);
      if (generation !== this.generation) return; // a newer check is on its way
      this.viewer.setResult(result.valid ? result : null);
      this.showResult(result, saved);
      this.showStrip(result.valid ? result : null, generation);
    } catch (error) {
      if (generation === this.generation) this.q("#g-result").replaceChildren(line(error.message, "bad"));
    }
  }

  /** The strip the reader got with the draft on this frame, and its score per row. */
  showStrip(result, generation) {
    const canvas = this.q("#g-strip");
    const c = colors();
    const strip = result && result.strip;
    if (!strip) {
      drawStrip(canvas, null, null, null, c, STRIP_SCALE);
      return;
    }
    const r = result.reading;
    const d = { rows: strip.rows, ticks: strip.ticks, frames: [], disc_row: strip.disc_row, problem: r.problem, level: r.level };
    const img = new Image();
    img.onload = () => {
      if (generation === this.generation) drawStrip(canvas, img, d, { scores: strip.scores }, c, STRIP_SCALE);
    };
    img.src = strip.png;
  }

  /** The saved calibration's check of `frame` (kept: it only changes when the saved one does). */
  savedCheck(frame) {
    const cal = draft.calibration;
    const key = `${cal.etag}:${JSON.stringify(frame)}`;
    if (!this.savedChecks.has(key)) {
      const body = { points: cal.points, ...geometryOf(cal), frames: [frame] };
      const pending = postJSON("api/calibration/check", body);
      pending.catch(() => this.savedChecks.delete(key));
      this.savedChecks.set(key, pending);
    }
    return this.savedChecks.get(key);
  }

  showResult(result, saved) {
    const out = [];
    if (!result.valid) {
      for (const e of result.errors) out.push(line(`Not a working calibration: ${e}`, "bad"));
      if (saved && saved.valid) out.push(line(`Saved calibration: ${readingText(saved.reading)}`));
      this.q("#g-result").replaceChildren(...out);
      return;
    }
    const r = result.reading;
    const tone = !r.ok ? "bad" : r.confidence < 0.8 ? "warn" : "ok";
    if (saved && saved.valid) {
      out.push(line(`Saved calibration: ${readingText(saved.reading)}`));
      out.push(line(`With your changes: ${readingText(r)}`, tone));
    } else {
      out.push(line(`On this frame: ${readingText(r)}`, tone));
    }
    const f = r.frames[0];
    if (f && f.dx != null && (Math.abs(f.dx) >= 0.5 || Math.abs(f.dy) >= 0.5)) {
      out.push(line(`The camera has moved ${f.dx.toFixed(1)}, ${f.dy.toFixed(1)} px since the reference frame (match ${f.score.toFixed(2)}).`));
    }
    const a = result.anchor;
    if (a.ok) out.push(line(`The anchor is good: contrast ${a.contrast}, finds known shifts to within ${a.max_error_px} px.`, "ok"));
    for (const p of a.problems) out.push(line(`Anchor: ${p}.`, "warn"));
    for (const w of result.warnings) out.push(line(`${w}.`, "warn"));
    this.q("#g-result").replaceChildren(...out);
  }

  async scan() {
    const button = this.q("#g-scan");
    const text = this.q("#g-scan-text");
    button.disabled = true;
    text.textContent = "Scanning disc sizes… (a few seconds)";
    try {
      const scan = await draft.scan([this.frame]);
      const pick = drawHeatmap(this.q("#g-heatmap"), scan, colors());
      this.q("#g-heatmap").onclick = (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        const size = pick([e.clientX - r.left, e.clientY - r.top]);
        if (size && !draft.readOnly) draft.setGeometry({ disc: size });
      };
      const parts = [];
      if (scan.best) parts.push(`The scan suggests ${scan.best.width} × ${scan.best.height} px (the dot).`);
      else parts.push("No disc size agrees with the current one; check the axis and the disc.");
      if (scan.at_edge) parts.push("Your size (ringed) is at the edge of the sizes that agree: a little smaller or larger reads elsewhere.");
      for (const alt of scan.alternatives) parts.push(`${alt.sizes} sizes read the disc somewhere else, at position ${alt.t.toFixed(3)}.`);
      text.textContent = `${parts.join(" ")} Click a cell to use that size.`;
    } catch (error) {
      text.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  // ---- rebase -----------------------------------------------------------------------------------

  async findShift() {
    const text = this.q("#g-rebase-text");
    text.textContent = "Looking for the anchor…";
    try {
      this.rebaseResult = await postJSON("api/calibration/rebase", { frame: this.frame });
      const { dx, dy, score } = this.rebaseResult.shift;
      text.textContent = `The camera has moved ${dx.toFixed(1)}, ${dy.toFixed(1)} px since the reference frame (match ${score.toFixed(2)}). Making this frame the reference moves the gauge's geometry with it and keeps your readings where they were. Unlike other changes here, it's saved straight away${draft.dirty ? ", so save or discard your other changes first" : ""}.`;
      this.q("#g-rebase-save").hidden = draft.readOnly;
      this.q("#g-rebase-save").disabled = draft.dirty;
    } catch (error) {
      text.textContent = error.message;
    }
  }

  async saveRebase() {
    const text = this.q("#g-rebase-text");
    this.rebasing = true;
    try {
      await draft.saveRebase(this.rebaseResult, true);
      this.frame = REFERENCE;
      await this.loadFrames();
      text.textContent = "Saved: that frame is now the reference.";
    } catch (error) {
      text.textContent = `Not saved: ${error.message}`;
    } finally {
      this.rebasing = false;
    }
  }
}
