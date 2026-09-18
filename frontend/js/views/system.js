// System: integration health and Frigate performance (the numbers that matter on a CPU-only box).

import { api } from "../api.js";
import { on, state } from "../store.js";
import { html, icon, relTime, setHTML } from "../util.js";

const n = (v, digits = 1) => (v === null || v === undefined || v === "" ? "–" : Number(v).toFixed(digits));

function sourceRow(s) {
  return html`<tr>
    <td>${s.label}</td>
    <td>${s.connected
      ? html`<span class="chip" data-tone="good">${icon("check")}Connected</span>`
      : html`<span class="chip" data-tone="serious">${icon("alert")}Down</span>`}</td>
    <td class="small">${s.detail}</td>
    <td class="small muted">${relTime(s.since)}</td>
  </tr>`;
}

function inferenceTone(ms) {
  if (ms === null || ms === undefined) return "";
  if (ms > 60) return html`<span class="chip" data-tone="serious">${icon("alert")}Slow</span>`;
  if (ms > 30) return html`<span class="chip" data-tone="warning">${icon("alert")}Busy</span>`;
  return html`<span class="chip" data-tone="good">${icon("check")}Healthy</span>`;
}

function storageCard(stats) {
  const entries = Object.entries(stats?.storage || {});
  if (!entries.length) return "";
  return html`<section class="card"><div class="card-head"><h2>Recording storage</h2></div><div class="card-body stack">
    ${entries.map(([path, s]) => {
      const used = s.total ? (s.used / s.total) * 100 : 0;
      const tone = used > 92 ? "critical" : used > 80 ? "warning" : "";
      return html`<div>
        <div class="row small"><span class="mono">${path}</span><span class="spacer"></span>
          <span>${n(s.used / 1024, 0)} GB of ${n(s.total / 1024, 0)} GB</span></div>
        <div class="meter" data-tone="${tone}" data-pct="${used.toFixed(1)}"><span></span></div>
        <div class="small muted">${n(used, 0)}% used · ${n(s.free / 1024, 0)} GB free${tone ? " – lower Frigate's retention days" : ""}</div>
      </div>`;
    })}
  </div></section>`;
}

export function mount(root) {
  let system = null;

  function render() {
    const stats = state.stats || system?.stats;
    const sources = state.sources.length ? state.sources : system?.sources || [];
    const detectors = Object.entries(stats?.detectors || {});
    const cameras = Object.entries(stats?.cameras || {});
    const inference = detectors[0]?.[1]?.inference_ms;
    const dropping = cameras.filter(([, c]) => (c.skipped_fps || 0) > 0.5);
    setHTML(root, html`
      <div class="page-head"><div><h1>System</h1>
        <p class="hint">Integration health and detection load${stats?.version ? ` · Frigate ${stats.version}` : ""}.</p></div></div>
      <div class="stack">
        <div class="stat-row">
          <div class="card stat"><div class="label">Detector inference</div>
            <div class="value">${n(inference)}<span class="unit">ms</span></div><div class="row">${inferenceTone(inference)}</div></div>
          <div class="card stat"><div class="label">Detections / s</div><div class="value">${n(stats?.detection_fps)}</div></div>
          <div class="card stat"><div class="label">Frigate CPU</div><div class="value">${n(stats?.cpu_percent, 0)}<span class="unit">%</span></div></div>
          <div class="card stat"><div class="label">Events logged</div><div class="value">${system ? system.events_total.toLocaleString() : "–"}</div></div>
          <div class="card stat"><div class="label">Dashboards open</div><div class="value">${system ? system.dashboard_clients : "–"}</div></div>
        </div>
        ${dropping.length ? html`<div class="card card-body row">${icon("alert")}
          <span><strong>${dropping.map(([c]) => state.cameras.find((cam) => cam.name === c)?.label || c).join(", ")}</strong> ${dropping.length === 1 ? "is" : "are"} skipping frames: the detector can't keep up.
          Lower <span class="mono">detect.fps</span> or the sub-stream resolution for ${dropping.length === 1 ? "that camera" : "those cameras"}, or pass the Intel iGPU through to OpenVINO.</span></div>` : ""}
        <section class="card"><div class="card-head"><h2>Connections</h2></div>
          <table class="table"><thead><tr><th>Service</th><th>Status</th><th>Detail</th><th>Since</th></tr></thead>
          <tbody>${sources.map(sourceRow)}</tbody></table></section>
        <section class="card"><div class="card-head"><h2>Cameras</h2></div>
          ${cameras.length ? html`<table class="table"><thead><tr><th>Camera</th><th class="num">Stream fps</th>
            <th class="num">Processed fps</th><th class="num">Detections / s</th><th class="num">Skipped fps</th></tr></thead>
            <tbody>${cameras.map(([name, c]) => html`<tr><td>${state.cameras.find((cam) => cam.name === name)?.label || name}</td><td class="num">${n(c.camera_fps)}</td>
              <td class="num">${n(c.process_fps)}</td><td class="num">${n(c.detection_fps)}</td>
              <td class="num">${(c.skipped_fps || 0) > 0.5 ? html`<span class="chip" data-tone="warning">${icon("alert")}${n(c.skipped_fps)}</span>` : n(c.skipped_fps)}</td></tr>`)}</tbody></table>`
            : html`<div class="empty">Waiting for Frigate stats (published every 15 s over MQTT).</div>`}
        </section>
        ${storageCard(stats)}
        ${system ? html`<section class="card"><div class="card-head"><h2>Configuration</h2></div><div class="card-body">
          <dl class="dl">
            <dt>Database</dt><dd>${system.database}</dd>
            <dt>Alert on</dt><dd>${system.alerts.labels} · cameras: ${system.alerts.cameras} · zones: ${system.alerts.zones}${system.alerts.only_when_armed ? " · only while armed" : ""}</dd>
            <dt>Push notifications</dt><dd>${system.alerts.push ? "ntfy enabled" : "Off (set NTFY_TOPIC)"}</dd>
            <dt>Snapshot archive</dt><dd>${system.retention.archive_snapshots ? `kept ${system.retention.snapshot_days} days` : "off"}</dd>
            <dt>Event log</dt><dd>${system.retention.event_days ? `kept ${system.retention.event_days} days` : "kept forever"}</dd>
          </dl></div></section>` : ""}
      </div>`);
    // Meter widths go through CSSOM (the CSP forbids inline style attributes).
    root.querySelectorAll(".meter[data-pct]").forEach((m) => m.style.setProperty("--pct", `${m.dataset.pct}%`));
  }

  async function load() {
    try { system = await api("/api/system"); } catch { /* keep last */ }
    render();
  }

  render();
  load();
  const timer = setInterval(load, 15000);
  const unsubscribe = [on("stats", render), on("sources", render)];
  return () => { clearInterval(timer); unsubscribe.forEach((u) => u()); };
}
