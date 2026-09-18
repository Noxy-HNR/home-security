"""In-process fan-out of real-time messages to connected dashboard WebSockets."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

RESYNC = {"type": "resync"}


class Hub:
    def __init__(self, queue_size: int = 256):
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._clients.discard(queue)

    def publish(self, message: dict[str, Any]) -> None:
        for queue in list(self._clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A client that stalls this long has missed too much to patch up; tell it
                # to reload full state instead of silently dropping alarm updates.
                log.warning("websocket client fell behind; asking it to resync")
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(RESYNC)
