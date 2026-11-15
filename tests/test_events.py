"""The Mission Control event bus: thread-safe, never raises, bounded, scrubbed."""

from __future__ import annotations

import threading
import time
import unittest

from core import events
from core.audit import clean_text, redact, safe_display
from core.events import BUS, EventBus, PROTOCOL_VERSION, PUBLISH_MAX_STR, emit, emit_throttled, scrub
from mission_control.protocol import validate_event


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class BusTestCase(unittest.TestCase):
    """Restores the module singleton so no test leaks state into another."""

    def _guard_singleton(self) -> None:
        saved_enabled, saved_subs = BUS.enabled, list(BUS._subscribers)
        saved_ring, saved_seq = list(BUS._ring), BUS._seq
        saved_evicted, saved_throttle = BUS._evicted_seq, dict(BUS._last_emit)

        def restore() -> None:
            BUS.enabled = saved_enabled
            BUS._subscribers = saved_subs
            BUS._ring.clear()
            BUS._ring.extend(saved_ring)
            BUS._seq, BUS._evicted_seq = saved_seq, saved_evicted
            BUS._last_emit = saved_throttle

        self.addCleanup(restore)


class TestEventBus(BusTestCase):
    def test_disabled_bus_emit_is_noop(self) -> None:
        self._guard_singleton()
        BUS.enabled = False
        before = BUS.recent()
        emit("tool.call", name="read_file")
        emit_throttled("k", 0.2, "voice.level", level=0.5)
        self.assertEqual(BUS.recent(), before)

    def test_enabled_bus_emit_publishes(self) -> None:
        self._guard_singleton()
        BUS.enabled = True
        seen: list[dict] = []
        BUS.subscribe(seen.append)
        emit("tool.call", name="read_file")
        self.assertEqual([e["type"] for e in seen], ["tool.call"])
        self.assertEqual(seen[0]["name"], "read_file")

    def test_publish_assigns_monotonic_seq_and_envelope(self) -> None:
        bus = EventBus(clock=FakeClock(12.5))
        first = bus.publish("a", x=1)
        second = bus.publish("b")
        third = bus.publish("c")
        self.assertEqual([first["seq"], second["seq"], third["seq"]], [1, 2, 3])
        for key in ("v", "seq", "type", "timestamp"):
            self.assertIn(key, first)
        self.assertEqual(first["v"], PROTOCOL_VERSION)
        self.assertEqual(first["timestamp"], 12500)
        self.assertEqual(first["x"], 1)
        self.assertNotIn("request_id", first)

    def test_payload_cannot_overwrite_envelope(self) -> None:
        bus = EventBus()
        event = bus.publish("a", seq=99, v=7, timestamp=0)
        self.assertEqual((event["seq"], event["v"]), (1, PROTOCOL_VERSION))
        self.assertNotEqual(event["timestamp"], 0)

    def test_failing_subscriber_never_raises_into_caller(self) -> None:
        bus = EventBus()
        seen: list[dict] = []

        def boom(_event: dict) -> None:
            raise RuntimeError("subscriber exploded")

        bus.subscribe(boom)
        bus.subscribe(seen.append)
        event = bus.publish("a")
        self.assertEqual(event["seq"], 1)
        self.assertEqual(len(seen), 1)

    def test_publish_with_unserialisable_data_never_raises(self) -> None:
        bus = EventBus()
        event = bus.publish("a", thing=object(), nested={"k": {1, 2}})
        self.assertIsNotNone(event)
        self.assertIsInstance(event["thing"], str)

    def test_unsubscribe_stops_delivery_and_is_idempotent(self) -> None:
        bus = EventBus()
        seen: list[dict] = []
        unsubscribe = bus.subscribe(seen.append)
        bus.publish("a")
        unsubscribe()
        unsubscribe()
        bus.publish("b")
        self.assertEqual([e["type"] for e in seen], ["a"])

    def test_ring_is_bounded_and_recent_reports_gap(self) -> None:
        bus = EventBus(history=3)
        for _ in range(5):
            bus.publish("a")
        self.assertEqual([e["seq"] for e in bus.recent()], [3, 4, 5])
        self.assertIsNone(bus.recent(after_seq=1))
        self.assertEqual([e["seq"] for e in bus.recent(after_seq=2)], [3, 4, 5])
        self.assertEqual([e["seq"] for e in bus.recent(after_seq=3)], [4, 5])
        self.assertEqual(bus.recent(after_seq=5), [])

    def test_recent_from_a_future_seq_asks_for_a_snapshot(self) -> None:
        bus = EventBus()
        bus.publish("a")
        self.assertIsNone(bus.recent(after_seq=50))

    def test_transient_events_skip_the_ring(self) -> None:
        bus = EventBus(history=2)
        seen: list[dict] = []
        bus.subscribe(seen.append)
        bus.publish("kept")
        bus.publish("level", _transient=True, level=0.3)
        bus.publish("kept2")
        bus.publish("kept3")
        self.assertEqual(len(seen), 4)
        self.assertEqual([e["type"] for e in bus.recent()], ["kept2", "kept3"])
        # Transient events do not make a resuming client think it lost history.
        fresh = EventBus(history=2)
        fresh.publish("a")
        fresh.publish("level", _transient=True)
        fresh.publish("b")
        self.assertEqual([e["type"] for e in fresh.recent(after_seq=1)], ["b"])

    def test_request_scope_tags_events(self) -> None:
        bus = EventBus()
        before = bus.publish("before")
        with bus.request_scope("voice") as request_id:
            inside = bus.publish("inside")
            again = bus.publish("again")
        after = bus.publish("after")
        self.assertTrue(request_id)
        self.assertEqual(inside["request_id"], request_id)
        self.assertEqual(again["request_id"], request_id)
        self.assertNotIn("request_id", before)
        self.assertNotIn("request_id", after)
        with bus.request_scope("voice") as other:
            pass
        self.assertNotEqual(other, request_id)

    def test_request_scope_is_per_thread(self) -> None:
        bus = EventBus()
        seen: list[dict] = []
        with bus.request_scope("terminal"):
            thread = threading.Thread(target=lambda: seen.append(bus.publish("other")))
            thread.start()
            thread.join()
        self.assertNotIn("request_id", seen[0])

    def test_concurrent_publishes_get_unique_seqs(self) -> None:
        bus = EventBus(history=5000)
        seqs: list[int] = []
        collect = threading.Lock()

        def worker() -> None:
            mine = [bus.publish("a")["seq"] for _ in range(200)]
            with collect:
                seqs.extend(mine)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(seqs), 1600)
        self.assertEqual(len(set(seqs)), 1600)
        self.assertEqual([e["seq"] for e in bus.recent()], sorted(seqs))

    def test_subscriber_may_publish_without_deadlock(self) -> None:
        bus = EventBus()
        bus.subscribe(lambda e: bus.publish("echo") if e["type"] == "a" else None)
        bus.publish("a")
        self.assertEqual([e["type"] for e in bus.recent()], ["a", "echo"])

    def test_publish_scrubs_secrets(self) -> None:
        bus = EventBus()
        event = bus.publish("a", text="key gsk_" + "k" * 30)
        self.assertNotIn("gsk_k", event["text"])

    def test_publish_keeps_400_line_payloads_and_8000_char_strings(self) -> None:
        bus = EventBus()
        lines = [{"k": "add" if i % 2 else "ctx", "t": "L" * 1000} for i in range(400)]
        event = bus.publish("confirm.request", lines=lines, output="o" * 8000)
        self.assertEqual(len(event["lines"]), 400)
        self.assertEqual(event["lines"][399]["t"], "L" * 1000)
        self.assertEqual(len(event["output"]), 8000)
        self.assertEqual(len(bus.publish("x", output="o" * 9000)["output"]), 8000)

    def test_emit_throttled_limits_rate(self) -> None:
        clock = FakeClock()
        bus = EventBus(clock=clock)
        bus.enabled = True
        bus.emit_throttled("level", 0.2, "voice.level", level=0.1)
        clock.now += 0.1
        bus.emit_throttled("level", 0.2, "voice.level", level=0.2)
        self.assertEqual(len(bus.recent()), 1)
        clock.now += 0.15
        bus.emit_throttled("level", 0.2, "voice.level", level=0.3)
        bus.emit_throttled("other", 0.2, "voice.level", level=0.4)
        self.assertEqual([e["level"] for e in bus.recent()], [0.1, 0.3, 0.4])

    def test_emit_throttled_transient_reaches_subscribers_but_not_the_ring(self) -> None:
        clock = FakeClock()
        bus = EventBus(clock=clock, history=3)
        bus.enabled = True
        seen: list[dict] = []
        bus.subscribe(seen.append)
        bus.emit("tool.execution", tool="read_file", status="success")
        for i in range(10):
            bus.emit_throttled("level", 0.2, "voice.level", _transient=True, rms=0.1, phase="armed")
            clock.now += 0.3
        self.assertEqual(len(seen), 11)
        self.assertNotIn("_transient", seen[-1])
        self.assertEqual([e["type"] for e in bus.recent()], ["tool.execution"])
        self.assertEqual(bus.recent(0)[0]["type"], "tool.execution")

    def test_module_emit_throttled_passes_transient_through(self) -> None:
        self._guard_singleton()
        BUS.enabled = True
        BUS._last_emit.clear()
        before = BUS.recent()
        seen: list[dict] = []
        BUS.subscribe(seen.append)
        emit_throttled("unit-test-transient", 60.0, "voice.level", _transient=True, rms=0.2, phase="armed")
        self.assertEqual(len(seen), 1)
        self.assertEqual(BUS.recent(), before)

    def test_module_emit_throttled_uses_the_singleton(self) -> None:
        self._guard_singleton()
        BUS.enabled = True
        BUS._last_emit.clear()
        seen: list[dict] = []
        BUS.subscribe(seen.append)
        emit_throttled("unit-test-key", 60.0, "x")
        emit_throttled("unit-test-key", 60.0, "x")
        self.assertEqual(len(seen), 1)


