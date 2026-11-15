"""Mission Control end to end: a browser command that needs an approval, answered from the browser.

Everything is real except the model and the terminal: `start_mission_control` builds the server (port 0
on 127.0.0.1), the broker and the bridge; the JARVIS loop runs on this test's main thread exactly as in
`main`; an aiohttp client on another thread plays the browser tab. FakeGroqClient scripts the model, the
workspace is a temp dir, and stdin is a pipe that claims to be a terminal (nothing is ever typed into
it; closing it ends the loop the way Ctrl+D would)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import unittest
from unittest import mock

from helpers import AgentTestCase, FakeGroqClient, response, tool_call  # noqa: E402 - sets up sys.path
import aiohttp  # noqa: E402

import jarvis  # noqa: E402
from core.events import BUS  # noqa: E402
from mission_control.protocol import validate_event  # noqa: E402


DEADLINE = 20.0  # seconds; the whole exchange normally takes well under one


class PipeTerminal:
    """A stdin that select() can watch and that says it is a terminal. Closing the writer is end of input."""

    def __init__(self) -> None:
        read_fd, self._write_fd = os.pipe()
        self._reader = os.fdopen(read_fd, "r", encoding="utf-8")

    def fileno(self) -> int:
        return self._reader.fileno()

    def readline(self) -> str:
        return self._reader.readline()

    def isatty(self) -> bool:
        return True

    def end_input(self) -> None:
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None

    def close(self) -> None:
        self.end_input()
        self._reader.close()


class BrowserCommandEndToEndTests(AgentTestCase):
    def make_config(self, **overrides):
        overrides.setdefault("backup_dir", self.tmp / "backups")  # never the real home directory
        return super().make_config(**overrides)

    def setUp(self):
        super().setUp()
        saved = (BUS.enabled, list(BUS._subscribers), list(BUS._ring), BUS._seq, BUS._evicted_seq,
                 dict(BUS._last_emit))

        def restore():
            BUS.enabled, BUS._subscribers = saved[0], saved[1]
            BUS._ring.clear()
            BUS._ring.extend(saved[2])
            BUS._seq, BUS._evicted_seq, BUS._last_emit = saved[3], saved[4], saved[5]

        self.addCleanup(restore)
        for target in ("mission_control.server.probe_groq", "mission_control.confirm.audit"):
            patcher = mock.patch(target, return_value={"groq": "up", "checked_at": 0, "source": "probe"})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.voice = jarvis.VoiceEngine(enabled=False, player=["fake-player"])
        self.addCleanup(self.voice.shutdown)
        self.notes = self.workspace / "notes.txt"
        self.notes.write_text("helo world\n", encoding="utf-8")

    async def run_tab(self, port: int, token: str, frames: list[dict]) -> None:
        """The tab: submit a command, approve the confirmation it raises, wait for the request to end."""
        origin = f"http://127.0.0.1:{port}"

        async def next_of(ws, type_):
            while True:
                msg = await ws.receive(timeout=DEADLINE)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    raise AssertionError(f"socket ended early: {msg!r}")
                frame = json.loads(msg.data)
                frames.append(frame)
                if frame.get("type") == type_:
                    return frame

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"{origin}/ws", origin=origin) as ws:
                await ws.send_json({"type": "auth", "token": token, "last_seq": None})
                await next_of(ws, "auth.ok")
                await next_of(ws, "snapshot")
                await ws.send_json({"type": "command.submit", "id": "cmd-e2e",
                                    "text": "fix the typo in the notes.txt file"})
                request = await next_of(ws, "confirm.request")
                await ws.send_json({"type": "confirm.answer", "id": request["id"], "digest": request["digest"],
                                    "approved": True})
                result = await next_of(ws, "confirm.answer.result")
                self.assertEqual(result, {"type": "confirm.answer.result", "id": request["id"], "result": "ok"})
                await next_of(ws, "request.end")

    def test_browser_command_tool_approval_reply(self):
        script = [
            response(tool_calls=[tool_call("call-1", "edit_workspace_file",
                                           {"path": "notes.txt", "old_text": "helo", "new_text": "hello"})]),
            response("Fixed the typo in notes.txt, Sir."),
        ]
        client = FakeGroqClient(script, default=response("Done, Sir."))
        self.policy.confirm = jarvis.terminal_confirm
        ctx = jarvis.AgentContext(client, self.config, self.registry, self.policy, self.memory)
        with mock.patch("builtins.print"):
            cli = jarvis.JarvisCLI(ctx, self.voice, self.memory)
        config = self.make_config(ui_enabled=True, ui_port=0, ui_open_browser=False, confirm_timeout=30)

        stdin = PipeTerminal()
        self.addCleanup(stdin.close)
        frames: list[dict] = []
        failures: list[BaseException] = []

        with mock.patch("sys.stdin", stdin), mock.patch("builtins.print"):
            srv = jarvis.start_mission_control(config, cli, self.voice)
            self.assertIsNotNone(srv)
            self.addCleanup(srv.stop)
            self.assertEqual(self.policy.confirm, srv.broker.confirm)

            def tab():
                try:
                    asyncio.run(asyncio.wait_for(self.run_tab(srv.port, srv.token, frames), DEADLINE))
                except BaseException as exc:  # noqa: BLE001 - reported on the main thread
                    failures.append(exc)
                finally:
                    stdin.end_input()  # Ctrl+D: the loop leaves, whatever happened

            thread = threading.Thread(target=tab, name="e2e-browser", daemon=True)
            started = time.monotonic()
            thread.start()
            self.assertEqual(cli.loop(), 0)  # the real loop, on the main thread
            thread.join(DEADLINE)
            self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, DEADLINE)
        if failures:
            raise failures[0]

        self.assertEqual(self.notes.read_text(encoding="utf-8"), "hello world\n")

        events = [f for f in frames if isinstance(f.get("seq"), int) and "v" in f]
        for event in events:
            self.assertEqual(validate_event(event), [], event)
        watched = {"request.start", "confirm.request", "confirm.resolved", "assistant.message", "request.end"}
        timeline = []
        for event in sorted(events, key=lambda e: e["seq"]):
            if event["type"] == "tool.execution" and event["status"] in ("awaiting_confirmation", "success"):
                timeline.append(f"tool.execution {event['status']}")
            elif event["type"] == "confirm.resolved":
                timeline.append(f"confirm.resolved {event['channel']}")
            elif event["type"] in watched:
                timeline.append(event["type"])
        self.assertEqual(timeline, [
            "request.start",
            "tool.execution awaiting_confirmation",
            "confirm.request",
            "confirm.resolved browser",
            "tool.execution success",
            "assistant.message",
            "request.end",
        ])
        by_type = {e["type"]: e for e in events}
        start = by_type["request.start"]
        self.assertEqual((start["source"], start["command_id"]), ("browser", "cmd-e2e"))
        self.assertEqual(by_type["request.end"]["status"], "complete")
        self.assertTrue(by_type["confirm.resolved"]["approved"])
        self.assertEqual(by_type["command.ack"]["command_id"], "cmd-e2e")
        request_ids = {e["request_id"] for e in events if e["type"] in
                       ("request.start", "confirm.request", "tool.execution", "assistant.message", "request.end")}
        self.assertEqual(len(request_ids), 1)  # one request, from the browser, end to end
        self.assertIn("Fixed the typo", by_type["assistant.message"]["text"])


if __name__ == "__main__":
    unittest.main()
