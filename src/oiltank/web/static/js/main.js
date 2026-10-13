// Starts the page: polls the status, and wires up Read now.

import { getJSON, postJSON } from "./api.js";
import { render } from "./status.js";

const POLL_MS = 5000;
let etag = null;
let lastReading = null;
let timer = null;

async function refresh() {
  try {
    const result = await getJSON("api/status", etag);
    if (result.changed) {
      etag = result.etag;
      render(result.body);
      const ts = result.body.state ? result.body.state.ts : null;
      if (ts !== lastReading) {
        lastReading = ts;
        showGauge(ts);
      }
    }
  } catch (error) {
    const chip = document.getElementById("service");
    chip.textContent = "Can't reach oiltank";
    chip.dataset.tone = "bad";
  }
}

function showGauge(ts) {
  const img = document.getElementById("gauge");
  if (!ts) {
    img.hidden = true;
    return;
  }
  img.onload = () => { img.hidden = false; };
  img.onerror = () => { img.hidden = true; };
  img.src = `api/gauge.jpg?ts=${encodeURIComponent(ts)}`;
}

function schedule() {
  clearTimeout(timer);
  if (document.visibilityState === "visible") timer = setTimeout(tick, POLL_MS);
}

async function tick() {
  await refresh();
  schedule();
}

document.getElementById("read-now").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await postJSON("api/read-now");
  } catch (error) {
    document.getElementById("schedule").textContent = `Read now failed: ${error.message}`;
    button.disabled = false;
    return;
  }
  await refresh();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") tick();
  else clearTimeout(timer);
});

tick();
