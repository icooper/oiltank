// The Calibration view: the points (to change or remove), a point from a delivery, a preview of
// the last month with the changes, and the bar to save or discard them.

import { getJSON } from "./api.js";
import { colors } from "./canvas.js";
import { drawChart, PLOT } from "./chart.js";
import { draft } from "./draft.js";
import { DAY, TimeScale } from "./timescale.js";

const PREVIEW_DAYS = 30;

function cell(...children) {
  const td = document.createElement("td");
  td.append(...children);
  return td;
}

const pct = (fraction) => Math.round(fraction * 1000) / 10;

export class CalibrationView {
  constructor(root) {
    this.root = root;
    this.q = (sel) => root.querySelector(sel);
    this.previewTimer = null;
    draft.addEventListener("change", () => {
      if (!this.root.hidden) this.render();
    });
    this.q("#delivery-form").addEventListener("submit", (e) => this.addDelivery(e));
    new ResizeObserver(() => {
      if (!this.root.hidden && this.previewData) this.drawPreview();
    }).observe(this.q("#preview-plot"));
  }

  async open() {
    try {
      if (!draft.calibration) await draft.load();
    } catch (error) {
      this.q("#points-message").textContent = `Couldn't load the calibration: ${error.message}`;
      return;
    }
    this.render();
  }

  render() {
    const cal = draft.calibration;
    if (!cal) return;
    const readOnly = draft.readOnly;
    this.q("#cal-read-only").hidden = !readOnly;
    const warnings = this.q("#cal-warnings");
    warnings.replaceChildren(...cal.warnings.map((w) => Object.assign(document.createElement("li"), { textContent: w })));
    warnings.hidden = cal.warnings.length === 0;

    const body = this.q("#points-body");
    const points = draft.points || [];
    body.replaceChildren(
      ...points.map((point, i) => {
        const row = document.createElement("tr");
        const level = Object.assign(document.createElement("input"), {
          type: "number", min: 0, max: 100, step: 0.1, value: pct(point.fraction), disabled: readOnly,
        });
        level.setAttribute("aria-label", "Reading, percent");
        level.addEventListener("change", () => this.edit(i, { fraction: Number(level.value) / 100 }));
        const note = Object.assign(document.createElement("input"), { type: "text", value: point.note || "", disabled: readOnly });
        note.setAttribute("aria-label", "Note");
        note.addEventListener("change", () => this.edit(i, { note: note.value }));
        const where = draft.positions && draft.positions[i] != null ? `t = ${draft.positions[i].toFixed(3)}` : "";
        const remove = Object.assign(document.createElement("button"), { type: "button", className: "quiet", textContent: "Remove", hidden: readOnly });
        remove.addEventListener("click", () => {
          draft.set(points.filter((_, j) => j !== i), draft.positions && draft.positions.filter((_, j) => j !== i));
        });
        const percent = document.createElement("span");
        percent.append(level, " %");
        row.append(cell(percent), cell(where), cell(note), cell(remove));
        return row;
      }),
    );
    this.q("#points-empty").hidden = points.length > 0;
    this.q("#delivery-card").hidden = readOnly;
    const select = this.q("#fill-select");
    select.replaceChildren(
      ...cal.fills.map((f) => {
        const when = new Date(f.ts).toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
        return Object.assign(document.createElement("option"), {
          value: f.ts, textContent: `${when}: ${Math.round(f.before)}% → ${Math.round(f.after)}% (about ${Math.round(f.volume)} ${cal.unit})`,
        });
      }),
    );
    this.q("#delivery-form").hidden = cal.fills.length === 0;
    this.q("#no-fills").hidden = cal.fills.length > 0;
    this.q("#delivered-unit").textContent = cal.unit;
    this.schedulePreview();
  }

  edit(i, change) {
    const points = draft.points.map((p, j) => (j === i ? { ...p, ...change } : p));
    draft.set(points, draft.positions);
  }

  async addDelivery(event) {
    event.preventDefault();
    const message = this.q("#delivery-message");
    try {
      const result = await draft.fromFill(this.q("#fill-select").value, Number(this.q("#delivered").value), this.q("#delivery-note").value);
      message.textContent = `Added a ${result.after.toFixed(1)}% point (${result.before.toFixed(1)}% before the delivery). Save it below.`;
    } catch (error) {
      message.textContent = error.message;
    }
  }

  schedulePreview() {
    clearTimeout(this.previewTimer);
    this.previewTimer = setTimeout(() => this.preview(), 300);
  }

  async preview() {
    const end = new Date();
    const start = new Date(end.getTime() - PREVIEW_DAYS * DAY * 1000);
    const range = new URLSearchParams({ start: start.toISOString(), end: end.toISOString() });
    const message = this.q("#preview-message");
    try {
      const [now, changed] = await Promise.all([
        getJSON(`api/readings?${range}`).then((r) => r.body),
        draft.dirty ? draft.preview(start.toISOString(), end.toISOString()) : null,
      ]);
      this.previewData = { now, changed, start: start / 1000, end: end / 1000 };
      message.textContent = now.readings.ts.length ? "" : "No readings in the last 30 days.";
      this.drawPreview();
    } catch (error) {
      message.textContent = error.message;
    }
  }

  drawPreview() {
    const { now, changed, start, end } = this.previewData;
    const plot = this.q("#preview-plot");
    const width = Math.max(200, plot.clientWidth);
    const scale = new TimeScale(start, end, PLOT.left, width - PLOT.left - PLOT.right);
    drawChart(this.q("#preview-chart"), width, 200, scale, changed || now, colors(), { before: changed ? now : null });
  }
}

/** The bar that says there are unsaved changes, on every view, with Save and Discard. */
export function wireDraftBar(onSaved) {
  const bar = document.getElementById("draft-bar");
  const message = document.getElementById("draft-message");
  const update = () => {
    bar.hidden = !draft.dirty || draft.readOnly;
    if (bar.hidden) message.textContent = "";
  };
  draft.addEventListener("change", update);
  document.getElementById("draft-discard").addEventListener("click", () => draft.discard());
  document.getElementById("draft-save").addEventListener("click", async () => {
    message.textContent = "Saving…";
    try {
      await draft.save(document.getElementById("read-after").checked);
      message.textContent = "";
      onSaved();
    } catch (error) {
      message.textContent = error.status === 412 ? `${error.message}.` : `Not saved: ${error.message}`;
    }
  });
  update();
}
