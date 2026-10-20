"""Spoken output: text cleanup, the voice engine and the listening chime."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

import jarvis  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
