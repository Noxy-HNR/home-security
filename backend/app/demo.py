"""DEMO_MODE: simulated Home Assistant, Frigate and MQTT.

Everything here feeds the *real* ingest paths (AlarmService.on_ready/on_state and
DetectionService.handle_event/handle_stats) with payloads shaped exactly like the real
services send, so demo mode exercises the same normalisation code production uses.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import random
import secrets
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from html import escape
from typing import Any

from starlette.responses import JSONResponse, Response, StreamingResponse

from .alarm import AlarmService
from .db import Store, iso, utcnow
from .detections import DetectionService
from .ha_client import HAError
from .live import LiveState
from .media import cameras_from_config

log = logging.getLogger(__name__)

LOCATION = "3071"
DEMO_CODE = "1234"

DEMO_FRIGATE_CONFIG = {
    "cameras": {
        "front_door": {"ui": {"order": 1}, "zones": {"porch": {}, "walkway": {}}, "live": {"streams": {"Main": "front_door"}}, "detect": {"width": 640, "height": 360, "fps": 5}},
        "driveway": {"ui": {"order": 2}, "zones": {"driveway": {}, "street": {}}, "live": {"streams": {"Main": "driveway"}}, "detect": {"width": 640, "height": 360, "fps": 5}},
        "backyard": {"ui": {"order": 3}, "zones": {"patio": {}, "gate": {}}, "live": {"streams": {"Main": "backyard"}}, "detect": {"width": 640, "height": 360, "fps": 5}},
        "garage": {"ui": {"order": 4}, "zones": {"side_door": {}}, "live": {"streams": {"Main": "garage"}}, "detect": {"width": 640, "height": 360, "fps": 5}},
    }
}

# (zone number, name, device class, can be bypassed)
DEMO_ZONES = [
    ("1", "Front Door", "door", True),
    ("2", "Back Door", "door", True),
    ("3", "Garage Entry", "door", True),
    ("4", "Living Room Window", "door", True),
    ("5", "Kitchen Window", "door", True),
    ("6", "Primary Bedroom Window", "door", True),
    ("7", "Hallway Motion", "motion", True),
    ("8", "Basement Motion", "motion", True),
    ("9", "Upstairs Smoke", "smoke", False),
    ("10", "Kitchen CO", "gas", False),
]

SCENES = {
    "front_door": ("#2b3a4a", "#3b4636", "#6b5a48"),
    "driveway": ("#27364a", "#44443f", "#5c5c64"),
    "backyard": ("#233447", "#34482f", "#5b4a3a"),
    "garage": ("#2a3340", "#3d3f44", "#6a6258"),
}
LABEL_WEIGHTS = [("person", 6), ("car", 3), ("dog", 1), ("cat", 1)]


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_")


def _state(entity_id: str, value: str, attributes: dict[str, Any]) -> dict[str, Any]:
    return {"entity_id": entity_id, "state": value, "attributes": attributes, "last_changed": iso(utcnow())}


class DemoHomeAssistant:
    """Stands in for Home Assistant + the Total Connect integration (including its quirks)."""

    PANEL = "alarm_control_panel.home"

    def __init__(self, alarm: AlarmService, require_code: bool = True, time_scale: float = 1.0):
        self.alarm = alarm
        self.require_code = require_code
        self.time_scale = time_scale
        self.registry: list[dict[str, Any]] = []
        self.states: dict[str, dict[str, Any]] = {}
        self.bypassed: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._build()

    def _add(self, entity_id: str, unique_id: str, value: str, attributes: dict[str, Any]) -> None:
        self.registry.append({"entity_id": entity_id, "unique_id": unique_id, "platform": "totalconnect", "disabled_by": None})
        if not entity_id.startswith("button."):
            self.states[entity_id] = _state(entity_id, value, attributes)

    def _build(self) -> None:
        self._add(self.PANEL, LOCATION, "disarmed", {
            "friendly_name": "Home",
            "code_format": "number" if self.require_code else None,
            "code_arm_required": self.require_code,
            "supported_features": 7,
        })
        for key, name, device_class in (
            ("low_battery", "Home Battery", "battery"),
            ("tamper", "Home Tamper", "tamper"),
            ("power", "Home Power", "power"),
            ("smoke", "Home Smoke", "smoke"),
            ("carbon_monoxide", "Home Carbon monoxide", "carbon_monoxide"),
            ("police", "Home Police emergency", None),
        ):
            attrs = {"friendly_name": name, "location_id": LOCATION}
            if device_class:
                attrs["device_class"] = device_class
            self._add(f"binary_sensor.home_{key}", f"{LOCATION}_{key}", "off", attrs)
        self._add("button.home_bypass_all", f"{LOCATION}_bypass_all", "", {})
        self._add("button.home_clear_bypass", f"{LOCATION}_clear_bypass", "", {})

        for number, name, device_class, can_bypass in DEMO_ZONES:
            slug = _slug(name)
            base = {"zone_id": number, "location_id": LOCATION, "partition": "1"}
            self._add(f"binary_sensor.{slug}", f"{LOCATION}_{number}_zone", "off",
                      {**base, "friendly_name": name, "device_class": device_class})
            battery = "on" if name == "Basement Motion" else "off"
            self._add(f"binary_sensor.{slug}_battery", f"{LOCATION}_{number}_low_battery", battery,
                      {**base, "friendly_name": f"{name} Battery", "device_class": "battery"})
            self._add(f"binary_sensor.{slug}_tamper", f"{LOCATION}_{number}_tamper", "off",
                      {**base, "friendly_name": f"{name} Tamper", "device_class": "tamper"})
            if can_bypass:
                self._add(f"button.{slug}_bypass", f"{LOCATION}_{number}_bypass", "", {})

    async def start(self) -> None:
        await self.alarm.on_ready(copy.deepcopy(self.registry), copy.deepcopy(list(self.states.values())))
        self.alarm.commander = self

    @property
    def panel_state(self) -> str:
        return self.states[self.PANEL]["state"]

    async def set(self, entity_id: str, value: str) -> None:
        state = self.states[entity_id]
        if state["state"] == value:
            return
        state["state"] = value
        state["last_changed"] = iso(utcnow())
        await self.alarm.on_state(entity_id, copy.deepcopy(state))

    def _later(self, delay: float, entity_id: str, value: str) -> None:
        async def run() -> None:
            await asyncio.sleep(delay * self.time_scale)
            await self.set(entity_id, value)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def zone_entity(self, name: str) -> str:
        return f"binary_sensor.{_slug(name)}"

    def open_zones(self) -> list[str]:
        return [
            name for _, name, device_class, _ in DEMO_ZONES
            if device_class == "door" and self.states[self.zone_entity(name)]["state"] == "on"
            and self.zone_entity(name) not in self.bypassed
        ]

    async def call_service(self, domain: str, service: str, *, entity_id: str, data: dict[str, Any] | None = None) -> None:
        await asyncio.sleep(0.8 * self.time_scale)  # the real thing round-trips to the Resideo cloud
        data = data or {}
        if domain == "alarm_control_panel":
            if self.require_code and data.get("code") != DEMO_CODE:
                raise HAError("Invalid user code", "service_validation_error")  # as HA reports it
            if service == "alarm_disarm":
                await self.set(self.PANEL, "disarming")
                self._later(1.5, self.PANEL, "disarmed")
                self.bypassed.clear()
                return
            target = {"alarm_arm_home": "armed_home", "alarm_arm_away": "armed_away", "alarm_arm_night": "armed_night"}.get(service)
        elif domain == "totalconnect":
            target = {"arm_home_instant": "armed_home", "arm_away_instant": "armed_away"}.get(service)
        elif domain == "button" and service == "press":
            await self._press(entity_id)
            return
        else:
            target = None
        if target is None:
            raise HAError(f"Service {domain}.{service} not found")
        if self.panel_state != "disarmed":
            raise HAError("Failed to arm: the system is not disarmed")
        if not_ready := self.open_zones():
            raise HAError(f"Failed to arm: zones not ready ({', '.join(not_ready)})")
        await self.set(self.PANEL, "arming")
        self._later(3, self.PANEL, target)

    async def _press(self, entity_id: str) -> None:
        if entity_id == "button.home_bypass_all":
            self.bypassed.update(self.zone_entity(name) for name in self.open_zones())
        elif entity_id == "button.home_clear_bypass":
            self.bypassed.clear()
        elif entity_id.endswith("_bypass"):
            self.bypassed.add(entity_id.replace("button.", "binary_sensor.").removesuffix("_bypass"))
        else:
            raise HAError(f"Unknown button {entity_id}")

    async def trigger(self) -> None:
        """Demo-only: simulate a break-in on the back door."""
        await self.set(self.zone_entity("Back Door"), "on")
        await self.set(self.PANEL, "triggered")


class DemoMedia:
    """Camera frames and snapshots drawn as SVG (no video; the UI falls back to MJPEG)."""

    def __init__(self, live: LiveState, store: Store):
        self.live = live
        self.store = store

    async def close(self) -> None:
        pass

    async def list_cameras(self) -> list[dict[str, Any]]:
        return cameras_from_config(DEMO_FRIGATE_CONFIG)

    def frame(self, camera: str, objects: list[dict[str, Any]], *, height: int = 360, caption: str | None = None) -> bytes:
        width = round(height * 16 / 9)
        sky, ground, building = SCENES.get(camera, SCENES["driveway"])
        label = camera.replace("_", " ").upper()
        stamp = utcnow().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 640 360">',
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{sky}"/>'
            f'<stop offset="1" stop-color="#11161d"/></linearGradient></defs>',
            '<rect width="640" height="360" fill="url(#g)"/>',
            f'<rect y="210" width="640" height="150" fill="{ground}"/>',
            f'<rect x="60" y="110" width="220" height="110" fill="{building}"/>',
            '<polygon points="50,112 170,50 290,112" fill="#3a3330"/>',
            '<rect x="150" y="150" width="40" height="70" fill="#1d1a18"/>',
            '<rect x="85" y="140" width="40" height="30" fill="#c9b27a" opacity="0.55"/>',
            '<rect x="400" y="170" width="190" height="50" fill="#262b31"/>',
            '<path d="M0 300 L640 260" stroke="#0e1115" stroke-width="18" opacity="0.5"/>',
        ]
        now = time.time()
        for obj in objects:
            seed = int(hashlib.sha1(obj["id"].encode()).hexdigest()[:6], 16)
            w, h = (120, 70) if obj.get("label") == "car" else (48, 110) if obj.get("label") == "person" else (60, 40)
            span = 640 - w - 20
            x = 10 + (seed + int((now - seed % 1000) * 18)) % span
            y = 330 - h - (seed % 40)
            name = escape(str(obj.get("label") or "object"))
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#9aa3ad" opacity="0.35"/>')
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="none" stroke="#ffcc33" stroke-width="3"/>')
            parts.append(f'<rect x="{x}" y="{y - 18}" width="{len(name) * 9 + 40}" height="18" fill="#ffcc33"/>')
            parts.append(f'<text x="{x + 4}" y="{y - 4}" font-family="monospace" font-size="13" fill="#111">{name} {80 + seed % 18}%</text>')
        parts.append(f'<text x="12" y="24" font-family="monospace" font-size="16" fill="#fff" stroke="#000" stroke-width="0.6">{escape(label)}</text>')
        parts.append(f'<text x="628" y="24" text-anchor="end" font-family="monospace" font-size="16" fill="#fff" stroke="#000" stroke-width="0.6">{stamp}</text>')
        if caption:
            parts.append(f'<text x="12" y="348" font-family="monospace" font-size="14" fill="#ffcc33">{escape(caption)}</text>')
        parts.append('<text x="628" y="348" text-anchor="end" font-family="monospace" font-size="12" fill="#8a96a3">SIMULATED</text>')
        parts.append("</svg>")
        return "".join(parts).encode()

    def _objects(self, camera: str) -> list[dict[str, Any]]:
        return [o for o in self.live.active_objects.values() if o["camera"] == camera]

    async def mjpeg(self, camera: str, fps: int, height: int) -> Response:
        boundary = "frame"
        interval = 1 / max(1, min(fps, 5))

        async def frames() -> AsyncIterator[bytes]:
            while True:
                data = self.frame(camera, self._objects(camera), height=height)
                yield (
                    f"--{boundary}\r\nContent-Type: image/svg+xml\r\nContent-Length: {len(data)}\r\n\r\n".encode()
                    + data + b"\r\n"
                )
                await asyncio.sleep(interval)

        return StreamingResponse(
            frames(), media_type=f"multipart/x-mixed-replace; boundary={boundary}", headers={"cache-control": "no-store"}
        )

    async def latest(self, camera: str, height: int | None) -> Response:
        return Response(self.frame(camera, self._objects(camera), height=height or 360), media_type="image/svg+xml")

    async def live_mp4(self, stream: str) -> Response:
        return JSONResponse({"detail": "No video in demo mode"}, status_code=404)

    async def _event_frame(self, frigate_id: str, height: int) -> bytes:
        row = await self.store.get_external_event("frigate", frigate_id)
        camera = row.subject_id if row else "driveway"
        label = row.label if row else "object"
        caption = f"{row.start_time.astimezone():%Y-%m-%d %H:%M:%S}" if row else None
        return self.frame(camera, [{"id": frigate_id, "label": label}], height=height, caption=caption)

    async def event_snapshot(self, frigate_id: str) -> Response:
        return Response(await self._event_frame(frigate_id, 360), media_type="image/svg+xml")

    async def event_thumbnail(self, frigate_id: str) -> Response:
        return Response(await self._event_frame(frigate_id, 180), media_type="image/svg+xml")

    async def event_clip(self, frigate_id: str, range_header: str | None) -> Response:
        return JSONResponse({"detail": "No clips in demo mode"}, status_code=404)

    async def event_snapshot_bytes(self, frigate_id: str, height: int | None = None) -> tuple[bytes, str] | None:
        return await self._event_frame(frigate_id, height or 360), "image/svg+xml"


class DemoRunner:
    """Background activity: people and cars walking past, doors opening, Frigate stats."""

    def __init__(self, ha: DemoHomeAssistant, detections: DetectionService, live: LiveState, store: Store, autogen: bool):
        self.ha = ha
        self.detections = detections
        self.live = live
        self.store = store
        self.autogen = autogen
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self) -> list[asyncio.Task[None]]:
        for source in ("home_assistant", "frigate_api", "mqtt", "frigate"):
            self.live.set_source(source, True, "Simulated")
        await self.detections.refresh_cameras()
        await self._backfill()
        await self.ha.start()
        self.detections.handle_stats(self._stats())
        if not self.autogen:
            return []
        return [asyncio.create_task(loop()) for loop in (self._object_loop, self._zone_loop, self._stats_loop)]

    async def stop(self) -> None:
        for task in list(self._tasks) + list(self.ha._tasks):
            task.cancel()

    # --- detections ---------------------------------------------------------------------

    async def _object_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            camera = random.choice(self.live.cameras)
            label = random.choices([l for l, _ in LABEL_WEIGHTS], [w for _, w in LABEL_WEIGHTS])[0]
            self.spawn(self.simulate_object(camera["name"], label, camera["zones"]))
            await asyncio.sleep(random.uniform(8, 20))

    async def simulate_object(self, camera: str, label: str, zones: list[str], *, fast: bool = False) -> str:
        frigate_id = f"{time.time():.6f}-{secrets.token_hex(3)}"
        score = round(random.uniform(0.72, 0.95), 3)
        after: dict[str, Any] = {
            "id": frigate_id, "camera": camera, "label": label, "sub_label": None,
            "top_score": score, "score": score, "false_positive": False,
            "start_time": time.time(), "end_time": None, "current_zones": [], "entered_zones": [],
            "has_snapshot": False, "has_clip": False, "stationary": False, "box": [100, 100, 200, 300],
        }
        pause = (lambda: 0.01) if fast else (lambda: random.uniform(2, 5))
        await self.detections.handle_event({"type": "new", "before": dict(after), "after": after})
        for step in range(random.randint(2, 4) if not fast else 2):
            await asyncio.sleep(pause())
            before, after = after, copy.deepcopy(after)
            after["has_snapshot"] = True
            after["top_score"] = min(0.99, after["top_score"] + 0.02)
            if zones and (step == 0 or random.random() < 0.5):
                zone = zones[0] if fast else random.choice(zones)
                if zone not in after["entered_zones"]:
                    after["entered_zones"].append(zone)
                after["current_zones"] = [zone]
            await self.detections.handle_event({"type": "update", "before": before, "after": after})
        await asyncio.sleep(pause())
        before, after = after, copy.deepcopy(after)
        after["end_time"] = time.time()
        await self.detections.handle_event({"type": "end", "before": before, "after": after})
        return frigate_id

    # --- zones --------------------------------------------------------------------------

    async def _zone_loop(self) -> None:
        await asyncio.sleep(5)
        busy = [(name, dc) for _, name, dc, _ in DEMO_ZONES if dc in ("door", "motion")]
        while True:
            await asyncio.sleep(random.uniform(10, 25))
            if self.ha.panel_state != "disarmed":
                continue  # an armed house stays quiet
            name, device_class = random.choice(busy)
            entity = self.ha.zone_entity(name)
            await self.ha.set(entity, "on")
            self.ha._later(random.uniform(2, 6) if device_class == "motion" else random.uniform(4, 15), entity, "off")

    # --- stats --------------------------------------------------------------------------

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            self.detections.handle_stats(self._stats())

    def _stats(self) -> dict[str, Any]:
        cameras = {
            cam["name"]: {
                "camera_fps": 15.0, "process_fps": round(random.uniform(4.6, 5.1), 1),
                "skipped_fps": round(random.uniform(0, 0.4), 1), "detection_fps": round(random.uniform(0.5, 3.5), 1),
                "detection_enabled": True,
            }
            for cam in self.live.cameras
        }
        return {
            "detection_fps": round(sum(c["detection_fps"] for c in cameras.values()), 1),
            "detectors": {"ov": {"inference_speed": round(random.uniform(14, 22), 1)}},
            "cameras": cameras,
            "cpu_usages": {"frigate.full_system": {"cpu": str(round(random.uniform(18, 34), 1))}},
            "service": {
                "uptime": int(time.monotonic()), "version": "0.18.0 (simulated)",
                "storage": {"/media/frigate/recordings": {"total": 1_900_000.0, "used": 412_000.0, "free": 1_488_000.0}},
            },
        }

    # --- history ------------------------------------------------------------------------

    async def _backfill(self) -> None:
        """Give a fresh demo database a couple of days of plausible history."""
        if await self.store.count_events():
            return
        rng = random.Random(7)
        now = utcnow()
        cams = self.live.cameras
        for _ in range(70):
            cam = rng.choice(cams)
            label = rng.choices([l for l, _ in LABEL_WEIGHTS], [w for _, w in LABEL_WEIGHTS])[0]
            start = now - timedelta(minutes=rng.uniform(20, 2880))
            zones = rng.sample(cam["zones"], k=rng.randint(0, len(cam["zones"])))
            alert = label == "person"
            await self.store.add_event(
                source="frigate", type="detection", subject=cam["label"], subject_id=cam["name"], label=label,
                confidence=round(rng.uniform(0.7, 0.97), 3), severity="warning" if alert else "info",
                start_time=start, end_time=start + timedelta(seconds=rng.uniform(8, 90)),
                external_id=f"{start.timestamp():.6f}-demo{rng.randrange(16**4):04x}", zones=zones,
                has_clip=False, has_snapshot=True, extra={},
            )
        for _ in range(45):
            _, name, device_class, _ = rng.choice([z for z in DEMO_ZONES if z[2] in ("door", "motion")])
            start = now - timedelta(minutes=rng.uniform(20, 2880))
            number = next(z[0] for z in DEMO_ZONES if z[1] == name)
            words = ("motion", "clear") if device_class == "motion" else ("open", "closed")
            for offset, (kind, word) in enumerate((("zone_fault", words[0]), ("zone_restore", words[1]))):
                await self.store.add_event(
                    source="sensor", type=kind, subject=name, subject_id=f"{LOCATION}-{number}", label=word,
                    severity="info", start_time=start + timedelta(seconds=offset * rng.uniform(5, 60)),
                    extra={"zone_number": number, "device_class": device_class},
                )
        for day in range(2):
            base = (now - timedelta(days=day)).replace(hour=7, minute=40, second=0, microsecond=0)
            for when, state, previous in ((base, "disarmed", "armed_night"), (base + timedelta(minutes=45), "armed_away", "disarmed"),
                                          (base + timedelta(hours=9, minutes=50), "disarmed", "armed_away"), (base + timedelta(hours=15, minutes=20), "armed_night", "disarmed")):
                if when > now:
                    continue
                await self.store.add_event(
                    source="alarm", type="alarm_state", subject="Home", subject_id=DemoHomeAssistant.PANEL,
                    label=state, severity="info", start_time=when, extra={"previous": previous},
                )
