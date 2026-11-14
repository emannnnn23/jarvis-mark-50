"""Mission Control server: HTTP/WebSocket security, the event hub, the server thread and startup wiring.

Every server binds 127.0.0.1 on port 0; the Groq probe is replaced by a fake so nothing leaves the
machine, and the shared BUS is restored after each test so no subscriber or `enabled` flag leaks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    AgentTestCase,
    FAKE_KEY,
    FakeGroqClient,
    make_status_error,
    printed_text,
    response,
)
import groq  # noqa: E402
import aiohttp  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from yarl import URL  # noqa: E402

import jarvis  # noqa: E402
import mission_control  # noqa: E402
from core.events import BUS, EventBus  # noqa: E402
from core.orchestrator import AgentResult  # noqa: E402
from mission_control import bridge, launch, server  # noqa: E402
from mission_control import confirm as confirm_module  # noqa: E402
from mission_control.protocol import validate_event  # noqa: E402
from mission_control.state import StateTracker  # noqa: E402


TOKEN = "test-token-" + "x" * 32
SECRET = "gsk_" + "Qz7" * 14  # a Groq-shaped key that the redaction patterns must catch

SNAPSHOT_KEYS = {"type", "seq", "session", "state", "subsystems", "task", "pending_confirmation",
                 "metrics", "network", "recent_events"}


def tagged_style(text: str, colour: str) -> str:
    return f"<{colour}>{text}"


def metrics_payload() -> dict:
    return {"cpu_percent": 12.5, "memory_percent": 40.0, "memory_total": 8, "memory_available": 4,
            "memory_used": 4, "disk_percent": 50.0, "disk_total": 100, "disk_used": 50, "uptime_s": 10,
            "process_uptime_s": 5, "load_avg": [0.1, 0.2, 0.3], "os": "Linux"}


class FakeSampler:
    """MetricsSampler stand-in that records which thread called sample()."""

    def __init__(self, available: bool = True):
        self.available = available
        self.threads: list[int] = []

    def sample(self):
        self.threads.append(threading.get_ident())
        return metrics_payload() if self.available else None


class FakeProbe:
    """probe_groq stand-in: never touches the network, records the calling thread."""

    def __init__(self):
        self.threads: list[int] = []

    def __call__(self, timeout: float = 3.0):
        self.threads.append(threading.get_ident())
        return {"groq": "up", "checked_at": int(time.time() * 1000), "source": "probe", "latency_ms": 12.0}


def mission_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("jarvis-mission")]


class BusIsolation:
    """Save and restore the shared BUS, so no test leaves it enabled or with subscribers attached."""

    def isolate_bus(self) -> None:
        saved = (BUS.enabled, list(BUS._subscribers), list(BUS._ring), BUS._seq, BUS._evicted_seq,
                 dict(BUS._last_emit))

        def restore():
            BUS.enabled, BUS._subscribers = saved[0], saved[1]
            BUS._ring.clear()
            BUS._ring.extend(saved[2])
            BUS._seq, BUS._evicted_seq, BUS._last_emit = saved[3], saved[4], saved[5]

        self.addCleanup(restore)


# --- the app, driven by aiohttp's test server ---------------------------------------------------------


class AppTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-mc-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dist = self.tmp / "dist"
        self.bus = EventBus()
        self.tracker = StateTracker()
        self.sampler = FakeSampler()
        self.probe = FakeProbe()
        patcher = mock.patch("mission_control.server.probe_groq", self.probe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build_dist(self) -> None:
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<!doctype html><title>MC</title><div id=root></div>", encoding="utf-8")
        (self.dist / "assets" / "app.js").write_text("console.log('mc')", encoding="utf-8")

    async def start(self, dev_origin: str = "", probe: bool = False) -> None:
        """`probe=False` marks the Groq probe as just done, so auth adds no network.status frame."""
        self.hub = server.Hub(self.bus, self.tracker, self.sampler, TOKEN, 0, dev_origin=dev_origin)
        app = server.create_app(self.hub, dist_dir=self.dist)
        self.test_server = TestServer(app, host="127.0.0.1", port=0)
        self.client = TestClient(self.test_server)
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.hub.port = self.test_server.port
        self.port = self.test_server.port
        self.origin = f"http://127.0.0.1:{self.port}"
        if not probe:
            self.hub._last_probe = asyncio.get_running_loop().time()

    async def connect(self, origin: str | None = "default"):
        kwargs = {} if origin is None else {"origin": self.origin if origin == "default" else origin}
        ws = await self.client.ws_connect("/ws", **kwargs)
        self.addAsyncCleanup(ws.close)
        return ws

    async def recv(self, ws) -> dict:
        msg = await ws.receive(timeout=3)
        self.assertEqual(msg.type, aiohttp.WSMsgType.TEXT, f"expected a text frame, got {msg!r}")
        return json.loads(msg.data)

    async def authed(self, last_seq=None):
        ws = await self.connect()
        await ws.send_json({"type": "auth", "token": TOKEN, "last_seq": last_seq})
        self.assertEqual(await self.recv(ws), {"type": "auth.ok", "protocol": 1})
        return ws

    async def expect_close(self, ws, code: int) -> None:
        while True:
            msg = await ws.receive(timeout=3)
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                break
        self.assertEqual(ws.close_code, code)


class HttpSecurityTests(AppTestCase):
    async def test_wrong_host_is_421(self):
        await self.start()
        for host in ("evil.example", f"evil.example:{self.port}", f"127.0.0.1:{self.port + 1}",
                     "127.0.0.1", f"0.0.0.0:{self.port}", f"[::1]:{self.port}"):
            with self.subTest(host=host):
                resp = await self.client.get("/api/health", headers={"Host": host})
                self.assertEqual(resp.status, 421)
                self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
                resp.release()
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            with self.subTest(host=host):
                resp = await self.client.get("/api/health", headers={"Host": host})
                self.assertEqual(resp.status, 200)
                resp.release()

    async def test_foreign_origin_on_http_is_403(self):
        await self.start()
        resp = await self.client.get("/api/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status, 403)
        resp.release()
        for origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            resp = await self.client.get("/api/health", headers={"Origin": origin})
            self.assertEqual(resp.status, 200)
            resp.release()

    async def test_ws_without_or_with_foreign_origin_is_403(self):
        await self.start(dev_origin="http://localhost:5173")
        for origin in (None, "http://evil.example", "null", f"http://127.0.0.1:{self.port + 1}",
                       f"https://127.0.0.1:{self.port}", "http://localhost:5174"):
            with self.subTest(origin=origin):
                with self.assertRaises(aiohttp.WSServerHandshakeError) as ctx:
                    await self.client.ws_connect("/ws", **({} if origin is None else {"origin": origin}))
                self.assertEqual(ctx.exception.status, 403)
        for origin in (f"http://localhost:{self.port}", "http://localhost:5173"):
            with self.subTest(origin=origin):
                ws = await self.connect(origin=origin)
                await ws.send_json({"type": "auth", "token": TOKEN, "last_seq": None})
                self.assertEqual((await self.recv(ws))["type"], "auth.ok")
                await ws.close()

    async def test_snapshot_api_requires_bearer(self):
        await self.start()
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
                        {"Authorization": f"Basic {TOKEN}"}, {"Authorization": f"Bearer {TOKEN}x"}):
            with self.subTest(headers=headers):
                resp = await self.client.get("/api/snapshot", headers=headers)
                self.assertEqual(resp.status, 401)
                self.assertNotIn(TOKEN, await resp.text())
        resp = await self.client.get("/api/snapshot", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(set(await resp.json()), SNAPSHOT_KEYS)
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

    async def test_health(self):
        await self.start()
        resp = await self.client.get("/api/health")
        self.assertEqual(await resp.json(), {"ok": True, "protocol": 1})

    async def test_missing_dist_serves_build_instructions(self):
        await self.start()
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.content_type.startswith("text/html"))
        body = await resp.text()
        self.assertIn("cd mission_control/web &amp;&amp; npm ci &amp;&amp; npm run build", body)
        self.assertEqual(mission_control.BUILD_COMMAND, "cd mission_control/web && npm ci && npm run build")
        self.assertNotIn("<script", body)
        self.assertIn("Content-Security-Policy", resp.headers)
        missing = await self.client.get("/assets/app.js")
        self.assertEqual(missing.status, 404)
        missing.release()

    async def test_dist_served_with_security_headers(self):
        self.build_dist()
        await self.start()
        csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob:; "
               f"connect-src 'self' ws://127.0.0.1:{self.port} ws://localhost:{self.port}; "
               "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        page = await self.client.get("/")
        self.assertEqual(page.status, 200)
        self.assertIn("<div id=root>", await page.text())
        for resp in (page, await self.client.get("/api/health"), await self.client.get("/nope")):
            with self.subTest(path=resp.url.path):
                self.assertEqual(resp.headers["Content-Security-Policy"], csp)
                self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
                self.assertEqual(resp.headers["Cache-Control"], "no-store")
                resp.release()
        asset = await self.client.get("/assets/app.js")
        self.assertEqual(asset.status, 200)
        self.assertEqual(await asset.text(), "console.log('mc')")
        self.assertEqual(asset.headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(asset.headers["Content-Security-Policy"], csp)
        self.assertEqual(asset.headers["X-Content-Type-Options"], "nosniff")

    async def test_asset_traversal_is_404(self):
        self.build_dist()
        (self.tmp / "secret.txt").write_text("top secret", encoding="utf-8")
        (self.dist / "secret.txt").write_text("dist secret", encoding="utf-8")
        os.symlink(self.tmp / "secret.txt", self.dist / "assets" / "link.js")
        await self.start()
        for path in ("/assets/../secret.txt", "/assets/%2e%2e/secret.txt", "/assets/..%2fsecret.txt",
                     "/assets/%2e%2e/%2e%2e/secret.txt", "/assets/link.js", "/assets/%2fetc%2fpasswd",
                     "/assets//etc/passwd", "/assets/..%5csecret.txt"):
            with self.subTest(path=path):
                resp = await self.client.get(URL(path, encoded=True))
                self.assertEqual(resp.status, 404)
                self.assertNotIn("secret", await resp.text())


class WebSocketTests(AppTestCase):
    async def test_ws_auth_timeout_closes_4408(self):
        await self.start()
        with mock.patch("mission_control.server.AUTH_TIMEOUT", 0.05):
            ws = await self.connect()
            await self.expect_close(ws, 4408)

    async def test_ws_bad_token_closes_4401(self):
        await self.start()
        for first in ({"type": "auth", "token": "wrong", "last_seq": None},
                      {"type": "auth", "token": TOKEN[:-1], "last_seq": None},
                      {"type": "auth", "token": 12345, "last_seq": None},
                      {"type": "auth", "token": "é" * 10, "last_seq": None},
                      {"type": "ping"}, "not json", [1, 2]):
            with self.subTest(first=first):
                ws = await self.connect()
                started = time.monotonic()
                await (ws.send_str(first) if isinstance(first, str) else ws.send_json(first))
                await self.expect_close(ws, 4401)
                self.assertGreaterEqual(time.monotonic() - started, 0.2)

    async def test_ws_good_token_gets_auth_ok_then_snapshot(self):
        await self.start()
        self.bus.publish("session.start", version="1.0.0", provider="groq", model="m", permission_mode="standard",
                         voice_available=False, voice_enabled=False, mic_available=False, wake_available=False,
                         memory_status="ok", tasks_status="ok", developer_mode=False)
        self.bus.publish("request.start", source="terminal", text="hello")
        await asyncio.sleep(0.05)  # let the hub fold the events in
        ws = await self.authed()
        snap = await self.recv(ws)
        self.assertEqual(set(snap), SNAPSHOT_KEYS)
        self.assertEqual(snap["type"], "snapshot")
        self.assertEqual(snap["state"]["state"], "processing")
        self.assertEqual(snap["session"]["model"], "m")
        self.assertEqual([e["type"] for e in snap["recent_events"]][:2], ["session.start", "request.start"])
        self.assertEqual(snap["seq"], max(e["seq"] for e in snap["recent_events"]))
        self.assertIsNone(snap["pending_confirmation"])
        self.assertIsNone(snap["task"])
        self.assertEqual(set(snap["subsystems"]), {"llm", "memory", "tools", "system", "voice", "network"})
        live = self.bus.publish("assistant.message", text="Hello, Sir.")
        frame = await self.recv(ws)
        self.assertEqual(frame, live)
        self.assertEqual(validate_event(frame), [])
        await ws.send_json({"type": "ping"})
        self.assertEqual(await self.recv(ws), {"type": "pong"})
        for bad in ({"type": "nope"}, {"type": "auth", "token": TOKEN}, {"no": "type"}, [1], "garbage"):
            await (ws.send_str(bad) if isinstance(bad, str) else ws.send_json(bad))
            self.assertEqual(await self.recv(ws), {"type": "error", "code": "invalid"})

    async def test_recent_events_capped_at_200(self):
        await self.start()
        for i in range(300):
            self.bus.publish("notice", level="info", text=f"n{i}")
        ws = await self.authed()
        snap = await self.recv(ws)
        self.assertEqual(len(snap["recent_events"]), 200)
        self.assertEqual(snap["recent_events"][-1]["text"], "n299")

    async def test_last_seq_replays_and_gap_sends_snapshot(self):
        await self.start()
        first = self.bus.publish("notice", level="info", text="one")
        second = self.bus.publish("notice", level="info", text="two")
        third = self.bus.publish("notice", level="info", text="three")
        ws = await self.authed(last_seq=first["seq"])
        self.assertEqual(await self.recv(ws), second)
        self.assertEqual(await self.recv(ws), third)
        fourth = self.bus.publish("notice", level="info", text="four")
        # No duplicates of the replayed events and no snapshot: the next frame is the live one.
        self.assertEqual(await self.recv(ws), fourth)
        await ws.send_json({"type": "ping"})
        self.assertEqual(await self.recv(ws), {"type": "pong"})
        # A seq from another run (beyond this bus) is a gap: snapshot.
        ws2 = await self.authed(last_seq=10 ** 9)
        self.assertEqual((await self.recv(ws2))["type"], "snapshot")
        # So is history pushed out of the ring.
        small = EventBus(history=2)
        self.bus = small
        await self.client.close()
        await self.start()
        old = small.publish("notice", level="info", text="a")
        for text in "bcd":
            small.publish("notice", level="info", text=text)
        ws3 = await self.authed(last_seq=old["seq"])
        self.assertEqual((await self.recv(ws3))["type"], "snapshot")

    async def test_replay_and_live_are_not_duplicated(self):
        """Events published while a client authenticates arrive exactly once, in replay or live."""
        await self.start()
        base = self.bus.publish("notice", level="info", text="base")
        stop = threading.Event()
        published: list[int] = []

        def producer():
            while not stop.is_set():
                ev = self.bus.publish("notice", level="info", text="p")
                published.append(ev["seq"])
                time.sleep(0.001)

        thread = threading.Thread(target=producer)
        thread.start()
        try:
            await asyncio.sleep(0.02)
            ws = await self.authed(last_seq=base["seq"])
            await asyncio.sleep(0.05)
        finally:
            stop.set()
            thread.join()
        marker = self.bus.publish("notice", level="info", text="end")
        seen: list[int] = []
        while True:
            frame = await self.recv(ws)
            if frame.get("seq") == marker["seq"]:
                break
            seen.append(frame["seq"])
        self.assertEqual(sorted(seen), published)
        self.assertEqual(len(seen), len(set(seen)))

    async def test_snapshot_frames_are_scrubbed(self):
        await self.start()
        self.tracker.set_subsystem("llm", last_error_kind=f"auth failed with key {SECRET}")
        ws = await self.authed()
        snap = await self.recv(ws)
        self.assertNotIn(SECRET[4:14], json.dumps(snap))
        self.assertIn("[REDACTED]", snap["subsystems"]["llm"]["last_error_kind"])
        resp = await self.client.get("/api/snapshot", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertNotIn(SECRET[4:14], await resp.text())

    async def test_fifth_client_refused_4409(self):
        await self.start()
        clients = [await self.authed() for _ in range(4)]
        for ws in clients:
            self.assertEqual((await self.recv(ws))["type"], "snapshot")
        fifth = await self.connect()
        await self.expect_close(fifth, 4409)
        await clients[0].close()
        await asyncio.sleep(0.05)
        again = await self.authed()  # a slot is free again
        self.assertEqual((await self.recv(again))["type"], "snapshot")

    async def test_rate_limit_closes_4429(self):
        await self.start()
        ws = await self.authed()
        await self.recv(ws)  # snapshot
        for _ in range(30):
            await ws.send_json({"type": "ping"})
        await self.expect_close(ws, 4429)

    async def test_oversized_message_closes(self):
        await self.start()
        ws = await self.authed()
        await self.recv(ws)
        await ws.send_str(json.dumps({"type": "ping", "pad": "x" * 70000}))
        await self.expect_close(ws, 1009)

    async def stalled_client(self) -> socket.socket:
        """A raw WebSocket that authenticates and then never reads again (a frozen tab)."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)  # before connect, so the window stays small
        sock.setblocking(False)
        await loop.sock_connect(sock, ("127.0.0.1", self.port))
        request = (f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nOrigin: {self.origin}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                   "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n")
        await loop.sock_sendall(sock, request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(loop.sock_recv(sock, 1), 3)
            self.assertTrue(chunk, "the server closed the handshake")
            head += chunk
        self.assertIn(b" 101 ", head.split(b"\r\n", 1)[0] + b" ")
        payload = json.dumps({"type": "auth", "token": TOKEN, "last_seq": None}).encode()
        self.assertLess(len(payload), 126)
        mask = b"\x01\x02\x03\x04"
        frame = bytes([0x81, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        await loop.sock_sendall(sock, frame)
        return sock

    async def test_stalled_reader_is_dropped_and_frees_its_slot(self):
        await self.start()
        with mock.patch("mission_control.server.CLOSE_TIMEOUT", 0.5):
            await self.stalled_client()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3
            while not any(c.authenticated for c in self.hub.clients):
                self.assertLess(loop.time(), deadline, "the stalled client never authenticated")
                await asyncio.sleep(0.01)
            big = "y" * 7000
            deadline = loop.time() + 15
            while self.hub.clients:
                self.assertLess(loop.time(), deadline, "a client that stopped reading kept its slot")
                for _ in range(50):
                    self.bus.publish("notice", level="info", text=big)
                await asyncio.sleep(0.02)
        # The slot is free again: a normal tab can connect.
        ws = await self.authed()
        self.assertEqual((await self.recv(ws))["type"], "snapshot")

    async def test_send_queue_overflow_closes_1013(self):
        await self.start()
        ws = await self.authed()
        await self.recv(ws)
        for i in range(600):  # all queued before the sender task gets a turn
            self.bus.publish("notice", level="info", text=f"flood {i}")
        await self.expect_close(ws, 1013)


class HubLoopTests(AppTestCase):
    async def test_metrics_only_while_a_client_is_connected_and_on_the_loop_thread(self):
        with mock.patch("mission_control.server.METRICS_INTERVAL", 0.02):
            await self.start()
            await asyncio.sleep(0.1)
            before = len(self.sampler.threads)
            ws = await self.authed()
            await self.recv(ws)
            frame = None
            while frame is None or frame.get("type") != "system.metrics":
                frame = await self.recv(ws)
            self.assertEqual(validate_event(frame), [])
            await ws.close()
            await asyncio.sleep(0.05)
            idle = len(self.sampler.threads)
            await asyncio.sleep(0.1)
            self.assertEqual(len(self.sampler.threads), idle)  # nobody watching: no sampling
        self.assertLessEqual(before, 1)  # at most the one priming call at startup
        self.assertEqual(set(self.sampler.threads), {threading.get_ident()})  # the server loop's thread
        snap = self.hub.snapshot()
        self.assertEqual(snap["metrics"]["cpu_percent"], 12.5)
        self.assertEqual(snap["subsystems"]["system"]["status"], "online")
        # Metrics are transient: they never push real events out of the replay ring.
        self.assertFalse(any(e["type"] == "system.metrics" for e in self.bus.recent()))

    async def test_sampler_unavailable_marks_system_unavailable(self):
        self.sampler = FakeSampler(available=False)
        await self.start()
        ws = await self.authed()
        snap = await self.recv(ws)
        self.assertEqual(snap["subsystems"]["system"]["status"], "unavailable")
        self.assertEqual(snap["subsystems"]["system"]["reason"], "psutil not installed")
        self.assertIsNone(snap["metrics"])

    async def _system_statuses_while_sampling(self, script: list) -> list:
        """Run the metrics loop with a sampler that follows `script` (first entry: the startup priming
        call), recording the SYSTEM status the tracker shows at each loop call before it answers."""
        tracker = self.tracker

        class ScriptedSampler(FakeSampler):
            def __init__(self):
                super().__init__(available=True)
                self.seen: list = []

            def sample(self):
                self.threads.append(threading.get_ident())
                system = tracker.snapshot()["subsystems"]["system"]
                self.seen.append((system["status"], system.get("reason")))
                index = len(self.threads) - 1
                return script[index] if index < len(script) else metrics_payload()

        self.sampler = ScriptedSampler()
        with mock.patch("mission_control.server.METRICS_INTERVAL", 0.01):
            await self.start()
            ws = await self.authed()
            await self.recv(ws)
            for _ in range(300):
                if len(self.sampler.threads) > len(script) + 1:
                    break
                await asyncio.sleep(0.01)
            await ws.close()
        return self.sampler.seen[1:len(script) + 2]  # the loop's calls (index 0 was the priming one)

    async def test_three_failed_samples_mark_system_unavailable_until_a_good_one(self):
        seen = await self._system_statuses_while_sampling([metrics_payload(), None, None, None])
        failed = ("unavailable", "system metrics failed")
        self.assertEqual(seen[:3], [("unknown", None)] * 3)  # one or two failures are not enough
        self.assertEqual(seen[3], failed)  # after the third in a row
        self.assertEqual(seen[4][0], "online")  # the next good sample brings it back
        self.assertEqual(self.tracker.snapshot()["subsystems"]["system"]["status"], "online")

    async def test_failures_that_are_not_consecutive_keep_system_as_it_was(self):
        seen = await self._system_statuses_while_sampling([metrics_payload(), None, None, metrics_payload(), None, None])
        self.assertNotIn("unavailable", [status for status, _ in seen])

    async def test_probe_on_auth_runs_in_an_executor(self):
        await self.start(probe=True)
        ws = await self.authed()
        await self.recv(ws)
        frame = await self.recv(ws)
        self.assertEqual(frame["type"], "network.status")
        self.assertEqual(frame["groq"], "up")
        self.assertEqual(len(self.probe.threads), 1)
        self.assertNotEqual(self.probe.threads[0], threading.get_ident())  # never on the event loop
        await self.authed()  # the last probe is fresh: no second probe
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.probe.threads), 1)
        self.assertEqual(self.hub.snapshot()["network"]["groq"], "up")

    async def test_tracker_state_changes_are_published(self):
        await self.start()
        ws = await self.authed()
        await self.recv(ws)
        self.bus.publish("request.start", source="terminal", text="hi")
        types = []
        while "jarvis.state" not in types:
            frame = await self.recv(ws)
            types.append(frame["type"])
            if frame["type"] == "jarvis.state":
                self.assertEqual(frame["state"], "processing")
                self.assertEqual(validate_event(frame), [])
        self.assertEqual(types[0], "request.start")

    async def test_hub_unsubscribes_on_shutdown(self):
        await self.start()
        self.assertEqual(len(self.bus._subscribers), 1)
        await self.client.close()
        self.assertEqual(self.bus._subscribers, [])



class PassiveNetworkStatusTests(unittest.TestCase):
    """A finished model call is a free reachability check: Hub._dispatch turns it into network.status
    (source "passive") but only when the Groq reachability it shows differs from the last known one."""

    def setUp(self):
        self.bus = EventBus()
        self.hub = server.Hub(self.bus, StateTracker(), FakeSampler(), TOKEN, 0)
        self.published: list[dict] = []
        self.bus.subscribe(self.published.append)

    def feed(self, kind: str, **data) -> None:
        self.hub._dispatch(self.bus.publish(kind, **data))

    def call_end(self, ok: bool, error_kind: str | None = None) -> None:
        extra = {} if error_kind is None else {"error_kind": error_kind}
        self.feed("llm.call", phase="end", model="m", attempt=1, ok=ok, duration_ms=5, **extra)

    def network_events(self) -> list[dict]:
        return [e for e in self.published if e["type"] == "network.status"]

    def test_successful_call_marks_groq_up_once(self):
        self.feed("llm.call", phase="start", model="m", attempt=1)
        self.assertEqual(self.network_events(), [])
        self.call_end(True)
        self.call_end(True)  # no change: nothing new
        events = self.network_events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["groq"], events[0]["source"]), ("up", "passive"))
        self.assertIsInstance(events[0]["checked_at"], int)
        self.assertEqual(validate_event(events[0]), [])
        self.assertEqual(self.hub.snapshot()["network"]["groq"], "up")

    def test_network_failure_marks_groq_down_and_recovery_marks_it_up(self):
        self.call_end(False, "network")
        self.call_end(False, "network")
        self.call_end(True)
        events = self.network_events()
        self.assertEqual([(e["groq"], e["source"]) for e in events], [("down", "passive"), ("up", "passive")])
        self.assertTrue(all(validate_event(e) == [] for e in events))
        self.hub._dispatch(events[-1])  # the event itself coming back changes nothing
        self.assertEqual(len(self.network_events()), 2)

    def test_other_failures_say_nothing_about_reachability(self):
        for kind in ("rate_limit", "bad_request", "server", "auth", None):
            self.call_end(False, kind)
        self.assertEqual(self.network_events(), [])

    def test_matching_probe_result_is_not_repeated(self):
        self.feed("network.status", groq="up", checked_at=1, source="probe", latency_ms=12.0)
        self.call_end(True)
        self.assertEqual(len(self.network_events()), 1)  # only the probe's own event
        self.call_end(False, "network")
        self.assertEqual([e["groq"] for e in self.network_events()], ["up", "down"])

    def test_closed_hub_publishes_nothing(self):
        self.hub._closed = True
        self.call_end(True)
        self.assertEqual(self.network_events(), [])


