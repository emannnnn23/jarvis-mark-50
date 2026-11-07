"""Mission Control protocol validation and the pure state tracker."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from mission_control.protocol import EVENT_TYPES, validate_event
from mission_control.state import StateTracker


FIXTURES = Path(__file__).resolve().parent.parent / "mission_control" / "protocol_fixtures.json"


def load_fixtures() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["events"]


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


EXPECTED_TYPES = {
    "session.start", "session.end", "jarvis.state", "request.start", "request.end", "assistant.message",
    "llm.call", "llm.retry", "llm.error", "tool.execution", "memory.activity", "task.update", "task.progress",
    "voice.output", "voice.input", "voice.level", "confirm.request", "confirm.resolved", "command.ack",
    "command.rejected", "command.output", "system.metrics", "network.status", "notice",
}


class ProtocolTests(unittest.TestCase):
    def test_every_fixture_validates(self) -> None:
        for event in load_fixtures():
            with self.subTest(type=event.get("type")):
                self.assertEqual(validate_event(event), [])

    def test_fixtures_cover_every_event_type(self) -> None:
        types = [event["type"] for event in load_fixtures()]
        self.assertEqual(set(types), EXPECTED_TYPES)
        self.assertEqual(len(types), len(set(types)), "one sample per type")
        self.assertEqual(set(EVENT_TYPES), EXPECTED_TYPES)

    def test_required_fields_are_frozensets_and_present_in_fixtures(self) -> None:
        for event in load_fixtures():
            required = EVENT_TYPES[event["type"]]
            self.assertIsInstance(required, frozenset)
            self.assertTrue(required <= set(event), event["type"])

    def test_validate_reports_missing_fields_and_unknown_type(self) -> None:
        problems = validate_event({"v": 1, "seq": 1, "type": "tool.execution", "timestamp": 1})
        self.assertTrue(any("tool" in p for p in problems), problems)
        self.assertTrue(any("status" in p for p in problems), problems)
        unknown = validate_event({"v": 1, "seq": 1, "type": "nope.nope", "timestamp": 1})
        self.assertTrue(any("unknown" in p for p in unknown), unknown)

    def test_validate_checks_the_envelope(self) -> None:
        good = {"v": 1, "seq": 3, "type": "session.end", "timestamp": 1762500000000}
        self.assertEqual(validate_event(good), [])
        for patch, label in (
            ({"v": 2}, "v"), ({"seq": "3"}, "seq"), ({"seq": True}, "seq"), ({"timestamp": 1.5}, "timestamp"),
            ({"type": 7}, "type"), ({"request_id": 4}, "request_id"),
        ):
            with self.subTest(label=label, patch=patch):
                problems = validate_event({**good, **patch})
                self.assertTrue(any(label in p for p in problems), problems)
        self.assertTrue(validate_event("not a dict"))
        self.assertTrue(validate_event(None))

    def test_validate_checks_enumerations(self) -> None:
        by_type = {event["type"]: event for event in load_fixtures()}
        cases = [
            ("jarvis.state", "state", "dreaming"),
            ("request.start", "source", "telepathy"),
            ("request.end", "status", "done"),
            ("llm.call", "phase", "middle"),
            ("tool.execution", "status", "exploded"),
            ("memory.activity", "op", "delete"),
            ("voice.output", "phase", "mumble"),
            ("voice.output", "timing", "approx"),
            ("voice.input", "mode", "always"),
            ("voice.input", "phase", "thinking"),
            ("voice.level", "phase", "recording"),
            ("confirm.resolved", "channel", "carrier-pigeon"),
            ("command.ack", "status", "maybe"),
            ("command.rejected", "reason", "grumpy"),
            ("network.status", "groq", "sideways"),
            ("network.status", "source", "guess"),
            ("notice", "level", "fatal"),
            ("session.start", "provider", "openai"),
            ("session.start", "memory_status", "enabled"),
            ("session.start", "tasks_status", "broken"),
        ]
        for type_, field, bad in cases:
            with self.subTest(type=type_, field=field):
                event = copy.deepcopy(by_type[type_])
                event[field] = bad
                problems = validate_event(event)
                self.assertTrue(any(field in p for p in problems), problems)

    def test_validate_checks_ranges_and_lengths(self) -> None:
        by_type = {event["type"]: event for event in load_fixtures()}
        cases = [
            ("voice.level", "rms", 1.5),
            ("voice.level", "rms", -0.1),
            ("request.start", "text", "x" * 501),
            ("assistant.message", "text", "x" * 4001),
            ("command.output", "text", "x" * 8001),
            ("confirm.request", "lines", [{"k": "add"}]),
            ("confirm.request", "browser_approvable", "yes"),
            ("task.update", "task", "a string"),
            ("voice.output", "words", [[0]]),
            ("llm.error", "retryable", "maybe"),
        ]
        for type_, field, bad in cases:
            with self.subTest(type=type_, field=field):
                event = copy.deepcopy(by_type[type_])
                event[field] = bad
                problems = validate_event(event)
                self.assertTrue(any(field in p for p in problems), problems)

    def test_task_update_accepts_null_task(self) -> None:
        event = {"v": 1, "seq": 1, "type": "task.update", "timestamp": 1, "task": None}
        self.assertEqual(validate_event(event), [])

    def test_validate_does_not_mutate_the_event(self) -> None:
        event = load_fixtures()[0]
        before = copy.deepcopy(event)
        validate_event(event)
        self.assertEqual(event, before)


def ev(type_: str, **payload) -> dict:
    return {"v": 1, "seq": 1, "type": type_, "timestamp": 1762500000000, **payload}


REQUEST_START = ev("request.start", source="terminal", text="hi")
REQUEST_END = ev("request.end", status="complete", duration_ms=10)
TOOL_RUNNING = ev("tool.execution", tool="read_file", status="running")
TOOL_AWAITING = ev("tool.execution", tool="write_file", status="awaiting_confirmation")
TOOL_SUCCESS = ev("tool.execution", tool="read_file", status="success")
SPEAK_START = ev("voice.output", phase="start")
SPEAK_END = ev("voice.output", phase="end")


def voice_in(phase: str) -> dict:
    return ev("voice.input", mode="wake", phase=phase)


class StateSequenceTests(unittest.TestCase):
    # Each row: (name, [(event, expected_state_after_or_None_for_no_change_signal, expected_snapshot_state)]).
    SEQUENCES = [
        ("request_with_tool", [
            (REQUEST_START, "processing", "processing"),
            (TOOL_RUNNING, "tool", "tool"),
            (TOOL_SUCCESS, "processing", "processing"),
            (REQUEST_END, "idle", "idle"),
        ]),
        ("approval_wait", [
            (REQUEST_START, "processing", "processing"),
            (TOOL_AWAITING, "tool", "tool"),
            (ev("tool.execution", tool="write_file", status="running"), "tool", "tool"),
            (ev("tool.execution", tool="write_file", status="denied"), "processing", "processing"),
            (REQUEST_END, "idle", "idle"),
        ]),
        ("speaking", [
            (SPEAK_START, "speaking", "speaking"),
            (ev("voice.output", phase="chunk", index=0), None, "speaking"),
            (SPEAK_END, "idle", "idle"),
        ]),
        ("speech_error_ends_speaking", [
            (SPEAK_START, "speaking", "speaking"),
            (ev("voice.output", phase="error"), "idle", "idle"),
        ]),
        ("wake", [
            (voice_in("wake_detected"), "listening", "listening"),
            (voice_in("collecting"), None, "listening"),
            (voice_in("transcribing"), "processing", "processing"),
            (REQUEST_START, None, "processing"),
            (voice_in("idle"), None, "processing"),
            (REQUEST_END, "idle", "idle"),
        ]),
        ("recording_then_armed", [
            (voice_in("recording"), "listening", "listening"),
            (voice_in("armed"), "idle", "idle"),
        ]),
        ("stopped_input_clears_listening", [
            (voice_in("collecting"), "listening", "listening"),
            (voice_in("stopped"), "idle", "idle"),
        ]),
        ("tool_outranks_speaking", [
            (SPEAK_START, "speaking", "speaking"),
            (TOOL_RUNNING, "tool", "tool"),
            (TOOL_SUCCESS, "speaking", "speaking"),
            (SPEAK_END, "idle", "idle"),
        ]),
        ("processing_outranks_speaking_and_listening", [
            (voice_in("collecting"), "listening", "listening"),
            (SPEAK_START, "speaking", "speaking"),
            (REQUEST_START, "processing", "processing"),
            (REQUEST_END, "speaking", "speaking"),
        ]),
        ("retryable_llm_error_stays_processing", [
            (REQUEST_START, "processing", "processing"),
            (ev("llm.error", kind="network", message="down", retryable=True), None, "processing"),
            (ev("llm.retry", reason="rate limit", delay=2.0), None, "processing"),
        ]),
        ("request_end_error_holds_error", [
            (REQUEST_START, "processing", "processing"),
            (ev("request.end", status="error", duration_ms=5, error_kind="auth"), "error", "error"),
        ]),
        ("request_end_cancelled_is_not_error", [
            (REQUEST_START, "processing", "processing"),
            (ev("request.end", status="cancelled", duration_ms=5), "idle", "idle"),
        ]),
        ("tool_finishing_for_other_tool_keeps_tool", [
            (TOOL_RUNNING, "tool", "tool"),
            (ev("tool.execution", tool="other", status="success"), None, "tool"),
            (TOOL_SUCCESS, "idle", "idle"),
        ]),
        ("request_end_clears_stuck_tool", [
            (REQUEST_START, "processing", "processing"),
            (TOOL_RUNNING, "tool", "tool"),
            (REQUEST_END, "idle", "idle"),
        ]),
        ("session_end_resets", [
            (REQUEST_START, "processing", "processing"),
            (SPEAK_START, None, "processing"),
            (ev("session.end"), "idle", "idle"),
        ]),
    ]

    def test_state_sequence(self) -> None:
        for name, steps in self.SEQUENCES:
            with self.subTest(sequence=name):
                tracker = StateTracker(clock=FakeClock())
                for index, (event, change_to, snapshot_state) in enumerate(steps):
                    result = tracker.apply(event)
                    where = f"{name} step {index} ({event['type']})"
                    if change_to is None:
                        self.assertIsNone(result, where)
                    else:
                        self.assertIsNotNone(result, where)
                        self.assertEqual(result["state"], change_to, where)
                    self.assertEqual(tracker.snapshot()["state"], snapshot_state, where)

    def test_apply_returns_none_when_state_unchanged(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        self.assertIsNotNone(tracker.apply(REQUEST_START))
        self.assertIsNone(tracker.apply(REQUEST_START))
        self.assertIsNone(tracker.apply(ev("assistant.message", text="hello")))
        self.assertIsNone(tracker.apply(ev("memory.activity", op="search", count=1)))
        self.assertIsNone(tracker.apply(ev("system.metrics", cpu_percent=1.0)))
        self.assertIsNone(tracker.apply({"type": "totally.unknown"}))
        self.assertIsNone(tracker.apply("garbage"))  # type: ignore[arg-type]

    def test_changed_payload_shape(self) -> None:
        wall = FakeClock(1762500000.0)
        tracker = StateTracker(clock=FakeClock(), wall_clock=wall)
        change = tracker.apply(TOOL_RUNNING)
        self.assertEqual(change, {"state": "tool", "detail": "read_file", "since": 1762500000000})
        change = tracker.apply(TOOL_AWAITING)
        self.assertEqual(change["detail"], "awaiting approval")  # still "tool", detail moved
        self.assertEqual(tracker.snapshot()["detail"], "awaiting approval")
        wall.now += 5
        change = tracker.apply(TOOL_SUCCESS)
        # write_file is still awaiting, so the state stays tool; finish it too.
        self.assertIsNone(change)
        change = tracker.apply(ev("tool.execution", tool="write_file", status="success"))
        self.assertEqual(change["state"], "idle")
        self.assertEqual(change["since"], 1762500005000)
        self.assertIsNone(change["detail"])

    def test_detail_change_returns_payload(self) -> None:
        wall = FakeClock(1762500000.0)
        tracker = StateTracker(clock=FakeClock(), wall_clock=wall)
        first = tracker.apply(TOOL_RUNNING)
        wall.now += 3
        second = tracker.apply(ev("tool.execution", tool="list_dir", status="running"))
        self.assertEqual(second["state"], "tool")
        self.assertEqual(second["detail"], "list_dir")
        self.assertEqual(second["since"], first["since"], "since marks when the state began")
        self.assertIsNone(tracker.apply(ev("tool.execution", tool="list_dir", status="running")))
        # Finishing the newest tool moves the detail back to the one still running.
        back = tracker.apply(ev("tool.execution", tool="list_dir", status="success"))
        self.assertEqual((back["state"], back["detail"]), ("tool", "read_file"))

    def test_awaiting_approval_detail(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        change = tracker.apply(TOOL_AWAITING)
        self.assertEqual(change["detail"], "awaiting approval")

    def test_requested_status_does_not_enter_tool_state(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        self.assertIsNone(tracker.apply(ev("tool.execution", tool="read_file", status="requested")))
        self.assertEqual(tracker.snapshot()["state"], "idle")

    def test_nonretryable_llm_error_holds_error_then_idles(self) -> None:
        clock = FakeClock()
        tracker = StateTracker(clock=clock)
        tracker.apply(REQUEST_START)
        change = tracker.apply(ev("llm.error", kind="auth", message="Groq rejected the API key", retryable=False))
        self.assertEqual(change["state"], "error")
        self.assertIn("Groq rejected", change["detail"])
        # request.end follows the error in the real flow; error keeps outranking idle.
        self.assertIsNone(tracker.apply(ev("request.end", status="error", duration_ms=3, error_kind="auth")))
        self.assertEqual(tracker.snapshot()["state"], "error")
        clock.now += StateTracker.ERROR_HOLD - 0.1
        self.assertIsNone(tracker.tick())
        self.assertEqual(tracker.snapshot()["state"], "error")
        clock.now += 0.2
        change = tracker.tick()
        self.assertEqual(change["state"], "idle")
        self.assertIsNone(tracker.tick())
        self.assertEqual(tracker.snapshot()["state"], "idle")

    def test_error_hold_is_four_seconds(self) -> None:
        self.assertEqual(StateTracker.ERROR_HOLD, 4.0)

    def test_request_start_clears_error(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.apply(ev("llm.error", kind="auth", message="bad key", retryable=False))
        self.assertEqual(tracker.snapshot()["state"], "error")
        change = tracker.apply(REQUEST_START)
        self.assertEqual(change["state"], "processing")

    def test_error_expiry_falls_back_to_underlying_state(self) -> None:
        clock = FakeClock()
        tracker = StateTracker(clock=clock)
        tracker.apply(SPEAK_START)
        tracker.apply(ev("llm.error", kind="auth", message="bad key", retryable=False))
        clock.now += 5
        change = tracker.tick()
        self.assertEqual(change["state"], "speaking")

    def test_apply_notices_expired_error(self) -> None:
        clock = FakeClock()
        tracker = StateTracker(clock=clock)
        tracker.apply(ev("llm.error", kind="auth", message="bad key", retryable=False))
        clock.now += 10
        change = tracker.apply(ev("assistant.message", text="hi"))
        self.assertEqual(change["state"], "idle")

    def test_tick_is_quiet_when_nothing_changed(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        self.assertIsNone(tracker.tick())
        tracker.apply(REQUEST_START)
        self.assertIsNone(tracker.tick())

    def test_jarvis_state_events_are_ignored(self) -> None:
        # The tracker's own output may be fed back by a careless consumer; it must not drive the state.
        tracker = StateTracker(clock=FakeClock())
        self.assertIsNone(tracker.apply(ev("jarvis.state", state="error", since=1)))
        self.assertEqual(tracker.snapshot()["state"], "idle")

    def test_malformed_events_do_not_raise(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        for bad in ({}, {"type": None}, {"type": ["unhashable"]}, {"type": "tool.execution"}, {"type": "voice.input"},
                    {"type": "llm.error"}, {"type": "network.status", "groq": ["x"]}, {"type": "system.metrics"}):
            with self.subTest(bad=bad):
                tracker.apply(bad)
        self.assertEqual(tracker.snapshot()["state"], "idle")


class SubsystemTests(unittest.TestCase):
    NODES = {"llm", "memory", "tools", "system", "voice", "network"}
    STATUSES = {"online", "degraded", "offline", "unknown", "unavailable"}

    def sub(self, tracker: StateTracker, name: str) -> dict:
        return tracker.snapshot()["subsystems"][name]

    def test_fresh_snapshot_has_six_unknown_nodes(self) -> None:
        snap = StateTracker(clock=FakeClock()).snapshot()
        self.assertEqual(snap["state"], "idle")
        self.assertEqual(set(snap["subsystems"]), self.NODES)
        for name, block in snap["subsystems"].items():
            self.assertEqual(block["status"], "unknown", name)

    def test_snapshot_is_json_serialisable_and_a_copy(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.apply(TOOL_SUCCESS)
        snap = tracker.snapshot()
        json.dumps(snap)
        snap["subsystems"]["tools"]["recent"].append({"tool": "x", "status": "error"})
        self.assertEqual(len(tracker.snapshot()["subsystems"]["tools"]["recent"]), 1)

    def test_subsystem_llm_status_follows_calls(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.apply(ev("llm.call", phase="start", model="m1", attempt=1))
        llm = self.sub(tracker, "llm")
        self.assertEqual(llm["status"], "unknown")
        self.assertEqual(llm["model"], "m1")
        tracker.apply(ev("llm.call", phase="end", model="m1", attempt=1, duration_ms=900, ok=True))
        llm = self.sub(tracker, "llm")
        self.assertEqual((llm["status"], llm["last_duration_ms"], llm["last_status"]), ("online", 900, "success"))
        tracker.apply(ev("llm.retry", reason="rate limit", delay=2.0))
        self.assertEqual(self.sub(tracker, "llm")["status"], "degraded")
        tracker.apply(ev("llm.call", phase="end", model="m1", attempt=2, duration_ms=700, ok=True))
        self.assertEqual(self.sub(tracker, "llm")["status"], "online")
        tracker.apply(ev("llm.error", kind="network", message="Cannot reach Groq", retryable=True))
        llm = self.sub(tracker, "llm")
        self.assertEqual((llm["status"], llm["last_status"], llm["last_error_kind"]), ("offline", "error", "network"))

    def test_llm_non_network_error_is_degraded(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.apply(ev("llm.error", kind="rate_limit", message="slow", retryable=True))
        self.assertEqual(self.sub(tracker, "llm")["status"], "degraded")

    def test_llm_failed_call_end_is_degraded(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.apply(ev("llm.call", phase="end", model="m", attempt=1, duration_ms=50, ok=False, error_kind="timeout"))
        llm = self.sub(tracker, "llm")
        self.assertEqual((llm["status"], llm["last_error_kind"]), ("degraded", "timeout"))

    def test_llm_model_from_session_start(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        session = next(e for e in load_fixtures() if e["type"] == "session.start")
        tracker.apply(session)
        self.assertEqual(self.sub(tracker, "llm")["model"], session["model"])

    def test_tools_track_active_and_recent(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.apply(TOOL_RUNNING)
        tools = self.sub(tracker, "tools")
        self.assertEqual(tools["status"], "online")
        self.assertEqual(tools["active"], ["read_file"])
        tracker.apply(TOOL_SUCCESS)
        tools = self.sub(tracker, "tools")
        self.assertEqual(tools["active"], [])
        self.assertEqual(tools["recent"], [{"tool": "read_file", "status": "success"}])
        tracker.apply(ev("tool.execution", tool="run_command", status="error"))
        tools = self.sub(tracker, "tools")
        self.assertEqual(tools["status"], "degraded")
        tracker.apply(ev("tool.execution", tool="list_dir", status="denied"))
        self.assertEqual(self.sub(tracker, "tools")["status"], "online")

    def test_tools_recent_is_bounded(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        for index in range(12):
            tracker.apply(ev("tool.execution", tool=f"t{index}", status="success"))
        recent = self.sub(tracker, "tools")["recent"]
        self.assertEqual(len(recent), StateTracker.RECENT_TOOLS)
        self.assertEqual(recent[-1]["tool"], "t11")

    def test_memory_tracks_last_op(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.apply(ev("memory.activity", op="retrieve", count=3))
        memory = self.sub(tracker, "memory")
        self.assertEqual((memory["status"], memory["last_op"], memory["count"]), ("online", "retrieve", 3))
        tracker.apply(ev("memory.activity", op="store"))
        memory = self.sub(tracker, "memory")
        self.assertEqual((memory["last_op"], memory["count"]), ("store", None))

    def test_memory_status_from_session_start(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        session = copy.deepcopy(next(e for e in load_fixtures() if e["type"] == "session.start"))
        for memory_status, expected in (("ok", "online"), ("corrupted", "degraded"), ("disabled", "unavailable")):
            with self.subTest(memory_status=memory_status):
                session["memory_status"] = memory_status
                tracker.apply(session)
                self.assertEqual(self.sub(tracker, "memory")["status"], expected)

    def test_voice_follows_input_and_output(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.apply(ev("voice.input", mode="wake", phase="armed"))
        voice = self.sub(tracker, "voice")
        self.assertEqual((voice["status"], voice["mode"], voice["input_phase"], voice["speaking"]),
                         ("online", "wake", "armed", False))
        tracker.apply(SPEAK_START)
        self.assertTrue(self.sub(tracker, "voice")["speaking"])
        tracker.apply(ev("voice.output", phase="error"))
        voice = self.sub(tracker, "voice")
        self.assertEqual((voice["speaking"], voice["status"]), (False, "degraded"))
        tracker.apply(SPEAK_START)
        tracker.apply(SPEAK_END)
        self.assertEqual(self.sub(tracker, "voice")["status"], "online")

    def test_voice_unavailable_from_session_start(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        session = copy.deepcopy(next(e for e in load_fixtures() if e["type"] == "session.start"))
        session["voice_available"] = False
        tracker.apply(session)
        self.assertEqual(self.sub(tracker, "voice")["status"], "unavailable")

    def test_system_keeps_last_metrics(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        metrics = next(e for e in load_fixtures() if e["type"] == "system.metrics")
        tracker.apply(metrics)
        system = self.sub(tracker, "system")
        self.assertEqual(system["status"], "online")
        self.assertEqual(system["cpu_percent"], metrics["cpu_percent"])
        self.assertEqual(system["memory_percent"], metrics["memory_percent"])
        self.assertEqual(system["disk_percent"], metrics["disk_percent"])

    def test_set_subsystem_merges_and_next_metrics_restore_online(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        tracker.set_subsystem("system", status="unavailable", reason="psutil not installed")
        system = self.sub(tracker, "system")
        self.assertEqual((system["status"], system["reason"]), ("unavailable", "psutil not installed"))
        self.assertIn("cpu_percent", system, "existing fields are kept")
        self.assertIsNone(tracker.apply(next(e for e in load_fixtures() if e["type"] == "system.metrics")))
        self.assertEqual(self.sub(tracker, "system")["status"], "online")
        with self.assertRaises(ValueError):
            tracker.set_subsystem("nonsense", status="online")
        self.assertEqual(set(tracker.snapshot()["subsystems"]), self.NODES)

    def test_network_follows_status(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        for groq, expected in (("up", "online"), ("down", "offline"), ("unknown", "unknown")):
            tracker.apply(ev("network.status", groq=groq, checked_at=5, source="probe", latency_ms=40))
            network = self.sub(tracker, "network")
            self.assertEqual(network["status"], expected)
            self.assertEqual((network["groq"], network["source"], network["latency_ms"]), (groq, "probe", 40))

    def test_every_status_is_in_the_allowed_set(self) -> None:
        tracker = StateTracker(clock=FakeClock())
        for event in load_fixtures():
            tracker.apply(event)
        for name, block in tracker.snapshot()["subsystems"].items():
            self.assertIn(block["status"], self.STATUSES, name)


if __name__ == "__main__":
    unittest.main()
