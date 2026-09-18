"""Alarm service: turns Total Connect state changes into history, events and alerts; sends commands."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

from .alerts import AlertEngine
from .config import Settings
from .db import Store, event_to_dict, utcnow
from .ha_client import HAError
from .hub import Hub
from .totalconnect import (
    ARMED_STATES,
    INSTANT_SERVICES,
    MODE_SERVICES,
    STATE_LABELS,
    TRANSITIONAL_STATES,
    Change,
    TotalConnectModel,
    zone_words,
)

log = logging.getLogger(__name__)

# location flag -> (label when it turns on, label when it clears, severity when on)
LOCATION_FLAG_TEXT = {
    "ac_loss": ("AC power lost", "AC power restored", "warning"),
    "low_battery": ("Panel battery low", "Panel battery OK", "warning"),
    "tampered": ("Panel tamper", "Panel tamper cleared", "warning"),
    "fire": ("Fire alarm", "Fire alarm cleared", "critical"),
    "co": ("CO alarm", "CO alarm cleared", "critical"),
    "police": ("Police alarm", "Police alarm cleared", "critical"),
}


class Commander(Protocol):
    async def call_service(
        self, domain: str, service: str, *, entity_id: str, data: dict[str, Any] | None = None
    ) -> None: ...


class AlarmError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AlarmService:
    def __init__(self, settings: Settings, store: Store, hub: Hub, alerts: AlertEngine):
        self.settings = settings
        self.store = store
        self.hub = hub
        self.alerts = alerts
        self.model = TotalConnectModel()
        self.commander: Commander | None = None

    async def seed(self) -> None:
        zones = {
            zone_id: {"faulted": row.faulted, "low_battery": row.low_battery, "tampered": row.tampered}
            for zone_id, row in (await self.store.last_zone_states()).items()
        }
        self.model.seed(await self.store.last_alarm_states(), zones)

    def snapshot(self) -> dict[str, Any]:
        return self.model.snapshot()

    def is_armed(self) -> bool:
        return self.model.is_armed()

    # --- incoming state -----------------------------------------------------------------

    async def on_ready(self, registry: list[dict[str, Any]], states: list[dict[str, Any]]) -> None:
        self.model.load_registry(registry)
        if not self.model.panels:
            log.warning("No Total Connect alarm panel found in Home Assistant's entity registry")
        changes: list[Change] = []
        for state in states:
            entity_id = state.get("entity_id", "")
            if self.model.is_tracked(entity_id):
                changes.extend(self.model.apply(entity_id, state))
        await self._record(changes)
        self._publish()

    async def on_state(self, entity_id: str, state: dict[str, Any] | None) -> None:
        if not self.model.is_tracked(entity_id):
            return
        await self._record(self.model.apply(entity_id, state))
        self._publish()

    def _publish(self) -> None:
        self.hub.publish({"type": "alarm", "alarm": self.snapshot()})

    async def _record(self, changes: list[Change]) -> None:
        now = utcnow()
        touched_zones: set[str] = set()
        for change in changes:
            if change.target in self.model.panels:
                await self._panel_changed(change, now)
            elif change.target in self.model.zones:
                touched_zones.add(change.target)
                await self._zone_changed(change, now)
            else:
                await self._location_changed(change, now)
        for zone_id in touched_zones:
            zone = self.model.zones[zone_id]
            await self.store.add_zone_history(
                timestamp=now,
                zone_id=zone_id,
                name=zone["name"],
                faulted=zone["faulted"],
                low_battery=zone["low_battery"],
                tampered=zone["tampered"],
            )

    async def _add_event(self, **fields: Any) -> int:
        row = await self.store.add_event(**fields)
        self.hub.publish({"type": "event", "event": event_to_dict(row)})
        return row.id

    async def _panel_changed(self, change: Change, now: datetime) -> None:
        panel = self.model.panels[change.target]
        triggered_by = None
        if change.new == "triggered":
            triggered_by = ", ".join(self.model.faulted_zone_names(panel["location"])) or None
        await self.store.add_alarm_history(
            timestamp=now,
            panel_id=change.target,
            partition=panel["partition"],
            state=change.new,
            previous_state=change.old,
            triggered_by=triggered_by,
        )
        if change.old is None or change.new in TRANSITIONAL_STATES:
            return  # first sighting is a baseline; arming/disarming are only kept in history
        label = STATE_LABELS.get(change.new, change.new)
        event_id = await self._add_event(
            source="alarm",
            type="alarm_state",
            subject=panel["name"],
            subject_id=change.target,
            label=change.new,
            severity="critical" if change.new == "triggered" else "info",
            start_time=now,
            extra={"previous": change.old, "triggered_by": triggered_by, "partition": panel["partition"]},
        )
        if change.new == "triggered":
            # Best effort: Total Connect is polled every ~30 s, so this is the set of zones
            # that were open at the last poll, not a guaranteed first-trip zone.
            detail = f" Open zones: {triggered_by}." if triggered_by else ""
            self.alerts.emit("critical", "ALARM TRIGGERED", f"{panel['name']} alarm triggered.{detail}", event_id=event_id)
        elif change.new in ARMED_STATES or change.new == "disarmed":
            self.alerts.emit(
                "info", f"{panel['name']}: {label}", f"System {label.lower()}.",
                event_id=event_id, push=self.settings.notify_arm_changes,
            )

    async def _zone_changed(self, change: Change, now: datetime) -> None:
        if change.old is None:
            return
        zone = self.model.zones[change.target]
        on = bool(change.new)
        if change.field == "faulted":
            fault_word, normal_word = zone_words(zone["device_class"])
            kind = "zone_fault" if on else "zone_restore"
            label = (fault_word if on else normal_word).lower()
            severity = "warning" if on and self.model.is_armed() else "info"
        elif change.field == "low_battery":
            kind, label = ("low_battery", "low battery") if on else ("battery_ok", "battery ok")
            severity = "warning" if on else "info"
        else:
            kind, label = ("tamper", "tamper") if on else ("tamper_restore", "tamper cleared")
            severity = "warning" if on else "info"
        event_id = await self._add_event(
            source="sensor",
            type=kind,
            subject=zone["name"],
            subject_id=change.target,
            label=label,
            severity=severity,
            start_time=now,
            extra={"zone_number": zone["number"], "device_class": zone["device_class"]},
        )
        if on and change.field in ("low_battery", "tampered"):
            self.alerts.emit("warning", f"{zone['name']}: {label}", f"Zone {zone['number']} ({zone['name']}) reports {label}.", event_id=event_id)

    async def _location_changed(self, change: Change, now: datetime) -> None:
        if change.old is None or change.field not in LOCATION_FLAG_TEXT:
            return
        on_text, off_text, on_severity = LOCATION_FLAG_TEXT[change.field]
        panel = next((p for p in self.model.panels.values() if p["location"] == change.target), None)
        panel_name = panel["name"] if panel else "Alarm panel"
        text = on_text if change.new else off_text
        event_id = await self._add_event(
            source="alarm",
            type=f"panel_{change.field}",
            subject=panel_name,
            subject_id=panel["id"] if panel else change.target,
            label=text.lower(),
            severity=on_severity if change.new else "info",
            start_time=now,
        )
        if change.new:
            self.alerts.emit(on_severity, text, f"{panel_name}: {text}.", event_id=event_id)

    # --- commands -----------------------------------------------------------------------

    def _panel(self, panel_id: str) -> dict[str, Any]:
        panel = self.model.panels.get(panel_id)
        if panel is None:
            raise AlarmError("Unknown alarm panel", 404)
        return panel

    async def arm(self, panel_id: str, mode: str, code: str | None, instant: bool, actor: str) -> None:
        panel = self._panel(panel_id)
        if mode not in panel["modes"]:
            raise AlarmError(f"This panel doesn't support {mode} arming")
        if instant:
            if mode not in INSTANT_SERVICES:
                raise AlarmError("Instant arming is only available for Stay and Away")
            domain, service, data = "totalconnect", INSTANT_SERVICES[mode], {}
        else:
            domain, service = "alarm_control_panel", MODE_SERVICES[mode]
            data = {"code": code} if code else {}
        action = f"arm {mode}" + (" instant" if instant else "")
        await self._command(action, panel["name"], panel_id, domain, service, panel_id, data, actor)

    async def disarm(self, panel_id: str, code: str | None, actor: str) -> None:
        panel = self._panel(panel_id)
        if panel["code_required"] and not code:
            raise AlarmError("Enter your alarm code to disarm")
        data = {"code": code} if code else {}
        await self._command("disarm", panel["name"], panel_id, "alarm_control_panel", "alarm_disarm", panel_id, data, actor)

    async def bypass_zone(self, zone_id: str, actor: str) -> None:
        zone = self.model.zones.get(zone_id)
        if zone is None:
            raise AlarmError("Unknown zone", 404)
        if not zone.get("bypass_entity"):
            raise AlarmError("This zone can't be bypassed")
        await self._command("bypass", zone["name"], zone_id, "button", "press", zone["bypass_entity"], {}, actor)

    async def panel_button(self, panel_id: str, which: str, actor: str) -> None:
        panel = self._panel(panel_id)
        entity = self.model.locations.get(panel["location"], {}).get(f"{which}_entity")
        if not entity:
            raise AlarmError("Not supported by this panel")
        await self._command(which.replace("_", " "), panel["name"], panel_id, "button", "press", entity, {}, actor)

    async def _command(
        self, action: str, subject: str, subject_id: str, domain: str, service: str,
        entity_id: str, data: dict[str, Any], actor: str,
    ) -> None:
        """Send a command to Home Assistant and keep an audit record of who asked for what."""
        error: str | None = None
        try:
            if self.commander is None:
                raise AlarmError("Alarm control isn't connected", 503)
            await self.commander.call_service(domain, service, entity_id=entity_id, data=data)
        except HAError as exc:
            error = str(exc)
            # A wrong PIN is the user's mistake, not an upstream failure.
            raise AlarmError(error, 400 if exc.code == "service_validation_error" else 502) from exc
        except AlarmError as exc:
            error = str(exc)
            raise
        finally:
            await self._add_event(
                source="alarm",
                type="command",
                subject=subject,
                subject_id=subject_id,
                label=action,
                severity="warning" if error else "info",
                start_time=utcnow(),
                extra={"actor": actor, "ok": error is None, **({"error": error} if error else {})},
            )
