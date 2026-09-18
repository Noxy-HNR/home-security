"""Detection service: Frigate's MQTT feed -> event log, live state, alerts and archived snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any

import aiomqtt
import anyio
import httpx

from .alerts import AlertEngine
from .config import Settings
from .db import Store, event_to_dict, utcnow
from .hub import Hub
from .live import LiveState
from .media import MediaBackend

log = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), UTC) if value else None
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _sub_label(value: Any) -> str | None:
    # Frigate sends either "name" or ["name", score]
    if isinstance(value, list | tuple):
        return str(value[0]) if value else None
    return str(value) if value else None


def summarize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    service = stats.get("service") or {}
    cameras = {
        name: {k: cam.get(k) for k in ("camera_fps", "process_fps", "skipped_fps", "detection_fps", "detection_enabled")}
        for name, cam in (stats.get("cameras") or {}).items()
        if isinstance(cam, dict)
    }
    detectors = {
        name: {"inference_ms": det.get("inference_speed")}
        for name, det in (stats.get("detectors") or {}).items()
        if isinstance(det, dict)
    }
    storage = {
        path: {k: info.get(k) for k in ("total", "used", "free")}
        for path, info in (service.get("storage") or {}).items()
        if isinstance(info, dict) and path.startswith("/media")
    }
    cpu = ((stats.get("cpu_usages") or {}).get("frigate.full_system") or {}).get("cpu")
    return {
        "detection_fps": stats.get("detection_fps"),
        "detectors": detectors,
        "cameras": cameras,
        "storage": storage,
        "cpu_percent": cpu,
        "uptime": service.get("uptime"),
        "version": service.get("version"),
    }


class DetectionService:
    def __init__(
        self, settings: Settings, store: Store, hub: Hub, live: LiveState, alerts: AlertEngine, media: MediaBackend
    ):
        self.settings = settings
        self.store = store
        self.hub = hub
        self.live = live
        self.alerts = alerts
        self.media = media
        self._warned: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stats_sent = 0.0

    def _status(self, name: str, connected: bool, detail: str) -> None:
        if self.live.set_source(name, connected, detail):
            self.hub.publish({"type": "sources", "sources": self.live.sources_view()})

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- cameras ------------------------------------------------------------------------

    async def refresh_cameras(self) -> bool:
        try:
            cameras = await self.media.list_cameras()
        except (httpx.HTTPError, ValueError) as exc:
            self._status("frigate_api", False, f"Unreachable: {exc or type(exc).__name__}")
            return False
        changed = cameras != self.live.cameras
        self.live.cameras = cameras
        self._status("frigate_api", True, f"{len(cameras)} cameras")
        if changed:
            self.hub.publish({"type": "cameras", "cameras": cameras})
        return True

    async def run_camera_refresh(self) -> None:
        while True:
            ok = await self.refresh_cameras()
            await asyncio.sleep(300 if ok else 15)

    # --- MQTT ---------------------------------------------------------------------------

    async def run_mqtt(self) -> None:
        s = self.settings
        prefix = s.frigate_topic_prefix
        delay = 2.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=s.mqtt_host,
                    port=s.mqtt_port,
                    username=s.mqtt_user or None,
                    password=s.mqtt_password or None,
                    identifier=f"security-dashboard-{secrets.token_hex(3)}",
                ) as client:
                    for topic in ("events", "available", "stats"):
                        await client.subscribe(f"{prefix}/{topic}")
                    self._status("mqtt", True, "Connected")
                    delay = 2.0
                    async for message in client.messages:
                        topic = str(message.topic).removeprefix(f"{prefix}/")
                        try:
                            await self.dispatch(topic, message.payload)
                        except Exception:
                            log.exception("failed to process MQTT message on %s", message.topic)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT connection failed: %s", exc)
                self._status("mqtt", False, f"Disconnected: {exc}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def dispatch(self, topic: str, payload: Any) -> None:
        text = payload.decode() if isinstance(payload, bytes | bytearray) else str(payload)
        if topic == "events":
            await self.handle_event(json.loads(text))
        elif topic == "stats":
            self.handle_stats(json.loads(text))
        elif topic == "available":
            self.handle_available(text.strip())

    def handle_available(self, value: str) -> None:
        details = {"online": "Online", "offline": "Offline (lost MQTT connection)", "stopped": "Stopped"}
        self._status("frigate", value == "online", details.get(value, value))
        if value == "online":
            self._spawn(self.refresh_cameras())

    def handle_stats(self, stats: dict[str, Any]) -> None:
        self.live.frigate_stats = summarize_stats(stats)
        now = time.monotonic()
        if now - self._stats_sent >= 5:
            self._stats_sent = now
            self.hub.publish({"type": "stats", "stats": self.live.frigate_stats})

    async def handle_event(self, payload: dict[str, Any]) -> None:
        after = payload.get("after") or {}
        before = payload.get("before") or {}
        frigate_id = after.get("id")
        if not frigate_id or after.get("false_positive"):
            return
        kind = payload.get("type", "update")
        camera = str(after.get("camera") or "unknown")
        camera_info = self.live.camera(camera)
        camera_label = camera_info["label"] if camera_info else camera.replace("_", " ").title()
        label = after.get("label")
        zones = list(after.get("entered_zones") or [])
        end_time = _ts(after.get("end_time"))
        ended = kind == "end" or end_time is not None

        alertable = self.alerts.matches_detection(camera, label, zones)
        if alertable:
            self._warned.add(frigate_id)
        extra = {
            "sub_label": _sub_label(after.get("sub_label")),
            "plate": after.get("recognized_license_plate"),
            "stationary": after.get("stationary"),
        }
        row, created = await self.store.upsert_external_event(
            "frigate",
            frigate_id,
            {
                "type": "detection",
                "subject": camera_label,
                "subject_id": camera,
                "label": label,
                "confidence": after.get("top_score") or after.get("score"),
                "severity": "warning" if frigate_id in self._warned else "info",
                "start_time": _ts(after.get("start_time")) or utcnow(),
                "end_time": end_time,
                "zones": zones,
                "has_clip": bool(after.get("has_clip")),
                "has_snapshot": bool(after.get("has_snapshot")),
                "extra": {k: v for k, v in extra.items() if v is not None},
            },
        )

        if ended:
            self.live.active_objects.pop(frigate_id, None)
            self._warned.discard(frigate_id)
        else:
            self.live.active_objects[frigate_id] = {
                "id": frigate_id,
                "event_id": row.id,
                "camera": camera,
                "label": label,
                "zones": zones,
                "stationary": bool(after.get("stationary")),
            }

        interesting = (
            created
            or ended
            or set(zones) != set(before.get("entered_zones") or [])
            or bool(after.get("has_snapshot")) != bool(before.get("has_snapshot"))
            or after.get("sub_label") != before.get("sub_label")
            or bool(after.get("stationary")) != bool(before.get("stationary"))
        )
        if interesting:
            self.hub.publish({"type": "event", "event": event_to_dict(row)})
            self.hub.publish({"type": "active", "active": self.live.active_view()})

        if alertable:
            await self.alerts.on_detection(
                event_id=row.id,
                frigate_id=frigate_id,
                camera=camera,
                camera_label=camera_label,
                label=label or "object",
                zones=zones,
                snapshot=lambda: self.media.event_snapshot_bytes(frigate_id, height=720),
            )

        if ended and row.has_snapshot and self.settings.archive_snapshots and not row.snapshot_path:
            self._spawn(self._archive(row.id, frigate_id, row.start_time))

    async def _archive(self, event_id: int, frigate_id: str, start_time: datetime) -> None:
        """Keep our own copy of the snapshot so the log outlives Frigate's retention."""
        result = None
        for _ in range(3):
            result = await self.media.event_snapshot_bytes(frigate_id)
            if result:
                break
            await asyncio.sleep(5)
        if not result:
            log.info("no snapshot to archive for %s", frigate_id)
            return
        data, content_type = result
        ext = "svg" if "svg" in content_type else "webp" if "webp" in content_type else "jpg"
        relative = f"{start_time:%Y/%m/%d}/{_SAFE_ID.sub('_', frigate_id)}.{ext}"
        path = self.settings.snapshot_dir / relative

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await anyio.to_thread.run_sync(write)
        await self.store.set_snapshot_path(event_id, relative)
