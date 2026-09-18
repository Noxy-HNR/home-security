// Event rows, shared by the Live rail and the Events page.

import { dayLabel, describe, fmtShortTime, fmtTime, html, icon, relTime, severityChip } from "../util.js";

export function feedItem(ev, { fresh = false, precise = false } = {}) {
  const d = describe(ev);
  const thumb = ev.thumbnail_url
    ? html`<img src="${ev.thumbnail_url}" alt="" loading="lazy" decoding="async">`
    : icon(d.icon);
  return html`
    <li class="feed-item ${fresh ? "fresh" : ""}" data-event="${ev.id}" tabindex="0" role="button"
        aria-label="${d.title}, ${fmtTime(ev.start_time)}">
      <span class="feed-thumb">${thumb}</span>
      <div class="feed-text"><div class="feed-title">${d.title}</div><div class="feed-sub">${d.detail}</div></div>
      <span class="feed-meta">
        <time datetime="${ev.start_time}" title="${relTime(ev.start_time)}">${precise ? fmtTime(ev.start_time) : fmtShortTime(ev.start_time)}</time>
        ${ev.in_progress ? html`<span class="row small"><span class="live-dot"></span>live</span>` : severityChip(ev.severity)}
      </span>
    </li>`;
}

export function feedList(events, { grouped = false, freshIds = new Set(), precise = false } = {}) {
  let lastDay = null;
  return events.map((ev) => {
    const item = feedItem(ev, { fresh: freshIds.has(ev.id), precise });
    if (!grouped) return item;
    const day = dayLabel(ev.start_time);
    const header = day !== lastDay ? html`<li class="feed-day" role="presentation">${day}</li>` : "";
    lastDay = day;
    return html`${header}${item}`;
  });
}

/** Click/Enter on a row opens it. */
export function bindFeed(list, open) {
  const pick = (e) => {
    const id = e.target.closest("[data-event]")?.dataset.event;
    if (id) open(Number(id));
  };
  list.addEventListener("click", pick);
  list.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(e); } });
}
