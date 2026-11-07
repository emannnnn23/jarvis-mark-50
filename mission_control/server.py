"""The Mission Control server: a local web page's only door into a running JARVIS.

JARVIS can edit files and run commands, so this door is narrow on purpose:

- it listens on 127.0.0.1 only, on its own thread and event loop (`jarvis-mission-control`), so the
  terminal and the voice loop never wait on a browser;
- every request must name this server in its Host header (anti DNS-rebinding: a hostile site that
  points its own name at 127.0.0.1 is refused with 421) and, when it carries an Origin (and always
  for the WebSocket), come from this server's own page or the configured dev server;
- the WebSocket must present the per-run token in its first frame within AUTH_TIMEOUT; the token is
  compared in constant time and never logged, and a wrong one closes the socket after a short delay;
- clients, message rate, message size and each client's send queue are all bounded;
- every frame the server builds itself (snapshots, control frames) is scrubbed like bus events are.

Bus events reach the browser through `Hub`: it subscribes to the bus once, folds each event into the
StateTracker on the loop thread and fans it out to authenticated clients. aiohttp is imported here and
nowhere else, and this module is itself imported lazily, so JARVIS runs without it when Mission Control
is off."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hmac
import html
import json
import logging
import secrets
import socket
import threading
from collections import deque
from pathlib import Path
from typing import Any
from typing import AsyncIterator
from typing import Callable

from aiohttp import WSCloseCode
from aiohttp import WSMsgType
from aiohttp import web

from core.events import PROTOCOL_VERSION
from core.events import PUBLISH_MAX_ITEMS
from core.events import PUBLISH_MAX_STR
from core.events import scrub
from mission_control import BUILD_COMMAND
from mission_control import DIST_DIR
from mission_control.metrics import MetricsSampler
from mission_control.metrics import probe_groq
from mission_control.state import StateTracker


log = logging.getLogger("jarvis.mission_control.server")


HOST = "127.0.0.1"  # never anything else: the page must not be reachable from the network
THREAD_NAME = "jarvis-mission-control"

AUTH_TIMEOUT = 3.0  # seconds a new socket has to send its auth frame
BAD_TOKEN_DELAY = 0.25  # slows down token guessing; a stale tab only waits once
MAX_CLIENTS = 4
MAX_MESSAGES_PER_SECOND = 20
SEND_QUEUE_SIZE = 512
MAX_MESSAGE_SIZE = 65536
HEARTBEAT = 20.0
# A send or a close that has not finished in this time means the browser stopped reading (a frozen tab,
# a paused debugger): the connection is aborted, which frees its slot and its queued frames.
CLOSE_TIMEOUT = 3.0
CLOSE_REPLY_TIMEOUT = 1.0  # how long a close waits for the browser's reply; also bounds shutdown
RECENT_EVENTS = 200  # events carried in a snapshot
SNAPSHOT_DEPTH = 6  # a bus event (scrubbed at depth 4) sits two levels down in a snapshot

TICK_INTERVAL = 1.0
METRICS_INTERVAL = 2.0
PROBE_INTERVAL = 60.0
PROBE_MAX_AGE = 30.0  # a client that connects gets a fresh probe unless the last one is younger
PROBE_TIMEOUT = 3.0

CLOSE_UNAUTHORIZED = 4401
CLOSE_AUTH_TIMEOUT = 4408
CLOSE_TOO_MANY_CLIENTS = 4409
CLOSE_RATE_LIMITED = 4429
CLOSE_OVERLOADED = int(WSCloseCode.TRY_AGAIN_LATER)  # 1013: this client cannot keep up

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}; worker-src 'self' blob:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
ASSET_CACHE = "public, max-age=31536000, immutable"  # Vite names assets by content hash

NOT_BUILT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>JARVIS Mission Control</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;background:#05080d;color:#cfe8ff;margin:3rem auto;max-width:40rem;padding:0 1rem}}
code{{background:#0d1824;padding:.2rem .4rem;border-radius:4px}}</style></head>
<body><h1>Mission Control is not built yet</h1>
<p>JARVIS is running and this server works, but the page has not been built. From the JARVIS folder, run:</p>
<p><code>{command}</code></p>
<p>Then reload this page.</p></body></html>
"""


