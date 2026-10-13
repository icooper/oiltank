// A frame to edit the gauge's geometry on: zoom and pan around a 4K image, with the draft's
// geometry over it and handles to drag. Geometry is in reference-frame pixels; on a frame the
// camera has shifted, it's drawn moved by that shift so it sits where the gauge is.
//
// Events: "drag" while a handle moves and "change" when it's let go (`detail` is the geometry),
// and "fit" on a double-click.

import { colors, image, sized } from "./canvas.js";
import { discAngle, drag, handles, hit, mergeLabels, stripOutline, View } from "./geometry.js";

const ANCHOR = "#d53f8c";

export class Viewer extends EventTarget {
  constructor(canvas, { editable = true } = {}) {
    super();
    this.canvas = canvas;
    this.editable = editable;
    this.view = new View(0.25, 0, 0);
    this.geometry = null;
    this.result = null; // the last check of the draft on this frame
    this.url = null;
    this.width = 0;
    this.height = 0;
    this.pointers = new Map();
    this.wire();
  }

  /** The camera's shift on this frame (from the last check), which the geometry is drawn by. */
  get shift() {
    const f = this.result && this.result.reading && this.result.reading.frames[0];
    return f && f.dx != null ? [f.dx, f.dy] : [0, 0];
  }

  setImage(url) {
    this.url = url;
    image(url, () => this.render(), "frames");
    this.render();
  }

  setGeometry(geometry) {
    this.geometry = geometry;
    this.render();
  }

  setResult(result) {
    this.result = result;
    this.render();
  }

  resize(width, height) {
    this.width = width;
    this.height = height;
    this.render();
  }

  /** Show image rectangle `[x, y, w, h]`. */
  fit(rect) {
    if (this.width && this.height) this.view = View.fit(rect, this.width, this.height);
    this.render();
  }

  /** The area around the gauge: the strip and the anchor, with some room around them. */
  gaugeRect() {
    const g = this.geometry;
    const points = [...stripOutline(g.axis)];
    const [x, y, w, h] = g.anchor.rect;
    points.push([x, y], [x + w, y + h]);
    const xs = points.map((p) => p[0]);
    const ys = points.map((p) => p[1]);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const pad = 0.4 * Math.max(x1 - x0, y1 - y0);
    return [x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad];
  }

