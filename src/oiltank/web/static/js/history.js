// The History view: the level chart and the gauge over time on one time axis, a cursor to scrub
// through readings with, and the inspector for the reading under it.

import { getJSON } from "./api.js";
import { colors, sized } from "./canvas.js";
import { drawChart, levelY, PLOT } from "./chart.js";
import { percent } from "./format.js";
import { forget, Inspector, prefetch } from "./inspector.js";
import { around, nearestIndex, nextEvent, stepBy, stepDays, utcDay, utcDays } from "./series.js";
import { RANGES } from "./store.js";
import { drawStrips } from "./strips.js";
import { DAY, HOUR, TimeScale } from "./timescale.js";

const SHORTEST = HOUR;
const LONGEST = 2 * 365 * DAY;
const LIVE_SLACK = 10 * 60; // a view ending this close to now follows new readings
const REFRESH_MS = 60_000;
const SPEEDS = [2, 5, 10, 25];

const now = () => Date.now() / 1000;
const isLive = (scale) => now() - scale.max < LIVE_SLACK;

export class HistoryView {
  /** `onState({ range, end, at })` is called as the view or cursor moves. */
  constructor(root, { card = false, onState = () => {} } = {}) {
    this.root = root;
    this.card = card;
    this.onState = onState;
    this.plots = root.querySelector("#plots");
    this.chart = root.querySelector("#chart");
    this.strips = root.querySelector("#strips");
    this.overlay = root.querySelector("#overlay");
    this.status = root.querySelector("#history-status");
    this.inspector = new Inspector(root.querySelector("#inspector"));
    this.series = null;
    this.loaded = null; // [start, end] of this.series
    this.days = new Map(); // UTC day -> listing
    this.pending = new Map(); // UTC day -> promise
    this.cursor = -1;
    this.range = "7d";
    this.playing = null;
    this.etag = null;
    this.pointers = new Map();
    this.width = 0;
    this.wire();
  }

  /** Show `range` ending at `end` (null: now), with the cursor at `at` (null: the latest). */
  open({ range, end, at }) {
    this.range = range;
    const max = end ?? now();
    this.scale = new TimeScale(max - RANGES[range], max, PLOT.left, 100);
    this.wantAt = at;
    this.markRange();
    this.layout();
    this.render(); // with what's loaded: coming back to the view may need no new data
    this.load();
  }

  /** Fetch everything again: the calibration changed, so every level did. */
  reset() {
    this.loaded = null;
    this.etag = null;
    this.days.clear();
    this.inspector.showing = null;
    forget();
    if (this.scale) this.load();
  }

  // ---- data -----------------------------------------------------------------------------------

  async load() {
    const span = this.scale.span;
    const start = this.scale.min - span / 2;
    const end = isLive(this.scale) ? now() + 60 : this.scale.max + span / 2;
    const covered = this.loaded && this.loaded[0] <= this.scale.min && this.loaded[1] >= Math.min(this.scale.max, now());
    if (covered && this.loaded[1] - this.loaded[0] < 6 * span) return;
    const ticket = (this.ticket = Symbol("load"));
    this.status.textContent = "Loading…";
    try {
      const qs = new URLSearchParams({
        start: new Date(start * 1000).toISOString(),
        end: new Date(Math.min(end, start + LONGEST) * 1000).toISOString(),
      });
      const result = await getJSON(`api/readings?${qs}`);
      if (ticket !== this.ticket) return;
      this.useSeries(result.body, [start, end]);
    } catch (error) {
      if (ticket === this.ticket) this.status.textContent = `Couldn't load readings: ${error.message}`;
    }
  }

  useSeries(series, range) {
    const old = this.series && this.cursor >= 0 ? this.series.readings.ts[this.cursor] : null;
    this.series = series;
    this.loaded = range;
    const ts = series.readings.ts;
    this.status.textContent = ts.length ? "" : "No readings in this period.";
    const at = this.wantAt ?? old;
    this.wantAt = null;
    this.cursor = ts.length ? (at == null ? ts.length - 1 : nearestIndex(ts, at)) : -1;
    this.render();
    this.moved(false);
  }