class MissionControlError(Exception):
    """Mission Control could not start (port in use, bind refused, timeout). JARVIS carries on without it."""


def _frame(obj: dict[str, Any]) -> str:
    """Serialise a server-built frame. These bypass EventBus.publish, so they are scrubbed here."""
    return json.dumps(scrub(obj, max_str=PUBLISH_MAX_STR, max_items=PUBLISH_MAX_ITEMS, depth=SNAPSHOT_DEPTH),
                      separators=(",", ":"))


INVALID_FRAME = json.dumps({"type": "error", "code": "invalid"}, separators=(",", ":"))
PONG_FRAME = json.dumps({"type": "pong"}, separators=(",", ":"))


async def _close_or_abort(ws: web.WebSocketResponse, transport: asyncio.BaseTransport | None, code: int,
                          message: bytes, timeout: float | None = None) -> None:
    """ws.close(), but bounded: aiohttp's close waits for the send buffer to drain with no timeout, so
    a peer that stopped reading would hold it (and its slot) forever. On timeout the TCP connection is
    aborted; a plain transport.close() would also wait for the buffer to flush."""
    try:
        await asyncio.wait_for(ws.close(code=code, message=message),
                               CLOSE_TIMEOUT if timeout is None else timeout)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - TimeoutError, or the socket already broke: drop it either way
        _abort(transport)


def _abort(transport: asyncio.BaseTransport | None) -> None:
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.abort()


