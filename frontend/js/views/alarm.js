// Alarm: arm/disarm, panel health and every zone's state.

import { api } from "../api.js";
import { on, state } from "../store.js";
import { confirmDialog, plural, promptCode, toast, withBusy } from "../ui.js";
import {
  html, icon, MODES, panelIcon, panelTone, relTime, setHTML, STATE_LABELS, zoneIcon, zoneTone,
} from "../util.js";

let zoneFilter = "all";

function flagChips(panel) {
  const chip = (bad, okText, badText, glyph, tone = "warning") => {
    if (bad === null || bad === undefined) return "";
    return bad
      ? html`<span class="chip" data-tone="${tone}">${icon(glyph)}${badText}</span>`
      : html`<span class="chip" data-tone="good">${icon("check")}${okText}</span>`;
  };
  return html`
    ${chip(panel.ac_loss, "AC power", "AC power lost", "plug", "serious")}
    ${chip(panel.low_battery, "Panel battery", "Panel battery low", "battery")}
    ${chip(panel.tampered, "No tamper", "Panel tamper", "tamper", "serious")}
    ${panel.fire ? html`<span class="chip" data-tone="critical">${icon("flame")}Fire</span>` : ""}
    ${panel.co ? html`<span class="chip" data-tone="critical">${icon("flame")}Carbon monoxide</span>` : ""}
    ${panel.police ? html`<span class="chip" data-tone="critical">${icon("alert")}Police alarm</span>` : ""}`;
}

function panelCard(panel, zones) {
  const armedish = panel.state.startsWith("armed") || ["triggered", "arming", "pending"].includes(panel.state);
  const open = zones.filter((z) => z.faulted);
  const sub = panel.available === false
    ? "Home Assistant can't reach this panel right now."
    : [open.length ? `${plural(open.length, "zone")} open${panel.state === "disarmed" ? " – close or bypass before arming" : ""}` : "All zones closed",
      panel.changed_at ? `changed ${relTime(panel.changed_at)}` : ""].filter(Boolean).join(" · ");
  const buttons = armedish
    ? html`<button class="arm-btn disarm" type="button" data-disarm="${panel.id}">${icon("unlock")}Disarm</button>`
    : panel.modes.map((mode) => html`
        <button class="arm-btn" type="button" data-arm="${mode}" data-panel="${panel.id}" ${panel.available === false ? "disabled" : ""}>
          ${icon(MODES[mode].icon)}Arm ${MODES[mode].label}<span class="note">${MODES[mode].note}</span>
        </button>`);
  return html`
    <section class="card panel-card" data-panel-tone="${panelTone(panel.state)}" aria-label="${panel.name} alarm panel">
      <div class="panel-state">
        <div class="panel-icon">${icon(panelIcon(panel.state))}</div>
        <div>
          <h2>${STATE_LABELS[panel.state] || panel.state}</h2>
          <p>${panel.name}${panel.partition !== "1" ? ` · partition ${panel.partition}` : ""} · ${sub}</p>
        </div>
      </div>
      <div class="arm-buttons">${buttons}</div>
      <div class="row">
        ${!armedish && panel.instant_modes.length ? html`
          <label class="check"><input type="checkbox" data-instant> Instant (no entry delay)</label>` : ""}
        <span class="spacer"></span>
        ${panel.can_bypass_all && !armedish ? html`<button class="btn small" type="button" data-bypass-all="${panel.id}">${icon("bypass")}Bypass open zones</button>` : ""}
        ${panel.can_clear_bypass && !armedish ? html`<button class="btn small ghost" type="button" data-clear-bypass="${panel.id}">Clear bypass</button>` : ""}
      </div>
      <div class="flags">${flagChips(panel)}</div>
    </section>`;
}

function zoneCard(zone, armed) {
  const tone = zoneTone(zone);
  const statusChip = zone.available === false
    ? html`<span class="chip" data-tone="muted">${icon("x")}Unavailable</span>`
    : zone.faulted
      ? html`<span class="chip" data-tone="${tone}">${icon(zoneIcon(zone.device_class))}${zone.status}</span>`
      : html`<span class="chip" data-tone="good">${icon("check")}${zone.status}</span>`;
  return html`
    <article class="zone" data-tone="${tone}">
      <div class="zone-top">
        <span class="zone-icon">${icon(zoneIcon(zone.device_class))}</span>
        <span class="zone-name" title="${zone.name}">${zone.name}</span>
        <span class="zone-num">Zone ${zone.number}</span>
      </div>
      <div class="zone-foot">
        ${statusChip}
        ${zone.low_battery ? html`<span class="chip" data-tone="warning">${icon("battery")}Low battery</span>` : ""}
        ${zone.tampered ? html`<span class="chip" data-tone="serious">${icon("tamper")}Tamper</span>` : ""}
        <span class="spacer"></span>
        ${zone.can_bypass && !armed && zone.faulted ? html`<button class="btn small" type="button" data-bypass="${zone.id}" data-name="${zone.name}">Bypass</button>` : ""}
      </div>
      ${zone.changed_at ? html`<span class="zone-when">Changed ${relTime(zone.changed_at)}</span>` : ""}
    </article>`;
}

