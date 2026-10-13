// Starts the page: the Now and History views (chosen by the #hash), status polling and Read now.
// `?embed=card` shows a compact History view for a Home Assistant card; `?theme=` forces a theme.

import { getJSON, postJSON } from "./api.js";
import { CalibrationView, wireDraftBar } from "./calibration.js";
import { draft } from "./draft.js";
import { GaugeView } from "./gauge.js";
import { HistoryView } from "./history.js";
import { render } from "./status.js";
import { formatHash, parseHash, parseOptions } from "./store.js";

const POLL_MS = 5000;
const options = parseOptions(window.location.search);
if (options.theme) document.documentElement.dataset.theme = options.theme;
if (options.card) document.body.classList.add("embedded");

function showError(message) {
  const banner = document.getElementById("page-error");
  banner.textContent = `Something went wrong on this page: ${message}. Reloading may help.`;
  banner.hidden = false;
}
window.addEventListener("error", (e) => showError(e.message));
window.addEventListener("unhandledrejection", (e) => showError(e.reason?.message ?? String(e.reason)));

// ---- views --------------------------------------------------------------------------------------

let historyView = null;
let calibrationView = null;
let gaugeView = null;

function show() {
  const state = parseHash(window.location.hash);
  const view = options.card ? "history" : state.view;
  for (const name of ["now", "history", "calibration", "gauge"]) {
    document.getElementById(`view-${name}`).hidden = name !== view;
  }
  for (const tab of document.querySelectorAll(".tabs a")) {
    tab.setAttribute("aria-current", tab.dataset.view === view ? "page" : "false");
  }
  if (view === "calibration") {
    calibrationView ??= new CalibrationView(document.getElementById("view-calibration"));
    calibrationView.open();
    return;
  }
  if (view === "gauge") {
    gaugeView ??= new GaugeView(document.getElementById("view-gauge"));
    gaugeView.open();
    return;
  }
  if (view !== "history") return;
  if (!historyView) {
    historyView = new HistoryView(document.getElementById("view-history"), {
      card: options.card,
      onState: (s) => {
        const hash = formatHash({ view: "history", ...s });
        if (hash !== window.location.hash) window.history.replaceState(null, "", hash);
        // Coming back to History from another view returns to the same place.
        document.querySelector('.tabs a[data-view="history"]').setAttribute("href", hash);
      },
    });
  }
  historyView.open(state);
}

window.addEventListener("hashchange", show);

// The calibration draft: loaded for the "This reading was" form and the unsaved-changes bar.
// Once a change is saved (from the bar, or a rebase), every level may have changed.
wireDraftBar();
draft.addEventListener("saved", () => {
  if (historyView) historyView.reset();
  etag = null;
  refresh();
});
draft.load().catch(() => {}); // no usable calibration: nothing to change

// ---- status -------------------------------------------------------------------------------------

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
  } catch {
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
  img.onload = () => {
    img.hidden = false;
  };
  img.onerror = () => {
    img.hidden = true;
  };
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

show();
tick();