class _Client:
    """One browser socket: its bounded send queue, the task that drains it, and its message rate."""

    def __init__(self, ws: web.WebSocketResponse, transport: asyncio.BaseTransport | None = None) -> None:
        self.ws = ws
        self.transport = transport  # aborted when the browser stops reading
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=SEND_QUEUE_SIZE)
        self.authenticated = False
        # Seqs sent in the replay or snapshot; the same events may still be on their way live.
        self.replayed: set[int] = set()
        self.sender: asyncio.Task | None = None
        self.closer: asyncio.Task | None = None
        self.closing = False
        self._received: deque[float] = deque()

    def enqueue(self, text: str) -> None:
        if self.closing:
            return
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            # A client this far behind would show stale state; better to drop it and let it reconnect.
            self.close_soon(CLOSE_OVERLOADED, b"send queue full")

    def send_event(self, event: dict[str, Any], text: str) -> None:
        seq = event.get("seq")
        if seq in self.replayed:
            self.replayed.discard(seq)  # each event is dispatched live once, so one duplicate at most
            return
        self.enqueue(text)

    def over_rate(self, now: float) -> bool:
        self._received.append(now)
        while self._received and now - self._received[0] > 1.0:
            self._received.popleft()
        return len(self._received) > MAX_MESSAGES_PER_SECOND

    def close_soon(self, code: int, message: bytes) -> None:
        if self.closing:
            return
        self.closing = True
        self.closer = asyncio.get_running_loop().create_task(
            _close_or_abort(self.ws, self.transport, code, message))

    def abort(self) -> None:
        """Drop the connection now; serve()'s receive then ends and the handler frees the slot."""
        self.closing = True
        _abort(self.transport)

    async def run_sender(self) -> None:
        try:
            while True:
                text = await self.queue.get()
                try:
                    if text is None:  # flush marker from shutdown: everything before it was sent
                        return
                    # Bounded: send_str waits for the TCP buffer to drain, forever if the peer stopped reading.
                    await asyncio.wait_for(self.ws.send_str(text), CLOSE_TIMEOUT)
                finally:
                    self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.debug("mission control client stopped reading; dropping it")
            self.abort()
        except Exception as exc:  # noqa: BLE001 - the socket went away; the handler cleans up
            log.debug("mission control send stopped: %s", type(exc).__name__)

    async def stop(self) -> None:
        tasks = [t for t in (self.sender, self.closer) if t is not None]
        if self.sender is not None:
            self.sender.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class Hub:
    """Connects the event bus, the state tracker and the metrics/probe timers to the browser clients.

    Everything here runs on the server's event loop thread, except `_on_bus_event`, which producers
    call from any thread and which only hands the event over to the loop."""

    def __init__(self, bus, tracker: StateTracker, sampler, token: str, port: int, dev_origin: str = "",
                 bridge=None, broker=None) -> None:
        self.bus = bus
        self.tracker = tracker
        self.sampler = sampler
        self.token = token
        self.port = port
        self.dev_origin = dev_origin
        self.bridge = bridge
        self.broker = broker
        self.clients: set[_Client] = set()
        # Inbound message handlers by type, after authentication; Task 9 adds the command types.
        self.handlers: dict[str, Callable[[_Client, dict[str, Any]], None]] = {"ping": self._on_ping}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._last_seq = 0
        self._session: dict[str, Any] | None = None
        self._task: Any = None
        self._metrics: dict[str, Any] | None = None
        self._network: dict[str, Any] | None = None
        self._probe_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._probe_task: asyncio.Task | None = None
        self._probe_running = threading.Event()
        self._last_probe: float | None = None

    # --- request checks ---------------------------------------------------------------------------

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    @property
    def allowed_origins(self) -> frozenset[str]:
        origins = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
        if self.dev_origin:
            origins.add(self.dev_origin)
        return frozenset(origins)

    def csp(self) -> str:
        return CSP.format(port=self.port)

    def check_token(self, value: Any) -> bool:
        """Constant-time comparison, so response timing says nothing about how much of a guess was right."""
        if not isinstance(value, str):
            return False
        return hmac.compare_digest(value.encode("utf-8", "replace"), self.token.encode("utf-8"))

    @property
    def sampler_available(self) -> bool:
        return self.sampler is not None and bool(getattr(self.sampler, "available", False))

    # --- lifecycle --------------------------------------------------------------------------------

    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self._probe_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="jarvis-mission-probe")
        if self.sampler_available:
            self.sampler.sample()  # CPU baselines are per thread: prime this one so the next read is real
        else:
            self.tracker.set_subsystem("system", status="unavailable", reason="psutil not installed")
        self._unsubscribe = self.bus.subscribe(self._on_bus_event)
        self._tasks = [self._loop.create_task(loop()) for loop in (self._tick_loop, self._metrics_loop,
                                                                     self._probe_loop)]

    async def close_clients(self) -> None:
        """Send what is queued (session.end, say) and close every socket with 1001 Going Away."""
        clients = list(self.clients)
        for client in clients:
            if client.authenticated and not client.closing and client.sender is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    client.queue.put_nowait(None)
        drains = [asyncio.wait_for(client.queue.join(), 0.5) for client in clients
                  if client.authenticated and client.sender is not None and not client.sender.done()]
        if drains:
            await asyncio.gather(*drains, return_exceptions=True)
        # Each close is bounded and aborts a browser that stopped reading, so stop() stays fast.
        await asyncio.gather(*(_close_or_abort(client.ws, client.transport, WSCloseCode.GOING_AWAY,
                                               b"JARVIS is shutting down", CLOSE_REPLY_TIMEOUT)
                               for client in clients), return_exceptions=True)

    async def cleanup(self) -> None:
        self._closed = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        probe_running = self._probe_running.is_set()
        for task in self._tasks:
            task.cancel()
        if self._probe_task is not None:
            self._probe_task.cancel()
        await asyncio.gather(*self._tasks, *([self._probe_task] if self._probe_task else []),
                             return_exceptions=True)
        self._tasks = []
        if self._probe_executor is not None:
            # A probe stuck in DNS must not hold up shutdown; an idle worker is joined at once.
            self._probe_executor.shutdown(wait=not probe_running, cancel_futures=True)
            self._probe_executor = None
        self._loop = None

    # --- bus events -------------------------------------------------------------------------------

    def _on_bus_event(self, event: dict[str, Any]) -> None:
        """Called by EventBus.publish on the producer's thread: only hand the event to the loop."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, event)
        except RuntimeError:  # the loop closed between the check and the call
            pass

    def _dispatch(self, event: dict[str, Any]) -> None:
        """Fold one event into the state and send it to every authenticated client (loop thread).

        Bus events are shared with every subscriber and the replay ring: read them, never change them."""
        if self._closed:
            return
        seq = event.get("seq")
        if isinstance(seq, int):
            self._last_seq = max(self._last_seq, seq)
        kind = event.get("type")
        if kind == "session.start":
            self._session = event
        elif kind == "task.update":
            self._task = event.get("task")
        elif kind == "system.metrics":
            self._metrics = event
        elif kind == "network.status":
            self._network = event
        change = self.tracker.apply(event)
        if change is not None:
            self.bus.publish("jarvis.state", **change)  # comes back through _dispatch like any event
        receivers = [client for client in self.clients if client.authenticated and not client.closing]
        if not receivers:
            return
        try:
            text = json.dumps(event, separators=(",", ":"))
        except (TypeError, ValueError):
            log.debug("could not serialise a %r event", kind)
            return
        for client in receivers:
            client.send_event(event, text)

    # --- snapshot ---------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything a client needs to draw the current picture from nothing (scrubbed)."""
        tracked = self.tracker.snapshot()
        recent = (self.bus.recent() or [])[-RECENT_EVENTS:]
        seq = max([self._last_seq, *(event["seq"] for event in recent)])
        pending = None
        if self.broker is not None:
            try:
                pending = self.broker.pending_payload()
            except Exception:  # noqa: BLE001 - a broker fault must not break every reconnect
                log.debug("pending_payload failed", exc_info=True)
        frame = {
            "type": "snapshot",
            "seq": seq,
            "session": self._session,
            "state": {"state": tracked["state"], "detail": tracked["detail"], "since": tracked["since"]},
            "subsystems": tracked["subsystems"],
            "task": self._task,
            "pending_confirmation": pending,
            "metrics": self._metrics,
            "network": self._network,
            "recent_events": recent,
        }
        return scrub(frame, max_str=PUBLISH_MAX_STR, max_items=PUBLISH_MAX_ITEMS, depth=SNAPSHOT_DEPTH)

    # --- clients ----------------------------------------------------------------------------------

    async def serve(self, client: _Client) -> None:
        """Authenticate one socket, then serve it until it closes."""
        ws = client.ws
        loop = asyncio.get_running_loop()
        try:
            msg = await ws.receive(timeout=AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            await _close_or_abort(ws, client.transport, CLOSE_AUTH_TIMEOUT, b"authentication timed out")
            return
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            return
        client.over_rate(loop.time())
        auth = self._parse(msg)
        if auth is None or auth.get("type") != "auth" or not self.check_token(auth.get("token")):
            await asyncio.sleep(BAD_TOKEN_DELAY)
            await _close_or_abort(ws, client.transport, CLOSE_UNAUTHORIZED, b"unauthorized")
            return
        last_seq = auth.get("last_seq")
        if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
            last_seq = None
        self._admit(client, last_seq)
        client.sender = loop.create_task(client.run_sender())
        self._maybe_probe(PROBE_MAX_AGE)
        async for msg in ws:
            if client.closing:
                break
            if msg.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                break  # ERROR (for example a frame over MAX_MESSAGE_SIZE): aiohttp has closed the socket
            if client.over_rate(loop.time()):
                client.closing = True
                await _close_or_abort(ws, client.transport, CLOSE_RATE_LIMITED, b"too many messages")
                break
            self._handle(client, msg)

    def _admit(self, client: _Client, last_seq: int | None) -> None:
        """Make an authenticated client live, then catch it up. Synchronous on purpose: no other event
        can be dispatched between marking it live and reading the ring, so nothing is lost; events
        that were both in the ring and still on their way live are dropped once by `send_event`."""
        client.authenticated = True
        client.enqueue(_frame({"type": "auth.ok", "protocol": PROTOCOL_VERSION}))
        replay = self.bus.recent(last_seq) if last_seq is not None else None
        if replay is not None:
            for event in replay:
                client.replayed.add(event["seq"])
                client.enqueue(json.dumps(event, separators=(",", ":")))
            return
        snap = self.snapshot()
        client.replayed.update(event["seq"] for event in snap["recent_events"] if isinstance(event, dict)
                               and isinstance(event.get("seq"), int))
        client.enqueue(json.dumps(snap, separators=(",", ":")))

    @staticmethod
    def _parse(msg) -> dict[str, Any] | None:
        if msg.type != WSMsgType.TEXT:
            return None
        try:
            data = json.loads(msg.data)
        except (ValueError, RecursionError):
            return None
        return data if isinstance(data, dict) else None

    def _handle(self, client: _Client, msg) -> None:
        data = self._parse(msg)
        handler = self.handlers.get(data.get("type")) if data is not None else None  # type: ignore[arg-type]
        if handler is None:
            client.enqueue(INVALID_FRAME)
            return
        try:
            handler(client, data)
        except Exception:  # noqa: BLE001 - one bad message must not drop the connection
            log.debug("mission control handler failed", exc_info=True)
            client.enqueue(INVALID_FRAME)

    def _on_ping(self, client: _Client, data: dict[str, Any]) -> None:
        client.enqueue(PONG_FRAME)

    # --- timers -----------------------------------------------------------------------------------

    def _watched(self) -> bool:
        return any(client.authenticated and not client.closing for client in self.clients)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            change = self.tracker.tick()
            if change is not None:
                self.bus.publish("jarvis.state", **change)

    async def _metrics_loop(self) -> None:
        if not self.sampler_available:
            return
        while True:
            await asyncio.sleep(METRICS_INTERVAL)
            if not self._watched():
                continue  # nobody is looking: do not spend CPU measuring the CPU
            sample = self.sampler.sample()  # always on this thread (see MetricsSampler)
            if sample:
                # Transient: metrics every 2 s would otherwise push real events out of the replay ring.
                self.bus.publish("system.metrics", _transient=True, **sample)

    async def _probe_loop(self) -> None:
        while True:
            await asyncio.sleep(PROBE_INTERVAL)
            if self._watched():
                self._maybe_probe(0.0)

    def _maybe_probe(self, max_age: float) -> None:
        loop = asyncio.get_running_loop()
        if self._probe_task is not None and not self._probe_task.done():
            return
        if self._last_probe is not None and loop.time() - self._last_probe < max_age:
            return
        self._last_probe = loop.time()
        self._probe_task = loop.create_task(self._probe())

    def _run_probe(self) -> dict[str, Any]:
        self._probe_running.set()
        try:
            return probe_groq(PROBE_TIMEOUT)
        finally:
            self._probe_running.clear()

    async def _probe(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            # probe_groq blocks (DNS has no timeout), so it never runs on the loop itself.
            payload = await loop.run_in_executor(self._probe_executor, self._run_probe)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed probe is simply no news
            log.debug("groq probe failed", exc_info=True)
            return
        if not self._closed and isinstance(payload, dict):
            self.bus.publish("network.status", **payload)


# --- the aiohttp application ------------------------------------------------------------------------


HUB_KEY = web.AppKey("mission_control_hub", Hub)
DIST_KEY = web.AppKey("mission_control_dist", Path)


@web.middleware
async def _guard(request: web.Request, handler):
    """Host and Origin checks for every request, before any handler runs."""
    hub = request.app[HUB_KEY]
    hosts = request.headers.getall("Host", [])
    if len(hosts) != 1 or hosts[0].lower() not in hub.allowed_hosts:
        raise web.HTTPMisdirectedRequest(text="Misdirected request.")
    origins = request.headers.getall("Origin", [])
    is_socket = request.path == "/ws" or request.headers.get("Upgrade", "").lower() == "websocket"
    if origins or is_socket:
        # A browser always sends Origin on a WebSocket; one without it is not our page.
        if len(origins) != 1 or origins[0] not in hub.allowed_origins:
            raise web.HTTPForbidden(text="Forbidden.")
    return await handler(request)


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    hub = request.app[HUB_KEY]
    headers = response.headers
    headers["Content-Security-Policy"] = hub.csp()
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/assets/") and response.status < 400:
        headers["Cache-Control"] = ASSET_CACHE
    else:
        headers["Cache-Control"] = "no-store"


async def _index(request: web.Request) -> web.StreamResponse:
    index = request.app[DIST_KEY] / "index.html"
    if index.is_file() and not index.is_symlink():
        return web.FileResponse(index)
    return web.Response(text=NOT_BUILT_PAGE.format(command=html.escape(BUILD_COMMAND)), content_type="text/html")


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "protocol": PROTOCOL_VERSION})


