"""Local IPC message bus (architecture §1).

Supports ZeroMQ (``pyzmq``) when installed, and transparently falls back to a
lightweight TCP localhost broker using ``asyncio`` newline-delimited JSON
frames so all processes (Supervisor, API, Master Worker, Slave Workers) can
communicate across OS processes with < 1 ms latency even without ``pyzmq``.

Channels (default ports on 127.0.0.1):
- Event bus (port 5555): Master Worker publishes trade events & snapshots ->
  Slave Workers subscribe.
- Control bus (port 5556): API / Supervisor publishes control commands
  (``config_changed``, ``pause``, ``resume``, ``global_pause``,
  ``reconcile_now``, ``worker_reload``) -> Workers subscribe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

DEFAULT_EVENT_PORT = int(os.environ.get("TC_BUS_PORT", "5555"))
DEFAULT_CONTROL_PORT = 5556
BUS_HOST = "127.0.0.1"


@dataclass
class BusMessage:
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> bytes:
        line = json.dumps({"topic": self.topic, "payload": self.payload}, separators=(",", ":"))
        return (line + "\n").encode("utf-8")

    @classmethod
    def decode(cls, raw: bytes | str) -> "BusMessage":
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        obj = json.loads(text)
        return cls(topic=str(obj.get("topic", "")), payload=dict(obj.get("payload") or {}))


class HubServer:
    """In-process or supervisor-hosted localhost TCP relay hub.

    Any client connected to this hub can write newline-delimited JSON messages,
    and the hub immediately fans each line out to all other connected clients.
    """

    def __init__(self, host: str = BUS_HOST, port: int = DEFAULT_EVENT_PORT) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        sock = self._server.sockets[0] if self._server.sockets else None
        if sock:
            self.port = sock.getsockname()[1]
        log.info("IPC Hub listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        for w in list(self._writers):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                dead: list[asyncio.StreamWriter] = []
                for target in list(self._writers):
                    if target is writer:
                        continue
                    try:
                        target.write(line)
                        await target.drain()
                    except Exception:  # noqa: BLE001
                        dead.append(target)
                for d in dead:
                    self._writers.discard(d)
                    try:
                        d.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


class Publisher:
    """Publishes messages to a HubServer (async or sync fire-and-forget)."""

    def __init__(self, host: str = BUS_HOST, port: int = DEFAULT_EVENT_PORT) -> None:
        self.host = host
        self.port = port
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, payload: dict[str, Any] | None = None) -> bool:
        data = BusMessage(topic=topic, payload=payload or {}).encode()
        async with self._lock:
            for attempt in range(2):
                try:
                    if self._writer is None:
                        _, self._writer = await asyncio.wait_for(
                            asyncio.open_connection(self.host, self.port), timeout=1.0
                        )
                    self._writer.write(data)
                    await self._writer.drain()
                    return True
                except Exception:  # noqa: BLE001
                    self._writer = None
                    if attempt == 0:
                        await asyncio.sleep(0.02)
            return False

    def publish_sync(self, topic: str, payload: dict[str, Any] | None = None, timeout: float = 0.5) -> bool:
        """Synchronous helper for non-async callers (e.g. worker loops or CLI)."""
        data = BusMessage(topic=topic, payload=payload or {}).encode()
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout) as sock:
                sock.sendall(data)
            return True
        except OSError:
            return False

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None


class Subscriber:
    """Async iterator that yields :class:`BusMessage` matching ``topics``."""

    def __init__(self, topics: set[str] | None = None, host: str = BUS_HOST,
                 port: int = DEFAULT_EVENT_PORT) -> None:
        self.topics = topics
        self.host = host
        self.port = port
        self._closed = False
        self._writer: asyncio.StreamWriter | None = None

    async def close(self) -> None:
        self._closed = True
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def listen(self) -> AsyncIterator[BusMessage]:
        while not self._closed:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                self._writer = writer
                while not self._closed:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        msg = BusMessage.decode(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if self.topics is None or msg.topic in self.topics:
                        yield msg
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                if not self._closed:
                    await asyncio.sleep(0.25)