  /** Re-fetch the readings when following now (an unchanged series costs a 304). */
  async refresh() {
    if (!this.series || !isLive(this.scale) || document.visibilityState !== "visible") return;
    const qs = new URLSearchParams({
      start: new Date(this.loaded[0] * 1000).toISOString(),
      end: new Date((now() + 60) * 1000).toISOString(),
    });
    try {
      const result = await getJSON(`api/readings?${qs}`, this.etag);
      this.etag = result.etag;
      if (!result.changed) return;
      const atEnd = this.cursor === this.series.readings.ts.length - 1;
      const max = now();
      this.scale = new TimeScale(max - this.scale.span, max, this.scale.left, this.scale.width);
      this.days.clear(); // today's tile has a new column
      this.useSeries(result.body, [this.loaded[0], max + 60]);
      if (atEnd) this.go(this.series.readings.ts.length - 1);
    } catch {
      // the next refresh tries again
    }
  }

  /** The UTC days in view, fetched once each (today's again on refresh). */
  ensureDays() {
    for (const day of utcDays(this.scale.min, this.scale.max)) {
      if (this.days.has(day) || this.pending.has(day)) continue;
      const p = getJSON(`api/archive/days/${day}`)
        .then((r) => {
          this.days.set(day, r.body);
          this.renderStrips();
          if (this.cursor >= 0 && utcDay(this.series.readings.ts[this.cursor]) === day) this.inspect();
        })
        .catch(() => this.days.set(day, { records: [], tile: null }))
        .finally(() => this.pending.delete(day));
      this.pending.set(day, p);
    }
    for (const day of this.days.keys()) {
      if (day < utcDay(this.scale.min - 7 * DAY) || day > utcDay(this.scale.max + 7 * DAY)) this.days.delete(day);
    }
  }

  /** The archive record of reading `i`, if its day has been listed and it has one. */
  recordOf(i) {
    const t = this.series.readings.ts[i];
    const day = utcDay(t);
    const listing = this.days.get(day);
    const r = listing && listing.records.find((x) => x.ts === t);
    return r ? { day, key: r.key } : null;
  }

  // ---- drawing --------------------------------------------------------------------------------

  layout() {
    const width = Math.max(200, this.plots.clientWidth);
    this.width = width;
    this.chartHeight = this.card ? 150 : 220;
    this.stripsHeight = this.card ? 96 : 150;
    this.scale = this.scale.placed(PLOT.left, width - PLOT.left - PLOT.right);
  }

  render() {
    this.c = colors();
    drawChart(this.chart, this.width, this.chartHeight, this.scale, this.series, this.c);
    this.renderStrips();
    this.ensureDays();
    this.renderCursor();
  }

  renderStrips() {
    drawStrips(
      this.strips, this.width, this.stripsHeight, this.scale, this.days, this.series,
      this.series && this.series.scale, this.c || colors(), () => this.renderStrips(),
    );
  }

