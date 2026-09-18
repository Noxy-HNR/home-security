// Client-side state, kept current by the backend's WebSocket. Views subscribe by topic.

import { api } from "./api.js";

export const state = {
  alarm: { panels: [], zones: [] },
  cameras: [],
  active: [],
  sources: [],
  stats: null,
  config: {},
  recent: [],
  connection: "connecting",
};

const listeners = new Map();

export function on(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(fn);
  return () => listeners.get(topic)?.delete(fn);
}

export function emit(topic, payload) {
  for (const fn of [...(listeners.get(topic) || [])]) {
    try { fn(payload); } catch (err) { console.error(`listener for ${topic} failed`, err); }
  }
}

const RECENT_MAX = 60;

export function upsertRecent(ev) {
  const i = state.recent.findIndex((e) => e.id === ev.id);
  const isNew = i < 0;
  if (isNew) state.recent.unshift(ev);
  else state.recent[i] = ev;
  state.recent.sort((a, b) => (a.start_time < b.start_time ? 1 : a.start_time > b.start_time ? -1 : b.id - a.id));
  state.recent.length = Math.min(state.recent.length, RECENT_MAX);
  emit("event", { event: ev, isNew });
}

function applyHello(s) {
  Object.assign(state, {
    alarm: s.alarm, cameras: s.cameras, active: s.active, sources: s.sources, stats: s.stats, config: s.config,
  });
  for (const topic of ["config", "alarm", "cameras", "active", "sources", "stats"]) emit(topic);
}

async function refreshRecent() {
  try {
    const data = await api("/api/events", { params: { limit: 40 } });
    state.recent = data.events;
    emit("recent");
  } catch (err) {
    console.warn("could not load recent events", err);
  }
}

function setConnection(value) {
  if (state.connection === value) return;
  state.connection = value;
  emit("connection");
}

function handle(msg) {
  switch (msg.type) {
    case "hello":
      applyHello(msg.state);
      setConnection("online");
      refreshRecent();
      break;
    case "alarm": state.alarm = msg.alarm; emit("alarm"); break;
    case "event": upsertRecent(msg.event); break;
    case "active": state.active = msg.active; emit("active"); break;
    case "cameras": state.cameras = msg.cameras; emit("cameras"); break;
    case "sources": state.sources = msg.sources; emit("sources"); break;
    case "stats": state.stats = msg.stats; emit("stats"); break;
    case "alert": emit("alert", msg.alert); break;
    default: break;
  }
}

// ---- WebSocket lifecycle ------------------------------------------------------------------

let socket = null;
let attempts = 0;
let stopped = true;
let retryTimer = null;

export function connect() {
  stopped = false;
  clearTimeout(retryTimer);
  if (socket) return;
  setConnection("connecting");
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
  socket = ws;
  ws.onopen = () => { attempts = 0; };
  ws.onmessage = (e) => {
    try { handle(JSON.parse(e.data)); } catch (err) { console.error("bad message", err); }
  };
  ws.onclose = async (e) => {
    if (socket === ws) socket = null;
    if (stopped) return;
    setConnection("offline");
    if (e.code === 4401 || e.code === 4001) {
      // session missing or expired: find out which before reconnecting
      const session = await api("/api/auth/session").catch(() => null);
      if (session && !session.authenticated) {
        emit("unauthorized");
        return;
      }
    }
    const delay = Math.min(10000, 800 * 2 ** attempts++);
    retryTimer = setTimeout(connect, delay);
  };
}

export function disconnect() {
  stopped = true;
  clearTimeout(retryTimer);
  socket?.close();
  socket = null;
}

// When the tab wakes from sleep the socket may be dead without having closed yet.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !stopped && !socket) connect();
});