async def _snapshot(request: web.Request) -> web.Response:
    hub = request.app[HUB_KEY]
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hub.check_token(value):
        raise web.HTTPUnauthorized(text="Unauthorized.", headers={"WWW-Authenticate": "Bearer"})
    return web.json_response(hub.snapshot())


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    hub = request.app[HUB_KEY]
    ws = web.WebSocketResponse(heartbeat=HEARTBEAT, max_msg_size=MAX_MESSAGE_SIZE, timeout=CLOSE_REPLY_TIMEOUT)
    if len(hub.clients) >= MAX_CLIENTS:
        await ws.prepare(request)
        await _close_or_abort(ws, request.transport, CLOSE_TOO_MANY_CLIENTS, b"too many clients")
        return ws
    client = _Client(ws, request.transport)
    hub.clients.add(client)  # before the first await, so two racing sockets cannot both take the last slot
    try:
        await ws.prepare(request)
        await hub.serve(client)
    finally:
        hub.clients.discard(client)
        await client.stop()
    return ws


async def _hub_context(app: web.Application) -> AsyncIterator[None]:
    hub = app[HUB_KEY]
    await hub.startup()
    try:
        yield
    finally:
        await hub.cleanup()


async def _close_clients(app: web.Application) -> None:
    await app[HUB_KEY].close_clients()