class TestScrub(unittest.TestCase):
    def test_scrub_redacts_before_truncating(self) -> None:
        text = "x" * 1990 + " gsk_" + "k" * 30
        out = scrub(text)
        self.assertLessEqual(len(out), 2000)
        self.assertNotIn("gsk_k", out)
        self.assertNotIn("gsk_", out.replace("[REDACTED]", ""))

    def test_secret_near_the_cut_is_still_redacted(self) -> None:
        text = "x" * 96 + "gsk_" + "k" * 30 + " tail " * 200
        out = scrub(text, max_str=100)
        self.assertEqual(len(out), 100)
        self.assertNotIn("gsk_", out)
        self.assertNotIn("kkkk", out)
        self.assertTrue(out.startswith("x" * 96))

    def test_long_quoted_secret_across_the_cut_is_fully_redacted(self) -> None:
        text = "x" * 49 + " password=" + '"' + " ".join(["hunter2word"] * 50) + '"'
        out = scrub(text, max_str=100)
        self.assertEqual(out, safe_display(redact(clean_text(text)))[:100])
        self.assertNotIn("hunter2word", out)

    def test_scrub_is_fast_on_long_hyphenated_strings(self) -> None:
        # redact() used to backtrack quadratically on "a-a-a-..."; publish scrubs on the caller's
        # thread, so even the biggest payload string must finish quickly.
        for text in ("a-" * 4256, "a-" * 50000, "a_" * 50000):
            started = time.perf_counter()
            out = scrub(text, max_str=events.PUBLISH_MAX_STR)
            elapsed = time.perf_counter() - started
            self.assertEqual(len(out), events.PUBLISH_MAX_STR)
            self.assertLess(elapsed, 1.0)

    def test_scrub_strips_control_codes(self) -> None:
        self.assertEqual(scrub("a\x1b[31mb\x07"), "a[31mb")

    def test_scrub_caps_depth_and_items(self) -> None:
        deep = {"a": {"b": {"c": {"d": {"e": "too deep"}}}}}
        out = scrub(deep, depth=2)
        self.assertEqual(out["a"]["b"], events.TRUNCATED)
        self.assertEqual(len(scrub(list(range(500)), max_items=10)), 10)
        self.assertEqual(len(scrub({str(i): i for i in range(500)}, max_items=10)), 10)

    def test_scrub_scalars_and_odd_types(self) -> None:
        self.assertEqual(scrub([1, 2.5, True, None, "s"]), [1, 2.5, True, None, "s"])
        self.assertEqual(scrub((1, 2)), [1, 2])
        self.assertIsNone(scrub(float("nan")))
        self.assertIsInstance(scrub(object()), str)
        self.assertEqual(scrub({1: "a"}), {"1": "a"})

    def test_scrub_redacts_dict_keys_and_values(self) -> None:
        out = scrub({"password": "hunter2", "note": "token=abc123"})
        self.assertNotIn("hunter2", str(out))
        self.assertNotIn("abc123", str(out))

    def test_cut_inside_redacted_marker_stays_within_the_protocol_limit(self) -> None:
        """A first cut at 500 that lands inside [REDACTED] ("password=[RED") used to be expanded again
        by publish's own scrub to 506 characters, and the browser dropped the request.start."""
        said = "x" * 486 + " password=hunter2 and more"
        first = scrub(said, max_str=500)
        self.assertLessEqual(len(first), 500)
        self.assertFalse(first.endswith("[RED"))
        self.assertNotIn("hunter2", first)
        event = EventBus().publish("request.start", source="terminal", text=first)
        self.assertLessEqual(len(event["text"]), 500)
        self.assertEqual(validate_event(event), [])

    def test_partial_marker_standing_for_a_secret_value_is_removed_after_a_cut(self) -> None:
        marker = "[REDACTED]"
        for keep in range(1, len(marker)):
            with self.subTest(keep=keep):
                head = "x" * (40 - keep) + " password="
                out = scrub(head + "hunter2 and more", max_str=len(head) + keep)
                self.assertEqual(out, head)
                self.assertEqual(scrub(out, max_str=PUBLISH_MAX_STR), out)
        whole = scrub("x" * 30 + " password=hunter2 and more", max_str=50)
        self.assertEqual(whole, "x" * 30 + " password=" + marker)  # a complete marker is kept

    def test_partial_marker_that_cannot_grow_again_is_left_alone(self) -> None:
        # Not after a credential name, so a second scrub leaves "[RED" as it is; nothing to remove.
        out = scrub("x" * 46 + "gsk_" + "k" * 30 + " tail", max_str=50)
        self.assertEqual(out, "x" * 46 + "[RED")
        self.assertEqual(scrub(out, max_str=50), out)

    def test_scrub_is_idempotent_and_never_grows_past_the_first_cut(self) -> None:
        secrets = [
            "password=hunter2", "password = hunter2 more", 'token: "a b c"', "api_key=abc",
            "gsk_" + "k" * 30, "my pin is 1234", "Bearer " + "t" * 20, "x-auth-token=zz",
            "sk-" + "s" * 20, '"api_key": "v"', "secret=a", "pin code is 12",
        ]
        for max_str in (20, 64, 500):
            for secret in secrets:
                for offset in range(-len(secret) - 12, 13):
                    prefix = max(0, max_str + offset)
                    text = "x" * prefix + " " + secret + " and more text after it"
                    with self.subTest(max_str=max_str, secret=secret, offset=offset):
                        first = scrub(text, max_str=max_str)
                        self.assertLessEqual(len(first), max_str)
                        self.assertLessEqual(len(scrub(first, max_str=PUBLISH_MAX_STR)), max_str)
                        self.assertEqual(scrub(first, max_str=max_str), first)

    def test_scrub_is_idempotent_when_control_codes_split_a_secret_name(self) -> None:
        """Removing control codes can join "pass\x01word" into a credential name; the second scrub
        (publish) must not find a new secret in the first one's output."""
        for text in ("pass\x01word=hunter2", "my\x01password=hunter2", "password\x01=hunter2",
                     "token\x1b: abc"):
            with self.subTest(text=text):
                first = scrub(text)
                self.assertEqual(scrub(first), first)
                self.assertNotIn("hunter2", first)

    def test_format_characters_cannot_split_a_secret_name(self) -> None:
        # Zero-width and other format characters draw nothing, so "pass\u200bword" reads as
        # "password" in the page: they are removed before redaction, which then sees the name.
        for text in ("pass\u200bword=x", "pass\u200bword=hunter2", "api\u200d_key=hunter2",
                     "pass\u00adword=hunter2", "pass\u2060word=hunter2", "to\ufeffken: hunter2",
                     "pass\u202eword=hunter2", "pass\U000e0041word=hunter2"):
            with self.subTest(text=text):
                out = scrub(text)
                self.assertNotIn("hunter2", out)
                self.assertTrue(out.endswith("=[REDACTED]") or out.endswith(": [REDACTED]"), out)
                self.assertEqual(scrub(out), out)
        self.assertEqual(scrub("pass\u200bword=x"), "password=[REDACTED]")

    def test_emoji_joiners_are_kept(self) -> None:
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        self.assertEqual(scrub(f"hi {family}!"), f"hi {family}!")
        self.assertEqual(scrub("a\u200db"), "ab")

    def test_any_value_under_a_secret_name_is_redacted(self) -> None:
        for value in (["hunter2"], 12345678, {"v": "hunter2"}, ("a", "b"), 3.5, True, None, "hunter2"):
            with self.subTest(value=value):
                self.assertEqual(scrub({"password": value}), {"password": "[REDACTED]"})
        self.assertEqual(scrub({"api_key": {"v": "hunter2"}, "count": 3}), {"api_key": "[REDACTED]", "count": 3})
        self.assertEqual(scrub({"tokens": [1, 2], "keys": ["a"]}), {"tokens": [1, 2], "keys": ["a"]})

    def test_scrub_survives_cycles(self) -> None:
        loop: list = []
        loop.append(loop)
        self.assertIsNotNone(scrub(loop))


if __name__ == "__main__":
    unittest.main()
