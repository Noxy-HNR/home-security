// App shell: session, routing, the always-visible alarm state, alerts.

import { api, onUnauthorized } from "./api.js";
import { connect, disconnect, on, state } from "./store.js";
import { beep, confirmDialog, notifications, promptCode, toast } from "./ui.js";
import { html, icon, panelIcon, panelTone, setHTML, STATE_LABELS, storage } from "./util.js";
import * as alarm from "./views/alarm.js";
import * as camera from "./views/camera.js";
import * as events from "./views/events.js";
import * as live from "./views/live.js";
import { mountLogin } from "./views/login.js";
import * as system from "./views/system.js";

const VIEWS = { live, camera, alarm, events, system };
const TABS = [["live", "Live", "grid"], ["alarm", "Alarm", "shield"], ["events", "Events", "list"], ["system", "System", "cpu"]];
const $ = (id) => document.getElementById(id);
const main = $("view");

let cleanup = null;
let started = false;
let loginShown = false;
let appUnsubscribe = [];

// ---- theme ----------------------------------------------------------------------------------

const THEMES = { system: ["contrast", "Theme: follow system"], light: ["sun", "Theme: light"], dark: ["moon", "Theme: dark"] };

function applyTheme() {
  const theme = storage.get("theme", "system");
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const [glyph, label] = THEMES[theme] || THEMES.system;
  setHTML($("theme-btn"), icon(glyph));
  $("theme-btn").title = label;
  $("theme-btn").setAttribute("aria-label", label);
}

$("theme-btn").addEventListener("click", () => {
  const order = Object.keys(THEMES);
  const next = order[(order.indexOf(storage.get("theme", "system")) + 1) % order.length];
  storage.set("theme", next);
  applyTheme();
});

// ---- routing ------------------------------------------------------------------------------

