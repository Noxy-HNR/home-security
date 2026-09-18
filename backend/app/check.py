"""Verify every integration the dashboard depends on, from where the dashboard runs.

    docker compose run --rm dashboard python -m app.check

Checks Home Assistant (token, Total Connect panel and zones), Frigate (API, cameras,
go2rtc streams) and MQTT (credentials, Frigate publishing). Exit code 1 if anything failed.
"""

from __future__ import annotations

import asyncio
import json
import sys

import aiomqtt
import httpx

from .config import Settings, get_settings
from .ha_client import HAConnection, HAError
from .media import cameras_from_config
from .totalconnect import TotalConnectModel

failures = 0


def ok(msg: str) -> None:
    print(f"  [ ok ] {msg}")


def warn(msg: str) -> None:
    print(f"  [warn] {msg}")


def fail(msg: str) -> None:
    global failures
    failures += 1
    print(f"  [FAIL] {msg}")


async def check_home_assistant(s: Settings) -> None:
    print(f"Home Assistant ({s.ha_url})")
    if not s.ha_token:
        fail("HA_TOKEN is empty: create a long-lived access token in HA (RUNBOOK Phase 1)")
        return

    def noop_status(_connected: bool, _detail: str) -> None: ...

    async def noop(*_args: object) -> None: ...

    conn = HAConnection(s.ha_url, s.ha_token, noop, noop, noop_status)
    try:
        registry, states = await asyncio.wait_for(conn.probe(), 20)
    except HAError as exc:
        fail(f"rejected: {exc}")
        return
    except (OSError, TimeoutError) as exc:
        fail(f"unreachable: {exc or type(exc).__name__}")
        return
    ok("connected and authenticated")
    model = TotalConnectModel()
    model.load_registry(registry)
    for state in states:
        model.apply(state.get("entity_id", ""), state)
    snap = model.snapshot()
    if not snap["panels"]:
        fail("no Total Connect alarm panel: add the integration in HA (Settings > Devices & services)")
        return
    for panel in snap["panels"]:
        ok(f"panel '{panel['name']}' is {panel['state']}; modes: {', '.join(panel['modes']) or 'none'}")
        if not panel["code_required"]:
            warn("'Require code' is off in the Total Connect options: anyone signed in to the dashboard can disarm")
    zones = snap["zones"]
    if not zones:
        fail("no zones: enable Sensor Activities in the Total Connect app/portal, then reload the integration")
        return
    open_zones = [z["name"] for z in zones if z["faulted"]]
    ok(f"{len(zones)} zones ({len(open_zones)} open{': ' + ', '.join(open_zones) if open_zones else ''})")
    issues = [z["name"] for z in zones if z["low_battery"] or z["tampered"]]
    if issues:
        warn(f"sensor issues: {', '.join(issues)}")
    if not any(z["can_bypass"] for z in zones):
        warn("no zone bypass buttons found (zones that can't be bypassed, or buttons disabled in HA)")


async def check_frigate(s: Settings) -> None:
    print(f"Frigate ({s.frigate_url})")
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            version = (await client.get(f"{s.frigate_url}/api/version")).text.strip()
            response = await client.get(f"{s.frigate_url}/api/config")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            fail(f"unreachable: {exc}")
            return
        ok(f"API answering, version {version}")
        cameras = cameras_from_config(response.json())
        if not cameras:
            fail("no cameras configured in Frigate")
            return
        ok(f"{len(cameras)} cameras: {', '.join(c['name'] for c in cameras)}")
        try:
            streams = (await client.get(f"{s.go2rtc_url}/api/streams")).json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            warn(f"go2rtc not reachable at {s.go2rtc_url} ({exc}); live video will fall back to MJPEG")
            return
        for camera in cameras:
            if camera["live_stream"] in streams:
                ok(f"{camera['name']}: live stream '{camera['live_stream']}' present in go2rtc")
            else:
                warn(f"{camera['name']}: no go2rtc stream '{camera['live_stream']}' (set live.streams in Frigate)")


async def check_mqtt(s: Settings) -> None:
    print(f"MQTT ({s.mqtt_host}:{s.mqtt_port})")
    prefix = s.frigate_topic_prefix
    try:
        async with aiomqtt.Client(
            hostname=s.mqtt_host, port=s.mqtt_port, username=s.mqtt_user or None,
            password=s.mqtt_password or None, identifier="security-dashboard-check",
        ) as client:
            ok("connected and authenticated")
            await client.subscribe(f"{prefix}/available")
            await client.subscribe(f"{prefix}/stats")
            try:
                async with asyncio.timeout(25):
                    async for message in client.messages:
                        topic = str(message.topic)
                        if topic.endswith("/available"):
                            state = message.payload.decode() if isinstance(message.payload, bytes) else message.payload
                            (ok if state == "online" else fail)(f"Frigate reports '{state}' on {topic}")
                            if state != "online":
                                return
                        else:
                            ok(f"Frigate stats arriving on {topic}")
                            return
            except TimeoutError:
                fail(f"nothing from Frigate on {prefix}/# within 25 s: check Frigate's mqtt settings")
    except aiomqtt.MqttError as exc:
        fail(f"cannot connect: {exc} (credentials must match MQTT_USER/MQTT_PASSWORD; re-run scripts/setup.sh)")


async def main() -> int:
    s = get_settings()
    if s.demo_mode:
        print("DEMO_MODE is on: nothing real to check.")
        return 0
    for check in (check_home_assistant, check_frigate, check_mqtt):
        await check(s)
        print()
    print("All checks passed." if not failures else f"{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.platform == "win32":  # paho-mqtt needs a selector loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
