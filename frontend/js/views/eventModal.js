// Event detail: snapshot, clip and everything we know about one event.

import { api } from "../api.js";
import { state } from "../store.js";
import { openModal, toast } from "../ui.js";
import { describe, duration, fmtFull, html, humanize, icon, pct, severityChip, titleCase, STATE_LABELS } from "../util.js";

function rows(ev) {
  const x = ev.extra || {};
  const out = [];
  if (ev.source === "frigate") {
    out.push(["Camera", ev.subject], ["Object", titleCase(ev.label)], ["Confidence", pct(ev.confidence)]);
    if (x.sub_label) out.push(["Recognised", x.sub_label]);
    if (x.plate) out.push(["Plate", x.plate]);
    out.push(["Zones", ev.zones?.length ? ev.zones.map(humanize).join(", ") : "None"]);
    out.push(["Duration", ev.in_progress ? "In progress" : duration(ev.start_time, ev.end_time) || "–"]);
  } else if (ev.type === "alarm_state") {
    out.push(["Panel", ev.subject], ["State", STATE_LABELS[ev.label] || ev.label]);
    if (x.previous) out.push(["Previous", STATE_LABELS[x.previous] || x.previous]);
    if (x.triggered_by) out.push(["Open zones", x.triggered_by]);
  } else if (ev.type === "command") {
    out.push(["Target", ev.subject], ["Command", titleCase(ev.label)], ["From", x.actor || "–"],
      ["Result", x.ok === false ? `Failed: ${x.error}` : "Accepted by Home Assistant"]);
  } else {
    out.push(["Sensor", ev.subject], ["Change", titleCase(ev.label)]);
    if (x.zone_number) out.push(["Zone", x.zone_number]);
    if (x.device_class) out.push(["Type", titleCase(x.device_class)]);
  }
  out.push(["Started", fmtFull(ev.start_time)]);
  if (ev.end_time) out.push(["Ended", fmtFull(ev.end_time)]);
  out.push(["Event #", ev.id]);
  return out;
}

export async function openEvent(evOrId) {
  let ev = evOrId;
  if (typeof evOrId !== "object") {
    try {
      ev = await api(`/api/events/${encodeURIComponent(evOrId)}`);
    } catch (err) {
      toast({ tone: "warning", title: "Event not found", body: err.message });
      return;
    }
  }
  const d = describe(ev);
  const frigateUrl = state.config.frigate_url;
  const media = ev.snapshot_url || ev.clip_url
    ? html`<div class="event-media">
        ${ev.snapshot_url ? html`<img src="${ev.snapshot_url}" alt="Snapshot: ${d.title}">` : ""}
        ${ev.clip_url ? html`<button class="btn" type="button" data-play>${icon("play")}Play clip</button>` : ""}
        ${ev.source === "frigate" && !ev.clip_url ? html`<p class="small muted">No recording for this event${ev.in_progress ? " yet" : ""}.</p>` : ""}
      </div>`
    : html`<div class="event-media"><div class="empty">${icon(d.icon)}<p>${d.detail || "No media for this event."}</p></div></div>`;
  const { el } = openModal({
    title: d.title,
    wide: Boolean(ev.snapshot_url || ev.clip_url),
    body: html`
      <div class="${ev.snapshot_url || ev.clip_url ? "event-detail" : ""}">
        ${media}
        <div class="stack">
          <div class="row">${severityChip(ev.severity)}</div>
          <dl class="dl">${rows(ev).map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>
          ${ev.source === "frigate" && frigateUrl
            ? html`<a class="btn small" href="${frigateUrl}" target="_blank" rel="noopener">${icon("external")}Open Frigate</a>` : ""}
        </div>
      </div>`,
  });
  el.querySelector("[data-play]")?.addEventListener("click", (e) => {
    const video = document.createElement("video");
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    video.src = ev.clip_url;
    e.currentTarget.replaceWith(video);
  });
}
