"""Decides what deserves an alert and delivers it: dashboard toast/notification + optional ntfy push."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .config import Settings, csv_list
from .db import iso, utcnow
from .hub import Hub

log = logging.getLogger(__name__)

SnapshotFetcher = Callable[[], Awaitable[tuple[bytes, str] | None]]

PRIORITY = {"critical": "5", "warning": "4", "info": "3"}
TAGS = {"critical": "rotating_light", "warning": "warning", "info": "information_source"}


def _header(value: str) -> str:
    """HTTP headers must be latin-1; ntfy accepts RFC 2047 encoded words for anything else."""
    if value.isascii():
        return value
    return "=?UTF-8?B?" + base64.b64encode(value.encode()).decode() + "?="


class Notifier:
    """ntfy (https://ntfy.sh or self-hosted). Disabled unless NTFY_TOPIC is set."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = bool(settings.ntfy_topic)
        self._client = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, alert: dict[str, Any], image: tuple[bytes, str] | None) -> None:
        if not self.enabled:
            return
        s = self.settings
        url = f"{s.ntfy_url.rstrip('/')}/{s.ntfy_topic}"
        headers = {
            "Title": _header(alert["title"]),
            "Priority": PRIORITY.get(alert["severity"], "3"),
            "Tags": TAGS.get(alert["severity"], ""),
        }
        if s.ntfy_token:
            headers["Authorization"] = f"Bearer {s.ntfy_token}"
        if s.public_url:
            target = f"#/events/{alert['event_id']}" if alert.get("event_id") else "#/alarm"
            headers["Click"] = f"{s.public_url.rstrip('/')}/{target}"
        try:
            if image:
                data, content_type = image
                ext = "svg" if "svg" in content_type else "webp" if "webp" in content_type else "jpg"
                headers["Message"] = _header(alert["body"])
                headers["Filename"] = f"snapshot.{ext}"
                response = await self._client.put(url, content=data, headers=headers)
            else:
                response = await self._client.post(url, content=alert["body"].encode(), headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("ntfy delivery failed: %s", exc)


class AlertEngine:
    def __init__(self, settings: Settings, hub: Hub, notifier: Notifier, is_armed: Callable[[], bool]):
        self.settings = settings
        self.hub = hub
        self.notifier = notifier
        self.is_armed = is_armed
        self.labels = set(csv_list(settings.alert_labels))
        self.cameras = set(csv_list(settings.alert_cameras))
        self.zones = set(csv_list(settings.alert_zones))
        self._sent: OrderedDict[str, None] = OrderedDict()
        self._camera_last: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def matches_detection(self, camera: str, label: str | None, zones: list[str]) -> bool:
        if label not in self.labels:
            return False
        if self.cameras and camera not in self.cameras:
            return False
        if self.zones and not self.zones.intersection(zones):
            return False
        return not (self.settings.alert_only_when_armed and not self.is_armed())

    async def on_detection(
        self, *, event_id: int, frigate_id: str, camera: str, camera_label: str, label: str,
        zones: list[str], snapshot: SnapshotFetcher,
    ) -> None:
        """Called on every update of a matching tracked object; alerts at most once per object."""
        if frigate_id in self._sent:
            return
        self._remember(frigate_id)
        now = time.monotonic()
        if now - self._camera_last.get(camera, -1e9) < self.settings.alert_cooldown_seconds:
            return
        self._camera_last[camera] = now
        where = f" in {', '.join(z.replace('_', ' ') for z in zones)}" if zones else ""
        self.emit(
            "warning",
            f"{label.title()} at {camera_label}",
            f"{label.title()} detected on {camera_label}{where}.",
            event_id=event_id,
            camera=camera,
            snapshot=snapshot,
            push=True,
        )

    def emit(
        self, severity: str, title: str, body: str, *, event_id: int | None = None,
        camera: str | None = None, snapshot: SnapshotFetcher | None = None, push: bool = True,
    ) -> dict[str, Any]:
        alert = {
            "id": uuid.uuid4().hex,
            "time": iso(utcnow()),
            "severity": severity,
            "title": title,
            "body": body,
            "event_id": event_id,
            "camera": camera,
        }
        self.hub.publish({"type": "alert", "alert": alert})
        if push and self.notifier.enabled:
            task = asyncio.create_task(self._push(alert, snapshot))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return alert

    async def _push(self, alert: dict[str, Any], snapshot: SnapshotFetcher | None) -> None:
        image = None
        if snapshot:
            try:
                image = await snapshot()
            except Exception:  # a missing snapshot must never block the alert itself
                log.exception("snapshot for alert failed")
        await self.notifier.send(alert, image)

    def _remember(self, frigate_id: str) -> None:
        self._sent[frigate_id] = None
        while len(self._sent) > 2000:
            self._sent.popitem(last=False)
