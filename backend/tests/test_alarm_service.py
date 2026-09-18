import pytest

from app.alarm import AlarmError, AlarmService
from app.db import EventFilter
from app.ha_client import HAError

from .conftest import make_alerts
from .test_totalconnect import LOC, registry, state

PANEL = "alarm_control_panel.home"
DOOR = "binary_sensor.front_door"


class FakeCommander:
    def __init__(self, error: HAError | None = None):
        self.calls: list[tuple] = []
        self.error = error

    async def call_service(self, domain, service, *, entity_id, data=None):
        self.calls.append((domain, service, entity_id, data))
        if self.error:
            raise self.error


def initial_states() -> list[dict]:
    return [
        state(PANEL, "disarmed", friendly_name="Home", code_format="number", supported_features=7),
        state(DOOR, "off", friendly_name="Front Door", device_class="door", zone_id="1"),
        state("binary_sensor.front_door_battery", "off", device_class="battery"),
        state("binary_sensor.home_power", "off"),
    ]


@pytest.fixture
async def service(settings, store, hub):
    svc = AlarmService(settings, store, hub, make_alerts(settings, hub))
    await svc.seed()
    await svc.on_ready(registry(), initial_states())
    return svc


async def events(store):
    rows, _ = await store.query_events(EventFilter(limit=200))
    return rows


async def test_first_sync_is_a_baseline_not_events(service, store, hub):
    assert await events(store) == []
    assert [r.state for r in await store.alarm_history()] == ["disarmed"]
    assert hub.of_type("alarm")[-1]["alarm"]["panels"][0]["state"] == "disarmed"


async def test_zone_open_logs_event_and_history(service, store, hub):
    await service.on_state(DOOR, state(DOOR, "on", friendly_name="Front Door", device_class="door"))
    (row,) = await events(store)
    assert (row.source, row.type, row.subject, row.label, row.severity) == ("sensor", "zone_fault", "Front Door", "open", "info")
    assert row.subject_id == f"{LOC}-1"
    history = await store.zone_history(f"{LOC}-1")
    assert [h.faulted for h in history] == [True, False]
    assert hub.of_type("event")[-1]["event"]["type"] == "zone_fault"


async def test_trigger_records_open_zones_and_raises_critical_alert(service, store, hub):
    await service.on_state(PANEL, state(PANEL, "armed_away", friendly_name="Home", supported_features=7))
    await service.on_state(DOOR, state(DOOR, "on", friendly_name="Front Door", device_class="door"))
    await service.on_state(PANEL, state(PANEL, "triggered", friendly_name="Home", supported_features=7))

    rows = await events(store)
    assert rows[0].label == "triggered" and rows[0].severity == "critical"
    assert rows[0].extra["triggered_by"] == "Front Door"
    fault = next(r for r in rows if r.type == "zone_fault")
    assert fault.severity == "warning"  # opened while armed
    alert = hub.of_type("alert")[-1]["alert"]
    assert alert["severity"] == "critical" and "Front Door" in alert["body"]
    assert (await store.alarm_history())[0].triggered_by == "Front Door"


async def test_restart_does_not_duplicate_events(settings, store, hub):
    first = AlarmService(settings, store, hub, make_alerts(settings, hub))
    await first.seed()
    await first.on_ready(registry(), initial_states())
    await first.on_state(PANEL, state(PANEL, "armed_home"))
    second = AlarmService(settings, store, hub, make_alerts(settings, hub))
    await second.seed()
    await second.on_ready(registry(), [state(PANEL, "armed_home"), *initial_states()[1:]])
    assert [r.label for r in await events(store)] == ["armed_home"]


async def test_ac_loss_is_a_warning_alert(service, store, hub):
    await service.on_state("binary_sensor.home_power", state("binary_sensor.home_power", "on"))
    (row,) = await events(store)
    assert (row.type, row.label, row.severity) == ("panel_ac_loss", "ac power lost", "warning")
    assert hub.of_type("alert")[-1]["alert"]["title"] == "AC power lost"


async def test_disarm_requires_code_when_panel_does(service):
    service.commander = FakeCommander()
    with pytest.raises(AlarmError, match="code"):
        await service.disarm(PANEL, None, "1.2.3.4")
    await service.disarm(PANEL, "1234", "1.2.3.4")
    assert service.commander.calls == [("alarm_control_panel", "alarm_disarm", PANEL, {"code": "1234"})]


async def test_arm_modes_and_instant(service):
    service.commander = FakeCommander()
    await service.arm(PANEL, "away", "1234", False, "me")
    await service.arm(PANEL, "stay", None, True, "me")
    assert service.commander.calls == [
        ("alarm_control_panel", "alarm_arm_away", PANEL, {"code": "1234"}),
        ("totalconnect", "arm_home_instant", PANEL, {}),
    ]
    with pytest.raises(AlarmError, match="Instant"):
        await service.arm(PANEL, "night", None, True, "me")


async def test_rejected_command_is_audited(service, store):
    service.commander = FakeCommander(HAError("Invalid PIN", "service_validation_error"))
    with pytest.raises(AlarmError) as info:
        await service.disarm(PANEL, "0000", "10.0.0.5")
    assert info.value.status == 400
    (audit,) = await events(store)
    assert (audit.type, audit.label, audit.severity) == ("command", "disarm", "warning")
    assert audit.extra == {"actor": "10.0.0.5", "ok": False, "error": "Invalid PIN"}


async def test_bypass_uses_zone_button(service):
    service.commander = FakeCommander()
    await service.bypass_zone(f"{LOC}-1", "me")
    await service.panel_button(PANEL, "bypass_all", "me")
    assert service.commander.calls == [
        ("button", "press", "button.front_door_bypass", {}),
        ("button", "press", "button.home_bypass_all", {}),
    ]
    with pytest.raises(AlarmError):
        await service.panel_button(PANEL, "clear_bypass", "me")  # not in this registry


async def test_commands_fail_cleanly_when_disconnected(service):
    service.commander = None
    with pytest.raises(AlarmError) as info:
        await service.arm(PANEL, "away", "1234", False, "me")
    assert info.value.status == 503
