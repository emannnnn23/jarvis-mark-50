"""Hands-free mode: wake word, voice interruption and their CLI integration."""

from __future__ import annotations

import array
import io
import json
import os
import shutil
import signal
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    AgentTestCase,
    FAKE_KEY,
    FakeDetector,
    FakeGroqClient,
    FakeMicRecorder,
    FakeWakeListener,
    PromptFakeTranscriber,
    TempDirTestCase,
    close_logging_handlers,
    const_frame,
    fake_openwakeword_modules,
    loud,
    pcm_tone,
    printed_text,
    quiet,
)
import jarvis  # noqa: E402


class WakeConfigTests(unittest.TestCase):
    def env(self, **extra):
        values = {"GROQ_API_KEY": FAKE_KEY}
        values.update(extra)
        return values

    def test_defaults(self):
        for source, config in (("env", jarvis.Config.from_env(env=self.env())), ("bare", jarvis.Config(api_key=FAKE_KEY))):
            with self.subTest(source=source):
                self.assertEqual((config.wake_enabled, config.wake_model, config.wake_threshold,
                                  config.wake_speaking_threshold, config.wake_end_silence, config.wake_chime),
                                 (False, "hey_jarvis", 0.6, 0.75, 1.0, True))

    def test_env_overrides(self):
        config = jarvis.Config.from_env(env=self.env(
            JARVIS_WAKE_ENABLED="on", JARVIS_WAKE_MODEL=" alexa ", JARVIS_WAKE_THRESHOLD=" 0.5 ",
            JARVIS_WAKE_SPEAKING_THRESHOLD="0.9", JARVIS_WAKE_END_SILENCE="2.5", JARVIS_WAKE_CHIME="off"))
        self.assertTrue(config.wake_enabled)
        self.assertEqual(config.wake_model, "alexa")
        self.assertEqual(config.wake_threshold, 0.5)
        self.assertEqual(config.wake_speaking_threshold, 0.9)
        self.assertEqual(config.wake_end_silence, 2.5)
        self.assertFalse(config.wake_chime)

    def test_invalid_floats_rejected(self):
        for name, value in (("JARVIS_WAKE_THRESHOLD", "high"), ("JARVIS_WAKE_THRESHOLD", "0.05"),
                            ("JARVIS_WAKE_THRESHOLD", "1.5"), ("JARVIS_WAKE_THRESHOLD", "nan"),
                            ("JARVIS_WAKE_SPEAKING_THRESHOLD", "loud"), ("JARVIS_WAKE_SPEAKING_THRESHOLD", "1"),
                            ("JARVIS_WAKE_END_SILENCE", "0.2"), ("JARVIS_WAKE_END_SILENCE", "4")):
            with self.subTest(name=name, value=value):
                with self.assertRaises(jarvis.ConfigError) as ctx:
                    jarvis.Config.from_env(env=self.env(**{name: value}))
                self.assertIn(name, str(ctx.exception))

    def test_env_float_helper(self):
        self.assertEqual(jarvis._env_float({}, "X", 0.6, 0.1, 0.99), 0.6)
        self.assertEqual(jarvis._env_float({"X": "  "}, "X", 0.6, 0.1, 0.99), 0.6)
        self.assertEqual(jarvis._env_float({"X": "0.1"}, "X", 0.6, 0.1, 0.99), 0.1)
        self.assertEqual(jarvis._env_float({"X": "0.99"}, "X", 0.6, 0.1, 0.99), 0.99)
        with self.assertRaises(jarvis.ConfigError) as ctx:
            jarvis._env_float({"X": "abc"}, "X", 0.6, 0.1, 0.99)
        self.assertIn("must be a number", str(ctx.exception))
        with self.assertRaises(jarvis.ConfigError) as ctx:
            jarvis._env_float({"X": "1.5"}, "X", 0.6, 0.1, 0.99)
        self.assertIn("between 0.1 and 0.99", str(ctx.exception))

    def test_parse_args_wake_flag(self):
        self.assertTrue(jarvis.parse_args(["--wake"]).wake)
        self.assertFalse(jarvis.parse_args([]).wake)


