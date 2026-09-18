// Small shared helpers: escaped HTML templating, icons, formatting and wording.

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

class Raw {
  constructor(text) { this.text = text; }
  toString() { return this.text; }
}

export const raw = (text) => new Raw(text);
export const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);

function part(value) {
  if (value instanceof Raw) return value.text;
  if (Array.isArray(value)) return value.map(part).join("");
  if (value === null || value === undefined || value === false) return "";
  return esc(value);
}

/** Tagged template: every interpolation is escaped unless it is itself html`` or raw(). */
export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, i) => { out += part(value) + strings[i + 1]; });
  return new Raw(out);
}

/** Set trusted markup (html`` results). Arrays are joined without the commas String() would add. */
export function setHTML(el, content) {
  el.innerHTML = Array.isArray(content) ? content.map(String).join("") : String(content ?? "");
}

// ---- icons (24px grid, stroked) ---------------------------------------------------------

const SHIELD = "M12 3l7 3v5c0 4.5-3 8.3-7 9.5C8 19.3 5 15.5 5 11V6l7-3z";
const PATHS = {
  shield: `<path d="${SHIELD}"/>`,
  shieldCheck: `<path d="${SHIELD}"/><path d="M9 12l2 2 4-4"/>`,
  shieldAlert: `<path d="${SHIELD}"/><path d="M12 8v4M12 15.5h.01"/>`,
  lock: `<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>`,
  unlock: `<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.6-1.7"/>`,
  moon: `<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>`,
  home: `<path d="M4 11l8-7 8 7v9a1 1 0 0 1-1 1h-5v-6h-4v6H5a1 1 0 0 1-1-1z"/>`,
  camera: `<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10.5l5-3v9l-5-3z"/>`,
  list: `<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>`,
  activity: `<path d="M3 12h4l3-8 4 16 3-8h4"/>`,
  door: `<path d="M6 21V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v17M3 21h18M14.5 12h.01"/>`,
  motion: `<path d="M12 12h.01M8.5 8.5a5 5 0 0 0 0 7M15.5 8.5a5 5 0 0 1 0 7M5.6 5.6a9 9 0 0 0 0 12.8M18.4 5.6a9 9 0 0 1 0 12.8"/>`,
  flame: `<path d="M12 3c1 3 5 5 5 10a5 5 0 0 1-10 0c0-2 1-3.5 2-4.5 0 2 1 3 2 3 0-3-1-5 1-8.5z"/>`,
  battery: `<rect x="2" y="8" width="17" height="9" rx="2"/><path d="M22 11v3M6 11.5v2"/>`,
  tamper: `<path d="M8 3h8l5 5v8l-5 5H8l-5-5V8z"/><path d="M12 8v5M12 16h.01"/>`,
  alert: `<path d="M12 4l9.5 16h-19z"/><path d="M12 10v4M12 17h.01"/>`,
  check: `<path d="M5 12.5l4.5 4.5L19 7.5"/>`,
  x: `<path d="M6 6l12 12M18 6L6 18"/>`,
  bell: `<path d="M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>`,
  bellOff: `<path d="M6 16v-5a6 6 0 0 1 9.5-4.9M18 11v5l2 2H8M10 20.5a2 2 0 0 0 4 0M3 3l18 18"/>`,
  plug: `<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>`,
  zap: `<path d="M13 3L4 14h7l-1 7 9-11h-7z"/>`,
  external: `<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>`,
  user: `<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>`,
  car: `<path d="M3 16v-4l2.2-5h13.6L21 12v4zM3 16v3M21 16v3M7 12.5h.01M17 12.5h.01"/>`,
  target: `<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>`,
  sun: `<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>`,
  contrast: `<circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor"/>`,
  logout: `<path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l5-5-5-5M15 12H3"/>`,
  grid: `<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/>`,
  cpu: `<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>`,
  terminal: `<path d="M4 17l6-5-6-5M12 19h8"/>`,
  back: `<path d="M15 18l-6-6 6-6"/>`,
  play: `<path d="M7 4l13 8-13 8z"/>`,
  bypass: `<path d="M4 12h5M15 12h5M9 12a3 3 0 0 1 6 0M12 4v3M12 17v3"/>`,
  clock: `<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>`,
};

export const icon = (name, label) =>
  raw(`<svg class="i" viewBox="0 0 24 24" ${label ? `role="img" aria-label="${esc(label)}"` : 'aria-hidden="true"'}>${PATHS[name] || PATHS.target}</svg>`);

// ---- formatting -----------------------------------------------------------------------------

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" });
const shortTimeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "short", day: "numeric" });
const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

export const toDate = (iso) => (iso ? new Date(iso) : null);
export const fmtTime = (iso) => (iso ? timeFmt.format(new Date(iso)) : "");
export const fmtShortTime = (iso) => (iso ? shortTimeFmt.format(new Date(iso)) : "");
export const fmtFull = (iso) => (iso ? fullFmt.format(new Date(iso)) : "–");

export function dayLabel(iso) {
  const d = new Date(iso);
  const today = new Date();
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return dateFmt.format(d);
}