# --- commands, cancel and approvals from the browser ---------------------------------------------------


class FakeBridge:
    """MissionBridge stand-in: records what the server hands over, answers with a scripted result."""

    def __init__(self, result=None):
        self.result = result if result is not None else bridge.SubmitResult.ACCEPTED
        self.submitted: list[tuple] = []
        self.cancels = 0
        self.answers: list[tuple] = []

    def submit(self, command_id, text):
        self.submitted.append((command_id, text))
        return self.result

    def cancel(self):
        self.cancels += 1
        return True

    def answer(self, confirm_id, digest, approved):
        self.answers.append((confirm_id, digest, approved))
        return "ok"


class FakeBroker:
    def __init__(self, result="ok", pending=None):
        self.result = result
        self.pending = pending
        self.answers: list[tuple] = []

    def answer_from_browser(self, confirm_id, digest, approved):
        self.answers.append((confirm_id, digest, approved))
        return self.result

    def pending_payload(self):
        return None if self.pending is None else json.loads(json.dumps(self.pending))


def confirm_payload(confirm_id="c0ffee", digest="d" * 64) -> dict:
    return {"id": confirm_id, "digest": digest, "tool": "edit_workspace_file", "label": "SENSITIVE ACTION",
            "operation": "Edit notes.txt", "affected_paths": ["/w/notes.txt"], "reason": "", "hidden": 0,
            "lines": [{"k": "del", "t": "-helo"}, {"k": "add", "t": "+hello"}], "omitted": 0,
            "browser_approvable": True, "expires_at": None}


