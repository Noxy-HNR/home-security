// Events: the unified, searchable log of detections, alarm changes, sensor activity and commands.

import { api } from "../api.js";
import { on } from "../store.js";
import { toast } from "../ui.js";
import { debounce, html, icon, setHTML, titleCase } from "../util.js";
import { openEvent } from "./eventModal.js";
import { bindFeed, feedList } from "./feed.js";

const SOURCES = [["", "All"], ["frigate", "Cameras"], ["alarm", "Alarm"], ["sensor", "Sensors"]];
const PERIODS = [["24h", "Last 24 hours", 1], ["7d", "Last 7 days", 7], ["30d", "Last 30 days", 30], ["all", "All time", 0]];

// Filters survive switching tabs within the session.
const filters = { source: "", subject: "", label: "", period: "7d", alerts: false, q: "" };

function sinceFor(period) {
  const days = PERIODS.find(([key]) => key === period)?.[2];
  return days ? new Date(Date.now() - days * 86400000).toISOString() : undefined;
}

export function mount(root, params = []) {
  let events = [];
  let cursor = null;
  let loading = false;
  let facets = { subjects: [], labels: [] };
  let request = null;
  const fresh = new Set();

  setHTML(root, html`
    <div class="page-head">
      <div><h1>Events</h1><p class="hint">Everything the cameras and the alarm have reported, newest first. Updates live.</p></div>
    </div>
    <div class="filters" role="search">
      <div class="seg" role="group" aria-label="Source" id="f-source"></div>
      <select class="input" id="f-subject" aria-label="Camera or zone"></select>
      <select class="input" id="f-label" aria-label="Object"></select>
      <select class="input" id="f-period" aria-label="Time range">
        ${PERIODS.map(([key, label]) => html`<option value="${key}">${label}</option>`)}
      </select>
      <label class="check"><input type="checkbox" id="f-alerts"> Alerts only</label>
      <input class="input search" id="f-q" type="search" placeholder="Search events" aria-label="Search events" maxlength="100">
    </div>
    <div class="card"><ul class="feed" id="list"></ul><div class="more" id="more" hidden>
      <button class="btn" type="button" id="more-btn">Load older events</button></div></div>`);

  const $ = (sel) => root.querySelector(sel);
  const list = $("#list");

  function drawControls() {
    setHTML($("#f-source"), SOURCES.map(([key, label]) =>
      html`<button type="button" data-source="${key}" aria-pressed="${filters.source === key}">${label}</button>`));
    const subjects = facets.subjects.filter((s) => !filters.source || s.source === filters.source);
    const group = (source, label) => {
      const items = subjects.filter((s) => s.source === source);
      return items.length ? html`<optgroup label="${label}">${items.map((s) =>
        html`<option value="${s.id}" ${s.id === filters.subject ? "selected" : ""}>${s.name}</option>`)}</optgroup>` : "";
    };
    setHTML($("#f-subject"), html`<option value="">All cameras &amp; zones</option>
      ${group("frigate", "Cameras")}${group("sensor", "Zones")}${group("alarm", "Alarm panel")}`);
    setHTML($("#f-label"), html`<option value="">Any object</option>${facets.labels.map((l) =>
      html`<option value="${l}" ${l === filters.label ? "selected" : ""}>${titleCase(l)}</option>`)}`);
    $("#f-label").disabled = filters.source && filters.source !== "frigate";
    $("#f-period").value = filters.period;
    $("#f-alerts").checked = filters.alerts;
    $("#f-q").value = filters.q;
  }

  function query(extra = {}) {
    return {
      source: filters.source || undefined,
      subject: filters.subject || undefined,
      label: filters.label || undefined,
      severity: filters.alerts ? ["warning", "critical"] : undefined,
      q: filters.q || undefined,
      since: sinceFor(filters.period),
      limit: 50,
      ...extra,
    };
  }

  function render() {
    setHTML(list, events.length
      ? feedList(events, { grouped: true, freshIds: fresh, precise: true })
      : html`<li class="empty">${loading ? "Loading…" : html`${icon("list")}<p>No events match these filters.</p>`}</li>`);
    fresh.clear();
    $("#more").hidden = !cursor;
  }

  async function load({ append = false } = {}) {
    request?.abort();
    request = new AbortController();
    loading = true;
    if (!append) { events = []; cursor = null; render(); }
    try {
      const data = await api("/api/events", { params: query(append ? { cursor } : {}), signal: request.signal });
      events = append ? events.concat(data.events) : data.events;
      cursor = data.next_cursor;
    } catch (err) {
      if (err.name === "AbortError") return;
      toast({ tone: "warning", title: "Couldn't load events", body: err.message });
    } finally {
      loading = false;
    }
    render();
  }

  async function loadFacets() {
    try { facets = await api("/api/events/facets"); } catch { /* keep empty */ }
    drawControls();
  }

  function matches(ev) {
    if (filters.q) return false; // search results aren't live-patched; rerun the search to refresh
    if (filters.source && ev.source !== filters.source) return false;
    if (filters.subject && ev.subject_id !== filters.subject) return false;
    if (filters.label && ev.label !== filters.label) return false;
    if (filters.alerts && !["warning", "critical"].includes(ev.severity)) return false;
    return true;
  }

  const changed = () => { drawControls(); load(); };
  $("#f-source").addEventListener("click", (e) => {
    const b = e.target.closest("[data-source]");
    if (!b) return;
    filters.source = b.dataset.source;
    filters.subject = "";
    if (filters.source && filters.source !== "frigate") filters.label = "";
    changed();
  });
  $("#f-subject").addEventListener("change", (e) => { filters.subject = e.target.value; changed(); });
  $("#f-label").addEventListener("change", (e) => { filters.label = e.target.value; changed(); });
  $("#f-period").addEventListener("change", (e) => { filters.period = e.target.value; changed(); });
  $("#f-alerts").addEventListener("change", (e) => { filters.alerts = e.target.checked; changed(); });
  $("#f-q").addEventListener("input", debounce((e) => { filters.q = e.target.value.trim(); load(); }, 300));
  $("#more-btn").addEventListener("click", () => load({ append: true }));
  bindFeed(list, openEvent);

  const unsubscribe = on("event", ({ event }) => {
    const i = events.findIndex((e) => e.id === event.id);
    if (i >= 0) { events[i] = event; render(); return; }
    if (!matches(event)) return;
    events.unshift(event);
    fresh.add(event.id);
    render();
  });

  drawControls();
  loadFacets();
  load();
  if (params[0]) openEvent(params[0]);

  return () => { unsubscribe(); request?.abort(); };
}