def create_app(hub: Hub, *, dist_dir: Path | None = None) -> web.Application:
    """The aiohttp application; `dist_dir` is the built page (default mission_control/web/dist)."""
    app = web.Application(middlewares=[_guard], client_max_size=MAX_MESSAGE_SIZE)
    app[HUB_KEY] = hub
    dist = Path(dist_dir) if dist_dir is not None else DIST_DIR
    app[DIST_KEY] = dist
    app.on_response_prepare.append(_security_headers)
    app.cleanup_ctx.append(_hub_context)
    app.on_shutdown.append(_close_clients)
    app.router.add_get("/", _index)
    app.router.add_get("/api/health", _health)
    app.router.add_get("/api/snapshot", _snapshot)
    app.router.add_get("/ws", _websocket)
    assets = dist / "assets"
    if assets.is_dir():
        # Built after JARVIS started? Restart it to serve the assets. Symlinks are never followed.
        app.router.add_static("/assets", assets, follow_symlinks=False, show_index=False)
    return app


# --- the server thread ------------------------------------------------------------------------------


class _ForwardToJarvisLog(logging.Handler):
    """Hands aiohttp's records to the JARVIS logger, whose handlers write the (redacted) log file."""

    def emit(self, record: logging.LogRecord) -> None:
        logging.getLogger("jarvis").handle(record)


