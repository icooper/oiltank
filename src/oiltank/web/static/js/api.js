// Talking to the server. Every URL is relative, so the UI also works under a path prefix.

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `request failed (${status})`);
    this.status = status;
  }
}

async function detailOf(response) {
  try {
    const body = await response.json();
    return typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    return response.statusText;
  }
}

/** GET JSON. With `etag`, an unchanged resource comes back as `{ changed: false }`. */
export async function getJSON(url, etag = null) {
  const headers = etag ? { "If-None-Match": etag } : {};
  const response = await fetch(url, { headers, cache: "no-cache" });
  if (response.status === 304) return { changed: false, etag };
  if (!response.ok) throw new ApiError(response.status, await detailOf(response));
  return { changed: true, etag: response.headers.get("ETag"), body: await response.json() };
}

/** POST JSON (always JSON: the server refuses anything else for changes). */
export async function postJSON(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(response.status, await detailOf(response));
  return response.json();
}
