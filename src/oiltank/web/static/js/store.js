// What the page is showing, kept in the URL's #hash so a view can be bookmarked or shared.
// Pure: parsing and formatting only.

export const RANGES = { "1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400, "1y": 365 * 86400 };
export const VIEWS = ["now", "history", "calibration"];

const iso = (t) => new Date(Math.round(t) * 1000).toISOString().replace(".000Z", "Z");

function time(value) {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms / 1000;
}

/** `{ view, range, end, at }` from a hash like `#view=history&range=7d&at=2026-10-13T03:15:00Z`. */
export function parseHash(hash) {
  const p = new URLSearchParams(String(hash || "").replace(/^#/, ""));
  const view = VIEWS.includes(p.get("view")) ? p.get("view") : "now";
  const range = p.get("range") in RANGES ? p.get("range") : "7d";
  return { view, range, end: time(p.get("end")), at: time(p.get("at")) };
}

/** The hash for `state` (`end` and `at` are Unix seconds or null). */
export function formatHash(state) {
  const p = new URLSearchParams();
  p.set("view", state.view);
  if (state.view === "history") {
    p.set("range", state.range);
    if (state.end != null) p.set("end", iso(state.end));
    if (state.at != null) p.set("at", iso(state.at));
  }
  return `#${p.toString().replaceAll("%3A", ":")}`;
}

/** Options the embedding page sets in the query string: `?embed=card&theme=dark`. */
export function parseOptions(search) {
  const p = new URLSearchParams(String(search || ""));
  const theme = ["light", "dark"].includes(p.get("theme")) ? p.get("theme") : null;
  return { card: p.get("embed") === "card", theme };
}