class WakePhraseTests(unittest.TestCase):
    def test_strip_wake_phrase(self):
        for text, expected in (("Hey Jarvis, what time is it?", "what time is it?"),
                               ("Hello Jarvis open YouTube", "open YouTube"),
                               ("ok jarvis, check the network", "check the network"),
                               ("Jervis. Launch VS Code.", "Launch VS Code."),
                               ("JARVIS: system status", "system status"),
                               ("  what time is it?  ", "what time is it?"),
                               ("...a light breeze. Hey Jarvis, stop.", "stop."),
                               ("Hey Jarvis, tell me about Jarvis from Iron Man", "tell me about Jarvis from Iron Man"),
                               ("Hey Jarvis", "")):
            with self.subTest(text=text):
                self.assertEqual(jarvis.strip_wake_phrase(text), expected)

    def test_words_containing_jarvis_are_not_wake_phrases(self):
        self.assertEqual(jarvis.strip_wake_phrase("Jarvisson wrote this"), "Jarvisson wrote this")

    def test_is_stop_phrase(self):
        for text in ("stop.", "Never mind!", "that's enough", "STOP", "  cancel  ", "Be quiet."):
            with self.subTest(text=text):
                self.assertTrue(jarvis.is_stop_phrase(text))
        for text in ("stop the music in five minutes", "what time is it?", ""):
            with self.subTest(text=text):
                self.assertFalse(jarvis.is_stop_phrase(text))
        self.assertIn("stop", jarvis.STOP_PHRASES)

    def test_frame_rms(self):
        self.assertEqual(jarvis.frame_rms(quiet()), 0.0)
        self.assertEqual(jarvis.frame_rms(b""), 0.0)
        self.assertEqual(jarvis.frame_rms(b"\x01"), 0.0)  # a lone odd byte is not a sample
        square = array.array("h", [32767, -32768] * (jarvis.FRAME_SAMPLES // 2))
        if sys.byteorder == "big":
            square.byteswap()
        self.assertAlmostEqual(jarvis.frame_rms(square.tobytes()), 1.0, places=4)
        self.assertAlmostEqual(jarvis.frame_rms(loud()), 10000 / 32768, places=6)

    def test_frame_constants(self):
        self.assertEqual(jarvis.FRAME_SAMPLES, 1280)
        self.assertAlmostEqual(jarvis.FRAME_SECONDS, 0.08)
        self.assertEqual(jarvis.FRAME_BYTES, 2560)
        self.assertEqual(len(quiet()), jarvis.FRAME_BYTES)


class UtteranceCollectorTests(unittest.TestCase):
    def feed_all(self, collector, frames):
        outcomes = [collector.feed(frame) for frame in frames]
        return outcomes

    def test_done_after_speech_then_end_silence(self):
        collector = jarvis.UtteranceCollector(0.0, end_silence=0.2, start_timeout=5, max_seconds=20)
        outcomes = self.feed_all(collector, [loud(1), loud(2), quiet(1), quiet(2), quiet(3)])
        self.assertEqual(outcomes, [None, None, None, None, "done"])  # 0.24 s of quiet >= 0.2 s
        self.assertTrue(collector.heard_speech)
        self.assertEqual(collector.audio(), b"".join([loud(1), loud(2), quiet(1), quiet(2), quiet(3)]))

    def test_speech_resets_the_silence_timer(self):
        collector = jarvis.UtteranceCollector(0.0, end_silence=0.2)
        outcomes = self.feed_all(collector, [loud(), quiet(), quiet(), loud(), quiet(), quiet(), quiet()])
        self.assertEqual(outcomes, [None] * 6 + ["done"])

    def test_no_speech_after_start_timeout(self):
        collector = jarvis.UtteranceCollector(0.0, end_silence=0.2, start_timeout=0.3, max_seconds=20)
        outcomes = self.feed_all(collector, [quiet()] * 4)
        self.assertEqual(outcomes, [None, None, None, "no_speech"])
        self.assertFalse(collector.heard_speech)

    def test_max_seconds(self):
        speaking = jarvis.UtteranceCollector(0.0, end_silence=1.0, start_timeout=5, max_seconds=0.3)
        self.assertEqual(self.feed_all(speaking, [loud()] * 4), [None, None, None, "done"])
        silent = jarvis.UtteranceCollector(0.0, end_silence=1.0, start_timeout=5, max_seconds=0.3)
        self.assertEqual(self.feed_all(silent, [quiet()] * 4), [None, None, None, "no_speech"])

    def test_pre_roll_is_kept_but_is_not_speech(self):
        pre_roll = loud(7) * 3
        collector = jarvis.UtteranceCollector(0.0, pre_roll=pre_roll, start_timeout=0.15)
        self.assertFalse(collector.heard_speech)
        self.assertEqual(collector.audio(), pre_roll)
        self.assertIsNone(collector.feed(quiet()))
        self.assertEqual(collector.feed(quiet()), "no_speech")  # loud pre-roll did not count
        self.assertTrue(collector.audio().startswith(pre_roll))
        self.assertEqual(collector.audio(), pre_roll + quiet() + quiet())

    def test_speech_threshold_tracks_noise(self):
        self.assertEqual(jarvis.UtteranceCollector(0.0).threshold, jarvis.MIN_SPEECH_RMS)
        self.assertEqual(jarvis.UtteranceCollector(0.001).threshold, jarvis.MIN_SPEECH_RMS)
        self.assertAlmostEqual(jarvis.UtteranceCollector(0.02).threshold, 0.06)
        noisy = jarvis.UtteranceCollector(0.02, start_timeout=0.15)
        medium = const_frame(int(0.05 * 32768))  # louder than the 0.01 floor, quieter than 3 x noise
        self.assertIsNone(noisy.feed(medium))
        self.assertFalse(noisy.heard_speech)
        self.assertEqual(noisy.feed(medium), "no_speech")


class WakeWordListenerTests(TempDirTestCase):
    def make_listener(self, frames, detector=None, **kwargs) -> jarvis.WakeWordListener:
        self.events: list[tuple] = []
        self.detector = detector or FakeDetector()
        kwargs.setdefault("pre_roll_seconds", 0.24)  # 3 frames
        kwargs.setdefault("end_silence", 0.2)  # done on the 3rd quiet frame
        kwargs.setdefault("cooldown_seconds", 0.24)  # 3 frames
        kwargs.setdefault("start_timeout", 0.3)
        kwargs.setdefault("max_seconds", 5.0)
        kwargs.setdefault("stream_factory", lambda: io.BytesIO(b"".join(frames)))
        listener = jarvis.WakeWordListener(
            self.detector, on_wake=lambda: self.events.append(("wake", self.detector.calls)),
            on_command=lambda pcm: self.events.append(("command", pcm)),
            on_status=lambda message: self.events.append(("status", message)), **kwargs)
        self.addCleanup(listener.stop)
        return listener

    def run_to_end(self, listener) -> None:
        listener.start()
        thread = listener._thread
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "the listener should stop at the end of the stream")
        self.assertFalse(listener.running)

    def kinds(self) -> list[str]:
        return [event[0] for event in self.events]

    def statuses(self) -> list[str]:
        return [event[1] for event in self.events if event[0] == "status"]

    def test_wake_then_command_flow(self):
        wake = loud(99)
        frames = [quiet(1), quiet(2), quiet(3), quiet(4), wake, loud(1), loud(2), quiet(5), quiet(6), quiet(7),
                  quiet(8)]
        listener = self.make_listener(frames, FakeDetector({4: 0.9}))
        self.assertEqual(listener.pre_roll_frames, 3)
        self.run_to_end(listener)
        self.assertEqual(self.kinds(), ["wake", "command", "status"])
        pcm = self.events[1][1]
        pre_roll = quiet(3) + quiet(4) + wake
        self.assertTrue(pcm.startswith(pre_roll), "the command starts with the pre-roll frames")
        self.assertIn(loud(1) + loud(2), pcm)
        self.assertEqual(pcm, pre_roll + loud(1) + loud(2) + quiet(5) + quiet(6) + quiet(7))
        self.assertEqual(self.detector.calls, 6)  # command frames are not scored
        self.assertEqual(self.detector.resets, 2)  # after the detection and after the command

    def test_no_command_after_wake_reports_status(self):
        listener = self.make_listener([loud(), quiet(), quiet(), quiet(), quiet()], FakeDetector({0: 0.9}))
        self.run_to_end(listener)
        self.assertEqual(self.kinds(), ["wake", "status", "status"])
        self.assertIn("didn't hear a command", self.statuses()[0])
        self.assertIn("stream ended", self.statuses()[1])

    def test_cooldown_suppresses_immediate_retrigger(self):
        frames = [loud(99), loud(1), quiet(1), quiet(2), quiet(3),  # wake + command
                  loud(2), loud(3), loud(4), loud(5)]
        listener = self.make_listener(frames, FakeDetector({0: 0.9, 1: 0.95, 2: 0.95, 3: 0.95, 4: 0.95}))
        self.run_to_end(listener)
        wakes = [event for event in self.events if event[0] == "wake"]
        # Score calls 2-4 fall inside the 3-frame cooldown; the 5th score call triggers again.
        self.assertEqual(wakes, [("wake", 1), ("wake", 5)])
        self.assertEqual(self.kinds().count("command"), 1)

    def test_speaking_threshold_applies_while_speaking(self):
        for speaking, score, expected in ((False, 0.7, True), (True, 0.7, False), (True, 0.8, True)):
            with self.subTest(speaking=speaking, score=score):
                listener = self.make_listener([quiet(), loud()], FakeDetector({1: score}),
                                              is_speaking=lambda speaking=speaking: speaking,
                                              threshold=0.6, speaking_threshold=0.75)
                self.run_to_end(listener)
                self.assertEqual("wake" in self.kinds(), expected)

    def test_paused_listener_detects_nothing(self):
        listener = self.make_listener([loud()] * 5, FakeDetector({i: 0.99 for i in range(5)}))
        listener.paused = True
        self.run_to_end(listener)
        self.assertEqual(self.detector.calls, 0)
        self.assertNotIn("wake", self.kinds())
        self.assertEqual(len(self.statuses()), 1)
        self.assertIn("stream ended", self.statuses()[0])

    def test_stream_end_message_only_when_not_stopping(self):
        listener = self.make_listener([quiet()] * 3)
        self.run_to_end(listener)
        self.assertEqual(len(self.statuses()), 1)
        self.assertIn("Hands-free listening stopped: the microphone stream ended", self.statuses()[0])
        self.assertIn("/wake on", self.statuses()[0])

        class BlockingStream:
            """A microphone read that returns (end of stream) only once stop() has been called."""

            def __init__(self):
                self.reading = threading.Event()
                self.listener = None

            def read(self, n):
                self.reading.set()
                self.listener._stopping.wait(2)
                return b""

        stream = BlockingStream()
        stopping = self.make_listener([], stream_factory=lambda: stream)
        stream.listener = stopping
        stopping.start()
        self.assertTrue(stream.reading.wait(2))
        self.assertTrue(stopping.running)
        stopping.stop()
        self.assertFalse(stopping.running)
        self.assertEqual(self.events, [])

    def test_detector_exception_stops_listening_with_status(self):
        listener = self.make_listener([quiet()] * 5, FakeDetector(error_at=2, error=RuntimeError("model crashed")))
        with mock.patch.object(jarvis.log, "exception") as logged:
            self.run_to_end(listener)
        logged.assert_called_once()
        self.assertEqual(self.statuses(), ["Hands-free listening stopped: model crashed"])
        self.assertEqual(self.detector.calls, 3)

    def test_stop_is_idempotent(self):
        listener = self.make_listener([quiet()] * 3)
        listener.stop()  # before start
        self.run_to_end(listener)
        listener.stop()
        listener.stop()
        self.assertFalse(listener.running)
        self.assertIsNone(listener._thread)
        self.assertIsNone(listener._stream)

    def test_no_recorder_mentions_alsa_utils(self):
        listener = jarvis.WakeWordListener(FakeDetector(), lambda: None, lambda pcm: None, recorder_argv=[])
        self.addCleanup(listener.stop)
        with self.assertRaises(jarvis.SpeechInputError) as ctx:
            listener.start()
        self.assertIn("alsa-utils", str(ctx.exception))
        self.assertFalse(listener.running)

    def test_missing_recorder_executable(self):
        listener = jarvis.WakeWordListener(FakeDetector(), lambda: None, lambda pcm: None,
                                           recorder_argv=["jarvis-no-such-recorder-xyz", "-q"])
        self.addCleanup(listener.stop)
        with mock.patch("jarvis.subprocess.Popen") as popen:
            with self.assertRaises(jarvis.SpeechInputError) as ctx:
                listener.start()
        popen.assert_not_called()
        self.assertIn("not installed", str(ctx.exception))
        self.assertFalse(listener.running)

    def test_wake_word_audit_event_has_score_and_no_audio(self):
        jarvis.setup_logging(self.config)
        self.addCleanup(close_logging_handlers)
        listener = self.make_listener([quiet(), loud(5), loud(6), quiet(), quiet(), quiet()],
                                      FakeDetector({1: 0.9}))
        self.run_to_end(listener)
        self.assertIn("command", self.kinds())
        for handler in jarvis.audit_log.handlers:
            handler.flush()
        audit_text = (self.log_dir / "audit.log").read_text(encoding="utf-8")
        entries = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
        wake_entries = [e for e in entries if e.get("event") == "wake_word"]
        self.assertEqual(len(wake_entries), 1)
        entry = wake_entries[0]
        self.assertEqual(entry["model"], "fake_model")
        self.assertEqual(entry["score"], 0.9)
        self.assertEqual(set(entry), {"timestamp", "event", "model", "score"})
        self.assertLess(len(audit_text), 500, "no audio may be written to the audit log")


class OpenWakeWordDetectorTests(TempDirTestCase):
    def test_missing_engine_gives_install_hint(self):
        with mock.patch.dict(sys.modules, {"openwakeword": None}):
            with self.assertRaises(jarvis.SpeechInputError) as ctx:
                jarvis.OpenWakeWordDetector()
        self.assertIn("not installed", str(ctx.exception))
        self.assertIn(jarvis.WAKE_INSTALL_HINT, str(ctx.exception))
        self.assertIn("pip install", str(ctx.exception))

    def test_unknown_model_lists_builtin_models(self):
        with mock.patch.dict(sys.modules, fake_openwakeword_modules()):
            with self.assertRaises(jarvis.SpeechInputError) as ctx:
                jarvis.OpenWakeWordDetector("hey_tony")
        message = str(ctx.exception)
        self.assertIn("hey_tony", message)
        self.assertIn("alexa, hey_jarvis, timer", message)
        self.assertIn(".onnx", message)

    def test_missing_onnx_file_raises(self):
        modules = fake_openwakeword_modules()
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaises(jarvis.SpeechInputError) as ctx:
                jarvis.OpenWakeWordDetector(str(self.tmp / "missing.onnx"))
        self.assertIn("not found", str(ctx.exception))
        modules["openwakeword.model"].Model.assert_not_called()

    def test_download_and_load_failures_are_speech_input_errors(self):
        modules = fake_openwakeword_modules(download_error=OSError("offline"))
        with mock.patch.dict(sys.modules, modules), mock.patch("builtins.print"):
            with self.assertRaises(jarvis.SpeechInputError) as ctx:
                jarvis.OpenWakeWordDetector("hey_jarvis")
        self.assertIn("Could not download the wake-word model: offline", str(ctx.exception))
        modules["openwakeword.utils"].download_models.assert_called_once_with(model_names=["hey_jarvis"])

        custom = self.tmp / "my_wake.onnx"
        custom.write_bytes(b"not really a model")
        modules = fake_openwakeword_modules(model_error=ValueError("bad onnx"))
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaises(jarvis.SpeechInputError) as ctx:
                jarvis.OpenWakeWordDetector(str(custom))
        self.assertIn("Could not load the wake-word model: bad onnx", str(ctx.exception))
        modules["openwakeword.model"].Model.assert_called_once_with(wakeword_models=[str(custom)],
                                                                    inference_framework="onnx")

    def test_wake_engine_available_reports_missing_module(self):
        with mock.patch.dict(sys.modules, {"openwakeword": None}):
            ok, detail = jarvis.wake_engine_available()
        self.assertFalse(ok)
        self.assertIn("openwakeword is not installed", detail)
        self.assertIn(jarvis.WAKE_INSTALL_HINT, detail)


@unittest.skipUnless(jarvis.wake_engine_available()[0], "openWakeWord is not installed")
class RealOpenWakeWordDetectorTests(unittest.TestCase):
    """The real hey_jarvis model on synthetic audio only (at most two tests use it)."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.detector = jarvis.OpenWakeWordDetector("hey_jarvis")
        except jarvis.SpeechInputError as exc:  # e.g. the model is not downloaded
            raise unittest.SkipTest(str(exc))

    def frames(self, pcm: bytes) -> list[bytes]:
        return [pcm[i:i + jarvis.FRAME_BYTES] for i in range(0, len(pcm), jarvis.FRAME_BYTES)]

    def test_sine_and_silence_do_not_trigger(self):
        self.assertEqual(self.detector.name, "hey_jarvis")
        for label, pcm in (("silence", bytes(jarvis.FRAME_BYTES * 25)), ("sine", pcm_tone(2.0, 0.3, 440.0))):
            with self.subTest(audio=label):
                self.detector.reset()
                scores = [self.detector.score(frame) for frame in self.frames(pcm)]
                self.assertEqual(len(scores), 25)
                for score in scores:
                    self.assertIsInstance(score, float)
                    self.assertGreaterEqual(score, 0.0)
                    self.assertLessEqual(score, 1.0)
                self.assertLess(max(scores), 0.5)

    def test_reset_clears_state_and_scoring_continues(self):
        for frame in self.frames(pcm_tone(1.0, 0.3, 880.0)):
            self.detector.score(frame)
        self.assertIsNone(self.detector.reset())
        score = self.detector.score(bytes(jarvis.FRAME_BYTES))
        self.assertIsInstance(score, float)
        self.assertTrue(0.0 <= score <= 1.0)


class HandsFreeCLITests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.voice = jarvis.VoiceEngine(enabled=False, player=["fake-player"])
        self.addCleanup(self.voice.shutdown)
        self.listener = FakeWakeListener()

    def make_speech(self, text="Hey Jarvis, what time is it?", error=None):
        speech = jarvis.SpeechInput(PromptFakeTranscriber(text, error), recorder=FakeMicRecorder(),
                                    wait_for_stop=lambda: None, notify=lambda message: None)
        self.addCleanup(speech.shutdown)
        return speech

    def make_cli(self, speech="default", factory="default", wake=False, config=None):
        if speech == "default":
            speech = self.make_speech()
        if factory == "default":
            def factory(owner):
                return self.listener
        ctx = jarvis.AgentContext(FakeGroqClient(), config or self.config, self.registry, self.policy, self.memory)
        cli = jarvis.JarvisCLI(ctx, self.voice, self.memory, speech=speech, wake_factory=factory, wake=wake)
        self.addCleanup(cli.stop_wake)
        return cli

    def hands_free_cli(self):
        cli = self.make_cli()
        with mock.patch("builtins.print"):
            self.assertTrue(cli.start_wake())
        return cli

    @staticmethod
    def drain(cli) -> list[tuple]:
        items = []
        while not cli._wake_events.empty():
            items.append(cli._wake_events.get_nowait())
        return items

    # -- start / stop ------------------------------------------------------------
    def test_start_wake_prints_privacy_message(self):
        cli = self.make_cli()
        self.assertFalse(cli.hands_free)
        self.assertFalse(cli._speech_disclosed)
        with mock.patch("builtins.print") as printed:
            self.assertTrue(cli.start_wake())
        self.assertIn("Nothing leaves this machine", printed_text(printed))
        self.assertTrue(cli._speech_disclosed)
        self.assertTrue(cli.hands_free)
        self.assertEqual(self.listener.start_calls, 1)
        with mock.patch("builtins.print"):
            self.assertTrue(cli.start_wake())  # already on: no second listener
        self.assertEqual(self.listener.start_calls, 1)

    def test_start_wake_failure_leaves_hands_free_off(self):
        for label, factory in (
                ("factory raises", mock.Mock(side_effect=jarvis.SpeechInputError("No microphone recorder found."))),
                ("start raises", lambda owner: FakeWakeListener(jarvis.SpeechInputError("mic busy")))):
            with self.subTest(label):
                cli = self.make_cli(factory=factory)
                with mock.patch("builtins.print") as printed:
                    self.assertFalse(cli.start_wake())
                self.assertIn("Hands-free unavailable", printed_text(printed))
                self.assertFalse(cli.hands_free)
                self.assertIsNone(cli.wake)
                self.assertFalse(cli._speech_disclosed)

    def test_start_wake_without_speech_or_factory_is_unavailable(self):
        factory = mock.Mock()
        cli = self.make_cli(speech=None, factory=factory)
        with mock.patch("builtins.print") as printed:
            self.assertFalse(cli.start_wake())
        self.assertIn("Hands-free unavailable: Speech input is not configured.", printed_text(printed))
        factory.assert_not_called()
        self.assertFalse(cli.hands_free)
        cli = self.make_cli(factory=None)
        with mock.patch("builtins.print") as printed:
            self.assertFalse(cli.start_wake())
        self.assertIn("not configured", printed_text(printed))

    def test_stop_wake(self):
        cli = self.hands_free_cli()
        cli.stop_wake()
        self.assertEqual(self.listener.stop_calls, 1)
        self.assertIsNone(cli.wake)
        self.assertFalse(cli.hands_free)
        cli.stop_wake()  # no listener: nothing to do
        self.assertEqual(self.listener.stop_calls, 1)

    # -- wake callbacks -------------------------------------------------------------
    def test_on_wake_stops_voice_queues_status_and_chimes(self):
        cli = self.make_cli()
        with mock.patch.object(self.voice, "stop") as stop, mock.patch.object(self.voice, "chime") as chime, \
                mock.patch("jarvis.os.kill") as kill:
            cli._on_wake()
        stop.assert_called_once_with()
        chime.assert_called_once_with()
        kill.assert_not_called()
        self.assertEqual(self.drain(cli), [("status", "[wake] Listening...")])
        self.assertFalse(cli._consume_interrupt())

    def test_on_wake_without_chime_setting(self):
        cli = self.make_cli(config=self.make_config(wake_chime=False))
        with mock.patch.object(self.voice, "chime") as chime, mock.patch("jarvis.os.kill"):
            cli._on_wake()
        chime.assert_not_called()

    def test_on_wake_while_speaking_reports_interruption_without_signal(self):
        cli = self.make_cli()
        with mock.patch.object(jarvis.VoiceEngine, "is_speaking", new_callable=mock.PropertyMock,
                               return_value=True), \
                mock.patch.object(self.voice, "stop") as stop, mock.patch.object(self.voice, "chime"), \
                mock.patch("jarvis.os.kill") as kill:
            cli._on_wake()
        stop.assert_called_once_with()
        kill.assert_not_called()
        self.assertEqual(self.drain(cli), [("status", "[wake] Interrupted. Listening...")])
        self.assertFalse(cli._consume_interrupt())

    def test_on_wake_while_busy_sends_sigint(self):
        cli = self.make_cli()
        cli._busy = True
        with mock.patch.object(self.voice, "chime"), mock.patch("jarvis.os.kill") as kill:
            cli._on_wake()
        kill.assert_called_once_with(os.getpid(), signal.SIGINT)
        self.assertTrue(cli._interrupt_pending)
        self.assertEqual(self.drain(cli), [("status", "[wake] Interrupted. Listening...")])

    def test_consume_interrupt_returns_true_once(self):
        cli = self.make_cli()
        self.assertFalse(cli._consume_interrupt())
        cli._interrupt_pending = True
        self.assertTrue(cli._consume_interrupt())
        self.assertFalse(cli._consume_interrupt())

    def test_on_command_and_on_status_are_queued(self):
        cli = self.make_cli()
        cli._on_command(b"pcm")
        cli._on_status("Hands-free listening stopped: test")
        self.assertEqual(self.drain(cli), [("command", b"pcm"), ("status", "Hands-free listening stopped: test")])

    # -- spoken commands ------------------------------------------------------------
    def voice_command(self, cli, pcm):
        with mock.patch("builtins.print") as printed:
            result = cli._voice_command(pcm)
        return result, printed_text(printed)

    def test_voice_command_returns_transcript(self):
        cli = self.make_cli(speech=self.make_speech("Hey Jarvis, what time is it?"))
        text, output = self.voice_command(cli, pcm_tone(1.0))
        self.assertEqual(text, "what time is it?")
        self.assertIn("You (voice): what time is it?", output)

    def test_voice_command_stop_phrase(self):
        cli = self.make_cli(speech=self.make_speech("Hey JARVIS, stop."))
        text, output = self.voice_command(cli, pcm_tone(1.0))
        self.assertEqual(text, "")
        self.assertIn("Stopped.", output)

    def test_voice_command_nothing_to_send(self):
        cases = (("empty transcript", self.make_speech("Hey Jarvis"), pcm_tone(1.0), "didn't catch a request"),
                 ("silence", self.make_speech(), bytes(32000), "didn't hear a command"),
                 ("api error", self.make_speech(error=jarvis.JarvisAPIError("Cannot reach Groq.", "network", True)),
                  pcm_tone(1.0), "Transcription failed: Cannot reach Groq."))
        for label, speech, pcm, expected in cases:
            with self.subTest(label):
                cli = self.make_cli(speech=speech)
                text, output = self.voice_command(cli, pcm)
                self.assertEqual(text, "")
                self.assertIn(expected, output)

    def test_voice_command_removes_control_characters(self):
        cli = self.make_cli(speech=self.make_speech("Hey Jarvis, hello\x1b[31m there\x07"))
        text, _ = self.voice_command(cli, pcm_tone(1.0))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)

    # -- reading input in hands-free mode -------------------------------------------
    def test_read_line_returns_queued_command(self):
        cli = self.hands_free_cli()
        cli._on_command(pcm_tone(1.0))
        with mock.patch("jarvis.select.select", return_value=([], [], [])) as selected, \
                mock.patch("builtins.input", side_effect=AssertionError("input() would block")), \
                mock.patch("builtins.print"):
            self.assertEqual(cli._read_line(), "what time is it?")
        selected.assert_not_called()

    def test_read_line_prints_status_and_keeps_waiting(self):
        cli = self.hands_free_cli()
        polls = []

        def fake_select(readers, writers, errors, timeout):
            polls.append(timeout)
            if len(polls) == 1:  # the listener hears a command while we wait
                cli._on_status("[wake] Listening...")
                cli._on_command(pcm_tone(1.0))
            return [], [], []

        with mock.patch("jarvis.select.select", side_effect=fake_select), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cli._read_line(), "what time is it?")
        self.assertEqual(polls, [0.1])
        output = printed_text(printed)
        self.assertIn("[wake] Listening...", output)
        self.assertLess(output.index("[wake] Listening..."), output.index("You (voice): what time is it?"))

    def test_read_line_returns_typed_input(self):
        cli = self.hands_free_cli()
        stdin = io.StringIO("  open youtube \n")
        with mock.patch("sys.stdin", stdin), \
                mock.patch("jarvis.select.select", return_value=([stdin], [], [])) as selected, \
                mock.patch("builtins.print"):
            self.assertEqual(cli._read_line(), "open youtube")
        self.assertEqual(selected.call_args.args[0], [stdin])

    def test_read_line_eof_raises(self):
        cli = self.hands_free_cli()
        stdin = io.StringIO("")
        with mock.patch("sys.stdin", stdin), mock.patch("jarvis.select.select", return_value=([stdin], [], [])), \
                mock.patch("builtins.print"):
            with self.assertRaises(EOFError):
                cli._read_line()

    def test_read_line_returns_none_when_listener_stops(self):
        # None (not "") so that a dead listener can never be mistaken for an empty typed line,
        # which would start a push-to-talk recording.
        cli = self.hands_free_cli()

        def listener_dies(*args):
            self.listener.is_running = False
            return [], [], []

        with mock.patch("jarvis.select.select", side_effect=listener_dies) as selected, \
                mock.patch("builtins.print"):
            self.assertIsNone(cli._read_line())
        self.assertEqual(selected.call_count, 1)

    def test_read_line_without_hands_free_uses_input(self):
        cli = self.make_cli()
        with mock.patch("builtins.input", return_value="  hello  ") as typed, \
                mock.patch("jarvis.select.select") as selected:
            self.assertEqual(cli._read_line(), "hello")
        typed.assert_called_once()
        selected.assert_not_called()

    def test_real_listener_command_reaches_read_line(self):
        frames = [quiet(1), loud(99)] + [loud(i) for i in range(5)] + [quiet(2), quiet(3), quiet(4)]

        def factory(owner):
            return jarvis.WakeWordListener(
                FakeDetector({1: 0.9}), on_wake=owner._on_wake, on_command=owner._on_command,
                on_status=owner._on_status, pre_roll_seconds=0.24, end_silence=0.2,
                stream_factory=lambda: io.BytesIO(b"".join(frames)))

        cli = self.make_cli(factory=factory)
        with mock.patch.object(self.voice, "chime") as chime, mock.patch("jarvis.os.kill") as kill, \
                mock.patch("builtins.print"):
            self.assertTrue(cli.start_wake())
            thread = cli.wake._thread
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            with mock.patch("jarvis.select.select", return_value=([], [], [])):
                self.assertEqual(cli._read_line(), "what time is it?")
        chime.assert_called_once_with()
        kill.assert_not_called()
        self.assertEqual(self.drain(cli)[0][0], "status")  # "stream ended" is still queued

    # -- the session loop -------------------------------------------------------------
    def run_loop(self, cli, inputs, handle_side_effect=None):
        with mock.patch.object(cli, "handle_message", side_effect=handle_side_effect) as handle, \
                mock.patch("builtins.input", side_effect=inputs) as typed, \
                mock.patch("builtins.print") as printed, \
                mock.patch("jarvis.os.kill") as kill:
            code = cli.loop()
        return code, handle, typed, printed_text(printed), kill

    def test_wake_interrupt_escaping_handle_message_does_not_end_session(self):
        cli = self.make_cli()

        def interrupted(text):
            self.assertTrue(cli._busy)
            cli._on_wake()  # the listener thread hears "Hey JARVIS" mid-request ...
            raise KeyboardInterrupt  # ... and the SIGINT it sends lands here

        with mock.patch.object(self.voice, "chime"):
            code, handle, typed, output, kill = self.run_loop(cli, ["what is the weather", "exit"], interrupted)
        self.assertEqual(code, 0)
        kill.assert_called_once_with(os.getpid(), signal.SIGINT)
        handle.assert_called_once_with("what is the weather")
        self.assertEqual(typed.call_count, 2)
        self.assertNotIn("Cancelled", output)
        self.assertIn("Until next time", output)
        self.assertFalse(cli._busy)
        self.assertFalse(cli._consume_interrupt())

    def test_ctrl_c_at_prompt_without_pending_interrupt_exits(self):
        cli = self.make_cli()
        code, handle, typed, output, _ = self.run_loop(cli, [KeyboardInterrupt()])
        self.assertEqual(code, 0)
        self.assertEqual(typed.call_count, 1)
        handle.assert_not_called()
        self.assertIn("Until next time", output)

    def test_interrupt_landing_at_prompt_is_absorbed(self):
        cli = self.make_cli()
        cli._interrupt_pending = True  # the wake word was heard just as the request finished
        code, _, typed, output, _ = self.run_loop(cli, [KeyboardInterrupt(), "exit"])
        self.assertEqual(code, 0)
        self.assertEqual(typed.call_count, 2)
        self.assertNotIn("Cancelled", output)

    def test_genuine_ctrl_c_during_request_still_prints_cancelled(self):
        cli = self.make_cli()
        code, _, typed, output, kill = self.run_loop(cli, ["hello", "exit"], KeyboardInterrupt())
        self.assertEqual(code, 0)
        self.assertIn("Cancelled", output)
        kill.assert_not_called()

    def test_wake_interrupt_caught_inside_handle_message_clears_pending_flag(self):
        """Usually the wake word's SIGINT lands inside run_agent, and handle_message catches it.
        The pending flag must not outlive that request, or the user's next real Ctrl+C at the
        prompt is mistaken for a wake-word interruption and does not exit."""
        cli = self.make_cli()

        def interrupted_agent(*args, **kwargs):
            cli._on_wake()
            raise KeyboardInterrupt

        with mock.patch.object(self.voice, "chime"), \
                mock.patch("ui.terminal.run_agent", side_effect=interrupted_agent), \
                mock.patch("builtins.input", side_effect=["what is the weather", KeyboardInterrupt(), "exit"]) as typed, \
                mock.patch("builtins.print"), mock.patch("jarvis.os.kill") as kill:
            code = cli.loop()
        self.assertEqual(code, 0)
        kill.assert_called_once_with(os.getpid(), signal.SIGINT)
        self.assertEqual(typed.call_count, 2,
                         "a real Ctrl+C at the prompt after a wake-word interruption should end the session")

    def test_run_message_marks_busy_only_during_handle_message(self):
        cli = self.make_cli()
        seen = []
        with mock.patch.object(cli, "handle_message", side_effect=lambda text: seen.append(cli._busy)):
            self.assertFalse(cli._busy)
            cli._run_message("hello")
        self.assertEqual(seen, [True])
        self.assertFalse(cli._busy)
        with mock.patch.object(cli, "handle_message", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cli._run_message("hello")
        self.assertFalse(cli._busy)
        with mock.patch.object(cli, "handle_message", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                cli._run_message("hello")
        self.assertFalse(cli._busy)

    def test_commands_are_not_marked_busy(self):
        cli = self.make_cli()
        seen = []
        with mock.patch.object(cli, "handle_command", side_effect=lambda line: seen.append(cli._busy)), \
                mock.patch("builtins.input", side_effect=["/status"]), mock.patch("builtins.print"):
            self.assertFalse(cli._step())
        self.assertEqual(seen, [False])

    def test_voice_turn_pauses_the_wake_listener(self):
        cli = self.hands_free_cli()
        speech = cli.speech
        seen = []
        with mock.patch.object(speech, "listen", side_effect=lambda: seen.append(self.listener.paused) or "hello"), \
                mock.patch("builtins.print"):
            self.assertEqual(cli.voice_turn(), "hello")
        self.assertEqual(seen, [True])
        self.assertFalse(self.listener.paused)
        for error in (jarvis.SpeechInputError("too short"), RuntimeError("driver exploded")):
            with self.subTest(error=type(error).__name__):
                seen.clear()

                def failing_listen():
                    seen.append(self.listener.paused)
                    raise error

                with mock.patch.object(speech, "listen", side_effect=failing_listen), \
                        mock.patch("builtins.print"), mock.patch.object(jarvis.log, "exception"):
                    self.assertEqual(cli.voice_turn(), "")
                self.assertEqual(seen, [True])
                self.assertFalse(self.listener.paused)

    def test_wake_command(self):
        cli = self.make_cli()
        with mock.patch("builtins.print") as printed:
            cli.handle_command("/wake")
            cli.handle_command("/wake on")
            self.assertTrue(cli.hands_free)
            cli.handle_command("/wake status")
            cli.handle_command("/wake off")
            self.assertFalse(cli.hands_free)
            cli.handle_command("/wake")
        lines = [" ".join(str(a) for a in call.args) for call in printed.call_args_list]
        status_lines = [line for line in lines if "Hands-free is" in line]
        self.assertEqual(len(status_lines), 3)
        self.assertIn("Hands-free is off", status_lines[0])
        self.assertIn("Hands-free is on", status_lines[1])
        self.assertIn("Hands-free is off", status_lines[2])
        self.assertIn("Use /wake on or /wake off", status_lines[0])
        self.assertTrue(any("Nothing leaves this machine" in line for line in lines))
        self.assertTrue(any("the microphone is closed" in line for line in lines))
        self.assertEqual((self.listener.start_calls, self.listener.stop_calls), (1, 1))

    def test_loop_stops_wake_on_exit(self):
        cli = self.hands_free_cli()
        stdin = io.StringIO("exit\n")
        with mock.patch("sys.stdin", stdin), mock.patch("jarvis.select.select", return_value=([stdin], [], [])), \
                mock.patch("builtins.print"):
            self.assertEqual(cli.loop(), 0)
        self.assertEqual(self.listener.stop_calls, 1)
        self.assertIsNone(cli.wake)

    def test_loop_stops_wake_when_it_crashes(self):
        cli = self.hands_free_cli()
        with mock.patch.object(cli, "_step", side_effect=SystemExit(3)), mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                cli.loop()
        self.assertEqual(self.listener.stop_calls, 1)

    def test_banner_starts_hands_free_when_requested(self):
        cli = self.make_cli(wake=True)
        self.assertEqual(self.listener.start_calls, 0)  # not before the banner
        with mock.patch("builtins.print") as printed:
            cli.banner()
            cli.banner()
        self.assertTrue(cli.hands_free)
        self.assertEqual(self.listener.start_calls, 1)
        self.assertIn("Hands-free on", printed_text(printed))
        idle = self.make_cli(wake=False)
        with mock.patch("builtins.print"):
            idle.banner()
        self.assertIsNone(idle.wake)

    def test_status_shows_hands_free(self):
        cli = self.make_cli()
        with mock.patch("builtins.print") as printed:
            cli.handle_command("/status")
            cli.start_wake()
            cli.handle_command("/status")
        lines = [line for line in printed_text(printed).splitlines() if line.lstrip().startswith("Hands-free  ")]
        self.assertEqual(len(lines), 2)
        self.assertIn("off (hey_jarvis, threshold 0.6)", lines[0])
        self.assertIn("on (hey_jarvis, threshold 0.6)", lines[1])

    def test_help_mentions_wake(self):
        self.assertIn("/wake", jarvis.HELP_TEXT)
        self.assertIn("Hey JARVIS, stop", jarvis.HELP_TEXT)


class WakeChecksTests(TempDirTestCase):
    def run_checks(self, available):
        with mock.patch("jarvis.wake_engine_available", return_value=available), \
                mock.patch("jarvis.initialize_client", side_effect=AssertionError("no network allowed")), \
                mock.patch("builtins.print") as printed:
            jarvis.run_checks(self.make_config(api_key="not-a-key", wake_model="alexa"))
        return [line for line in printed_text(printed).splitlines() if "wake-word engine" in line]

    def test_engine_present_is_ok(self):
        lines = self.run_checks((True, "openWakeWord"))
        self.assertEqual(len(lines), 1)
        self.assertIn("[ok  ] wake-word engine: openWakeWord (alexa)", lines[0])

    def test_engine_missing_is_a_warning(self):
        lines = self.run_checks((False, f"openwakeword is not installed. {jarvis.WAKE_INSTALL_HINT}"))
        self.assertEqual(len(lines), 1)
        self.assertIn("[warn] wake-word engine", lines[0])
        self.assertIn("pip install", lines[0])
        self.assertNotIn("(alexa)", lines[0])


class MainWakeWiringTests(TempDirTestCase):
    def test_main_builds_the_wake_listener_from_config(self):
        captured = {}

        class CapturingCLI:
            def __init__(self, ctx, voice, memory, **kwargs):
                captured.update(kwargs, ctx=ctx, voice=voice)
                self._on_wake, self._on_command, self._on_status = mock.Mock(), mock.Mock(), mock.Mock()

            def loop(self):
                captured["listener"] = captured["wake_factory"](self)
                return 0

        env = {"GROQ_API_KEY": FAKE_KEY, "JARVIS_MEMORY_PATH": str(self.memory_path),
               "JARVIS_LOG_DIR": str(self.log_dir), "JARVIS_WORKSPACE": str(self.workspace),
               "JARVIS_WAKE_THRESHOLD": "0.5", "JARVIS_WAKE_SPEAKING_THRESHOLD": "0.8",
               "JARVIS_WAKE_END_SILENCE": "1.5", "JARVIS_WAKE_MODEL": "alexa", "JARVIS_STT_MAX_SECONDS": "30"}
        detector = FakeDetector()
        with mock.patch.dict(os.environ, env), mock.patch("jarvis.load_env_file"), \
                mock.patch("jarvis.setup_logging"), mock.patch("jarvis.audit"), \
                mock.patch("jarvis.initialize_client", return_value=FakeGroqClient()), \
                mock.patch("jarvis.verify_model"), \
                mock.patch("jarvis.OpenWakeWordDetector", return_value=detector) as make_detector, \
                mock.patch("jarvis.JarvisCLI", CapturingCLI):
            self.assertEqual(jarvis.main(["--text", "--wake"]), 0)
        self.assertTrue(captured["wake"])
        make_detector.assert_called_once_with("alexa")
        listener = captured["listener"]
        self.assertIsInstance(listener, jarvis.WakeWordListener)
        self.assertIs(listener.detector, detector)
        self.assertEqual((listener.threshold, listener.speaking_threshold, listener.end_silence, listener.max_seconds),
                         (0.5, 0.8, 1.5, min(30, jarvis.WAKE_MAX_SECONDS)))
        self.assertFalse(listener.is_speaking())
        self.assertFalse(listener.running)  # built, not started

    def test_main_wake_defaults_off(self):
        captured = {}

        class CapturingCLI:
            def __init__(self, ctx, voice, memory, **kwargs):
                captured.update(kwargs)

            def loop(self):
                return 0

        env = {"GROQ_API_KEY": FAKE_KEY, "JARVIS_MEMORY_PATH": str(self.memory_path),
               "JARVIS_LOG_DIR": str(self.log_dir), "JARVIS_WAKE_ENABLED": ""}
        with mock.patch.dict(os.environ, env), mock.patch("jarvis.load_env_file"), \
                mock.patch("jarvis.setup_logging"), mock.patch("jarvis.audit"), \
                mock.patch("jarvis.initialize_client", return_value=FakeGroqClient()), \
                mock.patch("jarvis.verify_model"), mock.patch("jarvis.JarvisCLI", CapturingCLI):
            self.assertEqual(jarvis.main(["--text"]), 0)
        self.assertFalse(captured["wake"])
        self.assertTrue(callable(captured["wake_factory"]))


class HandsFreeReviewRegressionTests(AgentTestCase):
    """Regressions for the hands-free concurrency and privacy review."""

    def setUp(self):
        super().setUp()
        self.voice = jarvis.VoiceEngine(enabled=False, player=["fake-player"])
        self.addCleanup(self.voice.shutdown)
        self.listener = FakeWakeListener()

    def make_cli(self):
        speech = jarvis.SpeechInput(PromptFakeTranscriber("Hey Jarvis, what time is it?"), recorder=FakeMicRecorder(),
                                    wait_for_stop=lambda: None, notify=lambda message: None)
        self.addCleanup(speech.shutdown)
        ctx = jarvis.AgentContext(FakeGroqClient(), self.config, self.registry, self.policy, self.memory)
        cli = jarvis.JarvisCLI(ctx, self.voice, self.memory, speech=speech, wake_factory=lambda owner: self.listener)
        self.addCleanup(cli.stop_wake)
        return cli

    def hands_free_cli(self):
        cli = self.make_cli()
        with mock.patch("builtins.print"):
            self.assertTrue(cli.start_wake())
        return cli

    # Privacy: an empty hands-free turn must never open the microphone for push-to-talk.
    def test_empty_hands_free_turn_does_not_start_push_to_talk(self):
        cli = self.hands_free_cli()
        cli.listen_mode = True
        cli._wake_events.put(("command", loud() * 10))
        with mock.patch.object(cli, "_voice_command", return_value=""), \
                mock.patch.object(cli, "voice_turn") as voice_turn, \
                mock.patch("jarvis.select.select", return_value=([], [], [])), mock.patch("builtins.print"):
            self.assertFalse(cli._step())
        voice_turn.assert_not_called()

    def test_typed_empty_line_still_starts_push_to_talk(self):
        cli = self.make_cli()
        cli.listen_mode = True
        with mock.patch.object(cli, "_read_line", return_value=""), \
                mock.patch.object(cli, "voice_turn", return_value="") as voice_turn:
            self.assertFalse(cli._step())
        voice_turn.assert_called_once()

    # Privacy: nothing captured before /wake off is uploaded afterwards.
    def test_wake_off_discards_captured_commands(self):
        cli = self.hands_free_cli()
        cli._wake_events.put(("command", loud() * 10))
        with mock.patch("builtins.print"):
            cli.handle_command("/wake off")
        self.assertTrue(cli._wake_events.empty())
        with mock.patch.object(cli.speech, "transcribe_pcm") as transcribe, \
                mock.patch("builtins.input", return_value="exit"):
            self.assertEqual(cli._read_line(), "exit")
        transcribe.assert_not_called()

    # Noise floor: capped, and never learned from JARVIS's own voice.
    def run_listener(self, frames: bytes, is_speaking=lambda: False):
        done = threading.Event()
        listener = jarvis.WakeWordListener(FakeDetector(), on_wake=lambda: None, on_command=lambda pcm: None,
                                           on_status=lambda message: done.set(), is_speaking=is_speaking,
                                           stream_factory=lambda: io.BytesIO(frames))
        listener.start()
        self.assertTrue(done.wait(5))
        listener.stop()
        return listener

    def test_rising_background_cannot_ratchet_the_noise_floor_past_the_cap(self):
        ramp = b"".join(const_frame(100 + i * 7) for i in range(250))  # about 0.003 -> 0.056 RMS
        self.assertLessEqual(self.run_listener(ramp).noise_rms, jarvis.MAX_NOISE_RMS)

    def test_noise_floor_is_not_learned_while_jarvis_speaks(self):
        listener = self.run_listener(const_frame(300) * 50, is_speaking=lambda: True)
        self.assertAlmostEqual(listener.noise_rms, 0.003)

    def test_steady_background_after_speech_still_ends_the_request(self):
        collector = jarvis.UtteranceCollector(noise_rms=0.003, end_silence=0.4)
        for _ in range(5):
            self.assertIsNone(collector.feed(loud()))
        background = const_frame(1000)  # ~0.03 RMS: above the floor, far below the speaker's level
        outcomes = [collector.feed(background) for _ in range(10)]
        self.assertIn("done", outcomes)

    # Lifecycle: a crashed listener closes the microphone, and /wake on reaps it.
    @unittest.skipUnless(shutil.which("sh") and Path("/dev/zero").exists(), "needs sh and /dev/zero")
    def test_listener_crash_closes_the_microphone(self):
        done = threading.Event()
        listener = jarvis.WakeWordListener(FakeDetector(error_at=3, error=RuntimeError("onnx boom")),
                                           on_wake=lambda: None, on_command=lambda pcm: None,
                                           on_status=lambda message: done.set(),
                                           recorder_argv=["sh", "-c", "exec cat /dev/zero"])
        listener.start()
        self.addCleanup(listener.stop)
        self.assertTrue(done.wait(5))
        process = listener._process
        process.wait(timeout=3)
        self.assertIsNotNone(process.poll())

    def test_start_wake_reaps_a_dead_listener(self):
        cli = self.make_cli()
        dead = FakeWakeListener()
        cli.wake = dead  # is_running False: its thread died
        with mock.patch("builtins.print"):
            self.assertTrue(cli.start_wake())
        self.assertEqual(dead.stop_calls, 1)
        self.assertIs(cli.wake, self.listener)

    # Interruption reaches every stage of a request.
    def test_wake_at_confirmation_prompt_cancels_the_request(self):
        policy = jarvis.PermissionPolicy("standard", confirm=lambda request: False)
        policy.cancel_requested = lambda: True
        tool = self.registry.get("forget_fact")
        with self.assertRaises(KeyboardInterrupt):
            policy.evaluate(tool, {"fact_id": "f123456"})

    def test_cli_wires_cancel_requested_into_the_policy(self):
        cli = self.make_cli()
        self.assertFalse(self.policy.cancel_requested())
        cli._interrupt_pending = True
        self.assertTrue(self.policy.cancel_requested())

    def test_transcribing_a_spoken_request_is_interruptible(self):
        cli = self.hands_free_cli()
        seen = []
        cli._wake_events.put(("command", b"pcm"))
        with mock.patch.object(cli, "_voice_command", side_effect=lambda pcm: seen.append(cli._busy) or "hi"), \
                mock.patch("builtins.print"):
            self.assertEqual(cli._read_line(), "hi")
        self.assertEqual(seen, [True])
        self.assertFalse(cli._busy)

    def test_wake_during_transcription_cancels_without_exiting(self):
        cli = self.hands_free_cli()
        cli._wake_events.put(("command", b"pcm"))

        def interrupted(pcm):
            with mock.patch("jarvis.os.kill") as kill:
                cli._on_wake()
            kill.assert_called_once_with(os.getpid(), signal.SIGINT)
            raise KeyboardInterrupt

        with mock.patch.object(cli, "_voice_command", side_effect=interrupted), \
                mock.patch("jarvis.select.select", return_value=([], [], [])), mock.patch("builtins.print"):
            self.assertFalse(cli._step())  # False: the session continues
        self.assertFalse(cli._interrupt_pending)

    def test_turn_is_recorded_even_if_speaking_is_interrupted(self):
        cli = self.make_cli()
        with mock.patch("ui.terminal.run_agent", return_value=jarvis.AgentResult("Reply", "complete", [], 1)), \
                mock.patch.object(cli, "say", side_effect=KeyboardInterrupt), mock.patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                cli.handle_message("question")
        self.assertEqual(cli.conversation.turns, [("question", "Reply")])


if __name__ == "__main__":
    unittest.main()
