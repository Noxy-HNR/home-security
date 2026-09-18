import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from starlette.websockets import WebSocketDisconnect

from app.db import Event
from app.main import create_app

from .conftest import PASSWORD

PANEL = "alarm_control_panel.home"


def wait_for(fetch, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fetch()
        if predicate(value):
            return value
        time.sleep(0.05)
    raise AssertionError(f"condition not met; last value: {value!r}")


def panel_state(client):
    return client.get("/api/state").json()["alarm"]["panels"][0]["state"]


def test_everything_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/events").status_code == 401
    assert client.get("/api/cameras/front_door/mjpeg").status_code == 401
    assert client.get("/api/auth/session").json() == {"authenticated": False, "demo": True, "open": False}
    assert client.get("/healthz").json() == {"ok": True}


def test_login_rate_limit(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_session_cookie_and_logout(authed):
    assert authed.get("/api/auth/session").json()["authenticated"] is True
    cookie = authed.cookies.get("hs_session")
    assert cookie and "." in cookie
    authed.post("/api/auth/logout")
    assert authed.get("/api/state").status_code == 401


def test_bearer_token(settings):
    settings.api_tokens = "script-token-1"
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/state", headers={"Authorization": "Bearer script-token-1"}).status_code == 200
        assert c.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_state_payload(authed):
    state = authed.get("/api/state").json()
    assert [c["name"] for c in state["cameras"]] == ["front_door", "driveway", "backyard", "garage"]
    panel = state["alarm"]["panels"][0]
    assert panel["state"] == "disarmed" and panel["code_required"] is True
    zones = {z["name"]: z for z in state["alarm"]["zones"]}
    assert zones["Basement Motion"]["low_battery"] is True
    assert zones["Upstairs Smoke"]["can_bypass"] is False
    assert {s["id"] for s in state["sources"]} == {"home_assistant", "frigate_api", "mqtt", "frigate"}
    assert state["config"]["demo"] is True


def test_security_headers(authed):
    headers = authed.get("/api/state").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"


def test_arm_and_disarm_flow(authed):
    wrong = authed.post(f"/api/alarm/{PANEL}/arm", json={"mode": "away", "code": "9999"})
    assert wrong.status_code == 400 and "Invalid" in wrong.json()["detail"]
    assert authed.post(f"/api/alarm/{PANEL}/arm", json={"mode": "away", "code": "1234"}).status_code == 200
    wait_for(lambda: panel_state(authed), lambda s: s == "armed_away")

    assert authed.post(f"/api/alarm/{PANEL}/disarm", json={}).status_code == 400  # code required
    assert authed.post(f"/api/alarm/{PANEL}/disarm", json={"code": "1234"}).status_code == 200
    wait_for(lambda: panel_state(authed), lambda s: s == "disarmed")

    log = authed.get("/api/events", params={"source": "alarm"}).json()["events"]
    labels = [e["label"] for e in log if e["start_time"]]
    assert "armed_away" in labels and "disarmed" in labels
    audits = [e for e in log if e["type"] == "command"]
    assert {a["label"] for a in audits} == {"arm away", "disarm"}
    assert any(a["extra"]["ok"] is False for a in audits)


def test_arming_refused_with_open_zone_then_bypassed(authed):
    front = next(z for z in authed.get("/api/state").json()["alarm"]["zones"] if z["name"] == "Front Door")
    services = authed.app.state.services
    authed.portal.call(services.demo_ha.set, "binary_sensor.front_door", "on")
    refused = authed.post(f"/api/alarm/{PANEL}/arm", json={"mode": "stay", "code": "1234"})
    assert refused.status_code == 502 and "Front Door" in refused.json()["detail"]
    assert authed.post(f"/api/zones/{front['id']}/bypass").status_code == 200
    assert authed.post(f"/api/alarm/{PANEL}/arm", json={"mode": "stay", "code": "1234"}).status_code == 200
    wait_for(lambda: panel_state(authed), lambda s: s == "armed_home")


def test_validation(authed):
    assert authed.post(f"/api/alarm/{PANEL}/arm", json={"mode": "vacation"}).status_code == 422
    assert authed.post(f"/api/alarm/{PANEL}/disarm", json={"code": "12ab"}).status_code == 422
    assert authed.post("/api/alarm/alarm_control_panel.nope/disarm", json={"code": "1234"}).status_code == 404
    assert authed.post("/api/zones/nope/bypass").status_code == 404


def test_camera_names_are_whitelisted(authed):
    # "config" would otherwise proxy Frigate's /api/config, credentials and all
    assert authed.get("/api/cameras/config/latest").status_code == 404
    latest = authed.get("/api/cameras/front_door/latest")
    assert latest.status_code == 200 and latest.headers["content-type"].startswith("image/svg")


def test_events_filtering_and_paging(authed):
    everything = authed.get("/api/events", params={"limit": 200}).json()["events"]
    assert len(everything) > 100  # demo backfill

    page1 = authed.get("/api/events", params={"limit": 25}).json()
    page2 = authed.get("/api/events", params={"limit": 25, "cursor": page1["next_cursor"]}).json()
    ids1, ids2 = [e["id"] for e in page1["events"]], [e["id"] for e in page2["events"]]
    assert len(ids1) == 25 and not set(ids1) & set(ids2)
    times = [e["start_time"] for e in page1["events"] + page2["events"]]
    assert times == sorted(times, reverse=True)

    frigate = authed.get("/api/events", params={"source": "frigate", "subject": "driveway"}).json()["events"]
    assert frigate and all(e["subject_id"] == "driveway" for e in frigate)
    people = authed.get("/api/events", params={"label": "person"}).json()["events"]
    assert people and all(e["label"] == "person" for e in people)
    search = authed.get("/api/events", params={"q": "back door"}).json()["events"]
    assert search and all(e["subject"] == "Back Door" for e in search)
    assert authed.get("/api/events", params={"cursor": "garbage"}).status_code == 400

    facets = authed.get("/api/events/facets").json()
    assert "person" in facets["labels"]
    assert {"source": "frigate", "id": "driveway", "name": "Driveway"} in facets["subjects"]


def test_event_media(authed):
    event = authed.get("/api/events", params={"source": "frigate", "limit": 1}).json()["events"][0]
    assert authed.get(event["snapshot_url"]).status_code == 200
    assert authed.get(event["thumbnail_url"]).status_code == 200
    assert event["clip_url"] is None
    assert authed.get(f"/api/events/{event['id']}/clip").status_code == 404
    assert authed.get("/api/events/999999").status_code == 404


def test_websocket_pushes_live_updates(authed):
    with authed.websocket_connect("/api/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["state"]["alarm"]["panels"]
        assert authed.post("/api/demo/person/front_door").status_code == 200
        seen = set()
        while not {"event", "active"} <= seen:
            seen.add(ws.receive_json()["type"])


def test_websocket_rejects_unauthenticated_and_cross_origin(client):
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/api/ws") as ws:
            ws.receive_json()
    assert info.value.code == 4401
    client.post("/api/auth/login", json={"password": PASSWORD})
    with pytest.raises(WebSocketDisconnect) as info:
        with client.websocket_connect("/api/ws", headers={"origin": "http://evil.example"}) as ws:
            ws.receive_json()
    assert info.value.code == 4403


def test_retention_removes_old_snapshots(authed, settings):
    services = authed.app.state.services
    snap = settings.snapshot_dir / "2020/01/01/old.jpg"
    snap.parent.mkdir(parents=True)
    snap.write_bytes(b"x")
    event = authed.get("/api/events", params={"source": "frigate", "limit": 1}).json()["events"][0]

    async def age_event():
        async with services.store.sessions() as session:
            await session.execute(
                update(Event).where(Event.id == event["id"]).values(
                    snapshot_path="2020/01/01/old.jpg", start_time=datetime(2020, 1, 1, tzinfo=UTC)
                )
            )
            await session.commit()

    authed.portal.call(age_event)
    result = authed.portal.call(services.run_retention)
    assert result == {"snapshots": 1, "events": 0}
    assert not snap.exists()


def test_demo_without_password_needs_no_login(settings):
    settings.dashboard_password = ""
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/auth/session").json()["authenticated"] is True
        assert c.get("/api/state").status_code == 200


def test_real_mode_requires_a_password(settings):
    settings.demo_mode = False
    settings.dashboard_password = ""
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        create_app(settings)
