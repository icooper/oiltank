// Time along the x axis, shared by the level chart and the gauge-over-time image, so the two line
// up exactly. Pure: no DOM. Times are Unix seconds; positions are CSS pixels.

export const MINUTE = 60;
export const HOUR = 3600;
export const DAY = 86400;

export class TimeScale {
  constructor(min, max, left, width) {
    this.min = min;
    this.max = max;
    this.left = left;
    this.width = width;
  }

  x(t) {
    return this.left + ((t - this.min) / (this.max - this.min)) * this.width;
  }

  t(x) {
    return this.min + ((x - this.left) / this.width) * (this.max - this.min);
  }

  get span() {
    return this.max - this.min;
  }

  /** Zoomed by `factor` (below 1 zooms in), keeping time `at` where it is on screen. */
  zoom(factor, at = (this.min + this.max) / 2) {
    return new TimeScale(at - (at - this.min) * factor, at + (this.max - at) * factor, this.left, this.width);
  }

  pan(seconds) {
    return new TimeScale(this.min + seconds, this.max + seconds, this.left, this.width);
  }

  /** The same times over a different stretch of screen. */
  placed(left, width) {
    return new TimeScale(this.min, this.max, left, width);
  }

  /** Kept between `shortest` and `longest` seconds wide, and ending no later than `latest`. */
  clamped(shortest, longest, latest) {
    let { min, max } = this;
    const mid = (min + max) / 2;
    const span = Math.min(longest, Math.max(shortest, max - min));
    min = mid - span / 2;
    max = mid + span / 2;
    if (latest != null && max > latest) {
      min -= max - latest;
      max = latest;
    }
    return new TimeScale(min, max, this.left, this.width);
  }
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const FIXED = [MINUTE, 5 * MINUTE, 15 * MINUTE, 30 * MINUTE, HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR];

const pad = (n) => String(n).padStart(2, "0");
const seconds = (date) => date.getTime() / 1000;
const dayLabel = (d) => `${d.getDate()} ${MONTHS[d.getMonth()]}`;

/**
 * Tick marks for `scale`, in local time, at least `spacing` px apart: `{ t, label, major }`.
 * Major ticks are day (or month, or year) boundaries.
 */
export function ticks(scale, spacing = 80) {
  const wanted = (spacing / scale.width) * scale.span;
  const out = [];
  const start = new Date(scale.min * 1000);
  const end = scale.max;
  const step = FIXED.find((s) => s >= wanted);
  if (step != null) {
    const day = new Date(start.getFullYear(), start.getMonth(), start.getDate());
    for (; seconds(day) <= end; day.setDate(day.getDate() + 1)) {
      for (let s = 0; s < DAY; s += step) {
        const d = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 0, 0, s);
        const t = seconds(d);
        if (t < scale.min || t > end) continue;
        const midnight = s === 0;
        out.push({ t, major: midnight, label: midnight ? dayLabel(d) : `${pad(d.getHours())}:${pad(d.getMinutes())}` });
      }
    }
    return out;
  }
  const everyDays = [1, 2, 7].find((n) => n * DAY >= wanted);
  if (everyDays != null) {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate());
    for (; seconds(d) <= end; d.setDate(d.getDate() + 1)) {
      const t = seconds(d);
      const keep = everyDays === 7 ? d.getDay() === 1 : everyDays === 2 ? d.getDate() % 2 === 1 : true;
      if (t >= scale.min && keep) out.push({ t, major: d.getDate() === 1, label: dayLabel(d) });
    }
    return out;
  }
  const everyMonths = [1, 3, 6].find((n) => n * 30 * DAY >= wanted) ?? 12;
  const d = new Date(start.getFullYear(), start.getMonth(), 1);
  for (; seconds(d) <= end; d.setMonth(d.getMonth() + 1)) {
    const t = seconds(d);
    if (t < scale.min || d.getMonth() % everyMonths !== 0) continue;
    const january = d.getMonth() === 0;
    const label = everyMonths === 12 ? `${d.getFullYear()}` : january ? `${MONTHS[0]} ${d.getFullYear()}` : MONTHS[d.getMonth()];
    out.push({ t, major: january, label });
  }
  return out;
}
