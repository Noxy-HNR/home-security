"""REST + WebSocket API the dashboard talks to. The browser never reaches HA or Frigate directly."""

from __future__ import annotations

import binascii
import contextlib
from collections.abc import Awaitable
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from .alarm import AlarmError
from .auth import COOKIE_NAME, Authenticator, client_id, require_user
from .db import EventFilter, event_to_dict, iso
from .services import Services

Code = Annotated[str | None, Field(default=None, max_length=12, pattern=r"^\d*$")]


def get_services(request: Request) -> Services:
    return request.app.state.services


S = Annotated[Services, Depends(get_services)]

# --- auth ------------------------------------------------------------------------------

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    password: str = Field(max_length=256)


@auth_router.get("/session")
async def session(request: Request) -> dict[str, Any]:
    auth: Authenticator = request.app.state.auth
    settings = request.app.state.settings
    return {"authenticated": auth.is_authenticated(request), "demo": settings.demo_mode, "open": auth.open}


@auth_router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, bool]:
    auth: Authenticator = request.app.state.auth
    settings = request.app.state.settings
    if not auth.check_password(body.password, client_id(request)):
        raise HTTPException(401, "Wrong password")
    response.set_cookie(
        COOKIE_NAME,
        auth.issue(),
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=settings.cookie_secure,
        path="/",
    )
    return {"ok": True}


@auth_router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


# --- everything else requires a session ------------------------------------------------

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


@router.get("/state")
async def state(s: S) -> dict[str, Any]:
    return s.state_payload()


# alarm ---------------------------------------------------------------------------------


class ArmBody(BaseModel):
    mode: Literal["stay", "away", "night"]
    code: Code = None
    instant: bool = False


class DisarmBody(BaseModel):
    code: Code = None


async def _alarm(call: Awaitable[None]) -> dict[str, bool]:
    try:
        await call
    except AlarmError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"ok": True}


@router.post("/alarm/{panel_id}/arm")
async def arm(panel_id: str, body: ArmBody, request: Request, s: S) -> dict[str, bool]:
    return await _alarm(s.alarm.arm(panel_id, body.mode, body.code or None, body.instant, client_id(request)))


@router.post("/alarm/{panel_id}/disarm")
async def disarm(panel_id: str, body: DisarmBody, request: Request, s: S) -> dict[str, bool]:
    return await _alarm(s.alarm.disarm(panel_id, body.code or None, client_id(request)))


@router.post("/alarm/{panel_id}/bypass-all")
async def bypass_all(panel_id: str, request: Request, s: S) -> dict[str, bool]:
    return await _alarm(s.alarm.panel_button(panel_id, "bypass_all", client_id(request)))


@router.post("/alarm/{panel_id}/clear-bypass")
async def clear_bypass(panel_id: str, request: Request, s: S) -> dict[str, bool]:
    return await _alarm(s.alarm.panel_button(panel_id, "clear_bypass", client_id(request)))


@router.post("/zones/{zone_id}/bypass")
async def bypass_zone(zone_id: str, request: Request, s: S) -> dict[str, bool]:
    return await _alarm(s.alarm.bypass_zone(zone_id, client_id(request)))