  renderCursor() {
    const gap = 6;
    const height = this.chartHeight + gap + this.stripsHeight;
    const ctx = sized(this.overlay, this.width, height);
    if (!this.series || this.cursor < 0) return;
    const r = this.series.readings;
    const x = Math.round(this.scale.x(r.ts[this.cursor])) + 0.5;
    if (x < this.scale.left || x > this.scale.left + this.scale.width) return;
    const c = this.c || colors();
    ctx.strokeStyle = c.fg;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, 4);
    ctx.lineTo(x, height);
    ctx.stroke();
    const level = r.level[this.cursor] ?? r.smoothed[this.cursor];
    if (level != null) {
      ctx.fillStyle = c.accent;
      ctx.beginPath();
      ctx.arc(x, levelY(this.chartHeight, level), 4, 0, 2 * Math.PI);
      ctx.fill();
    }
    const when = new Date(r.ts[this.cursor] * 1000).toLocaleString(undefined, {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    });
    const label = `${percent(r.level[this.cursor])} · ${when}`;
    ctx.font = "12px system-ui, sans-serif";
    const w = ctx.measureText(label).width + 12;
    const lx = Math.min(Math.max(x - w / 2, this.scale.left), this.scale.left + this.scale.width - w);
    ctx.fillStyle = c.card;
    ctx.strokeStyle = c.grid;
    ctx.fillRect(lx, 0, w, 18);
    ctx.strokeRect(lx + 0.5, 0.5, w - 1, 17);
    ctx.fillStyle = c.fg;
    ctx.textBaseline = "middle";
    ctx.fillText(label, lx + 6, 9);
  }

  // ---- the cursor -----------------------------------------------------------------------------

  go(i, { follow = true } = {}) {
    if (!this.series || i < 0) return;
    this.cursor = i;
    if (this.noted) this.note("");
    const t = this.series.readings.ts[i];
    if (follow && (t < this.scale.min || t > this.scale.max)) {
      this.scale = this.scale.pan(t - (this.scale.min + this.scale.max) / 2);
      this.render();
      this.load();
    } else {
      this.renderCursor();
    }
    this.moved(true);
  }

  moved(byUser) {
    if (!this.series || this.cursor < 0) {
      this.reportState();
      return;
    }
    this.inspect();
    const ts = this.series.readings.ts;
    for (const j of around(this.cursor, ts.length, 5)) {
      if (j === this.cursor) continue;
      const rec = this.recordOf(j);
      if (rec) prefetch(rec.day, rec.key);
    }
    if (byUser) this.reportState();
  }

  inspect() {
    const r = this.series.readings;
    const i = this.cursor;
    const reading = {
      ts: r.ts[i], level: r.level[i], t: r.t[i], confidence: r.confidence[i],
      problem: r.problem[i], accepted: r.accepted[i],
    };
    this.inspector.show(reading, this.recordOf(i));
  }

  reportState() {
    clearTimeout(this.stateTimer);
    this.stateTimer = setTimeout(() => {
      const at = this.series && this.cursor >= 0 ? this.series.readings.ts[this.cursor] : null;
      this.onState({ range: this.range, end: isLive(this.scale) ? null : this.scale.max, at });
    }, 200);
  }

  // ---- input ----------------------------------------------------------------------------------

  wire() {
    const q = (sel) => this.root.querySelector(sel);
    for (const button of this.root.querySelectorAll("[data-range]")) {
      button.addEventListener("click", () => this.setRange(button.dataset.range));
    }
    q("#pan-back").addEventListener("click", () => this.panBy(-0.8));
    q("#pan-fwd").addEventListener("click", () => this.panBy(0.8));
    q("#pan-now").addEventListener("click", () => this.open({ range: this.range, end: null, at: null }));
    q("#play").addEventListener("click", () => this.togglePlay());
    q("#prev-event").addEventListener("click", () => this.jump(-1));
    q("#next-event").addEventListener("click", () => this.jump(1));

    const o = this.overlay;
    o.addEventListener("pointerdown", (e) => {
      o.setPointerCapture(e.pointerId);
      this.pointers.set(e.pointerId, e.clientX);
      if (this.pointers.size === 1) this.scrubTo(e.clientX);
      else this.pinchFrom = this.pinch();
      this.plots.focus({ preventScroll: true });
    });
    o.addEventListener("pointermove", (e) => {
      if (!this.pointers.has(e.pointerId)) return;
      this.pointers.set(e.pointerId, e.clientX);
      if (this.pointers.size === 1) this.scrubTo(e.clientX);
      else this.pinchMove();
    });
    const lift = (e) => {
      this.pointers.delete(e.pointerId);
      if (this.pointers.size < 2 && this.pinchFrom) {
        this.pinchFrom = null;
        this.load();
      }
    };
    o.addEventListener("pointerup", lift);
    o.addEventListener("pointercancel", lift);
    o.addEventListener("wheel", (e) => {
      e.preventDefault();
      const x = e.clientX - o.getBoundingClientRect().left;
      this.zoomBy(Math.exp(e.deltaY * 0.0015), this.scale.t(x));
    }, { passive: false });

    // Keys work anywhere in the view, except where a control needs them.
    document.addEventListener("keydown", (e) => {
      if (this.root.hidden) return;
      const tag = e.target.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      if (tag === "BUTTON" && (e.key === " " || e.key === "Enter")) return;
      this.key(e);
    });
    // Redraw on the next frame, and only for a new width: resizing the canvases inside the
    // observer's callback would set off another round of it.
    // Hidden (another view showing), the plots have no width: ignore that, and lay out again
    // when the view opens.
    new ResizeObserver(() => {
      cancelAnimationFrame(this.resizeFrame);
      this.resizeFrame = requestAnimationFrame(() => {
        const width = this.plots.clientWidth;
        if (!this.scale || this.root.hidden || width === 0) return;
        if (Math.max(200, width) === this.width) return;
        this.layout();
        this.render();
      });
    }).observe(this.plots);
    setInterval(() => this.refresh(), REFRESH_MS);
  }

  scrubTo(clientX) {
    if (!this.series || !this.series.readings.ts.length) return;
    const x = clientX - this.overlay.getBoundingClientRect().left;
    this.stopPlaying();
    this.go(nearestIndex(this.series.readings.ts, this.scale.t(x)), { follow: false });
  }

  pinch() {
    const [a, b] = [...this.pointers.values()];
    const left = this.overlay.getBoundingClientRect().left;
    return { scale: this.scale, distance: Math.max(10, Math.abs(a - b)), at: this.scale.t((a + b) / 2 - left) };
  }

  pinchMove() {
    if (!this.pinchFrom) return;
    const [a, b] = [...this.pointers.values()];
    const factor = this.pinchFrom.distance / Math.max(10, Math.abs(a - b));
    this.scale = this.pinchFrom.scale.zoom(factor, this.pinchFrom.at).clamped(SHORTEST, LONGEST, now() + HOUR);
    this.render();
  }

  zoomBy(factor, at) {
    this.scale = this.scale.zoom(factor, at).clamped(SHORTEST, LONGEST, now() + HOUR);
    this.render();
    clearTimeout(this.loadTimer);
    this.loadTimer = setTimeout(() => this.load(), 250);
    this.reportState();
  }

  panBy(fraction) {
    this.scale = this.scale.pan(fraction * this.scale.span).clamped(SHORTEST, LONGEST, now() + HOUR);
    this.render();
    this.load();
    this.reportState();
  }

  setRange(range) {
    this.range = range;
    const at = this.series && this.cursor >= 0 ? this.series.readings.ts[this.cursor] : null;
    const max = isLive(this.scale) ? now() : this.scale.max;
    this.scale = new TimeScale(max - RANGES[range], max, this.scale.left, this.scale.width);
    this.wantAt = at;
    this.markRange();
    this.render();
    this.load();
    this.reportState();
  }

  markRange() {
    for (const b of this.root.querySelectorAll("[data-range]")) {
      b.setAttribute("aria-pressed", String(b.dataset.range === this.range));
    }
  }

  jump(dir) {
    if (!this.series) return;
    const kind = this.root.querySelector("#jump-kind").value;
    const j = nextEvent(this.series, Math.max(0, this.cursor), dir, kind);
    if (j >= 0) this.go(j);
    else this.note(`No ${dir < 0 ? "earlier" : "later"} ${kind === "fill" ? "refills" : kind === "problem" ? "problems" : "rejected readings"} in this period.`);
  }

  /** A message that goes when the cursor next moves. */
  note(text) {
    this.status.textContent = text;
    this.noted = Boolean(text);
  }

  key(e) {
    if (!this.series || e.altKey || e.ctrlKey || e.metaKey) return;
    const ts = this.series.readings.ts;
    switch (e.key) {
      case "ArrowLeft":
      case "ArrowRight": {
        const dir = e.key === "ArrowLeft" ? -1 : 1;
        this.stopPlaying();
        this.go(e.shiftKey ? stepDays(ts, this.cursor, dir) : stepBy(ts, this.cursor, dir));
        break;
      }
      case "Home":
        this.go(0);
        break;
      case "End":
        this.go(ts.length - 1);
        break;
      case " ":
        this.togglePlay();
        break;
      case "[":
        this.jump(-1);
        break;
      case "]":
        this.jump(1);
        break;
      default:
        return;
    }
    e.preventDefault();
  }

  togglePlay() {
    if (this.playing) {
      this.stopPlaying();
      return;
    }
    if (!this.series || !this.series.readings.ts.length) return;
    const last = () => this.series.readings.ts.length - 1;
    if (this.cursor >= last()) this.go(0);
    const speed = Number(this.root.querySelector("#speed").value) || SPEEDS[1];
    this.playing = setInterval(() => {
      if (this.cursor >= last()) this.stopPlaying();
      else this.go(this.cursor + 1);
    }, 1000 / speed);
    this.root.querySelector("#play").setAttribute("aria-pressed", "true");
    this.root.querySelector("#play").textContent = "Pause";
  }

  stopPlaying() {
    if (!this.playing) return;
    clearInterval(this.playing);
    this.playing = null;
    this.root.querySelector("#play").setAttribute("aria-pressed", "false");
    this.root.querySelector("#play").textContent = "Play";
  }
}
