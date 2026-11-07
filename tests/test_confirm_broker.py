"""The confirmation broker: one approval answered from the terminal or Mission Control, first answer wins.

Readers and clocks are fakes, so every race is scripted; one test uses real threads. Nothing here
touches the real terminal, the network or Groq."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import AgentTestCase, BusCaptureMixin  # noqa: E402 - sets up sys.path for the project imports
import aiohttp  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import jarvis  # noqa: E402
from core.events import BUS, EventBus  # noqa: E402
from core.policy import ConfirmationRequest, Permission, PermissionPolicy  # noqa: E402
from mission_control import confirm, server  # noqa: E402
from mission_control.protocol import validate_event  # noqa: E402
from mission_control.state import StateTracker  # noqa: E402
from ui import terminal  # noqa: E402


SECRET = "gsk_" + "Qz7" * 14

EDIT = ConfirmationRequest(
    "edit_workspace_file", Permission.SENSITIVE_ACTION, "Edit app.py: 1 line(s) added, 1 removed.",
    ["/w/app.py"], "", "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2‮")


class FakeClock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeReader:
    """Scripted `poll`: item N is what poll N returns (None = nothing typed, an exception = raised).

    `hooks[N]` runs at the start of poll N (to answer from the browser mid-wait); each poll also
    advances the fake clock by the timeout it was given, as a real select would."""

    def __init__(self, script=(), hooks=None, clock: FakeClock | None = None, limit: int = 10_000):
        self.script = list(script)
        self.hooks = dict(hooks or {})
        self.clock = clock
        self.limit = limit
        self.timeouts: list[float] = []
        self.discarded = 0

    def poll(self, timeout: float):
        self.timeouts.append(timeout)
        n = len(self.timeouts)
        if n > self.limit:
            raise AssertionError("the broker kept polling")
        if n in self.hooks:
            self.hooks[n]()
        if self.clock is not None:
            self.clock.now += timeout
        item = self.script[n - 1] if n <= len(self.script) else None
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        return item

    def discard(self) -> None:
        self.discarded += 1


class BrokerTestCase(unittest.TestCase):
    def setUp(self):
        self.events: list[tuple[str, bool, dict]] = []
        self.audits: list[dict] = []
        patcher = mock.patch("mission_control.confirm.audit", side_effect=self.audits.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self, type_, _transient=False, **data):
        self.events.append((type_, _transient, data))

    def broker(self, reader, **kwargs) -> confirm.ConfirmationBroker:
        kwargs.setdefault("isatty", lambda: True)
        kwargs.setdefault("clock", FakeClock())
        kwargs.setdefault("wall_clock", lambda: 1_700_000_000.0)
        self.b = confirm.ConfirmationBroker(reader=reader, publish=self.publish, **kwargs)
        return self.b

    def ask(self, broker, request=EDIT, colour=False):
        with mock.patch("builtins.print") as printed, mock.patch.dict(os.environ, NO_COLOR="1"):
            result = broker.confirm(request)
        self.printed = "\n".join(" ".join(str(a) for a in call.args) for call in printed.call_args_list)
        return result

    def of_type(self, type_):
        return [data for t, _, data in self.events if t == type_]

    def request_event(self) -> dict:
        [event] = self.of_type("confirm.request")
        return event

    def resolved(self) -> dict:
        [event] = self.of_type("confirm.resolved")
        return event

    def browser(self, approved=True, **overrides):
        """A hook that answers the pending confirmation from the browser and records the result."""
        def hook():
            pending = self.b.pending_payload()
            self.browser_results.append(self.b.answer_from_browser(
                overrides.get("id", pending["id"]), overrides.get("digest", pending["digest"]), approved))
        return hook

    browser_results: list[str]

    def setup_results(self):
        self.browser_results = []


class TerminalAnswerTests(BrokerTestCase):
    def test_terminal_yes_approves(self):
        for answer in ("yes\n", " Y \n", "y\n"):
            with self.subTest(answer=answer):
                self.events.clear()
                self.audits.clear()
                broker = self.broker(FakeReader([None, answer]))
                self.assertIs(self.ask(broker), True)
                request = self.request_event()
                self.assertEqual(self.resolved(), {"id": request["id"], "approved": True, "channel": "terminal"})
                self.assertEqual(self.audits, [{"event": "confirmation", "tool": "edit_workspace_file",
                                                "approved": True, "channel": "terminal"}])
                self.assertIsNone(broker.pending_payload())

    def test_terminal_no_declines(self):
        for answer in ("no\n", "\n", "yes please\n", "nope\n"):
            with self.subTest(answer=answer):
                self.events.clear()
                broker = self.broker(FakeReader([answer]))
                self.assertIs(self.ask(broker), False)
                self.assertEqual(self.resolved()["channel"], "terminal")
                self.assertIs(self.resolved()["approved"], False)

    def test_request_is_transient_and_carries_the_view(self):
        broker = self.broker(FakeReader(["yes\n"]))
        self.ask(broker)
        [(type_, transient, data)] = [e for e in self.events if e[0] == "confirm.request"]
        self.assertTrue(transient)
        view = terminal.build_confirmation_view(EDIT)
        self.assertEqual(data["digest"], view.digest(broker._key))
        self.assertEqual(data["tool"], "edit_workspace_file")
        self.assertEqual(data["label"], "SENSITIVE ACTION")
        self.assertEqual(data["affected_paths"], ["/w/app.py"])
        self.assertEqual(data["hidden"], 1)
        self.assertEqual(data["omitted"], 0)
        self.assertEqual(data["lines"][-1], {"k": "add", "t": "+x = 2\\u{202e}"})
        self.assertEqual([line["k"] for line in data["lines"]], ["ctx", "ctx", "hunk", "del", "add"])
        self.assertIs(data["browser_approvable"], True)
        self.assertEqual(data["expires_at"], (1_700_000_000 + 600) * 1000)
        resolved = [e for e in self.events if e[0] == "confirm.resolved"]
        self.assertFalse(resolved[0][1])  # kept for replay, so a reconnecting tab closes a stale panel

    def test_prints_the_same_view_as_the_terminal_with_the_mission_prompt(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, NO_COLOR="1"):
            self.broker(FakeReader(["yes\n"])).confirm(EDIT)
        expected = io.StringIO()
        with contextlib.redirect_stdout(expected), mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)), \
                mock.patch("builtins.input", return_value="yes"):
            terminal.terminal_confirm(EDIT)
        self.assertEqual(out.getvalue(), expected.getvalue() + confirm.MISSION_PROMPT)
        self.assertEqual(confirm.MISSION_PROMPT,
                         "   Type 'yes' to proceed (or answer in Mission Control), anything else cancels: ")

    def test_not_a_tty_returns_false_and_publishes_nothing(self):
        reader = FakeReader(["yes\n"])
        broker = self.broker(reader, isatty=lambda: False)
        self.assertIs(self.ask(broker), False)
        self.assertEqual((self.events, self.audits, reader.timeouts, self.printed), ([], [], [], ""))
        self.assertIsNone(broker.pending_payload())

    def test_end_of_input_declines(self):
        broker = self.broker(FakeReader([None, EOFError]))
        self.assertIs(self.ask(broker), False)
        self.assertEqual(self.resolved()["channel"], "terminal")
        self.assertIs(self.resolved()["approved"], False)
        self.assertIsNone(broker.pending_payload())

    def test_keyboard_interrupt_is_reraised_and_resolved_cancelled(self):
        broker = self.broker(FakeReader([None, KeyboardInterrupt]))
        with self.assertRaises(KeyboardInterrupt):
            self.ask(broker)
        self.assertEqual(self.resolved()["channel"], "cancelled")
        self.assertIs(self.resolved()["approved"], False)
        self.assertEqual(self.audits[-1]["channel"], "cancelled")
        self.assertIsNone(broker.pending_payload())

    def test_keyboard_interrupt_after_a_browser_answer_is_still_cancelled(self):
        # The policy treats the interrupt as a decline, so the published outcome must say so too.
        self.setup_results()
        reader = FakeReader([None, KeyboardInterrupt])
        broker = self.broker(reader)
        reader.hooks = {2: self.browser(approved=True)}
        with self.assertRaises(KeyboardInterrupt):
            self.ask(broker)
        self.assertEqual(self.resolved(), {"id": self.request_event()["id"], "approved": False,
                                           "channel": "cancelled"})

    def test_reader_failure_declines(self):
        broker = self.broker(FakeReader([OSError("bad fd")]))
        self.assertIs(self.ask(broker), False)
        self.assertIs(self.resolved()["approved"], False)
        self.assertEqual(self.resolved()["channel"], "cancelled")

    def test_audit_never_carries_details(self):
        broker = self.broker(FakeReader(["yes\n"]))
        self.ask(broker, ConfirmationRequest("edit_workspace_file", Permission.SENSITIVE_ACTION, "Edit",
                                             ["/w/a"], "", f"+key = '{SECRET}'\n+private body text"))
        self.assertEqual(len(self.audits), 1)
        self.assertEqual(set(self.audits[0]), {"event", "tool", "approved", "channel"})
        self.assertNotIn("private body", json.dumps(self.audits))

    def test_only_one_pending_confirmation(self):
        inner: list[bool] = []
        reader = FakeReader([None, "yes\n"])
        broker = self.broker(reader)

        def ask_from_another_thread():
            thread = threading.Thread(target=lambda: inner.append(broker.confirm(EDIT)))
            thread.start()
            thread.join(5)

        reader.hooks = {1: ask_from_another_thread}
        self.assertIs(self.ask(broker), True)
        self.assertEqual(inner, [False])  # a second request while one is open is declined, unpublished
        self.assertEqual(len(self.of_type("confirm.request")), 1)
        self.assertEqual(len(self.of_type("confirm.resolved")), 1)


class StuckBrokerTests(BrokerTestCase):
    """However an interrupt lands, the broker never stays stuck with a confirmation nobody is asking."""

    def test_interrupt_right_after_registering_clears_pending(self):
        interrupts = [KeyboardInterrupt]

        def interrupted_publish(type_, _transient=False, **data):
            self.publish(type_, _transient, **data)
            if type_ == "confirm.request" and interrupts:
                raise interrupts.pop()  # the first statement after the confirmation is registered

        broker = confirm.ConfirmationBroker(reader=FakeReader(["yes\n"]), isatty=lambda: True,
                                            publish=interrupted_publish)
        with self.assertRaises(KeyboardInterrupt):
            self.ask(broker)
        self.assertIsNone(broker.pending_payload())
        self.assertIsNone(broker._pending)
        self.assertEqual(self.resolved()["channel"], "cancelled")
        self.events.clear()
        broker.reader = FakeReader(["yes\n"])
        self.assertIs(self.ask(broker), True)  # the next prompt works

    def test_interrupt_while_resolving_still_clears_pending(self):
        broker = self.broker(FakeReader(["yes\n"]))
        with mock.patch("mission_control.confirm.audit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.ask(broker)
        self.assertIsNone(broker._pending)
        self.assertEqual(len(self.of_type("confirm.resolved")), 1)

    def test_leftover_from_this_thread_is_closed_by_the_next_prompt(self):
        # Simulates an interrupt that landed in `finally` before the confirmation was unregistered.
        broker = self.broker(FakeReader(["yes\n"]))
        leftover = confirm._Pending("old-id", "d" * 64, {"tool": "open_url"}, True, None,
                                    threading.get_ident())
        broker._pending = leftover
        self.assertIs(self.ask(broker), True)
        resolved = self.of_type("confirm.resolved")
        self.assertEqual(resolved[0], {"id": "old-id", "approved": False, "channel": "cancelled"})
        self.assertEqual(resolved[1]["channel"], "terminal")
        self.assertIsNone(broker._pending)
        self.assertEqual(broker.answer_from_browser("old-id", "d" * 64, True), "already_answered")
        self.assertEqual(self.audits[0], {"event": "confirmation", "tool": "open_url", "approved": False,
                                          "channel": "cancelled"})


class BrowserAnswerTests(BrokerTestCase):
    def setUp(self):
        super().setUp()
        self.setup_results()

    def test_browser_approve(self):
        reader = FakeReader()
        broker = self.broker(reader)
        reader.hooks = {2: self.browser(approved=True)}
        self.assertIs(self.ask(broker), True)
        self.assertEqual(self.browser_results, ["ok"])
        self.assertEqual(self.resolved(), {"id": self.request_event()["id"], "approved": True, "channel": "browser"})
        self.assertEqual(self.audits[-1], {"event": "confirmation", "tool": "edit_workspace_file",
                                           "approved": True, "channel": "browser"})
        self.assertIn("Approved in Mission Control", self.printed)
        self.assertEqual(reader.discarded, 1)  # half-typed terminal input is not left for the next prompt

    def test_browser_decline(self):
        reader = FakeReader()
        broker = self.broker(reader)
        reader.hooks = {1: self.browser(approved=False)}
        self.assertIs(self.ask(broker), False)
        self.assertEqual(self.browser_results, ["ok"])
        self.assertEqual(self.resolved()["channel"], "browser")
        self.assertIs(self.resolved()["approved"], False)
        self.assertIn("Declined in Mission Control", self.printed)

    def test_browser_answer_after_terminal_is_already_answered(self):
        broker = self.broker(FakeReader(["yes\n"]))
        self.assertIs(self.ask(broker), True)
        request = self.request_event()
        self.assertEqual(broker.answer_from_browser(request["id"], request["digest"], False), "already_answered")
        self.assertEqual(len(self.of_type("confirm.resolved")), 1)

    def test_wrong_id_is_stale_and_changes_nothing(self):
        reader = FakeReader([None, "yes\n"])
        broker = self.broker(reader)
        reader.hooks = {1: self.browser(approved=False, id="not-the-id")}
        self.assertIs(self.ask(broker), True)
        self.assertEqual(self.browser_results, ["stale"])
        self.assertEqual(self.resolved()["channel"], "terminal")
        self.assertEqual(broker.answer_from_browser("never-seen", "x", True), "stale")

    def test_wrong_digest_is_rejected(self):
        reader = FakeReader([None, None, "no\n"])
        broker = self.broker(reader)
        reader.hooks = {1: self.browser(approved=True, digest="0" * 64),
                        2: self.browser(approved=True, digest="é‮ not ascii")}
        self.assertIs(self.ask(broker), False)
        self.assertEqual(self.browser_results, ["digest_mismatch", "digest_mismatch"])
        self.assertEqual(self.resolved()["channel"], "terminal")

    def test_only_a_true_boolean_approves(self):
        reader = FakeReader()
        broker = self.broker(reader)
        reader.hooks = {1: self.browser(approved="yes")}
        self.assertIs(self.ask(broker), False)
        self.assertEqual(self.browser_results, ["ok"])
        self.assertIs(self.resolved()["approved"], False)

    def test_secret_in_diff_can_only_be_approved_at_the_terminal(self):
        request = ConfirmationRequest("edit_workspace_file", Permission.SENSITIVE_ACTION, "Edit .env.example",
                                      ["/w/.env.example"], "", f"+GROQ_API_KEY={SECRET}\n+DEBUG=1")
        reader = FakeReader([None, None, None])
        broker = self.broker(reader)
        reader.hooks = {1: self.browser(approved=True), 2: self.browser(approved=False)}
        self.assertIs(self.ask(broker, request), False)
        self.assertEqual(self.browser_results, ["terminal_only", "ok"])
        payload = self.request_event()
        self.assertIs(payload["browser_approvable"], False)
        self.assertNotIn(SECRET, json.dumps(payload))
        self.assertNotIn(SECRET[4:20], json.dumps(payload))
        self.assertEqual(self.resolved()["channel"], "browser")

        # The terminal can still approve the same request.
        self.events.clear()
        self.assertIs(self.ask(self.broker(FakeReader(["yes\n"])), request), True)

    def test_secret_in_operation_or_path_is_terminal_only(self):
        for request in (
            ConfirmationRequest("open_url", Permission.CONTROLLED_ACTION, f"Open https://x/?k={SECRET}", []),
            ConfirmationRequest("write_note", Permission.CONTROLLED_ACTION, "Write", [f"/n/{SECRET}.md"]),
        ):
            with self.subTest(request=request.tool_name):
                self.events.clear()
                self.browser_results.clear()
                reader = FakeReader([None, None])
                broker = self.broker(reader)
                reader.hooks = {1: self.browser(approved=True)}
                self.assertIs(self.ask(broker, request), False)
                self.assertEqual(self.browser_results, ["terminal_only"])
                self.assertIs(self.request_event()["browser_approvable"], False)

    def test_line_too_long_for_the_wire_is_terminal_only(self):
        # The browser would see it cut short, so it must not be able to approve it.
        request = ConfirmationRequest("edit_workspace_file", Permission.SENSITIVE_ACTION, "Edit",
                                      ["/w/a"], "", "+" + "x" * 9000)
        broker = self.broker(FakeReader(["no\n"]))
        self.ask(broker, request)
        self.assertIs(self.request_event()["browser_approvable"], False)

    def test_payload_shown_is_exactly_what_is_approved(self):
        reader = FakeReader()
        broker = self.broker(reader)
        seen: list[dict] = []
        reader.hooks = {1: lambda: seen.append(broker.pending_payload()), 2: self.browser(approved=True)}
        self.ask(broker)
        published = self.request_event()
        self.assertEqual(seen, [published])
        view = terminal.build_confirmation_view(EDIT)
        shown = {key: published[key] for key in ("tool", "label", "operation", "affected_paths", "reason",
                                                 "hidden", "lines", "omitted")}
        self.assertEqual(shown, view.as_payload())
        self.assertEqual(published["digest"], view.digest(broker._key))

    def test_pending_payload_is_a_copy(self):
        reader = FakeReader([None, "no\n"])
        broker = self.broker(reader)

        def tamper():
            payload = broker.pending_payload()
            payload["lines"].clear()
            payload["browser_approvable"] = True
            self.assertTrue(broker.pending_payload()["lines"])

        reader.hooks = {1: tamper}
        self.ask(broker)


class DigestTests(BrokerTestCase):
    def test_published_digest_is_keyed_so_a_redacted_secret_cannot_be_checked_offline(self):
        request = ConfirmationRequest("edit_workspace_file", Permission.SENSITIVE_ACTION, "Edit settings.py",
                                      ["/w/settings.py"], "",
                                      "@@ -1 +1 @@\n-DB_PASSWORD = 'x'\n+DB_PASSWORD = 'summer2024'")
        broker = self.broker(FakeReader(["no\n"]))
        self.ask(broker, request)
        published = self.request_event()
        self.assertIs(published["browser_approvable"], False)
        self.assertNotIn("summer2024", json.dumps(published))
        view = terminal.build_confirmation_view(request)
        self.assertNotEqual(published["digest"], hashlib.sha256(view.canonical()).hexdigest())
        self.assertEqual(published["digest"], hmac.new(broker._key, view.canonical(), hashlib.sha256).hexdigest())
        self.assertNotIn(broker._key.hex(), json.dumps(self.events) + json.dumps(self.audits))

    def test_each_broker_has_its_own_key(self):
        first, second = confirm.ConfirmationBroker(), confirm.ConfirmationBroker()
        self.assertEqual(len(first._key), 32)
        self.assertNotEqual(first._key, second._key)


class MalformedBrowserAnswerTests(BrokerTestCase):
    def setUp(self):
        super().setUp()
        self.setup_results()

    def test_any_input_is_refused_without_raising(self):
        reader = FakeReader([None, "no\n"])
        broker = self.broker(reader)
        outcomes: list[tuple] = []

        def hammer():
            payload = broker.pending_payload()
            good_id, good_digest = payload["id"], payload["digest"]
            lone = json.loads('"\\ud800"')
            for confirm_id, digest, expected in (
                (lone, good_digest, "stale"),
                (good_id + lone, good_digest, "stale"),
                (None, good_digest, "stale"),
                (123, good_digest, "stale"),
                ([good_id], good_digest, "stale"),
                ({"id": good_id}, good_digest, "stale"),
                (b"bytes", good_digest, "stale"),
                ("x" * 1_000_000, good_digest, "stale"),
                (good_id, json.loads('"\\udfff"'), "digest_mismatch"),
                (good_id, None, "digest_mismatch"),
                (good_id, 7, "digest_mismatch"),
                (good_id, ["a"], "digest_mismatch"),
                (good_id, "f" * 1_000_000, "digest_mismatch"),
                (good_id, good_digest + lone, "digest_mismatch"),
                (good_id, "é" * 64, "digest_mismatch"),
            ):
                outcomes.append((repr(confirm_id)[:30], repr(digest)[:30],
                                 broker.answer_from_browser(confirm_id, digest, True), expected))

        reader.hooks = {1: hammer}
        self.assertIs(self.ask(broker), False)
        for confirm_id, digest, got, expected in outcomes:
            self.assertEqual(got, expected, (confirm_id, digest))
        self.assertEqual(self.resolved()["channel"], "terminal")  # none of it answered the prompt
        self.assertEqual(broker.answer_from_browser(json.loads('"\\ud800"'), None, None), "stale")

    def test_an_internal_fault_is_refused_not_raised(self):
        broker = self.broker(FakeReader(["no\n"]))
        with mock.patch.object(broker, "_answer", side_effect=RuntimeError("boom")):
            self.assertEqual(broker.answer_from_browser("a", "b", True), "stale")


class TimeoutTests(BrokerTestCase):
    def test_browser_answer_after_the_deadline_is_stale(self):
        self.setup_results()
        clock = FakeClock()
        reader = FakeReader([None, None], clock=clock)
        broker = self.broker(reader, clock=clock, timeout=5.0)

        def late():
            clock.now += 5.0  # the deadline passes before the terminal loop looks again
            self.browser(approved=True)()

        reader.hooks = {2: late}
        self.assertIs(self.ask(broker), False)
        self.assertEqual(self.browser_results, ["stale"])
        self.assertEqual(self.resolved()["channel"], "timeout")

    def test_deadline_and_expiry_are_taken_together_before_publishing(self):
        clock = FakeClock(now=50.0)
        walls = iter([1_700_000_000.0, 1_800_000_000.0])
        seen: list = []
        reader = FakeReader(["yes\n"])
        broker = self.broker(reader, clock=clock, timeout=30.0, wall_clock=lambda: next(walls))
        reader.hooks = {1: lambda: seen.append(broker._pending.deadline)}
        self.ask(broker)
        self.assertEqual(seen, [80.0])
        self.assertEqual(self.request_event()["expires_at"], (1_700_000_000 + 30) * 1000)

    def test_timeout_declines(self):
        clock = FakeClock()
        reader = FakeReader(clock=clock)
        broker = self.broker(reader, clock=clock, timeout=5.0)
        self.assertIs(self.ask(broker), False)
        self.assertEqual(self.resolved()["channel"], "timeout")
        self.assertIs(self.resolved()["approved"], False)
        self.assertAlmostEqual(sum(reader.timeouts), 5.0)
        self.assertTrue(all(0 < t <= confirm.POLL_INTERVAL for t in reader.timeouts))
        self.assertEqual(self.request_event()["expires_at"], (1_700_000_000 + 5) * 1000)
        self.assertIn("No answer", self.printed)
        self.assertEqual(self.audits[-1]["channel"], "timeout")

    def test_zero_timeout_never_expires(self):
        clock = FakeClock()
        reader = FakeReader([None] * 500 + ["yes\n"], clock=clock)
        broker = self.broker(reader, clock=clock, timeout=0)
        clock.now += 10 ** 6
        self.assertIs(self.ask(broker), True)
        self.assertIsNone(self.request_event()["expires_at"])
        self.assertEqual(self.resolved()["channel"], "terminal")

    def test_browser_answer_just_before_the_deadline_wins(self):
        self.setup_results()
        clock = FakeClock()
        reader = FakeReader(clock=clock)
        broker = self.broker(reader, clock=clock, timeout=0.2)
        reader.hooks = {2: self.browser(approved=True)}
        self.assertIs(self.ask(broker), True)
        self.assertEqual(self.resolved()["channel"], "browser")


class RaceTests(BrokerTestCase):
    def test_browser_answer_while_the_terminal_line_is_read_wins(self):
        # The browser answered first; the typed line that arrives just after must not override it.
        self.setup_results()
        reader = FakeReader([None, "yes\n"])
        broker = self.broker(reader)
        reader.hooks = {2: self.browser(approved=False)}
        self.assertIs(self.ask(broker), False)
        self.assertEqual(self.browser_results, ["ok"])
        self.assertEqual(self.resolved()["channel"], "browser")

    def test_eight_threads_and_the_terminal_exactly_one_winner(self):
        winners: list[str] = []
        for attempt in range(24):
            with self.subTest(attempt=attempt):
                self.events.clear()
                results: list[str] = []
                lock = threading.Lock()
                barrier = threading.Barrier(9)
                threads: list[threading.Thread] = []
                broker = self.broker(None)
                terminal_answers = attempt % 3 != 0  # every third round only the browsers race

                def answer(approved):
                    payload = broker.pending_payload()
                    barrier.wait()
                    result = broker.answer_from_browser(payload["id"], payload["digest"], approved)
                    with lock:
                        results.append(result)

                def start_threads():
                    for n in range(8):
                        thread = threading.Thread(target=answer, args=(n % 2 == 0,))
                        thread.start()
                        threads.append(thread)
                    barrier.wait()  # release the browsers and race them against the terminal's "yes"
                    time.sleep((attempt % 4) * 0.0005)

                broker.reader = FakeReader(["yes\n" if terminal_answers else None], hooks={1: start_threads})
                approved = self.ask(broker)
                for thread in threads:
                    thread.join(5)
                resolved = self.resolved()
                winners.append(resolved["channel"])
                terminal_won = resolved["channel"] == "terminal"
                self.assertEqual(results.count("ok") + int(terminal_won), 1, results)
                self.assertEqual(results.count("already_answered"), 8 - results.count("ok"))
                self.assertIs(approved, resolved["approved"])
                self.assertEqual(len(self.of_type("confirm.resolved")), 1)
        self.assertIn("browser", winners)


class StdinAnswerReaderTests(unittest.TestCase):
    def pipe(self):
        read_fd, write_fd = os.pipe()
        reader_file = os.fdopen(read_fd, "r", encoding="utf-8")
        writer_file = os.fdopen(write_fd, "w", encoding="utf-8")
        self.addCleanup(reader_file.close)
        self.addCleanup(lambda: writer_file.closed or writer_file.close())
        return reader_file, writer_file

    def test_poll_returns_a_line_none_or_raises_eof(self):
        stream, writer = self.pipe()
        reader = confirm.StdinAnswerReader(stream)
        self.assertIsNone(reader.poll(0.01))
        writer.write("yes\n")
        writer.flush()
        self.assertEqual(reader.poll(1.0), "yes\n")
        writer.close()
        with self.assertRaises(EOFError):
            reader.poll(1.0)

    def test_discard_never_raises_on_a_pipe(self):
        stream, _ = self.pipe()
        confirm.StdinAnswerReader(stream).discard()

    def test_default_reader_reads_sys_stdin(self):
        stream, writer = self.pipe()
        writer.write("no\n")
        writer.flush()
        with mock.patch("sys.stdin", stream):
            self.assertEqual(confirm.StdinAnswerReader().poll(1.0), "no\n")
            self.assertIsInstance(confirm.ConfirmationBroker().reader, confirm.StdinAnswerReader)


class PolicyIntegrationTests(AgentTestCase):
    def make_config(self, **overrides):
        overrides.setdefault("backup_dir", self.tmp / "backups")  # never the real home directory
        return super().make_config(**overrides)

    def setUp(self):
        super().setUp()
        self.target = self.workspace / "calc.py"
        self.target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        self.events: list[tuple] = []
        patcher = mock.patch("mission_control.confirm.audit")
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self, type_, _transient=False, **data):
        self.events.append((type_, data))

    def edit(self, broker):
        self.policy = PermissionPolicy("standard", confirm=broker.confirm)
        with mock.patch("builtins.print"):
            return jarvis.execute_tool(self.registry, self.policy, "edit_workspace_file",
                                       json.dumps({"path": "calc.py", "old_text": "return a - b",
                                                   "new_text": "return a + b"}))

    def approvals(self) -> dict:
        return self.tools.__dict__.get("_approved_changes", {})

    def browser_answer(self, broker, approved):
        def hook():
            payload = broker.pending_payload()
            self.assertEqual(self.approvals().__len__(), 1)  # prepared while describing the change
            self.assertEqual(broker.answer_from_browser(payload["id"], payload["digest"], approved), "ok")
        return hook

    def test_browser_decline_drops_developer_approval(self):
        reader = FakeReader()
        broker = confirm.ConfirmationBroker(reader=reader, isatty=lambda: True, publish=self.publish)
        reader.hooks = {1: self.browser_answer(broker, False)}
        result = self.edit(broker)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(self.approvals(), {})
        self.assertIn("return a - b", self.target.read_text())
        with self.assertRaises(jarvis.ToolError):  # nothing left for a later call to use
            self.tools.edit_workspace_file("calc.py", "return a - b", "return a + b")

    def test_browser_approve_writes_the_reviewed_change(self):
        reader = FakeReader()
        broker = confirm.ConfirmationBroker(reader=reader, isatty=lambda: True, publish=self.publish)
        reader.hooks = {1: self.browser_answer(broker, True)}
        result = self.edit(broker)
        self.assertEqual(result["status"], "success", result)
        self.assertIn("return a + b", self.target.read_text())
        self.assertEqual(self.approvals(), {})

    def test_interrupt_during_the_prompt_declines_through_the_policy(self):
        broker = confirm.ConfirmationBroker(reader=FakeReader([KeyboardInterrupt]), isatty=lambda: True,
                                            publish=self.publish)
        result = self.edit(broker)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(self.approvals(), {})
        self.assertEqual([d["channel"] for t, d in self.events if t == "confirm.resolved"], ["cancelled"])


class SharedBusTests(BusCaptureMixin, unittest.TestCase):
    def test_default_publish_reaches_subscribers_but_not_the_replay_ring(self):
        saved_ring, saved_seq = list(BUS._ring), BUS._seq
        self.addCleanup(lambda: (BUS._ring.clear(), BUS._ring.extend(saved_ring)))
        events = self.capture_events()
        BUS._ring.clear()
        broker = confirm.ConfirmationBroker(reader=FakeReader(["yes\n"]), isatty=lambda: True)
        with mock.patch("builtins.print"), mock.patch("mission_control.confirm.audit"):
            self.assertIs(broker.confirm(EDIT), True)
        self.assertEqual([e["type"] for e in events], ["confirm.request", "confirm.resolved"])
        self.assert_valid(events)
        self.assertEqual([e["type"] for e in BUS.recent()], ["confirm.resolved"])
        self.assertGreater(BUS._seq, saved_seq)

    def test_bus_off_publishes_nothing(self):
        events = self.capture_events()
        BUS.enabled = False
        broker = confirm.ConfirmationBroker(reader=FakeReader(["no\n"]), isatty=lambda: True)
        with mock.patch("builtins.print"), mock.patch("mission_control.confirm.audit"):
            self.assertIs(broker.confirm(EDIT), False)
        self.assertEqual(events, [])


class MissionControlDeliveryTests(unittest.IsolatedAsyncioTestCase):
    TOKEN = "test-token-" + "y" * 32

    async def asyncSetUp(self):
        self.bus = EventBus()
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-confirm-")
        self.addCleanup(self._tmp.cleanup)
        for target in ("mission_control.server.probe_groq", "mission_control.confirm.audit"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def recv(self, ws) -> dict:
        msg = await ws.receive(timeout=3)
        self.assertEqual(msg.type, aiohttp.WSMsgType.TEXT, f"expected a text frame, got {msg!r}")
        return json.loads(msg.data)

    async def authed(self, client):
        ws = await client.ws_connect("/ws", origin=self.origin)
        self.addAsyncCleanup(ws.close)
        await ws.send_json({"type": "auth", "token": self.TOKEN, "last_seq": None})
        self.assertEqual((await self.recv(ws))["type"], "auth.ok")
        self.assertEqual((await self.recv(ws))["type"], "snapshot")
        return ws

    async def next_of(self, ws, type_) -> dict:
        while True:
            frame = await self.recv(ws)
            if frame.get("type") == type_:
                return frame

    async def test_pending_payload_in_snapshot_and_resolution_reaches_all_clients(self):
        waiting, answered = threading.Event(), threading.Event()

        def poll_hook():
            waiting.set()

        class BlockingReader:
            def poll(self, timeout):
                poll_hook()
                answered.wait(timeout)
                return None

        broker = confirm.ConfirmationBroker(reader=BlockingReader(), isatty=lambda: True,
                                            publish=self.bus.publish)
        hub = server.Hub(self.bus, StateTracker(), None, self.TOKEN, 0, broker=broker)
        app = server.create_app(hub, dist_dir=Path(self._tmp.name) / "dist")
        client = TestClient(TestServer(app, host="127.0.0.1", port=0))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        hub.port = client.server.port
        hub._last_probe = asyncio.get_running_loop().time()
        self.origin = f"http://127.0.0.1:{hub.port}"

        first = await self.authed(client)
        outcome: list[bool] = []
        thread = threading.Thread(target=lambda: outcome.append(self._confirm(broker)))
        thread.start()
        try:
            live = await self.next_of(first, "confirm.request")
            self.assertEqual(validate_event(live), [])
            await asyncio.get_running_loop().run_in_executor(None, waiting.wait, 3)

            # A tab reloaded while the approval is open gets it from the snapshot, and can answer it.
            second = await client.ws_connect("/ws", origin=self.origin)
            self.addAsyncCleanup(second.close)
            await second.send_json({"type": "auth", "token": self.TOKEN, "last_seq": None})
            self.assertEqual((await self.recv(second))["type"], "auth.ok")
            snap = await self.recv(second)
            pending = snap["pending_confirmation"]
            envelope = {"v", "seq", "type", "timestamp", "request_id"}
            self.assertEqual(pending, {k: v for k, v in live.items() if k not in envelope})
            self.assertNotIn("confirm.request", [e["type"] for e in snap["recent_events"]])  # transient

            self.assertEqual(broker.answer_from_browser(pending["id"], pending["digest"], True), "ok")
            answered.set()
            for ws in (first, second):
                resolved = await self.next_of(ws, "confirm.resolved")
                self.assertEqual({k: resolved[k] for k in ("id", "approved", "channel")},
                                 {"id": pending["id"], "approved": True, "channel": "browser"})
                self.assertEqual(validate_event(resolved), [])
        finally:
            answered.set()
            await asyncio.get_running_loop().run_in_executor(None, thread.join, 5)
        self.assertEqual(outcome, [True])
        self.assertEqual([e["type"] for e in self.bus.recent()].count("confirm.resolved"), 1)
        self.assertIsNone(hub.snapshot()["pending_confirmation"])
        # Exactly one resolution reached each tab: nothing else confirm-related is queued behind it.
        for ws in (first, second):
            await ws.send_json({"type": "ping"})
            frames = []
            while (frame := await self.recv(ws)).get("type") != "pong":
                frames.append(frame)
            self.assertNotIn("confirm.resolved", [f.get("type") for f in frames])
            self.assertNotIn("confirm.request", [f.get("type") for f in frames])

    @staticmethod
    def _confirm(broker) -> bool:
        with mock.patch("builtins.print"):
            return broker.confirm(EDIT)


if __name__ == "__main__":
    unittest.main()