function route() {
  const [name = "live", ...params] = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const view = VIEWS[name] ? name : "live";
  cleanup?.();
  // A fresh container per view: listeners attached to it die with it.
  const container = document.createElement("div");
  main.replaceChildren(container);
  cleanup = VIEWS[view].mount(container, params) || null;
  const tab = view === "camera" ? "live" : view;
  document.querySelectorAll(".tab").forEach((a) => {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", () => { if (started) route(); });

// ---- header ---------------------------------------------------------------------------------

const PRIORITY = (s) => (s === "triggered" ? 4 : ["arming", "disarming", "pending"].includes(s) ? 3 : s.startsWith("armed") ? 2 : s === "disarmed" ? 1 : 0);

function renderPill() {
  const pill = $("alarm-pill");
  const panels = state.alarm.panels;
  if (!panels.length) {
    pill.dataset.tone = "muted";
    setHTML(pill, html`${icon("shield")}<span>No panel</span>`);
  } else {
    const panel = [...panels].sort((a, b) => PRIORITY(b.state) - PRIORITY(a.state))[0];
    const open = state.alarm.zones.filter((z) => z.faulted).length;
    pill.dataset.tone = panelTone(panel.state);
    setHTML(pill, html`${icon(panelIcon(panel.state))}<span>${STATE_LABELS[panel.state] || panel.state}</span>
      ${panel.state === "disarmed" && open ? html`<span class="sub">· ${open} open</span>` : ""}`);
    document.title = panel.state === "triggered" ? "⚠ ALARM · Home Security" : "Home Security";
  }
  renderBanner();
}

function renderBanner() {
  const banner = $("alarm-banner");
  const panel = state.alarm.panels.find((p) => p.state === "triggered" || p.fire || p.co || p.police);
  if (!panel) { banner.hidden = true; return; }
  const what = panel.fire ? "Fire alarm" : panel.co ? "Carbon monoxide alarm" : "Alarm triggered";
  banner.hidden = false;
  setHTML(banner, html`${icon("shieldAlert")}<span class="grow">${what} · ${panel.name}</span>
    <a class="btn small" href="#/events">What happened</a>
    ${panel.state === "triggered" ? html`<button class="btn small" type="button" data-disarm="${panel.id}">Disarm</button>` : ""}`);
}

$("alarm-banner").addEventListener("click", async (e) => {
  const id = e.target.closest("[data-disarm]")?.dataset.disarm;
  const panel = state.alarm.panels.find((p) => p.id === id);
  if (!panel) return;
  const send = (code) => api(`/api/alarm/${encodeURIComponent(panel.id)}/disarm`, { method: "POST", body: { code } });
  if (panel.code_required) {
    promptCode({ title: `Disarm ${panel.name}`, action: "Disarm", submit: send });
  } else if (await confirmDialog({ title: `Disarm ${panel.name}?`, message: "This silences the alarm.", confirm: "Disarm", tone: "danger" })) {
    send(undefined).catch((err) => toast({ tone: "warning", title: "Couldn't disarm", body: err.message }));
  }
});

function renderConn() {
  const conn = $("conn");
  const down = state.sources.filter((s) => !s.connected);
  let mode = state.connection;
  let label = { online: "Live", connecting: "Connecting…", offline: "Reconnecting…" }[mode];
  if (mode === "online" && down.length) {
    mode = "degraded";
    label = `${down.length} service${down.length > 1 ? "s" : ""} down`;
  }
  conn.dataset.state = mode;
  conn.title = down.length ? down.map((s) => `${s.label}: ${s.detail}`).join("\n") : "All services connected";
  setHTML(conn, html`<span class="dot"></span><span class="label">${label}</span>`);
}

function renderTabs() {
  setHTML($("tabs"), TABS.map(([key, label, glyph]) => html`<a class="tab" data-tab="${key}" href="#/${key}">${icon(glyph)}<span>${label}</span></a>`));
}

function renderDemo() {
  const strip = $("demo-strip");
  strip.hidden = !state.config.demo;
  if (!state.config.demo) return;
  setHTML(strip, html`<strong>Demo mode</strong><span>Simulated cameras and alarm. Alarm code 1234.</span>
    <button class="btn small" type="button" data-demo="person">Simulate a person</button>
    <button class="btn small" type="button" data-demo="trigger">Simulate a break-in</button>`);
}

$("demo-strip").addEventListener("click", async (e) => {
  const action = e.target.closest("[data-demo]")?.dataset.demo;
  if (!action) return;
  const cam = state.cameras[0]?.name;
  const url = action === "person" ? `/api/demo/person/${encodeURIComponent(cam)}` : "/api/demo/trigger";
  await api(url, { method: "POST" }).catch((err) => toast({ tone: "warning", title: "Demo action failed", body: err.message }));
});

function renderNotifyButton() {
  const btn = $("notify-btn");
  btn.hidden = !notifications.supported || Notification.permission !== "default";
  setHTML(btn, icon("bell"));
  btn.title = "Enable desktop notifications";
  btn.setAttribute("aria-label", btn.title);
}

$("notify-btn").addEventListener("click", async () => {
  await notifications.request();
  renderNotifyButton();
});

$("logout-btn").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ---- alerts ---------------------------------------------------------------------------------

function handleAlert(alert) {
  toast({
    tone: alert.severity === "info" ? "info" : alert.severity,
    title: alert.title,
    body: alert.body,
    href: alert.event_id ? `#/events/${alert.event_id}` : null,
    timeout: alert.severity === "critical" ? 0 : 8000,
  });
  notifications.show(alert);
  if (alert.severity !== "info") beep(alert.severity === "critical");
}

// ---- session --------------------------------------------------------------------------------

function showLogin(session = null) {
  if (loginShown) return;
  loginShown = true;
  started = false;
  disconnect();
  cleanup?.();
  cleanup = null;
  appUnsubscribe.forEach((u) => u());
  appUnsubscribe = [];
  document.getElementById("modal-root").replaceChildren();
  for (const id of ["topbar", "demo-strip", "alarm-banner"]) $(id).hidden = true;
  const container = document.createElement("div");
  main.replaceChildren(container);
  mountLogin(container, { demo: session?.demo ?? state.config.demo, onSuccess: startApp });
}

function startApp() {
  if (started) return;
  started = true;
  loginShown = false;
  $("topbar").hidden = false;
  appUnsubscribe = [
    on("alarm", renderPill),
    on("connection", renderConn),
    on("sources", renderConn),
    on("config", renderDemo),
    on("alert", handleAlert),
    on("unauthorized", () => showLogin()),
  ];
  renderTabs();
  renderPill();
  renderConn();
  renderNotifyButton();
  connect();
  route();
}

async function boot() {
  applyTheme();
  setHTML($("brand-mark"), icon("shieldCheck"));
  setHTML($("logout-btn"), icon("logout"));
  $("logout-btn").title = "Sign out";
  $("logout-btn").setAttribute("aria-label", "Sign out");
  onUnauthorized(() => showLogin());
  try {
    const session = await api("/api/auth/session");
    $("logout-btn").hidden = Boolean(session.open); // passwordless demo: nothing to sign out of
    if (session.authenticated) startApp();
    else showLogin(session);
  } catch (err) {
    setHTML(main, html`<div class="login"><div class="card card-body">${icon("alert")} ${err.message}.
      <button class="btn small" type="button" id="retry">Retry</button></div></div>`);
    $("retry").addEventListener("click", () => location.reload());
  }
}

boot();