def _route_aiohttp_logs() -> None:
    """aiohttp logs through its own loggers, which JARVIS never configures, so a warning (a client that
    hung up mid-request, say) would reach logging's last-resort handler and be printed into the terminal
    prompt. Send those records to the JARVIS log file instead. Idempotent."""
    logger = logging.getLogger("aiohttp")
    if any(isinstance(handler, _ForwardToJarvisLog) for handler in logger.handlers):
        return
    logger.addHandler(_ForwardToJarvisLog())
    logger.propagate = False


def _bind(port: int) -> socket.socket:
    """Bind 127.0.0.1:port ourselves, so the real port is known before anything can connect.

    SO_REUSEADDR lets a restart rebind straight after connections closed (TIME_WAIT); on Linux it does
    not let two servers listen on one port, so "port in use" is still detected. SO_REUSEPORT is never set."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        raise
    return sock


def _bind_error(port: int, exc: OSError) -> str:
    if exc.errno in (98, 48):  # EADDRINUSE (Linux, macOS)
        return (f"port {port} on 127.0.0.1 is already in use (another JARVIS or another app?). "
                "Set JARVIS_UI_PORT to a free port.")
    return f"could not listen on 127.0.0.1:{port} ({exc.strerror or type(exc).__name__})."


class MissionControlServer:
    """Runs the app on its own thread and event loop. `start` returns once it is listening."""

    def __init__(self, bus, *, port: int, dist_dir: Path, dev_origin: str = "", token: str | None = None,
                 sampler=None, bridge=None, broker=None) -> None:
        self.bus = bus
        self.port = port
        self.dist_dir = Path(dist_dir)
        self.dev_origin = dev_origin
        self.token = token or secrets.token_urlsafe(32)
        self.bridge = bridge
        self.broker = broker
        self.hub: Hub | None = None
        self._sampler = sampler
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def __repr__(self) -> str:  # never show the token
        return f"MissionControlServer(port={self.port})"

    def start(self, timeout: float = 5.0) -> str:
        """Start listening and return the base URL. Raises MissionControlError when that fails."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.url
            _route_aiohttp_logs()
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._stop_event = asyncio.Event()
            ready = threading.Event()
            outcome: dict[str, str] = {}
            self._thread = threading.Thread(target=self._run, args=(loop, ready, outcome), name=THREAD_NAME,
                                            daemon=True)  # a stuck server must never keep JARVIS from exiting
            self._thread.start()
            if not ready.wait(timeout):
                self._stop_locked(1.0)
                raise MissionControlError(f"the server did not start within {timeout:g} s.")
            if "error" in outcome:
                self._thread.join(timeout)
                self._thread = None
                raise MissionControlError(outcome["error"])
            return self.url

    def stop(self, timeout: float = 3.0) -> None:
        """Close every client and stop the thread. Safe to call more than once, or before start."""
        with self._lock:
            self._stop_locked(timeout)

    def _stop_locked(self, timeout: float) -> None:
        thread = self._thread
        if thread is None:
            return
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            with contextlib.suppress(RuntimeError):  # the loop already closed
                loop.call_soon_threadsafe(event.set)
        thread.join(timeout)
        if thread.is_alive():
            log.warning("Mission Control did not stop within %g s", timeout)
            return
        self._thread = None

    def _run(self, loop: asyncio.AbstractEventLoop, ready: threading.Event, outcome: dict[str, str]) -> None:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="jarvis-mission-io")
        loop.set_default_executor(executor)  # static files are read here; named so leaks are visible
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve(ready, outcome))
        except Exception as exc:  # noqa: BLE001 - reported to start(); the terminal carries on
            log.warning("Mission Control stopped: %s", type(exc).__name__, exc_info=True)
            outcome.setdefault("error", f"the server failed ({type(exc).__name__}).")
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor(timeout=2.0))
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                ready.set()  # unblocks start() if we never got as far as listening

    async def _serve(self, ready: threading.Event, outcome: dict[str, str]) -> None:
        try:
            sock = _bind(self.port)
        except OSError as exc:
            outcome["error"] = _bind_error(self.port, exc)
            return
        self.port = sock.getsockname()[1]
        # Created on this thread, so MetricsSampler primes its CPU baseline where it will be read.
        sampler = self._sampler if self._sampler is not None else MetricsSampler()
        hub = Hub(self.bus, StateTracker(), sampler, self.token, self.port, self.dev_origin,
                  bridge=self.bridge, broker=self.broker)
        runner = web.AppRunner(create_app(hub, dist_dir=self.dist_dir), access_log=None, handle_signals=False,
                               shutdown_timeout=1.0)
        try:
            await runner.setup()
            site = web.SockSite(runner, sock)
            await site.start()
            self.hub, self._runner = hub, runner
            ready.set()
            assert self._stop_event is not None
            await self._stop_event.wait()
        finally:
            await runner.cleanup()
            sock.close()  # already closed by the site when it was listening; closing twice is harmless
            self.hub = self._runner = None