class InboundHandlerTests(AppTestCase):
    async def start_with(self, bridge_=None, broker=None):
        await self.start()
        self.hub.bridge, self.hub.broker = bridge_, broker

    async def next_of(self, ws, type_) -> dict:
        for _ in range(200):  # bounded: the hub's ticks keep frames coming, so a missing one must fail
            frame = await self.recv(ws)
            if frame.get("type") == type_:
                return frame
        self.fail(f"no {type_} frame")

    async def until_pong(self, ws) -> list[dict]:
        """Every frame before the reply to a ping sent now (frames are sent in order)."""
        await ws.send_json({"type": "ping"})
        frames = []
        while (frame := await self.recv(ws)) != {"type": "pong"}:
            frames.append(frame)
        return frames

    async def test_command_submit_accepted_is_acked_as_an_event(self):
        fake = FakeBridge()
        await self.start_with(fake)
        ws = await self.authed()
        await self.recv(ws)  # snapshot
        await ws.send_json({"type": "command.submit", "id": "cmd-1", "text": "what time is it"})
        ack = await self.next_of(ws, "command.ack")
        self.assertEqual((ack["command_id"], ack["status"]), ("cmd-1", "accepted"))
        self.assertEqual(validate_event(ack), [])
        self.assertEqual(fake.submitted, [("cmd-1", "what time is it")])

    async def test_command_submit_rejections_carry_the_reason(self):
        fake = FakeBridge()
        await self.start_with(fake)
        ws = await self.authed()
        await self.recv(ws)
        for result in (r for r in bridge.SubmitResult if r is not bridge.SubmitResult.ACCEPTED):
            with self.subTest(result=result):
                fake.result = result
                await ws.send_json({"type": "command.submit", "id": "cmd-2", "text": "exit"})
                rejected = await self.next_of(ws, "command.rejected")
                self.assertEqual((rejected["command_id"], rejected["reason"]), ("cmd-2", result.value))
                self.assertEqual(validate_event(rejected), [])

    async def test_command_submit_text_is_passed_as_received(self):
        fake = FakeBridge(bridge.SubmitResult.INVALID)
        await self.start_with(fake)
        ws = await self.authed()
        await self.recv(ws)
        for text in (None, 42, ["a"], "x" * 5000):
            await ws.send_json({"type": "command.submit", "id": "c", "text": text})
            await self.next_of(ws, "command.rejected")
        await ws.send_json({"type": "command.submit", "id": "c"})
        await self.next_of(ws, "command.rejected")
        self.assertEqual([t for _, t in fake.submitted], [None, 42, ["a"], "x" * 5000, None])

    async def test_command_submit_without_a_usable_id_is_invalid(self):
        fake = FakeBridge()
        await self.start_with(fake)
        ws = await self.authed()
        await self.recv(ws)
        for bad in (None, 7, "", "x" * 129, ["id"]):
            with self.subTest(bad=bad):
                await ws.send_json({"type": "command.submit", "id": bad, "text": "hi"})
                self.assertEqual(await self.recv(ws), {"type": "error", "code": "invalid"})
        self.assertEqual(fake.submitted, [])

    async def test_request_cancel_calls_the_bridge(self):
        fake = FakeBridge()
        await self.start_with(fake)
        ws = await self.authed()
        await self.recv(ws)
        await ws.send_json({"type": "request.cancel"})
        await ws.send_json({"type": "ping"})
        self.assertEqual(await self.recv(ws), {"type": "pong"})  # cancel sends no reply of its own
        self.assertEqual(fake.cancels, 1)

    async def test_confirm_answer_fields_go_to_the_broker_as_received(self):
        broker = FakeBroker("digest_mismatch")
        await self.start_with(FakeBridge(), broker)
        ws = await self.authed()
        await self.recv(ws)
        cases = [
            ({"id": "abc", "digest": "d1", "approved": True}, ("abc", "d1", True), "abc"),
            ({"id": "abc", "digest": "d1", "approved": "true"}, ("abc", "d1", "true"), "abc"),
            ({"id": "abc", "digest": "d1", "approved": 1}, ("abc", "d1", 1), "abc"),
            ({"id": "abc"}, ("abc", None, None), "abc"),
            ({"id": 5, "digest": ["x"], "approved": {"a": 1}}, (5, ["x"], {"a": 1}), ""),
        ]
        for message, passed, echoed in cases:
            with self.subTest(message=message):
                await ws.send_json({"type": "confirm.answer", **message})
                result = await self.recv(ws)
                self.assertEqual(result, {"type": "confirm.answer.result", "id": echoed, "result": "digest_mismatch"})
                self.assertEqual(broker.answers[-1], passed)
                self.assertIs(type(broker.answers[-1][2]), type(passed[2]))  # never coerced

    async def test_without_a_bridge_the_inbound_types_are_invalid(self):
        await self.start_with(None, None)
        ws = await self.authed()
        await self.recv(ws)
        for message in ({"type": "command.submit", "id": "c", "text": "hi"}, {"type": "request.cancel"},
                        {"type": "confirm.answer", "id": "a", "digest": "b", "approved": True}):
            with self.subTest(message=message["type"]):
                await ws.send_json(message)
                self.assertEqual(await self.recv(ws), {"type": "error", "code": "invalid"})

    async def test_replay_also_sends_the_open_confirmation(self):
        payload = confirm_payload()
        broker = FakeBroker(pending=None)
        await self.start_with(FakeBridge(), broker)
        ws = await self.authed()
        snap = await self.recv(ws)
        last_seq = snap["seq"]
        await ws.close()

        # While the tab is away: a request starts and asks for approval (confirm.request is transient).
        with self.bus.request_scope("browser"):
            started = self.bus.publish("request.start", source="browser", text="fix the typo")
            live = self.bus.publish("confirm.request", _transient=True, **payload)
        broker.pending = payload
        await asyncio.sleep(0.05)  # let the hub see both

        back = await self.authed(last_seq=last_seq)
        replayed = await self.until_pong(back)  # the replay and the resend are queued before the pong
        self.assertIn(started["seq"], [f.get("seq") for f in replayed])
        resent = [f for f in replayed if f["type"] == "confirm.request"]
        self.assertEqual(resent, [live])  # once, the original event: seq and request id included
        self.assertEqual(validate_event(resent[0]), [])
        await back.close()

        # Once it is answered, a replaying tab is not shown it again.
        resolved = self.bus.publish("confirm.resolved", id=payload["id"], approved=True, channel="browser")
        broker.pending = None
        await asyncio.sleep(0.05)
        again = await self.authed(last_seq=last_seq)
        frames = await self.until_pong(again)
        self.assertIn(resolved["seq"], [f.get("seq") for f in frames])
        self.assertNotIn("confirm.request", [f["type"] for f in frames])

    async def test_replay_skips_a_confirmation_the_broker_no_longer_holds(self):
        payload = confirm_payload()
        broker = FakeBroker(pending=confirm_payload("other-id"))
        await self.start_with(FakeBridge(), broker)
        ws = await self.authed()
        last_seq = (await self.recv(ws))["seq"]
        await ws.close()
        self.bus.publish("notice", level="info", text="away")
        self.bus.publish("confirm.request", _transient=True, **payload)
        await asyncio.sleep(0.05)
        back = await self.authed(last_seq=last_seq)
        frames = await self.until_pong(back)
        self.assertIn("notice", [f["type"] for f in frames])
        self.assertNotIn("confirm.request", [f["type"] for f in frames])

    async def test_resent_confirmation_is_scrubbed(self):
        payload = confirm_payload()
        broker = FakeBroker(pending=dict(payload, operation=f"Edit with {SECRET}"))
        await self.start_with(FakeBridge(), broker)
        ws = await self.authed()
        last_seq = (await self.recv(ws))["seq"]
        await ws.close()
        self.bus.publish("confirm.request", _transient=True, **payload)
        await asyncio.sleep(0.05)
        back = await self.authed(last_seq=last_seq)
        frames = await self.until_pong(back)
        self.assertEqual([f["type"] for f in frames], ["confirm.request"])
        self.assertIn("[REDACTED]", frames[0]["operation"])
        self.assertNotIn("gsk_Qz7", json.dumps(frames))


