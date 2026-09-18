"""Current (non-historical) view of the world: cameras, in-progress detections, source health."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .db import iso, utcnow

SOURCE_LABELS = {
    "home_assistant": "Home Assistant",
    "frigate_api": "Frigate API",
    "mqtt": "MQTT",
    "frigate": "Frigate",
}


@dataclass
class SourceStatus:
    connected: bool = False
    detail: str = "Not connected yet"
    since: datetime = field(default_factory=utcnow)


class LiveState:
    def __init__(self) -> None:
        self.cameras: list[dict[str, Any]] = []
        self.active_objects: dict[str, dict[str, Any]] = {}  # frigate id -> summary
        self.frigate_stats: dict[str, Any] | None = None
        self.sources: dict[str, SourceStatus] = {}

    def camera_names(self) -> set[str]:
        return {camera["name"] for camera in self.cameras}

    def camera(self, name: str) -> dict[str, Any] | None:
        return next((camera for camera in self.cameras if camera["name"] == name), None)

    def set_source(self, name: str, connected: bool, detail: str = "") -> bool:
        """Record a source's health; returns True if it changed (worth broadcasting)."""
        current = self.sources.get(name)
        detail = detail or ("Connected" if connected else "Disconnected")
        if current and current.connected == connected and current.detail == detail:
            return False
        self.sources[name] = SourceStatus(connected=connected, detail=detail)
        return True

    def sources_view(self) -> list[dict[str, Any]]:
        return [
            {
                "id": name,
                "label": SOURCE_LABELS.get(name, name),
                "connected": status.connected,
                "detail": status.detail,
                "since": iso(status.since),
            }
            for name, status in self.sources.items()
        ]

    def active_view(self) -> list[dict[str, Any]]:
        return list(self.active_objects.values())
