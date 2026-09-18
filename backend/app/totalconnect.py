"""The Honeywell panel as Home Assistant's Total Connect integration exposes it.

Entities are identified by their registry unique_id, not their entity_id, so renaming
entities in Home Assistant can't break the dashboard. The integration builds them as:

    alarm_control_panel   "<location>"  or "<location>_<partition>"
    zone binary_sensors   "<location>_<zone>_zone" / "_low_battery" / "_tamper"
    zone bypass button    "<location>_<zone>_bypass"
    location sensors      "<location>_low_battery" / "_tamper" / "_power" (on = AC lost)
                          "<location>_smoke" / "_carbon_monoxide" / "_police"
    location buttons      "<location>_bypass_all" / "<location>_clear_bypass"

This module is pure state-keeping; transport (WebSocket) and persistence live elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PLATFORM = "totalconnect"
UNAVAILABLE = {"unavailable", "unknown"}
ARMED_STATES = {"armed_home", "armed_away", "armed_night", "armed_custom_bypass", "armed_vacation"}
TRANSITIONAL_STATES = {"arming", "disarming"}
STATE_LABELS = {
    "disarmed": "Disarmed",
    "armed_home": "Armed Stay",
    "armed_away": "Armed Away",
    "armed_night": "Armed Night",
    "armed_custom_bypass": "Armed (custom bypass)",
    "armed_vacation": "Armed Vacation",
    "arming": "Arming",
    "disarming": "Disarming",
    "pending": "Entry delay",
    "triggered": "Triggered",
}

_ZONE_RE = re.compile(r"^(?P<loc>[^_]+)_(?P<zone>[^_]+)_(?P<key>zone|low_battery|tamper|bypass)$")
_LOCATION_RE = re.compile(
    r"^(?P<loc>[^_]+)_(?P<key>low_battery|tamper|power|smoke|carbon_monoxide|police|bypass_all|clear_bypass)$"
)
_PANEL_RE = re.compile(r"^(?P<loc>[^_]+)(?:_(?P<partition>[^_]+))?$")

LOCATION_FLAGS = {
    "location_low_battery": "low_battery",
    "location_tamper": "tampered",
    "location_power": "ac_loss",
    "location_smoke": "fire",
    "location_carbon_monoxide": "co",
    "location_police": "police",
}
ZONE_FLAGS = {"zone_low_battery": "low_battery", "zone_tamper": "tampered"}

# Home Assistant AlarmControlPanelEntityFeature bits -> Honeywell arming mode names
MODE_FEATURES = {"stay": 1, "away": 2, "night": 4}
MODE_SERVICES = {"stay": "alarm_arm_home", "away": "alarm_arm_away", "night": "alarm_arm_night"}
INSTANT_SERVICES = {"stay": "arm_home_instant", "away": "arm_away_instant"}

_ZONE_WORDS = {
    "door": ("Open", "Closed"),
    "window": ("Open", "Closed"),
    "opening": ("Open", "Closed"),
    "garage_door": ("Open", "Closed"),
    "motion": ("Motion", "Clear"),
    "occupancy": ("Occupied", "Clear"),
    "smoke": ("Smoke", "Clear"),
    "gas": ("Gas", "Clear"),
    "carbon_monoxide": ("CO", "Clear"),
    "moisture": ("Wet", "Dry"),
    "safety": ("Alert", "OK"),
    "problem": ("Problem", "OK"),
}


def zone_words(device_class: str | None) -> tuple[str, str]:
    return _ZONE_WORDS.get(device_class or "", ("Fault", "Normal"))


@dataclass(frozen=True)
class Role:
    kind: str
    location: str
    zone: str | None = None
    partition: str | None = None

    @property
    def zone_key(self) -> str:
        return f"{self.location}-{self.zone}"


@dataclass(frozen=True)
class Change:
    target: str  # panel entity id, zone key or location id
    field: str
    old: Any  # None when this is the first value ever seen
    new: Any


def classify(entity_id: str, unique_id: str) -> Role | None:
    domain = entity_id.partition(".")[0]
    if domain == "alarm_control_panel":
        match = _PANEL_RE.match(unique_id)
        return Role("panel", match["loc"], partition=match["partition"] or "1") if match else None
    if domain not in ("binary_sensor", "button"):
        return None
    is_button = domain == "button"
    # Location first: "<loc>_clear_bypass" also fits the zone pattern (zone "clear", key "bypass"),
    # while no zone unique_id can fit the location pattern.
    if match := _LOCATION_RE.match(unique_id):
        key = match["key"]
        if is_button != (key in ("bypass_all", "clear_bypass")):
            return None
        return Role(f"location_{key}", match["loc"])
    if match := _ZONE_RE.match(unique_id):
        key = match["key"]
        if is_button != (key == "bypass"):
            return None
        return Role("zone" if key == "zone" else f"zone_{key}", match["loc"], zone=match["zone"])
    return None


def supported_modes(features: Any) -> list[str]:
    try:
        bits = int(features or 0)
    except (TypeError, ValueError):
        bits = 0
    return [mode for mode, bit in MODE_FEATURES.items() if bits & bit]


def _zone_number(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (1 << 30, value)


_MISSING = object()


class TotalConnectModel:
    def __init__(self) -> None:
        self.roles: dict[str, Role] = {}
        self.panels: dict[str, dict[str, Any]] = {}
        self.zones: dict[str, dict[str, Any]] = {}
        self.locations: dict[str, dict[str, Any]] = {}
        self._known: dict[tuple[str, str], Any] = {}

    # --- setup --------------------------------------------------------------------------

    def seed(self, panel_states: dict[str, str], zone_states: dict[str, dict[str, Any]]) -> None:
        """Prime last-known values from the database so a restart doesn't log phantom changes."""
        for panel_id, state in panel_states.items():
            self._known[(panel_id, "state")] = state
        for zone_id, values in zone_states.items():
            for field in ("faulted", "low_battery", "tampered"):
                if values.get(field) is not None:
                    self._known[(zone_id, field)] = values[field]

    def load_registry(self, entries: list[dict[str, Any]]) -> None:
        roles: dict[str, Role] = {}
        for entry in entries:
            if entry.get("platform") != PLATFORM or entry.get("disabled_by"):
                continue
            role = classify(entry.get("entity_id", ""), str(entry.get("unique_id") or ""))
            if role:
                roles[entry["entity_id"]] = role
        self.roles = roles

        panel_ids = {entity_id for entity_id, role in roles.items() if role.kind == "panel"}
        zone_keys = {role.zone_key for role in roles.values() if role.zone}
        self.panels = {k: v for k, v in self.panels.items() if k in panel_ids}
        self.zones = {k: v for k, v in self.zones.items() if k in zone_keys}

        for entity_id, role in roles.items():
            location = self.locations.setdefault(role.location, _new_location(role.location))
            if role.kind == "panel":
                self.panels.setdefault(entity_id, _new_panel(entity_id, role))
            elif role.zone:
                zone = self.zones.setdefault(role.zone_key, _new_zone(role))
                if role.kind == "zone":
                    zone["entity_id"] = entity_id
                elif role.kind == "zone_bypass":
                    zone["bypass_entity"] = entity_id
            elif role.kind == "location_bypass_all":
                location["bypass_all_entity"] = entity_id
            elif role.kind == "location_clear_bypass":
                location["clear_bypass_entity"] = entity_id

    def is_tracked(self, entity_id: str) -> bool:
        return entity_id in self.roles

    # --- state --------------------------------------------------------------------------

    def apply(self, entity_id: str, state: dict[str, Any] | None) -> list[Change]:
        role = self.roles.get(entity_id)
        if role is None:
            return []
        state = state or {}
        raw = state.get("state")
        attrs = state.get("attributes") or {}
        available = raw is not None and raw not in UNAVAILABLE
        changed_at = state.get("last_changed")

        if role.kind == "panel":
            panel = self.panels[entity_id]
            panel["name"] = attrs.get("friendly_name") or panel["name"]
            panel["code_required"] = attrs.get("code_format") is not None
            panel["modes"] = supported_modes(attrs.get("supported_features"))
            panel["available"] = available
            panel["state"] = raw if available else "unavailable"
            if not available:
                return []
            panel["changed_at"] = changed_at
            return self._diff(entity_id, "state", raw)

        if role.zone:
            zone = self.zones[role.zone_key]
            if role.kind == "zone":
                zone["name"] = attrs.get("friendly_name") or zone["name"]
                zone["device_class"] = attrs.get("device_class") or zone["device_class"]
                zone["partition"] = str(attrs.get("partition") or zone["partition"])
                zone["available"] = available
                if not available:
                    return []
                zone["faulted"] = raw == "on"
                zone["changed_at"] = changed_at
                return self._diff(role.zone_key, "faulted", zone["faulted"])
            field = ZONE_FLAGS.get(role.kind)
            if field is None or not available:
                return []
            zone[field] = raw == "on"
            return self._diff(role.zone_key, field, zone[field])

        field = LOCATION_FLAGS.get(role.kind)
        if field is None or not available:
            return []
        self.locations[role.location][field] = raw == "on"
        return self._diff(role.location, field, raw == "on")

    def _diff(self, target: str, field: str, new: Any) -> list[Change]:
        old = self._known.get((target, field), _MISSING)
        if old == new:
            return []
        self._known[(target, field)] = new
        return [Change(target, field, None if old is _MISSING else old, new)]

    # --- queries ------------------------------------------------------------------------

    def is_armed(self) -> bool:
        return any(p["state"] in ARMED_STATES or p["state"] == "triggered" for p in self.panels.values())

    def faulted_zone_names(self, location: str) -> list[str]:
        return [
            zone["name"]
            for zone in self._sorted_zones()
            if zone["location"] == location and zone.get("faulted")
        ]

    def location_of(self, panel_id: str) -> str | None:
        role = self.roles.get(panel_id)
        return role.location if role else None

    def _sorted_zones(self) -> list[dict[str, Any]]:
        return sorted(self.zones.values(), key=lambda z: _zone_number(z["number"]))

    def panel_view(self, panel: dict[str, Any]) -> dict[str, Any]:
        location = self.locations.get(panel["location"], {})
        return {
            "id": panel["id"],
            "name": panel["name"],
            "partition": panel["partition"],
            "state": panel["state"],
            "available": panel["available"],
            "code_required": panel["code_required"],
            "modes": panel["modes"],
            "instant_modes": [m for m in panel["modes"] if m in INSTANT_SERVICES],
            "changed_at": panel["changed_at"],
            "ac_loss": location.get("ac_loss"),
            "low_battery": location.get("low_battery"),
            "tampered": location.get("tampered"),
            "fire": location.get("fire"),
            "co": location.get("co"),
            "police": location.get("police"),
            "can_bypass_all": bool(location.get("bypass_all_entity")),
            "can_clear_bypass": bool(location.get("clear_bypass_entity")),
        }

    def zone_view(self, zone: dict[str, Any]) -> dict[str, Any]:
        fault_word, normal_word = zone_words(zone["device_class"])
        if not zone["available"]:
            status = "Unavailable"
        elif zone["faulted"] is None:
            status = "Unknown"
        else:
            status = fault_word if zone["faulted"] else normal_word
        return {
            "id": zone["id"],
            "name": zone["name"],
            "number": zone["number"],
            "partition": zone["partition"],
            "device_class": zone["device_class"],
            "available": zone["available"],
            "faulted": zone["faulted"],
            "low_battery": zone["low_battery"],
            "tampered": zone["tampered"],
            "status": status,
            "can_bypass": bool(zone.get("bypass_entity")),
            "changed_at": zone["changed_at"],
        }

    def snapshot(self) -> dict[str, Any]:
        panels = sorted(self.panels.values(), key=lambda p: _zone_number(p["partition"]))
        return {
            "panels": [self.panel_view(p) for p in panels],
            "zones": [self.zone_view(z) for z in self._sorted_zones()],
        }


def _new_location(location_id: str) -> dict[str, Any]:
    return {
        "id": location_id,
        "low_battery": None,
        "tampered": None,
        "ac_loss": None,
        "fire": None,
        "co": None,
        "police": None,
        "bypass_all_entity": None,
        "clear_bypass_entity": None,
    }


def _new_panel(entity_id: str, role: Role) -> dict[str, Any]:
    return {
        "id": entity_id,
        "location": role.location,
        "partition": role.partition or "1",
        "name": entity_id.partition(".")[2].replace("_", " ").title(),
        "state": "unknown",
        "available": False,
        "code_required": False,
        "modes": [],
        "changed_at": None,
    }


def _new_zone(role: Role) -> dict[str, Any]:
    return {
        "id": role.zone_key,
        "location": role.location,
        "number": role.zone or "",
        "name": f"Zone {role.zone}",
        "partition": "1",
        "device_class": None,
        "available": False,
        "faulted": None,
        "low_battery": None,
        "tampered": None,
        "entity_id": None,
        "bypass_entity": None,
        "changed_at": None,
    }