export function mount(root) {
  function render() {
    const { panels, zones } = state.alarm;
    const armed = panels.some((p) => p.state !== "disarmed");
    const counts = {
      all: zones.length,
      open: zones.filter((z) => z.faulted).length,
      issues: zones.filter((z) => z.low_battery || z.tampered || z.available === false).length,
    };
    const shown = zones.filter((z) => zoneFilter === "open" ? z.faulted
      : zoneFilter === "issues" ? (z.low_battery || z.tampered || z.available === false) : true);
    const instant = root.querySelector("[data-instant]")?.checked;
    setHTML(root, html`
      <div class="page-head">
        <div><h1>Alarm</h1>
          <p class="hint">Honeywell panel via Total Connect. Zone changes can take up to ~30 s to appear (Total Connect is polled).</p></div>
      </div>
      <div class="stack">
        ${panels.length ? panels.map((p) => panelCard(p, zones)) : html`
          <div class="card empty">${icon("shield")}<p>No alarm panel yet. Once Home Assistant's Total Connect integration is set up it shows up here.</p></div>`}
        <section class="card" aria-label="Zones">
          <div class="card-head"><h2>Zones</h2>
            <div class="seg" role="group" aria-label="Filter zones">
              ${["all", "open", "issues"].map((f) => html`<button type="button" data-filter="${f}" aria-pressed="${zoneFilter === f}">${f === "all" ? "All" : f === "open" ? "Open" : "Issues"} ${counts[f]}</button>`)}
            </div>
          </div>
          <div class="card-body">
            ${shown.length ? html`<div class="zone-grid">${shown.map((z) => zoneCard(z, armed))}</div>`
              : html`<div class="empty">${zoneFilter === "all" ? "No zones reported. Enable Sensor Activities in Total Connect." : "Nothing here."}</div>`}
          </div>
        </section>
      </div>`);
    const box = root.querySelector("[data-instant]");
    if (box) box.checked = Boolean(instant);
  }

  async function arm(button) {
    const panel = state.alarm.panels.find((p) => p.id === button.dataset.panel);
    const mode = button.dataset.arm;
    const instant = Boolean(root.querySelector("[data-instant]")?.checked);
    const send = (code) => api(`/api/alarm/${encodeURIComponent(panel.id)}/arm`, { method: "POST", body: { mode, code, instant } });
    const label = `Arm ${MODES[mode].label}${instant ? " (instant)" : ""}`;
    if (panel.code_required && !instant) {
      promptCode({ title: label, action: "Arm", submit: async (code) => { await send(code); toast({ tone: "good", title: `${label} sent`, body: "Waiting for the panel to confirm…" }); } });
      return;
    }
    await withBusy(button, async () => {
      try {
        await send(undefined);
        toast({ tone: "good", title: `${label} sent`, body: "Waiting for the panel to confirm…" });
      } catch (err) {
        toast({ tone: "warning", title: "Couldn't arm", body: err.message, timeout: 10000 });
      }
    });
  }

  async function disarm(button) {
    const panel = state.alarm.panels.find((p) => p.id === button.dataset.disarm);
    const send = (code) => api(`/api/alarm/${encodeURIComponent(panel.id)}/disarm`, { method: "POST", body: { code } });
    if (panel.code_required) {
      promptCode({ title: `Disarm ${panel.name}`, action: "Disarm", submit: async (code) => { await send(code); } });
      return;
    }
    if (!(await confirmDialog({ title: `Disarm ${panel.name}?`, message: "The system will stop monitoring.", confirm: "Disarm", tone: "danger" }))) return;
    await withBusy(button, async () => {
      try { await send(undefined); } catch (err) { toast({ tone: "warning", title: "Couldn't disarm", body: err.message, timeout: 10000 }); }
    });
  }

  async function post(button, url, confirmText) {
    if (confirmText && !(await confirmDialog(confirmText))) return;
    await withBusy(button, async () => {
      try { await api(url, { method: "POST" }); } catch (err) { toast({ tone: "warning", title: "Command failed", body: err.message, timeout: 10000 }); }
    });
  }

  root.addEventListener("click", (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.dataset.arm) arm(t);
    else if (t.dataset.disarm) disarm(t);
    else if (t.dataset.filter) { zoneFilter = t.dataset.filter; render(); }
    else if (t.dataset.bypass) {
      post(t, `/api/zones/${encodeURIComponent(t.dataset.bypass)}/bypass`, {
        title: `Bypass ${t.dataset.name}?`, message: "It won't be monitored until the system is next disarmed.", confirm: "Bypass",
      });
    } else if (t.dataset.bypassAll) {
      post(t, `/api/alarm/${encodeURIComponent(t.dataset.bypassAll)}/bypass-all`, {
        title: "Bypass all open zones?", message: "Every zone that is open right now won't be monitored until the system is next disarmed.", confirm: "Bypass all",
      });
    } else if (t.dataset.clearBypass) {
      post(t, `/api/alarm/${encodeURIComponent(t.dataset.clearBypass)}/clear-bypass`);
    }
  });

  render();
  const unsubscribe = on("alarm", render);
  const clock = setInterval(render, 30000);
  return () => { unsubscribe(); clearInterval(clock); };
}
