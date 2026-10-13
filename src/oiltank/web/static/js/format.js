// Formatting for display. Pure: no DOM, so `node --test` can check it.

export const NONE = "—";

/** A gauge level in percent (as Home Assistant gets it), e.g. "98.0%". */
export function percent(value, digits = 1) {
  return value == null ? NONE : `${value.toFixed(digits)}%`;
}

/** A volume or rate with its unit, e.g. "255 gal". */
export function amount(value, unit, digits = 0) {
  return value == null ? NONE : `${value.toFixed(digits)} ${unit}`;
}

/** How long ago or until `date`, relative to `now`: "just now", "4 min ago", "in 11 min". */
export function relative(date, now = new Date()) {
  if (date == null) return NONE;
  const seconds = (new Date(date).getTime() - now.getTime()) / 1000;
  const size = Math.abs(seconds);
  if (size < 45) return "just now";
  let text;
  if (size < 90 * 60) text = `${Math.round(size / 60)} min`;
  else if (size < 36 * 3600) text = `${Math.round(size / 3600)} h`;
  else text = `${Math.round(size / 86400)} days`;
  return seconds < 0 ? `${text} ago` : `in ${text}`;
}

/** A size in bytes, e.g. "120 MB" or "2.0 GB" (powers of 1024, as the archive limit is). */
export function size(bytes) {
  if (bytes == null) return NONE;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

/** A problem reason as the service reports it ("camera_unreachable"), in words. */
export function reason(text) {
  return text == null ? "" : text.replaceAll("_", " ");
}