@router.get("/history/alarm")
async def alarm_history(s: S, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    return [
        {"time": iso(r.timestamp), "panel_id": r.panel_id, "partition": r.partition, "state": r.state,
         "previous_state": r.previous_state, "triggered_by": r.triggered_by}
        for r in await s.store.alarm_history(limit)
    ]


@router.get("/history/zones/{zone_id}")
async def zone_history(zone_id: str, s: S, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    return [
        {"time": iso(r.timestamp), "faulted": r.faulted, "low_battery": r.low_battery, "tampered": r.tampered}
        for r in await s.store.zone_history(zone_id, limit)
    ]


# cameras -------------------------------------------------------------------------------


def _camera(s: Services, name: str) -> dict[str, Any]:
    # Only names Frigate reported: the name becomes part of an upstream URL path.
    camera = s.live.camera(name)
    if camera is None:
        raise HTTPException(404, "Unknown camera")
    return camera


@router.get("/cameras")
async def cameras(s: S) -> list[dict[str, Any]]:
    return s.live.cameras


@router.get("/cameras/{name}/mjpeg")
async def camera_mjpeg(
    name: str, s: S,
    fps: int | None = Query(None, ge=1, le=15),
    height: int | None = Query(None, ge=120, le=1080),
) -> Response:
    _camera(s, name)
    return await s.media.mjpeg(name, fps or s.settings.live_fps, height or s.settings.live_height)


@router.get("/cameras/{name}/latest")
async def camera_latest(name: str, s: S, height: int | None = Query(None, ge=60, le=2160)) -> Response:
    _camera(s, name)
    return await s.media.latest(name, height)


@router.get("/cameras/{name}/live.mp4")
async def camera_live(name: str, s: S) -> Response:
    return await s.media.live_mp4(_camera(s, name)["live_stream"])


# events --------------------------------------------------------------------------------


@router.get("/events")
async def list_events(
    s: S,
    source: Annotated[list[str] | None, Query()] = None,
    type: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[list[str] | None, Query()] = None,
    subject: str | None = None,
    label: str | None = None,
    q: str | None = Query(None, max_length=100),
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    flt = EventFilter(
        sources=source, types=type, severities=severity, subject_id=subject, label=label,
        q=q or None, since=since, until=until, cursor=cursor, limit=limit,
    )
    try:
        rows, next_cursor = await s.store.query_events(flt)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(400, "Invalid cursor") from exc
    return {"events": [event_to_dict(r) for r in rows], "next_cursor": next_cursor}


@router.get("/events/facets")
async def event_facets(s: S) -> dict[str, Any]:
    return await s.store.facets()


async def _event(s: Services, event_id: int):
    row = await s.store.get_event(event_id)
    if row is None:
        raise HTTPException(404, "Unknown event")
    return row


def _archived(s: Services, relative: str | None):
    if not relative:
        return None
    root = s.settings.snapshot_dir.resolve()
    path = (root / relative).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


@router.get("/events/{event_id}")
async def get_event(event_id: int, s: S) -> dict[str, Any]:
    return event_to_dict(await _event(s, event_id))


@router.get("/events/{event_id}/snapshot")
async def event_snapshot(event_id: int, s: S) -> Response:
    row = await _event(s, event_id)
    if path := _archived(s, row.snapshot_path):
        return FileResponse(path, headers={"cache-control": "private, max-age=86400"})
    if row.source == "frigate" and row.has_snapshot and row.external_id:
        return await s.media.event_snapshot(row.external_id)
    raise HTTPException(404, "No snapshot")


@router.get("/events/{event_id}/thumbnail")
async def event_thumbnail(event_id: int, s: S) -> Response:
    row = await _event(s, event_id)
    if row.source != "frigate" or not row.external_id:
        raise HTTPException(404, "No thumbnail")
    response = await s.media.event_thumbnail(row.external_id)
    if response.status_code == 404 and (path := _archived(s, row.snapshot_path)):
        return FileResponse(path, headers={"cache-control": "private, max-age=86400"})
    return response


@router.get("/events/{event_id}/clip")
async def event_clip(event_id: int, request: Request, s: S) -> Response:
    row = await _event(s, event_id)
    if row.source != "frigate" or not row.has_clip or not row.external_id:
        raise HTTPException(404, "No clip")
    return await s.media.event_clip(row.external_id, request.headers.get("range"))


# system --------------------------------------------------------------------------------


@router.get("/system")
async def system(s: S) -> dict[str, Any]:
    st = s.settings
    return {
        "sources": s.live.sources_view(),
        "stats": s.live.frigate_stats,
        "events_total": await s.store.count_events(),
        "dashboard_clients": s.hub.client_count,
        "database": s.store.engine.dialect.name,
        "retention": {
            "snapshot_days": st.snapshot_retention_days,
            "event_days": st.event_retention_days,
            "archive_snapshots": st.archive_snapshots,
        },
        "alerts": {
            "labels": st.alert_labels, "cameras": st.alert_cameras or "all", "zones": st.alert_zones or "anywhere",
            "only_when_armed": st.alert_only_when_armed, "push": s.notifier.enabled,
        },
    }


# demo helpers --------------------------------------------------------------------------


@router.post("/demo/trigger")
async def demo_trigger(s: S) -> dict[str, bool]:
    if not s.demo_ha:
        raise HTTPException(404)
    await s.demo_ha.trigger()
    return {"ok": True}


@router.post("/demo/person/{camera}")
async def demo_person(camera: str, s: S) -> dict[str, bool]:
    if not s.demo:
        raise HTTPException(404)
    cam = _camera(s, camera)
    s.demo.spawn(s.demo.simulate_object(cam["name"], "person", cam["zones"]))
    return {"ok": True}


# --- WebSocket -------------------------------------------------------------------------


async def websocket_endpoint(websocket: WebSocket) -> None:
    app = websocket.app
    auth: Authenticator = app.state.auth
    origin = websocket.headers.get("origin")
    if origin and urlparse(origin).netloc != websocket.headers.get("host"):
        await websocket.close(code=4403)  # cross-site WebSocket hijacking guard
        return
    if not auth.is_authenticated(websocket):
        await websocket.close(code=4401)
        return
    services: Services = app.state.services
    await websocket.accept()
    queue = services.hub.subscribe()
    gone = (WebSocketDisconnect, RuntimeError, OSError)  # however the server reports a vanished client

    async def pump(scope: anyio.CancelScope) -> None:
        with contextlib.suppress(*gone):
            await websocket.send_json({"type": "hello", "state": services.state_payload()})
            while True:
                message = await queue.get()
                if message.get("type") == "resync":
                    message = {"type": "hello", "state": services.state_payload()}
                await websocket.send_json(message)
        scope.cancel()

    async def drain(scope: anyio.CancelScope) -> None:
        with contextlib.suppress(*gone):
            while True:
                await websocket.receive_text()  # clients don't send anything; this notices disconnects
        scope.cancel()

    try:
        # Re-check the session when it would have expired: the client reconnects and,
        # if the cookie is no longer valid, lands on the login screen.
        with anyio.move_on_after(services.settings.session_hours * 3600) as timer:
            async with anyio.create_task_group() as tg:
                tg.start_soon(pump, tg.cancel_scope)
                tg.start_soon(drain, tg.cancel_scope)
        if timer.cancelled_caught:
            with contextlib.suppress(*gone):
                await websocket.close(code=4001)
    finally:
        services.hub.unsubscribe(queue)
