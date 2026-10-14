// The status view: the level as Home Assistant sees it, the gauge image and the service.

import { amount, NONE, percent, reason, relative, size } from "./format.js";

const $ = (id) => document.getElementById(id);

function text(id, value) {
  $(id).textContent = value;
}

function serviceChip(service, now) {
  if (service.here) {
    if (service.reading) return ["Reading…", "ok"];
    return service.mqtt_connected ? ["Running", "ok"] : ["MQTT disconnected", "warn"];
  }
  if (service.alive) return ["Service running elsewhere", "ok"];
  if (service.last_reading) return [`Service last read ${relative(service.last_reading, now)}`, "warn"];
  return ["Browsing only", ""];
}

export function render(status, now = new Date()) {
  const state = status.state;
  const unit = status.unit;

  const [label, tone] = serviceChip(status.service, now);
  text("service", label);
  $("service").dataset.tone = tone;

  const problem = $("problem");
  problem.hidden = !(state && state.problem);
  if (state && state.problem) problem.textContent = `Problem: ${reason(state.reason)}`;

  text("level", state ? percent(state.level) : NONE);
  text("volume", state && state.volume != null
    ? `${amount(state.volume, unit)} of ${amount(status.capacity, unit)}`
    : NONE);
  $("meter-fill").style.width = state && state.level != null ? `${Math.max(0, Math.min(100, state.level))}%` : "0";
  text("usage", state ? amount(state.usage_rate, `${unit}/day`, 1) : NONE);
  text("days-left", state && state.days_left != null ? `${Math.round(state.days_left)}` : NONE);
  text("last-fill", state && state.last_fill
    ? `${relative(state.last_fill, now)} (${amount(state.last_fill_volume, unit)})`
    : NONE);
  text("confidence", state ? state.confidence.toFixed(2) : NONE);

  const caption = state ? `Read ${relative(state.ts, now)}` : "No reading yet.";
  text("gauge-caption", caption);

  const button = $("read-now");
  button.hidden = !status.service.here;
  $("read-after").closest("label").hidden = !status.service.here; // only a service can read
  button.disabled = Boolean(status.service.reading);
  const schedule = [];
  if (status.service.here && status.service.next_reading && !status.service.reading) {
    schedule.push(`Next reading ${relative(status.service.next_reading, now)}`);
  }
  text("schedule", schedule.join(" · "));

  const archive = status.archive;
  text("archive", archive.enabled && archive.size != null
    ? `Archive ${size(archive.size)} of ${size(archive.max_size)}`
    : "");
}
