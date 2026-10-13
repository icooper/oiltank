// The calibration's points as you're changing them: one draft for the whole page, kept until it's
// saved or discarded (and across a reload, in this tab). The server works out new points and
// checks the draft; this keeps the list.

import { ApiError, getJSON, postJSON } from "./api.js";

const KEY = "oiltank-draft";

/** Whether two point lists are the same (points are `{ fraction, at | t, note }`). */
export function samePoints(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
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
  }

  get dirty() {
    return Boolean(this.calibration && this.points && !samePoints(this.points, this.calibration.points));
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
    this.changed();
    return this.calibration;
  }

  set(points, positions = null) {
    this.points = points;
    this.positions = positions;
    try {
      if (this.dirty) {
        sessionStorage.setItem(KEY, JSON.stringify({ etag: this.calibration.etag, points, positions }));
      } else {
        sessionStorage.removeItem(KEY);
      }
    } catch {
      // private browsing and the like: the draft just won't survive a reload
    }
    this.changed();
  }

  discard() {
    this.set(this.calibration.points, this.calibration.positions);
  }

  /** A point where the disc was at the reading at `ts` (Unix seconds), read as `fraction`. */
  async fromReading(ts, fraction, note) {
    const body = await postJSON("api/calibration/points/from-reading", {
      points: this.points, ts: new Date(ts * 1000).toISOString(), fraction, note,
    });
    this.set(body.points, body.positions);
    return body;
  }

  /** A point after the refill at `fillTs` (ISO), from the amount on its ticket. */
  async fromFill(fillTs, delivered, note) {
    const body = await postJSON("api/calibration/points/from-fill", {
      points: this.points, fill_ts: fillTs, delivered, note,
    });
    this.set(body.points, body.positions);
    return body;
  }

  /** The readings over `start`..`end` (ISO) as they'd be with the draft. */
  preview(start, end) {
    return postJSON("api/calibration/preview", { points: this.points, start, end });
  }

  /** Save the draft as the calibration (and take a reading with it, if `readNow`). */
  async save(readNow) {
    const saved = await send("PUT", "api/calibration", { points: this.points, read_now: readNow }, {
      "If-Match": `"${this.calibration.etag}"`,
    });
    this.calibration = saved;
    this.set(saved.points, saved.positions);
    this.dispatchEvent(new Event("saved"));
    return saved;
  }

  changed() {
    this.dispatchEvent(new Event("change"));
  }
}

export const draft = new Draft();
