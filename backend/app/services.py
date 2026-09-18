"""Wires the backend together and owns its background tasks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

import anyio

from .alarm import AlarmService
from .alerts import AlertEngine, Notifier
from .config import Settings, csv_list
from .db import Store
from .demo import DemoHomeAssistant, DemoMedia, DemoRunner
from .detections import DetectionService
from .ha_client import HAConnection
from .hub import Hub
from .live import LiveState
from .media import FrigateMedia, MediaBackend

log = logging.getLogger(__name__)

RETENTION_INTERVAL = 6 * 3600


@dataclass
class Services:
    settings: Settings
    store: Store
    hub: Hub
    live: LiveState
    notifier: Notifier
    alerts: AlertEngine
    alarm: AlarmService
    media: MediaBackend
    detections: DetectionService
    ha: HAConnection | None = None
    demo: DemoRunner | None = None
    demo_ha: DemoHomeAssistant | None = None
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    async def start(self) -> None:
        s = self.settings
        if s.demo_mode:
            log.warning("DEMO MODE: Home Assistant, Frigate and MQTT are simulated")
            self.demo_ha = DemoHomeAssistant(self.alarm, time_scale=s.demo_time_scale)
            self.demo = DemoRunner(self.demo_ha, self.detections, self.live, self.store, s.demo_autogen)
            self._tasks += await self.demo.start()
        else:
            for source in ("home_assistant", "frigate_api", "mqtt", "frigate"):
                self.live.set_source(source, False, "Connecting")
            self.ha = HAConnection(s.ha_url, s.ha_token, self.alarm.on_ready, self.alarm.on_state, self._ha_status)
            self.alarm.commander = self.ha
            self._tasks += [
                asyncio.create_task(self.ha.run(), name="home-assistant"),
                asyncio.create_task(self.detections.run_mqtt(), name="mqtt"),
                asyncio.create_task(self.detections.run_camera_refresh(), name="frigate-cameras"),
            ]
        self._tasks.append(asyncio.create_task(self._retention_loop(), name="retention"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.demo:
            await self.demo.stop()
        await self.media.close()
        await self.notifier.close()
        await self.store.close()

    def _ha_status(self, connected: bool, detail: str) -> None:
        if self.live.set_source("home_assistant", connected, detail):
            self.hub.publish({"type": "sources", "sources": self.live.sources_view()})

    def public_config(self) -> dict[str, Any]:
        s = self.settings
        return {
            "demo": s.demo_mode,
            "frigate_url": s.frigate_public_url or None,
            "live_fps": s.live_fps,
            "live_height": s.live_height,
            "alert_labels": csv_list(s.alert_labels),
            "alert_zones": csv_list(s.alert_zones),
            "push_enabled": self.notifier.enabled,
        }

    def state_payload(self) -> dict[str, Any]:
        return {
            "alarm": self.alarm.snapshot(),
            "cameras": self.live.cameras,
            "active": self.live.active_view(),
            "sources": self.live.sources_view(),
            "stats": self.live.frigate_stats,
            "config": self.public_config(),
        }

    # --- retention ----------------------------------------------------------------------

    async def _retention_loop(self) -> None:
        while True:
            try:
                await self.run_retention()
            except Exception:
                log.exception("retention pass failed")
            await asyncio.sleep(RETENTION_INTERVAL)

    async def run_retention(self) -> dict[str, int]:
        s = self.settings
        removed = 0
        # Files first: an event purge would otherwise orphan its archived snapshot.
        for days in {d for d in (s.snapshot_retention_days, s.event_retention_days) if d > 0}:
            for event_id, relative in await self.store.expired_snapshots(days):
                await anyio.to_thread.run_sync(self._delete_snapshot, relative)
                await self.store.set_snapshot_path(event_id, None)
                removed += 1
        purged = await self.store.purge_events(s.event_retention_days) if s.event_retention_days > 0 else 0
        if removed or purged:
            log.info("retention: removed %d snapshots, %d events", removed, purged)
        return {"snapshots": removed, "events": purged}

    def _delete_snapshot(self, relative: str) -> None:
        root = self.settings.snapshot_dir.resolve()
        path = (root / relative).resolve()
        if path.is_relative_to(root):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


async def build_services(settings: Settings) -> Services:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_url)
    await store.init()
    hub = Hub()
    live = LiveState()
    notifier = Notifier(settings)
    alarm: AlarmService | None = None
    alerts = AlertEngine(settings, hub, notifier, is_armed=lambda: alarm.is_armed() if alarm else False)
    alarm = AlarmService(settings, store, hub, alerts)
    await alarm.seed()
    media: MediaBackend = DemoMedia(live, store) if settings.demo_mode else FrigateMedia(settings)
    detections = DetectionService(settings, store, hub, live, alerts, media)
    return Services(settings, store, hub, live, notifier, alerts, alarm, media, detections)
