"""Camera and event media from Frigate (and go2rtc), proxied so the browser only ever talks to us."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

import anyio
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

from .config import Settings

log = logging.getLogger(__name__)

PASSTHROUGH_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")


class MediaBackend(Protocol):
    async def list_cameras(self) -> list[dict[str, Any]]: ...
    async def mjpeg(self, camera: str, fps: int, height: int) -> Response: ...
    async def latest(self, camera: str, height: int | None) -> Response: ...
    async def live_mp4(self, stream: str) -> Response: ...
    async def event_snapshot(self, frigate_id: str) -> Response: ...
    async def event_thumbnail(self, frigate_id: str) -> Response: ...
    async def event_clip(self, frigate_id: str, range_header: str | None) -> Response: ...
    async def event_snapshot_bytes(self, frigate_id: str, height: int | None = None) -> tuple[bytes, str] | None: ...
    async def close(self) -> None: ...


def cameras_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    cameras = []
    for name, cam in (config.get("cameras") or {}).items():
        cam = cam or {}
        ui = cam.get("ui") or {}
        detect = cam.get("detect") or {}
        streams = (cam.get("live") or {}).get("streams") or {}
        stream_names = list(streams.values()) if isinstance(streams, dict) else []
        cameras.append(
            {
                "name": name,
                "label": cam.get("friendly_name") or name.replace("_", " ").title(),
                "enabled": cam.get("enabled", True) is not False,
                "order": ui.get("order", 0) or 0,
                "zones": sorted((cam.get("zones") or {}).keys()),
                "live_stream": stream_names[0] if stream_names else name,
                "detect": {
                    "width": detect.get("width"),
                    "height": detect.get("height"),
                    "fps": detect.get("fps"),
                },
            }
        )
    cameras.sort(key=lambda c: (c["order"], c["name"]))
    return cameras


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


async def proxy(
    client: httpx.AsyncClient, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> Response:
    """Stream an upstream GET back to the browser, closing the upstream when the browser leaves."""
    request = client.build_request("GET", url, params=params, headers=headers)
    try:
        upstream = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        log.warning("media upstream %s failed: %s", url, exc)
        return _error(502, "Camera service unreachable")
    if upstream.status_code >= 400:
        await upstream.aclose()
        if upstream.status_code in (400, 404):
            return _error(404, "Not available")
        return _error(502, f"Camera service returned {upstream.status_code}")

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            # Starlette cancels the response task when the browser disconnects; shield the
            # close so the upstream stream (often an endless MJPEG feed) is really released.
            with anyio.CancelScope(shield=True):
                await upstream.aclose()

    passthrough = {k: v for k, v in upstream.headers.items() if k.lower() in PASSTHROUGH_HEADERS}
    passthrough["cache-control"] = "no-store"
    return StreamingResponse(body(), status_code=upstream.status_code, headers=passthrough)


class FrigateMedia:
    def __init__(self, settings: Settings):
        self.base = settings.frigate_url.rstrip("/")
        self.go2rtc = settings.go2rtc_url.rstrip("/")
        self.api = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5))
        # Live feeds never finish, so no read timeout on this client.
        self.streams = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5))

    async def close(self) -> None:
        await self.api.aclose()
        await self.streams.aclose()

    async def list_cameras(self) -> list[dict[str, Any]]:
        response = await self.api.get(f"{self.base}/api/config")
        response.raise_for_status()
        return cameras_from_config(response.json())

    async def mjpeg(self, camera: str, fps: int, height: int) -> Response:
        return await proxy(self.streams, f"{self.base}/api/{camera}", params={"fps": fps, "height": height, "bbox": 1})

    async def latest(self, camera: str, height: int | None) -> Response:
        params: dict[str, Any] = {"bbox": 1}
        if height:
            params["height"] = height
        return await proxy(self.api, f"{self.base}/api/{camera}/latest.jpg", params=params)

    async def live_mp4(self, stream: str) -> Response:
        return await proxy(self.streams, f"{self.go2rtc}/api/stream.mp4", params={"src": stream})

    async def event_snapshot(self, frigate_id: str) -> Response:
        return await proxy(self.api, f"{self.base}/api/events/{frigate_id}/snapshot.jpg", params={"bbox": 1})

    async def event_thumbnail(self, frigate_id: str) -> Response:
        return await proxy(self.api, f"{self.base}/api/events/{frigate_id}/thumbnail.webp")

    async def event_clip(self, frigate_id: str, range_header: str | None) -> Response:
        headers = {"Range": range_header} if range_header else None
        return await proxy(self.streams, f"{self.base}/api/events/{frigate_id}/clip.mp4", headers=headers)

    async def event_snapshot_bytes(self, frigate_id: str, height: int | None = None) -> tuple[bytes, str] | None:
        params: dict[str, Any] = {"bbox": 1}
        if height:
            params["height"] = height
        try:
            response = await self.api.get(f"{self.base}/api/events/{frigate_id}/snapshot.jpg", params=params)
        except httpx.HTTPError as exc:
            log.warning("snapshot fetch for %s failed: %s", frigate_id, exc)
            return None
        if response.status_code != 200:
            return None
        return response.content, response.headers.get("content-type", "image/jpeg")
