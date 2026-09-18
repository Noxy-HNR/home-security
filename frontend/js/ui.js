// Shared UI pieces: toasts, modals, the code keypad, notifications and the alarm sound.

import { esc, html, icon, setHTML } from "./util.js";

// ---- toasts -------------------------------------------------------------------------------

export function toast({ tone = "info", title, body = "", href = null, timeout = 6000 }) {
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.tone = tone;
  el.setAttribute("role", tone === "critical" ? "alert" : "status");
  const glyph = tone === "critical" || tone === "warning" ? "alert" : tone === "good" ? "check" : "bell";
  setHTML(el, html`
    ${icon(glyph)}
    <div><div class="t-title">${title}</div>${body ? html`<div class="t-body">${body}</div>` : ""}
      ${href ? html`<a href="${href}">View</a>` : ""}</div>
    <button class="icon-btn" type="button" aria-label="Dismiss">${icon("x")}</button>`);
  const close = () => el.remove();
  el.querySelector("button").addEventListener("click", close);
  el.querySelector("a")?.addEventListener("click", close);
  box.prepend(el);
  while (box.children.length > 4) box.lastElementChild.remove();
  if (timeout) setTimeout(close, timeout);
  return close;
}

// ---- modal ----------------------------------------------------------------------------------

export function openModal({ title, body, wide = false, onClose }) {
  const root = document.getElementById("modal-root");
  const previous = document.activeElement;
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  setHTML(backdrop, html`
    <div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${title}">
      <div class="modal-head"><h2>${title}</h2>
        <button class="icon-btn" type="button" data-close aria-label="Close">${icon("x")}</button></div>
      <div class="modal-body">${body}</div>
    </div>`);
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    backdrop.remove();
    onClose?.();
    previous?.focus?.();
  };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop || e.target.closest("[data-close]")) close();
  });
  document.addEventListener("keydown", onKey, true);
  root.append(backdrop);
  const modal = backdrop.querySelector(".modal");
  (modal.querySelector("[autofocus]") || modal.querySelector("button, input, select, video, a"))?.focus();
  return { el: modal, close };
}

export function confirmDialog({ title, message, confirm = "Confirm", tone = "primary" }) {
  return new Promise((resolve) => {
    let answered = false;
    const { el, close } = openModal({
      title,
      body: html`<p class="small">${message}</p>
        <div class="keypad-actions"><button class="btn" type="button" data-no>Cancel</button>
        <button class="btn ${tone}" type="button" data-yes autofocus>${confirm}</button></div>`,
      onClose: () => { if (!answered) resolve(false); },
    });
    el.querySelector("[data-no]").addEventListener("click", () => close());
    el.querySelector("[data-yes]").addEventListener("click", () => { answered = true; resolve(true); close(); });
  });
}

/**
 * Keypad for the alarm code. `submit(code)` runs the action; if it throws, the error is
 * shown in place and the keypad stays open so the code can be re-entered.
 */
export function promptCode({ title, action = "Enter", submit }) {
  let code = "";
  const MAX = 8;
  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "clear", "0", "back"];
  const { el, close } = openModal({
    title,
    body: html`
      <div class="keypad-display" aria-live="polite"></div>
      <div class="keypad-error" role="alert"></div>
      <div class="keypad">${keys.map((k) =>
        k === "clear" ? html`<button type="button" class="wide" data-key="clear">Clear</button>`
        : k === "back" ? html`<button type="button" class="wide" data-key="back" aria-label="Delete">⌫</button>`
        : html`<button type="button" data-key="${k}">${k}</button>`)}</div>
      <div class="keypad-actions"><button class="btn" type="button" data-close>Cancel</button>
        <button class="btn primary" type="button" data-submit>${action}</button></div>`,
  });
  const display = el.querySelector(".keypad-display");
  const error = el.querySelector(".keypad-error");
  const submitBtn = el.querySelector("[data-submit]");
  const draw = () => {
    const slots = Math.max(4, code.length);
    setHTML(display, Array.from({ length: slots }, (_, i) => `<span class="${i < code.length ? "on" : ""}"></span>`).join(""));
    display.setAttribute("aria-label", `${code.length} digits entered`);
  };
  const press = (key) => {
    if (submitBtn.classList.contains("busy")) return;
    error.textContent = "";
    if (key === "clear") code = "";
    else if (key === "back") code = code.slice(0, -1);
    else if (code.length < MAX) code += key;
    draw();
  };
  const go = async () => {
    if (!code || submitBtn.classList.contains("busy")) return;
    submitBtn.classList.add("busy");
    try {
      await submit(code);
      close();
    } catch (err) {
      error.textContent = err.message || "That didn't work";
      code = "";
      draw();
    } finally {
      submitBtn.classList.remove("busy");
    }
  };
  el.querySelector(".keypad").addEventListener("click", (e) => {
    const key = e.target.closest("[data-key]")?.dataset.key;
    if (key) press(key);
  });
  submitBtn.addEventListener("click", go);
  el.addEventListener("keydown", (e) => {
    if (/^\d$/.test(e.key)) press(e.key);
    else if (e.key === "Backspace") press("back");
    else if (e.key === "Enter") { e.preventDefault(); go(); }
  });
  el.tabIndex = -1;
  el.focus();
  draw();
}

// ---- busy buttons ---------------------------------------------------------------------------

export async function withBusy(button, fn) {
  if (!button || button.classList.contains("busy")) return;
  button.classList.add("busy");
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.classList.remove("busy");
    button.disabled = false;
  }
}

// ---- browser notifications + sound ----------------------------------------------------------

export const notifications = {
  // The Notification API only exists on secure origins (HTTPS or localhost).
  supported: "Notification" in window && window.isSecureContext,
  get enabled() { return this.supported && Notification.permission === "granted"; },
  async request() {
    if (!this.supported) return false;
    return (await Notification.requestPermission()) === "granted";
  },
  show(alert) {
    if (!this.enabled || document.visibilityState === "visible") return;
    const n = new Notification(alert.title, { body: alert.body, tag: alert.id, requireInteraction: alert.severity === "critical" });
    n.onclick = () => { window.focus(); n.close(); };
  },
};

let audio = null;
document.addEventListener("pointerdown", () => {
  // Browsers only allow audio after a user gesture; unlock it on the first click.
  if (!audio && window.AudioContext) audio = new AudioContext();
}, { once: true });

export function beep(critical = false) {
  if (!audio) return;
  const now = audio.currentTime;
  const tones = critical ? [880, 660, 880, 660, 880, 660] : [880];
  tones.forEach((freq, i) => {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.frequency.value = freq;
    osc.type = "square";
    gain.gain.setValueAtTime(0.0001, now + i * 0.28);
    gain.gain.exponentialRampToValueAtTime(0.12, now + i * 0.28 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.28 + 0.24);
    osc.connect(gain).connect(audio.destination);
    osc.start(now + i * 0.28);
    osc.stop(now + i * 0.28 + 0.26);
  });
}

export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
export { esc };
