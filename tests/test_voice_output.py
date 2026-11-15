"""Spoken output: text cleanup, the voice engine and the listening chime."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import sys
import threading
import time
import unittest
import warnings
import wave
from pathlib import Path
from unittest import mock

import jarvis  # noqa: E402
from core.events import BUS  # noqa: E402
from helpers import BusCaptureMixin  # noqa: E402
from voice import output  # noqa: E402


class SpeechTextTests(unittest.TestCase):
    def test_clean_text_for_speech(self):
        text = (
            "# Heading\n"
            "Here is **bold** and _italic_ text with `inline`.\n"
            "```python\nprint('secret code')\n```\n"
            '{"status": "success", "result": 1}\n'
            "[1, 2, 3]\n"
            "- bullet one\n"
            "Visit [the docs](https://docs.example.com/page) or https://example.com/raw?x=1 now.\n"
        )
        spoken = jarvis.clean_text_for_speech(text)
        for forbidden in ("```", "print(", "**", "#", "http", "{", "}", "[", "docs.example.com", "status"):
            self.assertNotIn(forbidden, spoken)
        self.assertIn("I've put the code on screen", spoken)
        self.assertIn("bold", spoken)
        self.assertIn("the docs", spoken)
        self.assertIn("the link on screen", spoken)
        self.assertIn("bullet one", spoken)
        self.assertTrue(spoken.endswith("."))

    def test_clean_text_empty(self):
        self.assertEqual(jarvis.clean_text_for_speech(""), "")
        self.assertEqual(jarvis.clean_text_for_speech("```\nonly code\n```").strip(), "I've put the code on screen.")

    def test_split_into_chunks_respects_max_chars(self):
        text = ("This is a sentence. " * 40) + ("averyveryverylongwordwithoutanyspaces" * 10) + " end."
        for max_chars in (20, 50, 120, 350):
            with self.subTest(max_chars=max_chars):
                chunks = jarvis.split_into_chunks(text, max_chars=max_chars)
                self.assertTrue(chunks)
                for chunk in chunks:
                    self.assertLessEqual(len(chunk), max_chars)
                    self.assertTrue(chunk.strip())
                self.assertEqual("".join(chunks).replace(" ", ""), text.replace(" ", ""))

    def test_split_short_text_single_chunk(self):
        self.assertEqual(jarvis.split_into_chunks("Hello. World."), ["Hello. World."])
        self.assertEqual(jarvis.split_into_chunks(""), [])


class VoiceEngineTests(unittest.TestCase):
    def setUp(self):
        self.voices: list[jarvis.VoiceEngine] = []
        self.notifications: list[str] = []

    def tearDown(self):
        for voice in self.voices:
            voice.shutdown()

    def make_voice(self, **kwargs) -> jarvis.VoiceEngine:
        kwargs.setdefault("player", ["fake-player"])
        kwargs.setdefault("notify", self.notifications.append)
        voice = jarvis.VoiceEngine(**kwargs)
        self.voices.append(voice)
        return voice

    def test_disabled_speaks_nothing(self):
        synth_calls = []
        voice = self.make_voice(enabled=False, synthesize=lambda *a: synth_calls.append(a))
        with mock.patch("asyncio.create_subprocess_exec") as spawn:
            self.assertIsNone(voice.say("Hello, Sir."))
            voice.wait(timeout=0.5)
        self.assertFalse(voice.enabled)
        self.assertEqual(synth_calls, [])
        spawn.assert_not_called()
        self.assertIsNone(voice._loop)

    def test_empty_text_not_spoken(self):
        voice = self.make_voice(enabled=True, synthesize=lambda *a: None)
        self.assertTrue(voice.enabled)
        self.assertIsNone(voice.say("   "))

    def test_synthesis_failure_does_not_raise_and_notifies(self):
        def failing_synth(text, voice_name, path):
            raise RuntimeError("tts service down")

        voice = self.make_voice(enabled=True, synthesize=failing_synth)
        played = []

        async def fake_play(path):
            played.append(path)

        voice._play = fake_play
        future = voice.say("Hello there.")
        self.assertIsNotNone(future)
        self.assertIsNone(future.result(timeout=5))  # no exception propagates
        self.assertEqual(played, [])
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("Voice output failed", self.notifications[0])
        self.assertIn("RuntimeError", self.notifications[0])

    def test_repeated_failures_disable_voice(self):
        voice = self.make_voice(enabled=True, synthesize=lambda *a: (_ for _ in ()).throw(OSError("offline")))
        for _ in range(jarvis.VoiceEngine.MAX_CONSECUTIVE_FAILURES):
            voice.say("Hello.").result(timeout=5)
        self.assertFalse(voice.enabled)
        self.assertIn("text-only", self.notifications[-1])
        self.assertIn("no network connection", self.notifications[0])

    def test_set_enabled_without_player_reports_unavailable(self):
        with mock.patch("jarvis.shutil.which", return_value=None):
            voice = self.make_voice(enabled=False, player=None, synthesize=lambda *a: None)
            self.assertIsNone(voice.player)
            self.assertFalse(voice.set_enabled(True))
        self.assertFalse(voice.enabled)
        self.assertIn("No audio player", voice.unavailable_reason)
        self.assertIsNone(voice.say("hello"))

    def test_set_enabled_false_stops(self):
        voice = self.make_voice(enabled=True, synthesize=lambda *a: None)
        self.assertTrue(voice.enabled)
        self.assertFalse(voice.set_enabled(False))
        self.assertFalse(voice.enabled)

    def test_real_play_uses_player_argv_without_shell(self):
        voice = self.make_voice(enabled=True, player=["fake-player", "-q"], synthesize=lambda *a: None)

        class FakeProcess:
            returncode = 0

            async def wait(self):
                return 0

        spawned = []

        async def fake_exec(*args, **kwargs):
            spawned.append((args, kwargs))
            return FakeProcess()

        with mock.patch("jarvis.asyncio.create_subprocess_exec", side_effect=fake_exec):
            voice.say("Hello there.").result(timeout=5)
        self.assertEqual(len(spawned), 1)
        args, kwargs = spawned[0]
        self.assertEqual(args[:2], ("fake-player", "-q"))
        self.assertTrue(args[2].endswith(".mp3"))
        self.assertEqual(self.notifications, [])

    def test_new_utterance_cancels_previous_and_never_overlaps(self):
        synthesized: list[str] = []

        async def synth(text, voice_name, path):
            synthesized.append(text)
            await asyncio.sleep(0.01)

        voice = self.make_voice(enabled=True, synthesize=synth)
        state = {"active": 0, "max_active": 0}
        started: list[str] = []
        finished: list[str] = []
        cancelled: list[str] = []
        first_playing = threading.Event()
        current_text = {}

        async def fake_play(path):
            text = current_text.get(path.name, "?")
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            started.append(text)
            try:
                if text.startswith("First"):
                    first_playing.set()
                    await asyncio.sleep(5)  # long utterance, will be interrupted
                else:
                    await asyncio.sleep(0.05)
                finished.append(text)
            except asyncio.CancelledError:
                cancelled.append(text)
                raise
            finally:
                state["active"] -= 1

        original_synth = voice._synthesize

        async def tracking_synth(text, path):
            current_text[path.name] = text
            await original_synth(text, path)

        voice._synthesize = tracking_synth
        voice._play = fake_play

        first = voice.say("First utterance that is long.")
        self.assertTrue(first_playing.wait(timeout=5))
        second = voice.say("Second utterance.")
        self.assertIsNone(second.result(timeout=5))
        self.assertTrue(first.cancelled())
        self.assertEqual(cancelled, ["First utterance that is long."])
        self.assertEqual(finished, ["Second utterance."])
        self.assertEqual(started, ["First utterance that is long.", "Second utterance."])
        self.assertEqual(state["max_active"], 1)

    def test_rapid_say_calls_never_play_concurrently(self):
        voice = self.make_voice(enabled=True, synthesize=lambda *a: None)
        state = {"active": 0, "max_active": 0, "plays": 0}

        async def fake_play(path):
            state["active"] += 1
            state["plays"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                await asyncio.sleep(0.02)
            finally:
                state["active"] -= 1

        voice._play = fake_play
        futures = [voice.say(f"Message number {i}.") for i in range(10)]
        futures[-1].result(timeout=5)
        self.assertTrue(all(f.done() for f in futures[:-1]))
        self.assertEqual(state["max_active"], 1)
        self.assertGreaterEqual(state["plays"], 1)

    def test_shutdown_removes_temp_dir(self):
        voice = self.make_voice(enabled=True, synthesize=lambda *a: None)
        voice._play = mock.AsyncMock()
        voice.say("Hello.").result(timeout=5)
        tmpdir = voice._tmpdir
        voice.shutdown()
        self.assertFalse(tmpdir.exists())


class VoiceEngineHandsFreeTests(unittest.TestCase):
    def setUp(self):
        self.voices: list[jarvis.VoiceEngine] = []

    def tearDown(self):
        for voice in self.voices:
            voice.shutdown()

    def make_voice(self, **kwargs) -> jarvis.VoiceEngine:
        kwargs.setdefault("player", ["fake-player"])
        kwargs.setdefault("notify", lambda message: None)
        voice = jarvis.VoiceEngine(**kwargs)
        self.voices.append(voice)
        return voice

    def endless_voice(self):
        voice = self.make_voice(enabled=True, synthesize=lambda *a: None)
        playing, cancelled = threading.Event(), threading.Event()

        async def endless_play(path):
            playing.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        voice._play = endless_play
        return voice, playing, cancelled

    def test_is_speaking_during_utterance_and_not_after_stop(self):
        voice, playing, cancelled = self.endless_voice()
        self.assertFalse(voice.is_speaking)
        future = voice.say("A long announcement, Sir.")
        self.assertTrue(playing.wait(2))
        self.assertTrue(voice.is_speaking)
        voice.stop()
        self.assertFalse(voice.is_speaking)
        self.assertTrue(future.cancelled())
        self.assertTrue(cancelled.wait(2), "the playback task should be cancelled")

    def test_stop_from_another_thread(self):
        voice, playing, cancelled = self.endless_voice()
        future = voice.say("Another long announcement.")
        self.assertTrue(playing.wait(2))
        stopper = threading.Thread(target=voice.stop)
        stopper.start()
        stopper.join(timeout=2)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(voice.is_speaking)
        self.assertTrue(future.cancelled())
        self.assertTrue(cancelled.wait(2))
        voice.stop()  # stopping again is harmless

    def test_chime_without_player_does_nothing(self):
        with mock.patch("jarvis.shutil.which", return_value=None):
            voice = self.make_voice(enabled=False, player=None)
        self.assertIsNone(voice.player)
        voice._play = mock.Mock(side_effect=AssertionError("must not play"))
        voice.chime()
        self.assertIsNone(voice._loop)
        self.assertIsNone(voice._chime_path)

    def test_chime_plays_a_wav_even_in_text_only_mode(self):
        voice = self.make_voice(enabled=False)
        played: list[Path] = []
        done = threading.Event()

        async def fake_play(path):
            played.append(path)
            self.assertTrue(path.exists())
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getframerate(), 16000)
            done.set()

        voice._play = fake_play
        voice.chime()
        self.assertTrue(done.wait(2))
        self.assertEqual(len(played), 1)
        self.assertEqual(played[0].suffix, ".wav")
        self.assertEqual(played[0].parent, voice._tmpdir)
        done.clear()
        voice.chime()  # the chime file is written once and reused
        self.assertTrue(done.wait(2))
        self.assertEqual(played[1], played[0])
        self.assertFalse(voice.is_speaking)

    def test_chime_failure_is_swallowed(self):
        voice = self.make_voice(enabled=False)
        finished = threading.Event()

        async def failing_play(path):
            finished.set()
            raise RuntimeError("player missing")

        voice._play = failing_play
        voice.chime()  # must not raise, now or from the background future
        self.assertTrue(finished.wait(2))

    def test_write_chime_is_valid_16k_mono_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chime.wav"
            jarvis.write_chime(path)
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getframerate(), 16000)
                duration = wav.getnframes() / wav.getframerate()
            self.assertAlmostEqual(duration, 0.18, delta=0.01)
            seconds, peak = jarvis.wav_stats(path)
            self.assertAlmostEqual(seconds, duration)
            self.assertGreater(peak, 0.1)
            self.assertLess(peak, 0.3)  # a soft cue, not a blast


class ShutdownCancelsPendingTasksTests(unittest.TestCase):
    def make_voice(self):
        voice = jarvis.VoiceEngine(enabled=False, player=["fake-player"], notify=lambda message: None)
        self.addCleanup(voice.shutdown)
        self.started, self.finished = threading.Event(), threading.Event()

        async def slow_play(path):
            self.started.set()
            try:
                await asyncio.sleep(30)
            finally:
                self.finished.set()

        voice._play = slow_play
        return voice

    def shutdown_and_collect(self, voice) -> list[str]:
        """Shut down, collect garbage, and return any 'never awaited' warnings (asyncio must log no errors)."""
        with warnings.catch_warnings(record=True) as caught, self.assertNoLogs("asyncio", level="ERROR"):
            warnings.simplefilter("always")
            began = time.monotonic()
            voice.shutdown()
            self.assertLess(time.monotonic() - began, 2.5)
            gc.collect()
        return [str(w.message) for w in caught if "never awaited" in str(w.message)]

    def test_chime_scheduled_just_before_shutdown_leaves_no_unawaited_coroutine(self):
        voice = self.make_voice()
        voice.chime()
        self.assertEqual(self.shutdown_and_collect(voice), [])
        self.assertIsNone(voice._loop)
        if self.started.is_set():
            self.assertTrue(self.finished.is_set(), "a chime that started must be cancelled and unwound")

    def test_playing_chime_is_cancelled_by_shutdown(self):
        voice = self.make_voice()
        voice.chime()
        self.assertTrue(self.started.wait(2))
        self.assertEqual(self.shutdown_and_collect(voice), [])
        self.assertTrue(self.finished.is_set())

    def test_cancel_pending_tasks_cancels_all_but_the_current_task(self):
        async def main():
            sleeper = asyncio.ensure_future(asyncio.sleep(30))
            await asyncio.sleep(0)
            await output._cancel_pending_tasks()
            return sleeper

        loop = asyncio.new_event_loop()
        try:
            sleeper = loop.run_until_complete(main())
        finally:
            loop.close()
        self.assertTrue(sleeper.cancelled())


class VoiceOutputEventTests(BusCaptureMixin, unittest.TestCase):
    def setUp(self):
        self.voices: list[jarvis.VoiceEngine] = []

    def tearDown(self):
        for voice in self.voices:
            voice.shutdown()

    def make_voice(self, **kwargs) -> jarvis.VoiceEngine:
        kwargs.setdefault("player", ["fake-player"])
        kwargs.setdefault("notify", lambda message: None)
        voice = jarvis.VoiceEngine(**kwargs)
        self.voices.append(voice)
        return voice

    @staticmethod
    def phases(events):
        return [e["phase"] for e in events if e["type"] == "voice.output"]

    def test_voice_output_start_end_events(self):
        events = self.capture_events()
        voice = self.make_voice(enabled=True, synthesize=lambda text, name, path: None)

        async def fake_play(path):
            pass

        voice._play = fake_play
        voice.say("First sentence. Second sentence.").result(timeout=5)
        outputs = [e for e in events if e["type"] == "voice.output"]
        # one start and one end per spoken reply (not per chunk); each played part is announced as a chunk
        self.assertEqual([e["phase"] for e in outputs], ["start", "chunk", "end"])
        self.assertTrue(all(e["timing"] == "none" for e in outputs))
        self.assertIsInstance(outputs[-1]["duration_ms"], int)
        self.assert_valid(events)

    def test_voice_output_error_event_when_synthesis_fails(self):
        events = self.capture_events()

        def failing(text, name, path):
            raise RuntimeError("tts service down")

        voice = self.make_voice(enabled=True, synthesize=failing)
        voice.say("Hello there.").result(timeout=5)
        self.assertEqual(self.phases(events), ["start", "error"])
        self.assert_valid(events)

    def test_interrupted_speech_still_ends(self):
        events = self.capture_events()
        playing = threading.Event()
        voice = self.make_voice(enabled=True, synthesize=lambda text, name, path: None)

        async def endless_play(path):
            playing.set()
            await asyncio.sleep(30)

        voice._play = endless_play
        voice.say("Hello there.")
        self.assertTrue(playing.wait(5))
        voice.stop()
        deadline = time.monotonic() + 5
        while "end" not in self.phases(events) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.phases(events), ["start", "chunk", "end"])

    def test_no_events_when_bus_disabled(self):
        events = self.capture_events()
        BUS.enabled = False
        voice = self.make_voice(enabled=True, synthesize=lambda text, name, path: None)

        async def fake_play(path):
            pass

        voice._play = fake_play
        voice.say("Hello there.").result(timeout=5)
        self.assertEqual(events, [])

    def test_override_synthesis_sends_a_chunk_without_timings(self):
        events = self.capture_events()
        voice = self.make_voice(enabled=True, synthesize=lambda text, name, path: None)

        async def fake_play(path):
            pass

        voice._play = fake_play
        voice.say("Hello there.").result(timeout=5)
        chunks = [e for e in events if e["type"] == "voice.output" and e["phase"] == "chunk"]
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["index"], 0)
        self.assertEqual(chunks[0]["timing"], "none")
        self.assertEqual(chunks[0]["words"], [])
        self.assertNotIn("duration_ms", chunks[0])
        self.assert_valid(events)


class FakeCommunicate:
    """Stands in for edge_tts.Communicate: records how it was built and what was called on it.

    `stream()` yields what the real service sends: audio frames and, when asked for word boundaries,
    WordBoundary items whose offset and duration are in 100 ns ticks."""

    instances: list["FakeCommunicate"] = []
    AUDIO = (b"ID3-frame-one|", b"frame-two|", b"frame-three")
    # (offset, duration) in ticks: 100 ms + 250 ms, 400 ms + 300 ms.
    BOUNDARIES = ((1_000_000, 2_500_000), (4_000_000, 3_000_000))

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.saved_to: str | None = None
        self.streamed = False
        FakeCommunicate.instances.append(self)

    async def stream(self):
        self.streamed = True
        yield {"type": "audio", "data": self.AUDIO[0]}
        yield {"type": "WordBoundary", "offset": self.BOUNDARIES[0][0], "duration": self.BOUNDARIES[0][1],
               "text": "Hello"}
        yield {"type": "audio", "data": self.AUDIO[1]}
        yield {"type": "WordBoundary", "offset": self.BOUNDARIES[1][0], "duration": self.BOUNDARIES[1][1],
               "text": "there"}
        yield {"type": "audio", "data": self.AUDIO[2]}

    async def save(self, path):
        self.saved_to = path
        Path(path).write_bytes(b"saved-audio")


class WordBoundaryTests(BusCaptureMixin, unittest.TestCase):
    """The real edge-tts path, with a fake edge_tts module: word timings only while Mission Control runs."""

    def setUp(self):
        FakeCommunicate.instances = []
        fake_module = mock.MagicMock()
        fake_module.Communicate = FakeCommunicate
        patcher = mock.patch.dict("sys.modules", {"edge_tts": fake_module})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.voices: list[jarvis.VoiceEngine] = []

    def tearDown(self):
        for voice in self.voices:
            voice.shutdown()

    def make_voice(self) -> jarvis.VoiceEngine:
        voice = jarvis.VoiceEngine(enabled=True, player=["fake-player"], notify=lambda message: None)
        self.voices.append(voice)
        return voice

    def test_word_boundaries_streamed_when_bus_enabled(self):
        events = self.capture_events()
        voice = self.make_voice()
        played: list[tuple[bytes, list[str]]] = []

        async def fake_play(path):
            played.append((Path(path).read_bytes(), [e["phase"] for e in events if e["type"] == "voice.output"]))

        voice._play = fake_play
        voice.say("Hello there.").result(timeout=5)

        self.assertEqual(len(FakeCommunicate.instances), 1)
        communicate = FakeCommunicate.instances[0]
        self.assertEqual(communicate.args, ("Hello there.", voice.voice))
        self.assertEqual(communicate.kwargs, {"boundary": "WordBoundary"})
        self.assertTrue(communicate.streamed)
        self.assertIsNone(communicate.saved_to)
        audio = b"".join(FakeCommunicate.AUDIO)
        self.assertEqual(played, [(audio, ["start", "chunk"])])  # the chunk is announced right before playing

        outputs = [e for e in events if e["type"] == "voice.output"]
        self.assertEqual([e["phase"] for e in outputs], ["start", "chunk", "end"])
        chunk = outputs[1]
        self.assertEqual(chunk["index"], 0)
        self.assertEqual(chunk["timing"], "exact")
        self.assertEqual(chunk["words"], [[100, 250], [400, 300]])  # 100 ns ticks -> ms
        self.assertEqual(chunk["duration_ms"], round(len(audio) * 8 / output.EDGE_TTS_BITRATE_BPS * 1000))
        self.assert_valid(events)

    def test_synthesize_returns_word_timings_in_ms(self):
        self.capture_events()
        voice = self.make_voice()
        path = voice._tmpdir / "direct.mp3"
        words = asyncio.run(voice._synthesize("Hello there.", path))
        self.assertEqual(words, [(100, 250), (400, 300)])
        self.assertEqual(path.read_bytes(), b"".join(FakeCommunicate.AUDIO))

    def speak_with_stream(self, items: list[dict]) -> tuple[list[dict], list[bytes]]:
        """Speak "Hello there." with a stream that yields exactly `items`; return the events and played bytes."""

        class ScriptedCommunicate(FakeCommunicate):
            async def stream(self):
                self.streamed = True
                for item in items:
                    yield item

        sys.modules["edge_tts"].Communicate = ScriptedCommunicate
        events = self.capture_events()
        voice = self.make_voice()
        played: list[bytes] = []

        async def fake_play(path):
            played.append(Path(path).read_bytes())

        voice._play = fake_play
        voice.say("Hello there.").result(timeout=5)
        return events, played

    def test_malformed_word_boundary_is_skipped_and_speech_still_plays(self):
        events, played = self.speak_with_stream([
            {"type": "audio", "data": b"one|"},
            {"type": "WordBoundary", "offset": 1_000_000, "duration": 2_500_000, "text": "Hello"},
            {"type": "WordBoundary", "offset": "soon", "duration": 2_000_000, "text": "bad offset"},
            {"type": "WordBoundary", "offset": 3_000_000, "text": "no duration"},
            {"type": "WordBoundary", "offset": None, "duration": True, "text": "not numbers"},
            {"type": "WordBoundary", "offset": float("nan"), "duration": 1_000_000, "text": "nan"},
            {"type": "audio", "data": b"two"},
            {"type": "WordBoundary", "offset": 4_000_000, "duration": 3_000_000, "text": "there"},
        ])
        self.assertEqual(played, [b"one|two"])
        outputs = [e for e in events if e["type"] == "voice.output"]
        self.assertEqual([e["phase"] for e in outputs], ["start", "chunk", "end"])
        self.assertEqual(outputs[1]["timing"], "exact")
        self.assertEqual(outputs[1]["words"], [[100, 250], [400, 300]])
        self.assert_valid(events)

    def test_huge_integer_word_boundary_is_skipped_and_speech_still_plays(self):
        events, played = self.speak_with_stream([
            {"type": "audio", "data": b"one|"},
            {"type": "WordBoundary", "offset": 10 ** 400, "duration": 2_000_000, "text": "huge offset"},
            {"type": "WordBoundary", "offset": 1_000_000, "duration": -(10 ** 400), "text": "huge negative"},
            {"type": "WordBoundary", "offset": 1_000_000, "duration": 2_500_000, "text": "Hello"},
            {"type": "audio", "data": b"two"},
        ])
        self.assertEqual(played, [b"one|two"])
        chunks = [e for e in events if e["type"] == "voice.output" and e["phase"] == "chunk"]
        self.assertEqual([c["words"] for c in chunks], [[[100, 250]]])
        self.assert_valid(events)

    def test_audio_without_word_boundaries_sends_a_chunk_without_timings(self):
        events, played = self.speak_with_stream([
            {"type": "audio", "data": b"only-audio"},
        ])
        self.assertEqual(played, [b"only-audio"])
        chunks = [e for e in events if e["type"] == "voice.output" and e["phase"] == "chunk"]
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["timing"], "none")
        self.assertEqual(chunks[0]["words"], [])
        self.assert_valid(events)

    def test_save_path_unchanged_when_bus_disabled(self):
        events = self.capture_events()
        BUS.enabled = False
        voice = self.make_voice()
        played: list[bytes] = []

        async def fake_play(path):
            played.append(Path(path).read_bytes())

        voice._play = fake_play
        voice.say("Hello there.").result(timeout=5)

        self.assertEqual(len(FakeCommunicate.instances), 1)
        communicate = FakeCommunicate.instances[0]
        self.assertEqual(communicate.args, ("Hello there.", voice.voice))
        self.assertEqual(communicate.kwargs, {})  # exactly Communicate(text, voice), as before
        self.assertFalse(communicate.streamed)
        self.assertIsNotNone(communicate.saved_to)
        self.assertEqual(played, [b"saved-audio"])
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
