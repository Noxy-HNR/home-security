// Camera image helpers.
//
// The grid polls /latest instead of holding an MJPEG stream per camera: browsers allow only
// six HTTP/1.1 connections per host, so eight endless streams would starve every other
// request on a plain-HTTP LAN setup. Polling pauses off-screen and in background tabs.

import { signalUnauthorized } from "./api.js";

export const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";

export function pollImage(img, url, { fps = 2, onStatus = () => {} } = {}) {
  const interval = 1000 / Math.max(0.2, fps);
  let stopped = false;
  let running = false;
  let visible = true;
  let timer = null;
  let controller = null;
  let current = null;

  const active = () => !stopped && visible && document.visibilityState === "visible";

  async function tick() {
    timer = null;
    if (!active() || running) return;
    running = true;
    const started = performance.now();
    let wait = interval;
    controller = new AbortController();
    try {
      const response = await fetch(url, { signal: controller.signal, cache: "no-store", credentials: "same-origin" });
      if (response.status === 401) { signalUnauthorized(); stopped = true; return; }
      if (!response.ok) throw new Error(response.status === 404 ? "Camera unavailable" : `Camera error (${response.status})`);
      const objectUrl = URL.createObjectURL(await response.blob());
      img.src = objectUrl;
      await img.decode().catch(() => {});
      if (current) URL.revokeObjectURL(current);
      current = objectUrl;
      onStatus(null);
      wait = interval - (performance.now() - started);
    } catch (err) {
      if (err.name === "AbortError") return;
      onStatus(err.message || "Camera offline");
      wait = 5000;
    } finally {
      running = false;
    }
    schedule(wait);
  }

  function schedule(wait = 0) {
    if (timer || !active()) return;
    timer = setTimeout(tick, Math.max(0, wait));
  }

  const observer = new IntersectionObserver((entries) => {
    visible = entries.some((e) => e.isIntersecting);
    if (visible) schedule();
  });
  observer.observe(img);
  const onVisibility = () => schedule();
  document.addEventListener("visibilitychange", onVisibility);
  schedule();

  return function stop() {
    stopped = true;
    clearTimeout(timer);
    controller?.abort();
    observer.disconnect();
    document.removeEventListener("visibilitychange", onVisibility);
    if (current) URL.revokeObjectURL(current);
    img.src = BLANK;
  };
}

/** Swap an element's stream source to nothing so the browser drops the connection. */
export function releaseMedia(el) {
  if (!el) return;
  if (el.tagName === "VIDEO") {
    el.pause();
    el.removeAttribute("src");
    el.load();
  } else {
    el.src = BLANK;
  }
}