# --- the real server thread --------------------------------------------------------------------------


class ServerThreadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-mc-")
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch("mission_control.server.probe_groq", FakeProbe())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def auth_cycle(self, srv) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"http://127.0.0.1:{srv.port}/ws",
                                          origin=f"http://127.0.0.1:{srv.port}") as ws:
                await ws.send_json({"type": "auth", "token": srv.token, "last_seq": None})
                ok = json.loads((await ws.receive(timeout=3)).data)
                snap = json.loads((await ws.receive(timeout=3)).data)
                self.assertEqual(snap["type"], "snapshot")
                return ok

    async def test_server_thread_start_stop_rebind(self):
        before = mission_threads()
        srv = server.MissionControlServer(EventBus(), port=0, dist_dir=Path(self._tmp.name), sampler=FakeSampler())
        self.assertEqual(len(srv.token), len(server.secrets.token_urlsafe(32)))
        url = srv.start()
        self.addCleanup(srv.stop)
        self.assertNotEqual(srv.port, 0)
        self.assertEqual(url, f"http://127.0.0.1:{srv.port}")
        self.assertEqual([t.name for t in mission_threads() if t not in before], ["jarvis-mission-control"])
        self.assertEqual(await self.auth_cycle(srv), {"type": "auth.ok", "protocol": 1})
        port = srv.port
        srv.stop()
        self.assertEqual(mission_threads(), before)
        srv.stop()  # idempotent
        self.assertEqual(srv.start(), f"http://127.0.0.1:{port}")  # same port, straight after a connection
        self.assertEqual(srv.port, port)
        self.assertEqual((await self.auth_cycle(srv))["type"], "auth.ok")
        srv.stop()
        self.assertEqual(mission_threads(), before)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()

    def test_binds_loopback_only(self):
        srv = server.MissionControlServer(EventBus(), port=0, dist_dir=Path(self._tmp.name), sampler=FakeSampler())
        srv.start()
        self.addCleanup(srv.stop)
        sockets = srv._runner.addresses
        self.assertTrue(sockets)
        self.assertTrue(all(addr[0] == "127.0.0.1" for addr in sockets))

    def test_aiohttp_warnings_go_to_the_jarvis_log_not_the_terminal(self):
        srv = server.MissionControlServer(EventBus(), port=0, dist_dir=Path(self._tmp.name), sampler=FakeSampler())
        srv.start()
        self.addCleanup(srv.stop)
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        jarvis_log = logging.getLogger("jarvis")
        jarvis_log.addHandler(handler)
        self.addCleanup(jarvis_log.removeHandler, handler)
        with mock.patch.object(logging, "lastResort") as last_resort, mock.patch("sys.stderr") as stderr:
            logging.getLogger("aiohttp.server").warning("client went away")
        self.assertEqual([r.getMessage() for r in records], ["client went away"])
        last_resort.handle.assert_not_called()
        stderr.write.assert_not_called()
        srv.stop()

    def test_port_in_use_raises_mission_control_error(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            srv = server.MissionControlServer(EventBus(), port=busy.getsockname()[1], dist_dir=Path(self._tmp.name),
                                              sampler=FakeSampler())
            with self.assertRaises(server.MissionControlError) as ctx:
                srv.start()
            self.assertIn(str(busy.getsockname()[1]), str(ctx.exception))
            srv.stop()
        self.assertEqual(mission_threads(), [])


# --- startup wiring and the first events ---------------------------------------------------------------


class StartupWiringTests(BusIsolation, AgentTestCase):
    def setUp(self):
        super().setUp()
        self.isolate_bus()
        self.voice = jarvis.VoiceEngine(enabled=False, player=["fake-player"])
        self.addCleanup(self.voice.shutdown)
        patcher = mock.patch("mission_control.server.probe_groq", FakeProbe())
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_cli(self, client=None):
        ctx = jarvis.AgentContext(client or FakeGroqClient(), self.config, self.registry, self.policy, self.memory)
        with mock.patch("builtins.print"):
            return jarvis.JarvisCLI(ctx, self.voice, self.memory)

    def record_bus(self) -> list[dict]:
        seen: list[dict] = []
        self.addCleanup(BUS.subscribe(seen.append))
        return seen

    def test_port_in_use_warns_and_terminal_still_runs(self):
        cli = self.make_cli()
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            config = self.make_config(ui_enabled=True, ui_port=port, ui_open_browser=True)
            with mock.patch("builtins.print") as printed, mock.patch("ui.terminal.style", side_effect=tagged_style), \
                    mock.patch("jarvis.style", side_effect=tagged_style), \
                    mock.patch("mission_control.launch.webbrowser.open") as browser:
                self.assertIsNone(jarvis.start_mission_control(config, cli, self.voice))
        lines = printed_text(printed).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("<yellow>"))
        self.assertIn("Mission Control", lines[0])
        self.assertIn(str(port), lines[0])
        browser.assert_not_called()
        self.assertFalse(BUS.enabled)
        self.assertIs(self.policy.confirm, self.confirm)
        self.assertEqual(mission_threads(), [])
        # The terminal still answers.
        with mock.patch("ui.terminal.run_agent", return_value=AgentResult("Still here.", "complete", [], 1)), \
                mock.patch("builtins.print") as printed:
            cli.handle_message("hello")
        self.assertIn("Still here.", printed_text(printed))

    def test_import_error_warns_and_returns_none(self):
        cli = self.make_cli()
        config = self.make_config(ui_enabled=True, ui_port=0, ui_open_browser=False)
        with mock.patch.dict(sys.modules, {"mission_control.server": None}), \
                mock.patch("builtins.print") as printed, mock.patch("jarvis.style", side_effect=tagged_style):
            self.assertIsNone(jarvis.start_mission_control(config, cli, self.voice))
        lines = printed_text(printed).splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("<yellow>"))
        self.assertFalse(BUS.enabled)

    def test_start_prints_link_and_publishes_session_start(self):
        cli = self.make_cli()
        cli.developer_mode = True
        config = self.make_config(ui_enabled=True, ui_port=0, ui_open_browser=False, model="test-model")
        seen = self.record_bus()
        with mock.patch("builtins.print") as printed:
            srv = jarvis.start_mission_control(config, cli, self.voice)
        self.assertIsNotNone(srv)
        self.addCleanup(srv.stop)
        self.assertTrue(BUS.enabled)
        self.assertEqual(printed_text(printed).splitlines(),
                         [f"Mission Control: http://127.0.0.1:{srv.port}/#token={srv.token}"])
        start = [e for e in seen if e["type"] == "session.start"]
        self.assertEqual(len(start), 1)
        self.assertEqual(validate_event(start[0]), [])
        self.assertEqual({k: start[0][k] for k in ("version", "provider", "model", "permission_mode",
                                                    "voice_enabled", "memory_status", "tasks_status",
                                                    "developer_mode")},
                         {"version": jarvis.VERSION, "provider": "groq", "model": "test-model",
                          "permission_mode": "standard", "voice_enabled": False, "memory_status": "ok",
                          "tasks_status": "disabled", "developer_mode": True})
        srv.stop()

    def test_start_mission_control_restores_terminal_confirm_on_failure(self):
        self.policy.confirm = jarvis.terminal_confirm
        cli = self.make_cli()
        config = self.make_config(ui_enabled=True, ui_port=0, ui_open_browser=False)
        for error in (server.MissionControlError("port 1 on 127.0.0.1 is already in use."), RuntimeError("boom")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(server.MissionControlServer, "start", side_effect=error), \
                        mock.patch("builtins.print") as printed, mock.patch("jarvis.style", side_effect=tagged_style), \
                        mock.patch("jarvis.log") as logged:
                    self.assertIsNone(jarvis.start_mission_control(config, cli, self.voice))
                # An unexpected failure goes to the log file with its traceback; the terminal gets one line.
                self.assertEqual(logged.warning.called, isinstance(error, RuntimeError))
                self.assertIs(self.policy.confirm, jarvis.terminal_confirm)
                self.assertIsNone(cli._bridge)
                self.assertFalse(BUS.enabled)
                lines = printed_text(printed).splitlines()
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("<yellow>"))
                with mock.patch("builtins.input", return_value="hi") as typed, \
                        mock.patch("jarvis.select.select") as selected:
                    self.assertEqual(cli._read_line(), "hi")
                typed.assert_called_once()
                selected.assert_not_called()
        self.assertEqual(mission_threads(), [])

    def test_start_mission_control_wires_broker_and_bridge(self):
        self.policy.confirm = jarvis.terminal_confirm
        cli = self.make_cli()
        config = self.make_config(ui_enabled=True, ui_port=0, ui_open_browser=False, confirm_timeout=45)
        with mock.patch("builtins.print"):
            srv = jarvis.start_mission_control(config, cli, self.voice)
        self.addCleanup(srv.stop)
        self.assertIsInstance(srv.broker, confirm_module.ConfirmationBroker)
        self.assertEqual(srv.broker.timeout, 45)
        self.assertEqual(self.policy.confirm, srv.broker.confirm)
        self.assertIsInstance(srv.bridge, bridge.MissionBridge)
        self.assertIs(cli._bridge, srv.bridge)
        self.assertIs(cli._inbox, srv.bridge.inbox)
        self.assertEqual(cli._inbox.maxsize, 1)
        self.assertIsNot(cli._inbox, cli._wake_events)
        self.assertIs(srv.bridge.broker, srv.broker)
        with mock.patch("jarvis.os.kill") as kill, mock.patch.object(self.voice, "stop") as stop:
            self.assertFalse(srv.bridge.cancel())  # not busy: only stops speaking
        kill.assert_not_called()
        stop.assert_called_once_with()
        srv.stop()

    def test_session_start_reports_corrupted_stores(self):
        cli = self.make_cli()
        self.memory.corruption_error = "bad json"
        tasks = mock.Mock(corruption_error="bad tasks")
        cli.ctx.tasks = tasks
        payload = jarvis.session_start_payload(self.config, cli, self.voice)
        self.assertEqual((payload["memory_status"], payload["tasks_status"]), ("corrupted", "corrupted"))
        tasks.corruption_error = None
        self.assertEqual(jarvis.session_start_payload(self.config, cli, self.voice)["tasks_status"], "ok")

    def test_browser_opened_through_redirect_file_without_token_in_argv(self):
        cli = self.make_cli()
        config = self.make_config(ui_enabled=True, ui_port=0, ui_open_browser=True)
        with mock.patch("builtins.print"), \
                mock.patch("jarvis.open_browser_securely", return_value=True) as opener:
            srv = jarvis.start_mission_control(config, cli, self.voice)
        self.addCleanup(srv.stop)
        opener.assert_called_once_with(f"http://127.0.0.1:{srv.port}/#token={srv.token}")
        srv.stop()

    def test_token_never_logged(self):
        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture(level=logging.DEBUG)
        loggers = [logging.getLogger(name) for name in ("jarvis", "jarvis.audit", "aiohttp", "")]
        saved = [(lg, lg.level) for lg in loggers]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)

        def restore():
            for lg, level in saved:
                lg.removeHandler(handler)
                lg.setLevel(level)

        self.addCleanup(restore)
        cli = self.make_cli()
        config = self.make_config(ui_enabled=True, ui_port=0, ui_open_browser=False)
        with mock.patch("builtins.print") as printed:
            srv = jarvis.start_mission_control(config, cli, self.voice)
        self.addCleanup(srv.stop)
        token = srv.token

        async def cycle():
            async with aiohttp.ClientSession() as session:
                origin = f"http://127.0.0.1:{srv.port}"
                async with session.ws_connect(f"{origin}/ws", origin=origin) as ws:
                    await ws.send_json({"type": "auth", "token": token, "last_seq": None})
                    self.assertEqual(json.loads((await ws.receive(timeout=3)).data)["type"], "auth.ok")
                    await ws.receive(timeout=3)
                    await ws.send_json({"type": "ping"})
                async with session.ws_connect(f"{origin}/ws", origin=origin) as bad:
                    await bad.send_json({"type": "auth", "token": token + "x", "last_seq": None})
                    await bad.receive(timeout=3)
                async with session.get(f"{origin}/api/snapshot", headers={"Authorization": f"Bearer {token}"}) as r:
                    self.assertEqual(r.status, 200)

        asyncio.run(cycle())
        srv.stop()
        self.assertTrue(records, "the capture should see at least some log lines")
        for record in records:
            text = f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
            self.assertNotIn(token, text)
        self.assertEqual(sum(token in str(c) for c in printed.call_args_list), 1)

    def test_secret_in_request_text_is_redacted_in_events(self):
        BUS.enabled = True
        seen = self.record_bus()
        cli = self.make_cli()
        # The key straddles the 500-character cut: truncating before redacting would leak "gsk_Qz7Qz".
        text = "x" * 495 + " " + SECRET
        reply = f"Your key is {SECRET}."
        with mock.patch("ui.terminal.run_agent", return_value=AgentResult(reply, "complete", [], 1)), \
                mock.patch("builtins.print"):
            cli.handle_message(text)
        start = next(e for e in seen if e["type"] == "request.start")
        self.assertLessEqual(len(start["text"]), 500)
        self.assertNotIn("gsk_", start["text"])
        dumped = json.dumps(seen)
        self.assertNotIn(SECRET[4:12], dumped)
        self.assertNotIn("gsk_Qz7", dumped)
        message = next(e for e in seen if e["type"] == "assistant.message")
        self.assertIn("[REDACTED]", message["text"])

    def test_request_events_from_handle_message(self):
        BUS.enabled = True
        cases = (
            ([response("Hello, Sir.")], "complete", None),
            ([KeyboardInterrupt()], "cancelled", None),
            ([make_status_error(groq.AuthenticationError, 401, "bad key")], "error", "auth"),
        )
        for script, status, kind in cases:
            with self.subTest(status=status):
                seen = []
                unsubscribe = BUS.subscribe(seen.append)
                cli = self.make_cli(FakeGroqClient(script))
                with mock.patch("builtins.print"):
                    cli.handle_message("hello there")
                unsubscribe()
                requests = [e for e in seen if e["type"].startswith("request.")]
                self.assertEqual([e["type"] for e in requests], ["request.start", "request.end"])
                start, end = requests
                self.assertEqual((start["source"], start["text"]), ("terminal", "hello there"))
                self.assertEqual(end["status"], status)
                self.assertIsInstance(end["duration_ms"], int)
                self.assertGreaterEqual(end["duration_ms"], 0)
                self.assertEqual(end.get("error_kind"), kind)
                if status == "complete":
                    self.assertIsInstance(end["rounds"], int)
                self.assertTrue(start["request_id"].startswith("terminal-"))
                self.assertEqual(end["request_id"], start["request_id"])
                for event in seen:
                    self.assertEqual(validate_event(event), [], event)
                messages = [e for e in seen if e["type"] == "assistant.message"]
                if status == "cancelled":
                    self.assertEqual(messages, [])
                else:
                    self.assertEqual(len(messages), 1)
                    self.assertEqual(messages[0]["request_id"], start["request_id"])
                    self.assertLess(seen.index(messages[0]), seen.index(end))

    def test_unexpected_error_still_ends_the_request(self):
        BUS.enabled = True
        seen = self.record_bus()
        cli = self.make_cli()
        with mock.patch("ui.terminal.run_agent", side_effect=RuntimeError("boom")), mock.patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                cli.handle_message("hello")
        end = [e for e in seen if e["type"] == "request.end"]
        self.assertEqual(len(end), 1)
        self.assertEqual(end[0]["status"], "error")
        self.assertEqual(validate_event(end[0]), [])

    def test_voice_turn_is_tagged_voice_and_next_typed_message_terminal(self):
        BUS.enabled = True
        seen = self.record_bus()
        cli = self.make_cli()
        cli.listen_mode = True
        with mock.patch("ui.terminal.run_agent", return_value=AgentResult("Ok.", "complete", [], 1)), \
                mock.patch("builtins.print"), mock.patch("builtins.input", side_effect=["", "typed"]), \
                mock.patch.object(cli, "voice_turn", return_value="spoken words"):
            cli._step()
            cli._step()
        starts = [(e["source"], e["text"]) for e in seen if e["type"] == "request.start"]
        self.assertEqual(starts, [("voice", "spoken words"), ("terminal", "typed")])

    def test_wake_command_is_tagged_voice(self):
        BUS.enabled = True
        seen = self.record_bus()
        cli = self.make_cli()
        cli._wake_events.put(("command", b"pcm"))
        with mock.patch("ui.terminal.run_agent", return_value=AgentResult("Ok.", "complete", [], 1)), \
                mock.patch("builtins.print"), mock.patch.object(cli, "_voice_command", return_value="what time"):
            cli._step()
        starts = [(e["source"], e["text"]) for e in seen if e["type"] == "request.start"]
        self.assertEqual(starts, [("voice", "what time")])

    def test_say_publishes_assistant_message(self):
        BUS.enabled = True
        seen = self.record_bus()
        cli = self.make_cli()
        with mock.patch("builtins.print"):
            cli.say("x" * 5000, speak=False)
        messages = [e for e in seen if e["type"] == "assistant.message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(messages[0]["text"]), 4000)
        self.assertEqual(validate_event(messages[0]), [])

    def test_mission_control_off_changes_nothing(self):
        BUS.enabled = False
        ring_before = BUS.recent()
        cli = self.make_cli()
        with mock.patch("ui.terminal.run_agent", return_value=AgentResult("Hi.", "complete", [], 1)), \
                mock.patch("builtins.print"):
            cli.handle_message("hello")
            cli.say("again")
        self.assertEqual(BUS.recent(), ring_before)
        self.assertEqual(BUS._subscribers, [])
        captured = {}

        class CapturingCLI:
            def __init__(self, ctx, voice, memory, **kwargs):
                captured["ctx"] = ctx

            def loop(self):
                captured["threads"] = mission_threads()
                captured["enabled"] = BUS.enabled
                return 0

        env = {"GROQ_API_KEY": FAKE_KEY, "JARVIS_MEMORY_PATH": str(self.memory_path),
               "JARVIS_LOG_DIR": str(self.log_dir), "JARVIS_WORKSPACE": str(self.workspace),
               "JARVIS_UI_ENABLED": ""}
        with mock.patch.dict(os.environ, env), mock.patch("jarvis.load_env_file"), \
                mock.patch("jarvis.setup_logging"), mock.patch("jarvis.audit"), \
                mock.patch("jarvis.initialize_client", return_value=FakeGroqClient()), \
                mock.patch("jarvis.verify_model"), mock.patch("jarvis.JarvisCLI", CapturingCLI), \
                mock.patch("jarvis.start_mission_control") as start:
            self.assertEqual(jarvis.main(["--text"]), 0)
        start.assert_not_called()
        self.assertEqual(captured["threads"], [])
        self.assertFalse(captured["enabled"])
        self.assertIs(captured["ctx"].policy.confirm, jarvis.terminal_confirm)

    def test_import_jarvis_does_not_import_aiohttp_or_the_server(self):
        import subprocess
        code = ("import sys, jarvis; jarvis.parse_args(['--mission-control']); "
                "print('aiohttp' in sys.modules, 'mission_control.server' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(jarvis.__file__).parent),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "False False", out.stderr)

    def test_main_starts_and_stops_mission_control_before_voice_shutdown(self):
        order: list[str] = []
        captured = {}
        fake_server = mock.Mock()
        fake_server.stop.side_effect = lambda *a, **k: order.append("server.stop")

        class CapturingCLI:
            def __init__(self, ctx, voice, memory, **kwargs):
                captured["voice"] = voice

            def loop(self):
                order.append("loop")
                return 0

        def fake_start(config, cli, voice):
            captured["config"] = config
            order.append("start")
            BUS.enabled = True
            return fake_server

        real_voice = jarvis.VoiceEngine

        def make_voice(*args, **kwargs):
            engine = real_voice(*args, **kwargs)
            original = engine.shutdown
            engine.shutdown = lambda: (order.append("voice.shutdown"), original())[1]
            return engine

        env = {"GROQ_API_KEY": FAKE_KEY, "JARVIS_MEMORY_PATH": str(self.memory_path),
               "JARVIS_LOG_DIR": str(self.log_dir), "JARVIS_WORKSPACE": str(self.workspace)}
        seen = self.record_bus()
        with mock.patch.dict(os.environ, env), mock.patch("jarvis.load_env_file"), \
                mock.patch("jarvis.setup_logging"), mock.patch("jarvis.audit"), \
                mock.patch("jarvis.initialize_client", return_value=FakeGroqClient()), \
                mock.patch("jarvis.verify_model"), mock.patch("jarvis.JarvisCLI", CapturingCLI), \
                mock.patch("jarvis.VoiceEngine", side_effect=make_voice), \
                mock.patch("jarvis.start_mission_control", side_effect=fake_start):
            self.assertEqual(jarvis.main(["--text", "--mission-control", "--no-browser"]), 0)
        self.assertEqual(order, ["start", "loop", "server.stop", "voice.shutdown"])
        self.assertTrue(captured["config"].ui_enabled)
        self.assertFalse(captured["config"].ui_open_browser)
        self.assertEqual([e["type"] for e in seen], ["session.end"])
        self.assertFalse(BUS.enabled)


class ParseArgsTests(unittest.TestCase):
    def test_new_flags(self):
        args = jarvis.parse_args([])
        self.assertFalse(args.mission_control)
        self.assertFalse(args.no_browser)
        args = jarvis.parse_args(["--mission-control", "--no-browser"])
        self.assertTrue(args.mission_control)
        self.assertTrue(args.no_browser)


class OpenBrowserSecurelyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-mc-")
        self.addCleanup(self._tmp.cleanup)
        self.runtime = Path(self._tmp.name)

    def test_writes_private_redirect_file_and_opens_it(self):
        url = f"http://127.0.0.1:8765/#token={TOKEN}"
        opened: list[str] = []
        timers = []

        class FakeTimer:
            def __init__(self, delay, fn, args=()):
                self.delay, self.fn, self.args, self.daemon = delay, fn, args, False
                timers.append(self)

            def start(self):
                pass

        def fake_open(target):
            opened.append(target)
            path = Path(URL(target).path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            content = path.read_text(encoding="utf-8")
            self.assertIn(url, content)
            self.assertIn('content="no-referrer"', content)
            return True

        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}), \
                mock.patch("mission_control.launch.webbrowser.open", side_effect=fake_open), \
                mock.patch("mission_control.launch.Timer", FakeTimer):
            self.assertTrue(launch.open_browser_securely(url))
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("file://"))
        self.assertNotIn(TOKEN, opened[0])
        path = Path(URL(opened[0]).path)
        self.assertEqual(path.parent, self.runtime)
        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].daemon)
        self.assertAlmostEqual(timers[0].delay, 10.0, delta=5)
        self.assertTrue(path.exists())
        timers[0].fn(*timers[0].args)
        self.assertFalse(path.exists())
        timers[0].fn(*timers[0].args)  # deleting twice is harmless

    def test_falls_back_to_temp_dir_and_reports_failure(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime / "missing")}), \
                mock.patch("mission_control.launch.tempfile.gettempdir", return_value=str(self.runtime)), \
                mock.patch("mission_control.launch.webbrowser.open", return_value=False) as opener:
            self.assertFalse(launch.open_browser_securely("http://127.0.0.1:8765/#token=abc"))
        target = Path(URL(opener.call_args.args[0]).path)
        self.assertEqual(target.parent, self.runtime)
        self.assertFalse(target.exists())  # nothing opened it, so it is removed at once

    def test_html_escapes_the_url(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)}), \
                mock.patch("mission_control.launch.Timer"), \
                mock.patch("mission_control.launch.webbrowser.open", return_value=True) as opener:
            launch.open_browser_securely('http://127.0.0.1:8765/#token="><script>x</script>')
        content = Path(URL(opener.call_args.args[0]).path).read_text(encoding="utf-8")
        self.assertNotIn("<script>x", content)


if __name__ == "__main__":
    unittest.main()
