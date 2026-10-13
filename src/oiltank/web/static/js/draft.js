// The calibration as you're changing it: its points and its geometry (axis, disc, anchor). One
// draft for the whole page, kept until it's saved or discarded (and across a reload, in this tab).
// The server works out new points and checks the draft; this keeps it.

import { ApiError, getJSON, postJSON } from "./api.js";

const KEY = "oiltank-draft";

/** Whether two point lists are the same (points are `{ fraction, at | t, note }`). */
export function samePoints(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

const GEOMETRY = ["axis", "disc", "anchor"];

/** The geometry part of a calibration: `{ axis, disc, anchor }`. */
export function geometryOf(calibration) {
  return Object.fromEntries(GEOMETRY.map((k) => [k, calibration[k]]));
}

async function send(method, url, body, headers = {}) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, payload.detail);
  return payload;
}

class Draft extends EventTarget {
  constructor() {
    super();
    this.calibration = null; // the saved calibration, as GET api/calibration gives it
    this.points = null;
    this.positions = null;
    this.geometry = null;
  }

  get dirty() {
    return Boolean(this.calibration && this.points) && (this.pointsChanged || this.geometryChanged);
  }

  get pointsChanged() {
    return Boolean(this.calibration && this.points && !samePoints(this.points, this.calibration.points));
  }

  get geometryChanged() {
    return Boolean(this.calibration && this.geometry && !samePoints(this.geometry, geometryOf(this.calibration)));
  }

  /** The draft as the server takes it. */
  get body() {
    return { points: this.points, ...this.geometry };
  }

  get readOnly() {
    return Boolean(this.calibration && this.calibration.read_only);
  }

  /** Fetch the saved calibration; keep a draft made against it, drop one made against another. */
  async load() {
    const result = await getJSON("api/calibration");
    this.calibration = result.body;
    let kept = null;
    try {
      const stored = JSON.parse(sessionStorage.getItem(KEY) || "null");
      if (stored && stored.etag === this.calibration.etag) kept = stored;
    } catch {
      kept = null;
    }
    this.points = kept ? kept.points : this.calibration.points;
    this.positions = kept ? kept.positions : this.calibration.positions;
    this.geometry = kept && kept.geometry ? kept.geometry : geometryOf(this.calibration);
    this.changed();
    return this.calibration;
  }

  set(points, positions = null) {
    this.points = points;
    this.positions = positions;
    this.keep();
  }

  /** Change some of the geometry: `{ axis }`, `{ disc }`, `{ anchor }` or several. */
  setGeometry(changes) {
    this.geometry = { ...this.geometry, ...changes };
    this.keep();
  }

  keep() {
    try {
      if (this.dirty) {
        const { points, positions, geometry } = this;
        sessionStorage.setItem(KEY, JSON.stringify({ etag: this.calibration.etag, points, positions, geometry }));
      } else {
        sessionStorage.removeItem(KEY);
      }
    } catch {
      // private browsing and the like: the draft just won't survive a reload
    }
    this.changed();
  }

  discard() {
    this.geometry = geometryOf(this.calibration);
    this.set(this.calibration.points, this.calibration.positions);
  }

  /** A point where the disc was at the reading at `ts` (Unix seconds), read as `fraction`. */
  async fromReading(ts, fraction, note) {
    const body = await postJSON("api/calibration/points/from-reading", {
      ...this.body, ts: new Date(ts * 1000).toISOString(), fraction, note,
    });
    this.set(body.points, body.positions);
    return body;
  }

  /** A point after the refill at `fillTs` (ISO), from the amount on its ticket. */
  async fromFill(fillTs, delivered, note) {
    const body = await postJSON("api/calibration/points/from-fill", {
      ...this.body, fill_ts: fillTs, delivered, note,
    });
    this.set(body.points, body.positions);
    return body;
  }

  /** The readings over `start`..`end` (ISO) as they'd be with the draft. */
  preview(start, end) {
    return postJSON("api/calibration/preview", { ...this.body, start, end });
  }

  /** What the draft's geometry makes of `frames` (FrameRefs): the reading, the anchor's
   * self-test, warnings, and where the gauge's marks are. */
  check(frames) {
    return postJSON("api/calibration/check", { ...this.body, frames });
  }

  /** The disc-size scan around the draft's size, on `frames`. */
  scan(frames) {
    return postJSON("api/calibration/scan", { ...this.body, frames });
  }

  /** Save the draft as the calibration (and take a reading with it, if `readNow`). */
  async save(readNow) {
    return this.put({ ...this.body, read_now: readNow });
  }

  /** Save a rebase (as `api/calibration/rebase` worked it out), onto `frame`. Only with no other
   * changes: readings can't follow a rebase and an axis edit at once. */
  async saveRebase(rebase, readNow) {
    if (this.dirty) throw new Error("save or discard your other changes first");
    const { points, axis, disc, anchor } = rebase;
    return this.put({ points, axis, disc, anchor, reference: rebase.frame, read_now: readNow });
  }

  async put(body) {
    const saved = await send("PUT", "api/calibration", body, { "If-Match": `"${this.calibration.etag}"` });
    this.calibration = saved;
    this.geometry = geometryOf(saved);
    this.set(saved.points, saved.positions);
    this.dispatchEvent(new Event("saved"));
    return saved;
  }

  changed() {
    this.dispatchEvent(new Event("change"));
  }
}

export const draft = new Draft();
