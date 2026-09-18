// Live: camera grid with detection overlays, alarm summary and the latest activity.

import { pollImage } from "../media.js";
import { on, state } from "../store.js";
import { plural } from "../ui.js";
import { html, icon, labelIcon, panelIcon, panelTone, relTime, setHTML, STATE_LABELS, titleCase } from "../util.js";
import { openEvent } from "./eventModal.js";
import { bindFeed, feedList } from "./feed.js";

export function mount(root) {
  setHTML(root, html`
    <div class="page-head">
      <div><h1>Live</h1><p class="hint">Boxes are drawn by Frigate's detector. Select a camera for full-quality video.</p></div>
    </div>
    <div class="split">
      <section class="cam-grid" id="grid" aria-label="Cameras"></section>
      <aside class="stack">
        <div class="card mini" id="mini"></div>
        <div class="card">
          <div class="card-head"><h2>Recent activity</h2><a class="small" href="#/events">All events</a></div>
          <ul class="feed" id="feed"></ul>
        </div>
      </aside>
    </div>`);

  const grid = root.querySelector("#grid");
  const mini = root.querySelector("#mini");
  const feed = root.querySelector("#feed");
  const stops = new Map();
  let names = "";
  const fresh = new Set();

  function buildTiles() {
    const next = state.cameras.map((c) => c.name).join("|");
    if (next === names) return;
    names = next;
    stops.forEach((stop) => stop());
    stops.clear();
    if (!state.cameras.length) {
      setHTML(grid, html`<div class="card empty">${icon("camera")}<p>No cameras yet. Add them to Frigate's config and they'll appear here.</p></div>`);
      return;
    }
    setHTML(grid, state.cameras.map((cam) => html`
      <a class="tile" href="#/camera/${encodeURIComponent(cam.name)}" data-cam="${cam.name}" aria-label="${cam.label}">
        <img alt="">
        <span class="tile-msg" hidden></span>
        <span class="tile-bar">${icon("camera")}<span>${cam.label}</span><span class="spacer"></span><span class="badges"></span></span>
      </a>`));
    const height = state.config.live_height || 360;
    const fps = Math.min(state.config.live_fps || 2, 4);
    for (const tile of grid.querySelectorAll("[data-cam]")) {
      const name = tile.dataset.cam;
      const msg = tile.querySelector(".tile-msg");
      stops.set(name, pollImage(tile.querySelector("img"), `/api/cameras/${encodeURIComponent(name)}/latest?height=${height}`, {
        fps,
        onStatus: (problem) => { msg.hidden = !problem; msg.textContent = problem || ""; },
      }));
    }
    updateBadges();
  }

  function updateBadges() {
    for (const tile of grid.querySelectorAll("[data-cam]")) {
      const moving = state.active.filter((o) => o.camera === tile.dataset.cam && !o.stationary);
      const counts = {};
      moving.forEach((o) => { counts[o.label] = (counts[o.label] || 0) + 1; });
      setHTML(tile.querySelector(".badges"), Object.entries(counts).map(([label, n]) =>
        html`<span class="badge ${label === "person" ? "hot" : ""}">${icon(labelIcon(label))}${n > 1 ? `${n} ` : ""}${titleCase(label)}</span>`));
    }
  }

  function renderMini() {
    const panel = state.alarm.panels[0];
    if (!panel) {
      setHTML(mini, html`<div class="card-body muted">No alarm panel connected.</div>`);
      return;
    }
    const open = state.alarm.zones.filter((z) => z.faulted);
    const issues = state.alarm.zones.filter((z) => z.low_battery || z.tampered);
    setHTML(mini, html`
      <div class="card-body panel-state">
        <div class="panel-icon">${icon(panelIcon(panel.state))}</div>
        <div>
          <div class="feed-title">${STATE_LABELS[panel.state] || panel.state} · ${panel.name}</div>
          <div class="feed-sub">${open.length ? `${plural(open.length, "zone")} open` : "All zones closed"}${issues.length ? ` · ${plural(issues.length, "sensor issue")}` : ""}${panel.changed_at ? ` · since ${relTime(panel.changed_at)}` : ""}</div>
        </div>
        <span class="spacer"></span>
        <a class="btn small" href="#/alarm">Controls</a>
      </div>`);
    mini.dataset.panelTone = panelTone(panel.state);
  }

  function renderFeed() {
    const events = state.recent.slice(0, 14);
    setHTML(feed, events.length ? feedList(events, { freshIds: fresh }) : html`<li class="empty">Nothing yet.</li>`);
    fresh.clear();
  }

  bindFeed(feed, openEvent);
  const unsubscribe = [
    on("cameras", buildTiles),
    on("active", updateBadges),
    on("alarm", renderMini),
    on("recent", renderFeed),
    on("event", ({ event, isNew }) => { if (isNew) fresh.add(event.id); renderFeed(); }),
  ];
  const clock = setInterval(renderMini, 30000);
  buildTiles();
  renderMini();
  renderFeed();

  return () => {
    unsubscribe.forEach((u) => u());
    clearInterval(clock);
    stops.forEach((stop) => stop());
  };
}
