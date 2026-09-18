// One camera: full-quality live video (go2rtc) or Frigate's detection view, plus its events.

import { api } from "../api.js";
import { releaseMedia } from "../media.js";
import { on, state } from "../store.js";
import { html, humanize, icon, setHTML, storage } from "../util.js";
import { openEvent } from "./eventModal.js";
import { bindFeed, feedList } from "./feed.js";

export function mount(root, [name]) {
  const camera = () => state.cameras.find((c) => c.name === name);
  let mode = storage.get("camera-mode", "video");
  let events = [];
  let media = null;
  let fallbackTimer = null;

  function shell() {
    const cam = camera();
    const frigate = state.config.frigate_url;
    setHTML(root, html`
      <div class="page-head">
        <div>
          <a class="btn ghost small" href="#/live">${icon("back")}All cameras</a>
          <h1>${cam ? cam.label : name}</h1>
          <p class="hint">${cam?.zones?.length ? `Zones: ${cam.zones.map(humanize).join(", ")}` : "No zones configured"}</p>
        </div>
        <div class="row">
          <div class="seg" role="group" aria-label="View">
            <button type="button" data-mode="video" aria-pressed="${mode === "video"}">Live video</button>
            <button type="button" data-mode="detect" aria-pressed="${mode === "detect"}">Detections</button>
          </div>
          ${frigate ? html`<a class="btn" href="${frigate}" target="_blank" rel="noopener" title="Draw zones and masks in Frigate">${icon("external")}Zones &amp; masks</a>` : ""}
        </div>
      </div>
      <div class="split">
        <div class="stack">
          <div class="player" id="player"></div>
          <p class="small muted" id="player-note"></p>
        </div>
        <div class="card">
          <div class="card-head"><h2>Recent on this camera</h2>
            <a class="small" href="#/events">Event log</a></div>
          <ul class="feed" id="cam-feed"><li class="empty">Loading…</li></ul>
        </div>
      </div>`);
    root.querySelector(".seg").addEventListener("click", (e) => {
      const next = e.target.closest("[data-mode]")?.dataset.mode;
      if (!next || next === mode) return;
      mode = next;
      storage.set("camera-mode", mode);
      root.querySelectorAll("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
      play();
    });
    bindFeed(root.querySelector("#cam-feed"), openEvent);
  }

  function note(text) { root.querySelector("#player-note").textContent = text; }

  function stopMedia() {
    clearTimeout(fallbackTimer);
    releaseMedia(media);
    media?.remove();
    media = null;
  }

  function play() {
    stopMedia();
    const player = root.querySelector("#player");
    if (!camera()) {
      setHTML(player, html`<div class="tile-msg">Unknown camera</div>`);
      return;
    }
    setHTML(player, "");
    const base = `/api/cameras/${encodeURIComponent(name)}`;
    if (mode === "video") {
      const video = document.createElement("video");
      Object.assign(video, { autoplay: true, muted: true, playsInline: true, controls: true });
      video.src = `${base}/live.mp4`;
      const fallback = (why) => {
        if (media !== video) return;
        note(`${why} Showing Frigate's detection stream instead.`);
        showDetect(player, base);
      };
      video.addEventListener("error", () => fallback("Live video isn't available for this camera (check go2rtc, or the stream may be H.265)."));
      video.addEventListener("playing", () => { clearTimeout(fallbackTimer); note("Main stream via go2rtc. Audio muted."); });
      fallbackTimer = setTimeout(() => fallback("Live video didn't start within 12 s."), 12000);
      media = video;
      player.append(video);
      note("Connecting…");
    } else {
      note("Frigate's detect stream with bounding boxes, zones in real time.");
      showDetect(player, base);
    }
  }

  function showDetect(player, base) {
    stopMedia();
    const img = document.createElement("img");
    img.alt = `${camera()?.label || name} with detections`;
    img.src = `${base}/mjpeg?height=720`;
    img.addEventListener("error", () => note("The camera stream is unavailable."));
    media = img;
    player.append(img);
  }

  function renderEvents() {
    const list = root.querySelector("#cam-feed");
    if (!list) return;
    setHTML(list, events.length ? feedList(events, { grouped: true }) : html`<li class="empty">No detections recorded yet.</li>`);
  }

  async function loadEvents() {
    try {
      const data = await api("/api/events", { params: { source: "frigate", subject: name, limit: 25 } });
      events = data.events;
    } catch (err) {
      events = [];
    }
    renderEvents();
  }

  shell();
  play();
  loadEvents();

  const unsubscribe = [
    on("cameras", () => { if (!media && camera()) { shell(); play(); renderEvents(); } }),
    on("event", ({ event }) => {
      if (event.source !== "frigate" || event.subject_id !== name) return;
      const i = events.findIndex((e) => e.id === event.id);
      if (i >= 0) events[i] = event;
      else events.unshift(event);
      renderEvents();
    }),
  ];

  return () => {
    unsubscribe.forEach((u) => u());
    stopMedia();
  };
}