export function relTime(iso) {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

export function duration(startIso, endIso) {
  if (!startIso || !endIso) return "";
  const s = Math.max(0, Math.round((new Date(endIso) - new Date(startIso)) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export const pct = (score) => (score == null ? "" : `${Math.round(score * 100)}%`);
export const titleCase = (s) => String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
export const humanize = (s) => String(s || "").replace(/_/g, " ");

// ---- domain wording -----------------------------------------------------------------------

export const STATE_LABELS = {
  disarmed: "Disarmed",
  armed_home: "Armed Stay",
  armed_away: "Armed Away",
  armed_night: "Armed Night",
  armed_custom_bypass: "Armed (bypass)",
  armed_vacation: "Armed Vacation",
  arming: "Arming…",
  disarming: "Disarming…",
  pending: "Entry delay",
  triggered: "ALARM",
  unavailable: "Unavailable",
  unknown: "Unknown",
};

export const MODES = {
  stay: { label: "Stay", icon: "home", note: "Perimeter only" },
  away: { label: "Away", icon: "lock", note: "Everything" },
  night: { label: "Night", icon: "moon", note: "Perimeter + night zones" },
};

export function panelTone(state) {
  if (state === "triggered") return "critical";
  if (state === "arming" || state === "disarming" || state === "pending") return "warning";
  if (state?.startsWith("armed")) return "armed";
  if (state === "disarmed") return "good";
  return "muted";
}

export function panelIcon(state) {
  if (state === "triggered") return "shieldAlert";
  if (state === "armed_night") return "moon";
  if (state?.startsWith("armed")) return "lock";
  if (state === "disarmed") return "shieldCheck";
  return "shield";
}

export function zoneIcon(deviceClass) {
  if (deviceClass === "motion" || deviceClass === "occupancy") return "motion";
  if (["smoke", "gas", "carbon_monoxide"].includes(deviceClass)) return "flame";
  return "door";
}

export function zoneTone(zone) {
  if (!zone.available) return "muted";
  if (zone.faulted && ["smoke", "gas", "carbon_monoxide"].includes(zone.device_class)) return "critical";
  if (zone.faulted) return "warning";
  return "good";
}

export const labelIcon = (label) => ({ person: "user", car: "car" })[label] || "target";

/** How an event reads in lists: icon, title, detail line. */
export function describe(ev) {
  const extra = ev.extra || {};
  switch (ev.type) {
    case "detection": {
      const zones = ev.zones?.length ? ev.zones.map(humanize).join(", ") : "";
      const bits = [zones, pct(ev.confidence), ev.in_progress ? "in progress" : duration(ev.start_time, ev.end_time)];
      const who = extra.sub_label || extra.plate;
      return {
        icon: labelIcon(ev.label),
        title: `${titleCase(ev.label)}${who ? ` (${who})` : ""} · ${ev.subject}`,
        detail: bits.filter(Boolean).join(" · "),
      };
    }
    case "alarm_state":
      return {
        icon: panelIcon(ev.label),
        title: `${STATE_LABELS[ev.label] || titleCase(ev.label)} · ${ev.subject}`,
        detail: [extra.previous ? `from ${STATE_LABELS[extra.previous] || humanize(extra.previous)}` : "",
          extra.triggered_by ? `open zones: ${extra.triggered_by}` : ""].filter(Boolean).join(" · "),
      };
    case "command":
      return {
        icon: "terminal",
        title: `${titleCase(ev.label)} requested · ${ev.subject}`,
        detail: [extra.actor ? `from ${extra.actor}` : "", extra.ok === false ? `failed: ${extra.error}` : "sent"].filter(Boolean).join(" · "),
      };
    case "zone_fault":
    case "zone_restore":
      return {
        icon: zoneIcon(extra.device_class),
        title: `${ev.subject} � ${titleCase(ev.label)}`,
        detail: extra.zone_number ? `Zone ${extra.zone_number}` : "Sensor",
      };
    case "low_battery":
    case "battery_ok":
      return { icon: "battery", title: `${ev.subject}: ${ev.label}`, detail: extra.zone_number ? `Zone ${extra.zone_number}` : "" };
    case "tamper":
    case "tamper_restore":
      return { icon: "tamper", title: `${ev.subject}: ${ev.label}`, detail: extra.zone_number ? `Zone ${extra.zone_number}` : "" };
    default:
      if (ev.type?.startsWith("panel_")) {
        const kind = ev.type.slice(6);
        return { icon: kind === "ac_loss" ? "plug" : kind === "low_battery" ? "battery" : "alert", title: `${titleCase(ev.label)}`, detail: ev.subject };
      }
      return { icon: "activity", title: `${titleCase(ev.type)} · ${ev.subject}`, detail: ev.label || "" };
  }
}

export function severityChip(severity) {
  if (severity === "critical") return html`<span class="chip" data-tone="critical">${icon("alert")}Critical</span>`;
  if (severity === "warning") return html`<span class="chip" data-tone="warning">${icon("alert")}Alert</span>`;
  return "";
}

export const debounce = (fn, ms) => {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
};

export const storage = {
  get(key, fallback = null) {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* private mode etc. */ }
  },
};
