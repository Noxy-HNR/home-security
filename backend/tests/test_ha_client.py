"""HAConnection against a fake Home Assistant WebSocket server speaking the real protocol."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from app.alarm import AlarmService
from app.ha_client import HAConnection, HAError

from .conftest import make_alerts
from .test_totalconnect import registry, state

PANEL = "alarm_control_panel.home"
TOKEN = "good-token"


class FakeHA:
    def __init__(self):
        self.states = {
            PANEL: state(PANEL, "disarmed", friendly_name="Home", code_format="number", supported_features=7),
            "binary_sensor.front_door": state("binary_sensor.front_door", "off", friendly_name="Front Door", device_class="door"),
        }
        self.calls: list[dict] = []
        self.subscriptions: dict[str, int] = {}
        self.clients = []
        self.early_event = None  # (entity, state) sent between subscribe and get_states

    async def handler(self, ws):
        self.clients.append(ws)
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != TOKEN:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        async for raw in ws:
            msg = json.loads(raw)
            kind, mid = msg["type"], msg["id"]
            if kind == "subscribe_events":
                self.subscriptions[msg["event_type"]] = mid
                await self.reply(ws, mid, None)
            elif kind == "config/entity_registry/list":
                await self.reply(ws, mid, registry())
            elif kind == "get_states":
                if self.early_event:
                    # an event older than the snapshot: must be discarded, not replayed after it
                    await self.push(ws, *self.early_event)
                await self.reply(ws, mid, list(self.states.values()))
            elif kind == "call_service":
                self.calls.append(msg)
                if msg["service_data"].get("code") == "0000":
                    await ws.send(json.dumps({"id": mid, "type": "result", "success": False,
                                              "error": {"code": "service_validation_error", "message": "Invalid PIN"}}))
                else:
                    await self.reply(ws, mid, {"context": {}})

    async def reply(self, ws, mid, result):
        await ws.send(json.dumps({"id": mid, "type": "result", "success": True, "result": result}))

    async def push(self, ws, entity_id, new_state):
        sub = self.subscriptions["state_changed"]
        await ws.send(json.dumps({"id": sub, "type": "event", "event": {
            "event_type": "state_changed", "data": {"entity_id": entity_id, "new_state": new_state}}}))


@pytest.fixture
async def fake_ha():
    fake = FakeHA()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        fake.url = f"http://127.0.0.1:{port}"
        yield fake


async def wait_until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def start(fake, settings, store, hub, token=TOKEN):
    service = AlarmService(settings, store, hub, make_alerts(settings, hub))
    statuses = []
    conn = HAConnection(fake.url, token, service.on_ready, service.on_state, lambda c, d: statuses.append((c, d)))
    service.commander = conn
    task = asyncio.create_task(conn.run())
    return service, conn, task, statuses


async def test_sync_live_updates_and_commands(fake_ha, settings, store, hub):
    service, conn, task, statuses = await start(fake_ha, settings, store, hub)
    try:
        await wait_until(lambda: (True, "Connected") in statuses)
        assert service.snapshot()["panels"][0]["state"] == "disarmed"

        await fake_ha.push(fake_ha.clients[0], "binary_sensor.front_door",
                           state("binary_sensor.front_door", "on", friendly_name="Front Door", device_class="door"))
        await wait_until(lambda: service.snapshot()["zones"][0]["faulted"] is True)

        await service.arm(PANEL, "away", "1234", False, "test")
        call = fake_ha.calls[-1]
        assert (call["domain"], call["service"], call["target"], call["service_data"]) == (
            "alarm_control_panel", "alarm_arm_away", {"entity_id": PANEL}, {"code": "1234"})

        with pytest.raises(Exception) as info:
            await service.disarm(PANEL, "0000", "test")
        assert info.value.status == 400 and "Invalid PIN" in str(info.value)
    finally:
        task.cancel()


async def test_events_older_than_snapshot_are_dropped(fake_ha, settings, store, hub):
    fake_ha.early_event = (PANEL, state(PANEL, "armed_away", friendly_name="Home", supported_features=7))
    service, conn, task, statuses = await start(fake_ha, settings, store, hub)
    try:
        await wait_until(lambda: (True, "Connected") in statuses)
        assert service.snapshot()["panels"][0]["state"] == "disarmed"
    finally:
        task.cancel()


async def test_bad_token_is_reported(fake_ha, settings, store, hub):
    service, conn, task, statuses = await start(fake_ha, settings, store, hub, token="nope")
    try:
        await wait_until(lambda: any("token rejected" in d for _, d in statuses))
    finally:
        task.cancel()


async def test_probe_and_disconnected_requests(fake_ha):
    async def noop(*_):
        pass

    conn = HAConnection(fake_ha.url, TOKEN, noop, noop, lambda c, d: None)
    reg, states = await conn.probe()
    assert len(reg) == len(registry()) and {s["entity_id"] for s in states} == set(fake_ha.states)
    with pytest.raises(HAError, match="not connected"):
        await conn.call_service("alarm_control_panel", "alarm_disarm", entity_id=PANEL)
