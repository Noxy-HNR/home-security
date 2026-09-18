"""Home Assistant WebSocket API client: one authenticated connection for state and commands."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus, WebSocketException

log = logging.getLogger(__name__)

ReadyCallback = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Awaitable[None]]
StateCallback = Callable[[str, dict[str, Any] | None], Awaitable[None]]
StatusCallback = Callable[[bool, str], None]


class HAError(Exception):
    """A request Home Assistant rejected, with its (user-presentable) message."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code  # HA error code, e.g. "service_validation_error" for a wrong PIN


class HAAuthError(HAError):
    pass


class HAConnection:
    def __init__(
        self,
        url: str,
        token: str,
        on_ready: ReadyCallback,
        on_state: StateCallback,
        on_status: StatusCallback,
    ) -> None:
        base = url.rstrip("/")
        self.ws_url = ("ws" + base[4:] if base.startswith("http") else base) + "/api/websocket"
        self._token = token
        self._on_ready = on_ready
        self._on_state = on_state
        self._on_status = on_status
        self._ws: ClientConnection | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._state_sub: int | None = None
        self._registry_sub: int | None = None
        self._states_request: int | None = None
        self._buffer: list[tuple[str, dict[str, Any] | None]] | None = None
        self._resync_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._buffer is None

    async def run(self) -> None:
        """Connect forever, reconnecting with backoff."""
        delay = 2.0
        while True:
            try:
                await self._session()
                delay = 2.0
                self._on_status(False, "Connection closed; reconnecting")
            except HAAuthError as exc:
                log.error("Home Assistant rejected the access token: %s", exc)
                self._on_status(False, "Access token rejected - check HA_TOKEN")
                await asyncio.sleep(300)
                continue
            except (OSError, TimeoutError, WebSocketException, HAError) as exc:
                detail = f"HTTP {exc.response.status_code}" if isinstance(exc, InvalidStatus) else str(exc)
                detail = detail or type(exc).__name__
                log.warning("Home Assistant connection failed: %s", detail)
                self._on_status(False, f"Unreachable: {detail}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    async def _session(self) -> None:
        async with connect(self.ws_url, max_size=64 * 1024 * 1024, open_timeout=10, ping_interval=30) as ws:
            await self._authenticate(ws)
            self._ws = ws
            self._buffer = []  # hold state events until the initial snapshot is applied
            reader = asyncio.create_task(self._reader(ws))
            try:
                self._state_sub = await self._subscribe("state_changed")
                self._registry_sub = await self._subscribe("entity_registry_updated")
                await self._load()
                # Drain in order; only switch to direct dispatch once nothing is queued,
                # so a newer event can't be applied before an older buffered one.
                while self._buffer:
                    entity_id, new_state = self._buffer.pop(0)
                    await self._on_state(entity_id, new_state)
                self._buffer = None
                self._on_status(True, "Connected")
                await reader
            finally:
                reader.cancel()
                self._ws = None
                self._buffer = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(HAError("Home Assistant connection lost"))
                self._pending.clear()

    async def probe(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """One-shot: connect, authenticate, fetch the entity registry and states (for app.check)."""
        async with connect(self.ws_url, max_size=64 * 1024 * 1024, open_timeout=10) as ws:
            await self._authenticate(ws)
            self._ws = ws
            reader = asyncio.create_task(self._reader(ws))
            try:
                registry = await self.request({"type": "config/entity_registry/list"})
                states = await self.request({"type": "get_states"})
                return registry or [], states or []
            finally:
                reader.cancel()
                self._ws = None

    async def _authenticate(self, ws: ClientConnection) -> None:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if hello.get("type") != "auth_required":
            raise HAError(f"unexpected greeting {hello.get('type')!r}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if reply.get("type") != "auth_ok":
            raise HAAuthError(reply.get("message", "auth_invalid"))

    async def _load(self) -> None:
        registry = await self.request({"type": "config/entity_registry/list"})
        states_id = self._reserve_id()
        self._states_request = states_id
        states = await self.request({"type": "get_states"}, request_id=states_id)
        await self._on_ready(registry or [], states or [])

    async def _subscribe(self, event_type: str) -> int:
        sub_id = self._reserve_id()
        await self.request({"type": "subscribe_events", "event_type": event_type}, request_id=sub_id)
        return sub_id

    async def _reader(self, ws: ClientConnection) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "result":
                if msg.get("id") == self._states_request and self._buffer is not None:
                    # Events that arrived before get_states answered are older than it.
                    self._buffer.clear()
                future = self._pending.pop(msg.get("id"), None)
                if future and not future.done():
                    if msg.get("success"):
                        future.set_result(msg.get("result"))
                    else:
                        error = msg.get("error") or {}
                        message = error.get("message") or error.get("code") or "request failed"
                        future.set_exception(HAError(message, error.get("code")))
            elif kind == "event":
                sub = msg.get("id")
                data = (msg.get("event") or {}).get("data") or {}
                if sub == self._state_sub:
                    entity_id = data.get("entity_id", "")
                    if self._buffer is not None:
                        self._buffer.append((entity_id, data.get("new_state")))
                    else:
                        await self._on_state(entity_id, data.get("new_state"))
                elif sub == self._registry_sub and self._buffer is None:
                    self._schedule_resync()

    def _schedule_resync(self) -> None:
        if self._resync_task and not self._resync_task.done():
            return

        async def resync() -> None:
            await asyncio.sleep(2)  # registry updates arrive in bursts
            try:
                await self._load()
            except HAError as exc:
                log.warning("registry resync failed: %s", exc)

        self._resync_task = asyncio.create_task(resync())

    def _reserve_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    async def request(self, payload: dict[str, Any], *, request_id: int | None = None, timeout: float = 30) -> Any:
        ws = self._ws
        if ws is None:
            raise HAError("Home Assistant is not connected")
        request_id = request_id or self._reserve_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await ws.send(json.dumps({"id": request_id, **payload}))
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise HAError("Home Assistant did not answer in time") from exc
        finally:
            self._pending.pop(request_id, None)

    async def call_service(
        self, domain: str, service: str, *, entity_id: str, data: dict[str, Any] | None = None
    ) -> None:
        # Total Connect round-trips to the Resideo cloud; arming can take 10-20 seconds.
        await self.request(
            {
                "type": "call_service",
                "domain": domain,
                "service": service,
                "service_data": data or {},
                "target": {"entity_id": entity_id},
            },
            timeout=60,
        )
