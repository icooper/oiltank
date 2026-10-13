// Canvas helpers shared by the drawings.

/** Size `canvas` to `width` x `height` CSS pixels, sharp on high-density screens. */
export function sized(canvas, width, height) {
  const dpr = window.devicePixelRatio || 1;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  canvas.width = Math.max(1, Math.round(width * dpr));
  canvas.height = Math.max(1, Math.round(height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return ctx;
}

/** The theme's colours (CSS custom properties on :root). */
export function colors() {
  const style = getComputedStyle(document.documentElement);
  const get = (name) => style.getPropertyValue(name).trim();
  return {
    fg: get("--fg"),
    muted: get("--muted"),
    grid: get("--border"),
    card: get("--card"),
    accent: get("--accent"),
    ok: get("--ok"),
    warn: get("--warn"),
    bad: get("--bad"),
    empty: get("--meter"),
    mark: get("--mark"),
  };
}

const POOLS = { strips: 400, frames: 4 };
const pools = { strips: new Map(), frames: new Map() };

/**
 * An image, loaded once and kept, or null until it has loaded (then `onload` is called). Pools
 * keep different numbers: hundreds of small strips, a few big frames.
 */
export function image(url, onload = null, pool = "strips") {
  const images = pools[pool];
  let entry = images.get(url);
  if (!entry) {
    const img = new Image();
    entry = { img, ready: false, waiting: [] };
    img.onload = () => {
      entry.ready = true;
      for (const f of entry.waiting.splice(0)) f(img);
    };
    img.onerror = () => images.delete(url);
    img.src = url;
    images.set(url, entry);
    if (images.size > POOLS[pool]) images.delete(images.keys().next().value);
  }
  if (entry.ready) return entry.img;
  if (onload) entry.waiting.push(onload);
  return null;
}

/** An image if it has already loaded, without loading it. */
export function peek(url, pool = "strips") {
  const entry = pools[pool].get(url);
  return entry && entry.ready ? entry.img : null;
}