  render() {
    if (!this.width) return;
    const ctx = sized(this.canvas, this.width, this.height);
    const c = colors();
    ctx.fillStyle = c.empty;
    ctx.fillRect(0, 0, this.width, this.height);
    const img = this.url ? image(this.url, () => this.render(), "frames") : null;
    if (img) {
      const dpr = window.devicePixelRatio || 1;
      const { scale, x0, y0 } = this.view;
      ctx.save();
      ctx.setTransform(dpr * scale, 0, 0, dpr * scale, -dpr * scale * x0, -dpr * scale * y0);
      ctx.imageSmoothingEnabled = scale < 2;
      ctx.drawImage(img, 0, 0);
      ctx.restore();
    } else if (this.url) {
      ctx.fillStyle = c.muted;
      ctx.font = "13px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Loading the frame…", this.width / 2, this.height / 2);
    }
    if (this.geometry) this.drawGeometry(ctx, c);
    ctx.fillStyle = c.muted;
    ctx.font = "12px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    ctx.fillText(`${Math.round(this.view.scale * 100)}%`, this.width - 8, this.height - 6);
  }

  drawGeometry(ctx, c) {
    const [sx, sy] = this.shift;
    const at = (p) => this.view.toScreen([p[0] + sx, p[1] + sy]);
    const g = this.geometry;
    ctx.lineWidth = 1.5;

    // The strip the reader looks in, and the axis the disc travels.
    ctx.strokeStyle = c.mark;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    stripOutline(g.axis).forEach((p, i) => (i ? ctx.lineTo(...at(p)) : ctx.moveTo(...at(p))));
    ctx.closePath();
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.strokeStyle = c.accent;
    ctx.beginPath();
    ctx.moveTo(...at(g.axis.top));
    ctx.lineTo(...at(g.axis.bottom));
    ctx.stroke();

    // The gauge's marks along it.
    const marks = (this.result && this.result.marks) || [];
    ctx.font = "12px system-ui, sans-serif";
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    const half = g.axis.half_width * this.view.scale;
    const placed = [];
    for (const mark of marks) {
      if (!mark.at) continue;
      const [x, y] = at(mark.at);
      ctx.strokeStyle = c.mark;
      ctx.beginPath();
      ctx.moveTo(x - half, y);
      ctx.lineTo(x + half, y);
      ctx.stroke();
      placed.push({ x, y, label: mark.label });
    }
    ctx.fillStyle = c.mark;
    const left = placed.length ? Math.min(...placed.map((m) => m.x)) - half - 4 : 0;
    for (const m of mergeLabels(placed, 14)) ctx.fillText(m.label, left, m.y);

    // Where the reader found the disc, at the draft's disc size.
    const reading = this.result && this.result.reading;
    if (reading && reading.position) {
      const [x, y] = at(reading.position);
      ctx.strokeStyle = !reading.ok ? c.bad : reading.confidence < 0.8 ? c.warn : c.ok;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.ellipse(
        x, y, (g.disc.width / 2) * this.view.scale, (g.disc.height / 2) * this.view.scale,
        discAngle(g.axis.top, g.axis.bottom), 0, 2 * Math.PI,
      );
      ctx.stroke();
      ctx.lineWidth = 1.5;
    }

    // The anchor: the static patch that tells how far the camera has moved.
    const [x, y, w, h] = g.anchor.rect;
    const [ax, ay] = at([x, y]);
    ctx.strokeStyle = ANCHOR;
    ctx.strokeRect(ax, ay, w * this.view.scale, h * this.view.scale);
    ctx.fillStyle = ANCHOR;
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText("anchor", ax, ay - 3);

    if (!this.editable) return;
    for (const handle of handles(g)) {
      const [hx, hy] = at(handle.at);
      ctx.fillStyle = c.card;
      ctx.strokeStyle = handle.id.startsWith("anchor") ? ANCHOR : c.accent;
      ctx.lineWidth = 2;
      ctx.beginPath();
      if (handle.id === "anchor") {
        ctx.moveTo(hx - 7, hy);
        ctx.lineTo(hx + 7, hy);
        ctx.moveTo(hx, hy - 7);
        ctx.lineTo(hx, hy + 7);
        ctx.stroke();
      } else if (handle.id.startsWith("anchor")) {
        ctx.rect(hx - 5, hy - 5, 10, 10);
        ctx.fill();
        ctx.stroke();
      } else {
        ctx.arc(hx, hy, 6, 0, 2 * Math.PI);
        ctx.fill();
        ctx.stroke();
      }
    }
    ctx.lineWidth = 1;
  }

  // ---- input ------------------------------------------------------------------------------------

  wire() {
    const c = this.canvas;
    const point = (e) => {
      const r = c.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top];
    };
    const shifted = () => {
      const [sx, sy] = this.shift;
      return handles(this.geometry).map((h) => ({ id: h.id, at: [h.at[0] + sx, h.at[1] + sy] }));
    };
    c.addEventListener("pointerdown", (e) => {
      c.setPointerCapture(e.pointerId);
      this.pointers.set(e.pointerId, point(e));
      if (this.pointers.size === 2) {
        this.gesture = { kind: "pinch", view: this.view, distance: this.spread(), mid: this.middle() };
        return;
      }
      const at = point(e);
      const id = this.editable && this.geometry ? hit(shifted(), this.view, at) : null;
      this.gesture = id
        ? { kind: "handle", id, start: this.geometry, from: this.view.toImage(at) }
        : { kind: "pan", view: this.view, from: at };
    });
    c.addEventListener("pointermove", (e) => {
      const at = point(e);
      if (!this.pointers.has(e.pointerId)) {
        const over = this.editable && this.geometry && hit(shifted(), this.view, at);
        c.style.cursor = over ? "grab" : "move";
        return;
      }
      this.pointers.set(e.pointerId, at);
      const g = this.gesture;
      if (!g) return;
      if (g.kind === "pinch" && this.pointers.size === 2) {
        const factor = this.spread() / g.distance;
        this.view = g.view.zoomAt(factor, g.mid);
        this.render();
      } else if (g.kind === "pan") {
        this.view = g.view.panBy(at[0] - g.from[0], at[1] - g.from[1]);
        this.render();
      } else if (g.kind === "handle") {
        const [ix, iy] = this.view.toImage(at);
        this.geometry = drag(g.start, g.id, [ix - g.from[0], iy - g.from[1]]);
        this.render();
        this.dispatchEvent(new CustomEvent("drag", { detail: this.geometry }));
      }
    });
    const lift = (e) => {
      this.pointers.delete(e.pointerId);
      if (this.gesture && this.gesture.kind === "handle") {
        this.dispatchEvent(new CustomEvent("change", { detail: this.geometry }));
      }
      this.gesture = null;
    };
    c.addEventListener("pointerup", lift);
    c.addEventListener("pointercancel", lift);
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      this.view = this.view.zoomAt(Math.exp(-e.deltaY * 0.0015), point(e));
      this.render();
    }, { passive: false });
    c.addEventListener("dblclick", () => this.dispatchEvent(new Event("fit")));
  }

  spread() {
    const [a, b] = [...this.pointers.values()];
    return Math.max(10, Math.hypot(a[0] - b[0], a[1] - b[1]));
  }

  middle() {
    const [a, b] = [...this.pointers.values()];
    return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  }
}
