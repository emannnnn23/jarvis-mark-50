"""Push-to-talk speech input and transcription."""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import unittest
import warnings
import wave
from types import SimpleNamespace
from unittest import mock

from helpers import (  # noqa: E402 - sets up sys.path for the project imports
    AgentTestCase,
    FAKE_KEY,
    FakeGroqClient,
    FakeMicRecorder,
    FakeTranscriber,
    FakeTranscriptionClient,
    PromptFakeTranscriber,
    TempDirTestCase,
    close_logging_handlers,
    make_connection_error,
    make_status_error,
    pcm_tone,
    write_wav,
)
import groq  # noqa: E402
import jarvis  # noqa: E402


class SpeechConfigTests(unittest.TestCase):
    def env(self, **extra):
        values = {"GROQ_API_KEY": FAKE_KEY}
        values.update(extra)
        return values

    def test_defaults(self):
        config = jarvis.Config.from_env(env=self.env())
        self.assertFalse(config.stt_enabled)
        self.assertEqual(config.stt_backend, "groq")
        self.assertEqual(config.stt_model, "whisper-large-v3-turbo")
        self.assertEqual(config.stt_language, "en")
        self.assertEqual(config.stt_max_seconds, 60)
        bare = jarvis.Config(api_key=FAKE_KEY)
        self.assertEqual((bare.stt_enabled, bare.stt_backend, bare.stt_model, bare.stt_language, bare.stt_max_seconds),
                         (False, "groq", "whisper-large-v3-turbo", "en", 60))

    def test_env_overrides(self):
        config = jarvis.Config.from_env(env=self.env(
            JARVIS_STT_ENABLED="yes", JARVIS_STT_BACKEND=" GROQ ", JARVIS_STT_MODEL=" whisper-large-v3 ",
            JARVIS_STT_LANGUAGE=" FR ", JARVIS_STT_MAX_SECONDS="120"))
        self.assertTrue(config.stt_enabled)
        self.assertEqual(config.stt_backend, "groq")
        self.assertEqual(config.stt_model, "whisper-large-v3")
        self.assertEqual(config.stt_language, "fr")
        self.assertEqual(config.stt_max_seconds, 120)

    def test_max_seconds_boundaries_accepted(self):
        for raw, expected in (("5", 5), ("300", 300)):
            with self.subTest(raw=raw):
                self.assertEqual(jarvis.Config.from_env(env=self.env(JARVIS_STT_MAX_SECONDS=raw)).stt_max_seconds,
                                 expected)

    def test_invalid_backend_rejected(self):
        with self.assertRaises(jarvis.ConfigError) as ctx:
            jarvis.Config.from_env(env=self.env(JARVIS_STT_BACKEND="openai"))
        self.assertIn("JARVIS_STT_BACKEND", str(ctx.exception))

    def test_max_seconds_out_of_range_rejected(self):
        for raw in ("2", "301", "0", "-5", "ten"):
            with self.subTest(raw=raw):
                with self.assertRaises(jarvis.ConfigError) as ctx:
                    jarvis.Config.from_env(env=self.env(JARVIS_STT_MAX_SECONDS=raw))
                self.assertIn("JARVIS_STT_MAX_SECONDS", str(ctx.exception))

    def test_invalid_enabled_flag_rejected(self):
        with self.assertRaises(jarvis.ConfigError):
            jarvis.Config.from_env(env=self.env(JARVIS_STT_ENABLED="maybe"))

    def test_empty_language_means_auto_detect(self):
        config = jarvis.Config.from_env(env=self.env(JARVIS_STT_LANGUAGE=""))
        self.assertEqual(config.stt_language, "")
        self.assertEqual(jarvis.Config.from_env(env=self.env(JARVIS_STT_LANGUAGE="   ")).stt_language, "")

    def test_parse_args_listen_flag(self):
        self.assertTrue(jarvis.parse_args(["--listen"]).listen)
        self.assertFalse(jarvis.parse_args([]).listen)


class RecorderDetectionTests(unittest.TestCase):
    @staticmethod
    def which_for(*available):
        return lambda name: f"/usr/bin/{name}" if name in available else None

    def test_prefers_arecord(self):
        with mock.patch("jarvis.shutil.which", side_effect=self.which_for("arecord", "pw-record")):
            argv = jarvis.detect_recorder()
        self.assertEqual(argv[0], "arecord")
        self.assertIn("16000", argv)
        self.assertIn("wav", argv)

    def test_falls_back_to_pw_record(self):
        with mock.patch("jarvis.shutil.which", side_effect=self.which_for("pw-record")):
            argv = jarvis.detect_recorder()
        self.assertEqual(argv[0], "pw-record")
        self.assertIn("16000", argv)

    def test_none_when_no_recorder(self):
        with mock.patch("jarvis.shutil.which", side_effect=self.which_for()):
            self.assertIsNone(jarvis.detect_recorder())

    def test_returns_a_copy(self):
        with mock.patch("jarvis.shutil.which", side_effect=self.which_for("arecord")):
            argv = jarvis.detect_recorder()
            argv.append("/tmp/out.wav")
            self.assertNotIn("/tmp/out.wav", jarvis.detect_recorder())


class WavStatsTests(TempDirTestCase):
    def test_sine_wave_duration_and_peak(self):
        path = write_wav(self.tmp / "speech.wav", 1.0, 0.3)
        duration, peak = jarvis.wav_stats(path)
        self.assertAlmostEqual(duration, 1.0, places=3)
        self.assertAlmostEqual(peak, 0.3, delta=0.01)

    def test_silence_has_zero_peak(self):
        path = write_wav(self.tmp / "silence.wav", 0.5, 0.0)
        duration, peak = jarvis.wav_stats(path)
        self.assertAlmostEqual(duration, 0.5, places=3)
        self.assertEqual(peak, 0.0)

    def test_zero_frame_header_with_trailing_data_is_still_measured(self):
        path = self.tmp / "abrupt.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"")
        self.assertEqual(path.stat().st_size, 44)
        pcm = write_wav(self.tmp / "pcm.wav", 0.75, 0.3).read_bytes()[44:]
        with path.open("ab") as handle:
            handle.write(pcm)
        duration, peak = jarvis.wav_stats(path)
        self.assertAlmostEqual(duration, 0.75, places=3)
        self.assertAlmostEqual(peak, 0.3, delta=0.01)

    def test_header_only_file_measures_zero(self):
        path = write_wav(self.tmp / "empty.wav", 0.0)
        self.assertEqual(jarvis.wav_stats(path), (0.0, 0.0))

    def test_unreadable_files_raise_speech_input_error(self):
        garbage = self.tmp / "garbage.wav"
        garbage.write_bytes(b"this is definitely not a RIFF file" * 4)
        empty = self.tmp / "empty-file.wav"
        empty.write_bytes(b"")
        for path in (garbage, empty, self.tmp / "missing.wav"):
            with self.subTest(path=path.name):
                with self.assertRaises(jarvis.SpeechInputError):
                    jarvis.wav_stats(path)


class MicrophoneRecorderTests(TempDirTestCase):
    def make_recorder(self, argv, settle_seconds=0.05) -> jarvis.MicrophoneRecorder:
        recorder = jarvis.MicrophoneRecorder(argv=argv, settle_seconds=settle_seconds)
        self.addCleanup(recorder.close)
        return recorder

    def start_until_exit(self, recorder, path):
        """start() a recorder that exits on its own, waiting for the exit instead of a fixed settle sleep."""
        def wait_for_exit(_seconds):
            recorder._process.wait(timeout=5)
        with mock.patch("jarvis.time.sleep", side_effect=wait_for_exit):
            recorder.start(path)

    def test_no_recorder_mentions_alsa_utils(self):
        with mock.patch("voice.input.detect_recorder", return_value=None):
            recorder = self.make_recorder(None)
        self.assertIsNone(recorder.argv)
        with self.assertRaises(jarvis.SpeechInputError) as ctx:
            recorder.start(self.tmp / "out.wav")
        self.assertIn("alsa-utils", str(ctx.exception))

    def test_missing_executable(self):
        recorder = self.make_recorder(["jarvis-no-such-recorder-xyz", "-q"])
        with mock.patch("jarvis.subprocess.Popen") as popen:
            with self.assertRaises(jarvis.SpeechInputError) as ctx:
                recorder.start(self.tmp / "out.wav")
        popen.assert_not_called()
        self.assertIn("jarvis-no-such-recorder-xyz", str(ctx.exception))
        self.assertIn("not installed", str(ctx.exception))

    def test_busy_device_reports_stderr_and_hint(self):
        recorder = self.make_recorder(["sh", "-c", "echo 'audio open error: Device or resource busy' >&2; exit 1"])
        with self.assertRaises(jarvis.SpeechInputError) as ctx:
            self.start_until_exit(recorder, self.tmp / "out.wav")
        message = str(ctx.exception)
        self.assertIn("Device or resource busy", message)
        self.assertIn("Another program is using the microphone", message)
        recorder.stop()  # already exited: must be a no-op

    def test_permission_denied_hint(self):
        recorder = self.make_recorder(["sh", "-c", "echo 'audio open error: Permission denied' >&2; exit 1"])
        with self.assertRaises(jarvis.SpeechInputError) as ctx:
            self.start_until_exit(recorder, self.tmp / "out.wav")
        self.assertIn("Permission denied", str(ctx.exception))
        self.assertIn("not allowed to use the microphone", str(ctx.exception))

    def test_silent_failure_gives_generic_hint(self):
        recorder = self.make_recorder(["sh", "-c", "exit 1"])
        with self.assertRaises(jarvis.SpeechInputError) as ctx:
            self.start_until_exit(recorder, self.tmp / "out.wav")
        self.assertIn("the recorder exited", str(ctx.exception))
        self.assertIn("Settings > Sound > Input", str(ctx.exception))

    def test_stop_is_safe_before_start_and_idempotent(self):
        recorder = self.make_recorder(["sh", "-c", "exit 0"])
        recorder.stop()
        recorder.stop()
        recorder.close()
        recorder.close()

    def test_sigint_stop_finishes_the_recording(self):
        fixture = write_wav(self.tmp / "fixture.wav", 0.5, 0.3)
        out = self.tmp / "recorded.wav"
        script = f"trap 'exit 0' INT; cp {shlex.quote(str(fixture))} \"$0\"; while :; do sleep 0.05; done"
        recorder = self.make_recorder(["sh", "-c", script], settle_seconds=0.02)
        recorder.start(out)
        process = recorder._process
        self.assertIsNone(process.poll(), "recorder should still be running")
        for _ in range(100):  # normally already there; wait at most ~1 s for a slow machine
            if out.exists():
                break
            threading.Event().wait(0.01)
        recorder.stop()
        self.assertEqual(process.returncode, 0, "the recorder should exit through its SIGINT trap")
        self.assertEqual(out.read_bytes(), fixture.read_bytes())
        recorder.stop()  # second stop is a no-op
        self.assertEqual(jarvis.wav_stats(out)[0], 0.5)

    def test_popen_gets_an_argv_list_without_shell(self):
        recorder = self.make_recorder(["arecord", "-q", "-t", "wav"], settle_seconds=0)
        fake_process = mock.Mock()
        fake_process.poll.return_value = None
        out = self.tmp / "out file; rm -rf ~.wav"
        with mock.patch("jarvis.shutil.which", return_value="/usr/bin/arecord"), \
                mock.patch("jarvis.subprocess.Popen", return_value=fake_process) as popen:
            recorder.start(out)
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0], ["/usr/bin/arecord", "-q", "-t", "wav", str(out)])
        self.assertIs(kwargs.get("shell"), False)
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        recorder.stop()
        fake_process.send_signal.assert_called_once_with(jarvis.signal.SIGINT)

    def test_repeated_recordings_do_not_leak_the_stderr_file(self):
        recorder = self.make_recorder(["sh", "-c", "trap 'exit 0' INT; while :; do sleep 0.05; done"],
                                      settle_seconds=0)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            for index in range(2):
                recorder.start(self.tmp / f"take{index}.wav")
                recorder.stop()
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaks, [], "start() should close the previous recording's stderr capture file")


class GroqTranscriberTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.audio = write_wav(self.tmp / "clip.wav", 0.5, 0.3)
        self.sleeps: list[float] = []

    def make(self, script, language="en", model="whisper-large-v3-turbo"):
        client = FakeTranscriptionClient(script)
        return client, jarvis.GroqTranscriber(client, model, language, sleep=self.sleeps.append)

    def test_request_shape_and_stripped_text(self):
        client, transcriber = self.make(["  Open YouTube, please.  \n"])
        self.assertEqual(transcriber.transcribe(self.audio), "Open YouTube, please.")
        self.assertEqual(len(client.calls), 1)
        kwargs = client.calls[0]
        self.assertEqual(kwargs["model"], "whisper-large-v3-turbo")
        self.assertEqual(kwargs["response_format"], "json")
        self.assertEqual(kwargs["temperature"], 0)
        self.assertEqual(kwargs["language"], "en")
        self.assertEqual(kwargs["file"], ("clip.wav", self.audio.read_bytes()))
        self.assertEqual(self.sleeps, [])

    def test_empty_language_omits_language_kwarg(self):
        config = jarvis.Config.from_env(env={"GROQ_API_KEY": FAKE_KEY, "JARVIS_STT_LANGUAGE": ""})
        client, transcriber = self.make(["Bonjour"], language=config.stt_language)
        self.assertEqual(transcriber.transcribe(self.audio), "Bonjour")
        self.assertNotIn("language", client.calls[0])

    def test_none_text_becomes_empty_string(self):
        client, transcriber = self.make([None])
        self.assertEqual(transcriber.transcribe(self.audio), "")

    def test_retries_once_on_transient_error(self):
        client, transcriber = self.make([make_connection_error(), "second time lucky"])
        self.assertEqual(transcriber.transcribe(self.audio), "second time lucky")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_retry_delay_is_capped(self):
        rate_limited = make_status_error(groq.RateLimitError, 429, "slow down", headers={"retry-after": "30"})
        client, transcriber = self.make([rate_limited, "ok"])
        self.assertEqual(transcriber.transcribe(self.audio), "ok")
        self.assertEqual(self.sleeps, [5.0])

    def test_gives_up_after_second_transient_error(self):
        client, transcriber = self.make([make_connection_error(), make_connection_error()])
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            transcriber.transcribe(self.audio)
        self.assertEqual(ctx.exception.kind, "network")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_auth_error_not_retried(self):
        client, transcriber = self.make([make_status_error(groq.AuthenticationError, 401, "bad key")])
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            transcriber.transcribe(self.audio)
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_not_found_maps_to_stt_model_error(self):
        client, transcriber = self.make([make_status_error(groq.NotFoundError, 404, "model not found")],
                                        model="whisper-gone")
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            transcriber.transcribe(self.audio)
        self.assertEqual(ctx.exception.kind, "model")
        self.assertIn("JARVIS_STT_MODEL", str(ctx.exception))
        self.assertIn("whisper-gone", str(ctx.exception))
        self.assertNotIn("GROQ_MODEL", str(ctx.exception))
        self.assertEqual(len(client.calls), 1)

    def test_disclosure_names_groq_and_model(self):
        _, transcriber = self.make([], model="whisper-large-v3")
        self.assertEqual(transcriber.name, "groq")
        self.assertIn("Groq", transcriber.disclosure)
        self.assertIn("whisper-large-v3", transcriber.disclosure)


class SpeechInputListenTests(TempDirTestCase):
    def tearDown(self):
        close_logging_handlers()
        super().tearDown()

    def make(self, recorder=None, transcriber=None, wait_for_stop=lambda: None, max_seconds=60):
        self.notes: list[str] = []
        self.recorder = recorder or FakeMicRecorder()
        self.transcriber = transcriber or FakeTranscriber()
        speech = jarvis.SpeechInput(self.transcriber, recorder=self.recorder, max_seconds=max_seconds,
                                    wait_for_stop=wait_for_stop, notify=self.notes.append)
        self.addCleanup(speech.shutdown)
        return speech

    def assert_no_temp_audio(self, speech):
        self.assertEqual(list(speech._tmpdir.iterdir()), [])

    def test_returns_transcript_for_speech(self):
        speech = self.make(transcriber=FakeTranscriber("What time is it?"))
        self.assertEqual(speech.listen(), "What time is it?")
        self.assertEqual(len(self.transcriber.paths), 1)
        self.assertTrue(self.transcriber.existed_during_upload)
        self.assertEqual(self.recorder.started, self.transcriber.paths)
        self.assertGreaterEqual(self.recorder.stop_calls, 1)
        self.assert_no_temp_audio(speech)
        self.assertTrue(any("Listening" in note for note in self.notes))

    def test_too_short_recording_is_not_uploaded(self):
        speech = self.make(recorder=FakeMicRecorder(seconds=0.2))
        with self.assertRaises(jarvis.SpeechInputError) as ctx:
            speech.listen()
        self.assertIn("too short", str(ctx.exception))
        self.assertEqual(self.transcriber.paths, [])
        self.assertGreaterEqual(self.recorder.stop_calls, 1)
        self.assert_no_temp_audio(speech)

    def test_silence_is_not_uploaded(self):
        speech = self.make(recorder=FakeMicRecorder(seconds=1.0, amplitude=0.0))
        with self.assertRaises(jarvis.SpeechInputError) as ctx:
            speech.listen()
        self.assertIn("silence", str(ctx.exception))
        self.assertEqual(self.transcriber.paths, [])
        self.assertGreaterEqual(self.recorder.stop_calls, 1)
        self.assert_no_temp_audio(speech)

    def test_missing_recording_file_is_an_error(self):
        speech = self.make(recorder=FakeMicRecorder(write=False))
        with self.assertRaises(jarvis.SpeechInputError):
            speech.listen()
        self.assertEqual(self.transcriber.paths, [])
        self.assert_no_temp_audio(speech)

    def test_cancel_returns_none_without_uploading(self):
        for exc in (KeyboardInterrupt, EOFError):
            with self.subTest(exc=exc.__name__):
                def cancel():
                    raise exc()
                speech = self.make(wait_for_stop=cancel)
                self.assertIsNone(speech.listen())
                self.assertEqual(self.transcriber.paths, [])
                self.assertGreaterEqual(self.recorder.stop_calls, 1)
                self.assertEqual(len(self.recorder.started), 1)
                self.assert_no_temp_audio(speech)
                self.assertTrue(any("nothing was sent" in note for note in self.notes))

    def test_transcription_error_propagates_and_cleans_up(self):
        error = jarvis.JarvisAPIError("Cannot reach Groq.", "network", True)
        speech = self.make(transcriber=FakeTranscriber(error=error))
        with self.assertRaises(jarvis.JarvisAPIError):
            speech.listen()
        self.assertGreaterEqual(self.recorder.stop_calls, 1)
        self.assert_no_temp_audio(speech)

    def test_recorder_start_failure_propagates_and_cleans_up(self):
        speech = self.make(recorder=FakeMicRecorder(start_error=jarvis.SpeechInputError("mic busy")))
        with self.assertRaises(jarvis.SpeechInputError):
            speech.listen()
        self.assertEqual(self.transcriber.paths, [])
        self.assert_no_temp_audio(speech)

    def test_whisper_phantom_on_quiet_audio_is_dropped(self):
        # Loud enough to pass the silence check, quiet enough to be a likely phantom.
        amplitude = (jarvis.SILENCE_PEAK + jarvis.PHANTOM_PEAK) / 2
        speech = self.make(recorder=FakeMicRecorder(seconds=1.0, amplitude=amplitude),
                           transcriber=FakeTranscriber("Thank you."))
        self.assertEqual(speech.listen(), "")
        self.assertEqual(len(self.transcriber.paths), 1)
        self.assert_no_temp_audio(speech)

    def test_thank_you_kept_when_clearly_spoken(self):
        speech = self.make(transcriber=FakeTranscriber("Thank you."))
        self.assertEqual(speech.listen(), "Thank you.")

    def test_time_limit_stops_the_recorder(self):
        limit_hit = threading.Event()
        recorder = FakeMicRecorder()

        def notify(message):
            self.notes.append(message)
            if "Time limit" in message:
                limit_hit.set()

        def wait_for_stop():
            self.assertTrue(limit_hit.wait(2), "the time-limit timer never fired")
            self.assertGreaterEqual(recorder.stop_calls, 1)

        speech = self.make(recorder=recorder, wait_for_stop=wait_for_stop, max_seconds=0.05)
        speech.notify = notify
        self.assertEqual(speech.listen(), "hello there")
        self.assertTrue(any("Time limit" in note for note in self.notes))
        self.assertGreaterEqual(recorder.stop_calls, 2)  # once by the timer, once after Enter
        self.assert_no_temp_audio(speech)

    def test_audit_line_has_no_transcript(self):
        jarvis.setup_logging(self.config)
        secret_words = "my bank PIN is four four seven one"
        speech = self.make(transcriber=FakeTranscriber(secret_words))
        self.assertEqual(speech.listen(), secret_words)
        for handler in jarvis.audit_log.handlers + jarvis.log.handlers:
            handler.flush()
        audit_text = (self.log_dir / "audit.log").read_text(encoding="utf-8")
        entries = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
        entry = [e for e in entries if e.get("event") == "speech_input"][-1]
        self.assertEqual(entry["backend"], "fake")
        self.assertEqual(entry["characters"], len(secret_words))
        self.assertAlmostEqual(entry["seconds"], 1.0)
        self.assertNotIn("PIN", audit_text)
        self.assertNotIn(secret_words, audit_text)
        self.assertNotIn(secret_words, (self.log_dir / "jarvis.log").read_text(encoding="utf-8"))

    def test_availability_and_shutdown(self):
        speech = self.make()
        self.assertEqual(speech.availability(), (True, None))
        speech.recorder.argv = None
        ok, reason = speech.availability()
        self.assertFalse(ok)
        self.assertIn("alsa-utils", reason)
        tmpdir = speech._tmpdir
        speech.shutdown()
        self.assertEqual(self.recorder.close_calls, 1)
        self.assertFalse(tmpdir.exists())


class SpeechCLITests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.voice = jarvis.VoiceEngine(enabled=False, player=["fake-player"])
        self.addCleanup(self.voice.shutdown)

    def make_speech(self, text="what time is it", recorder=None):
        speech = jarvis.SpeechInput(FakeTranscriber(text), recorder=recorder or FakeMicRecorder(),
                                    wait_for_stop=lambda: None, notify=lambda message: None)
        self.addCleanup(speech.shutdown)
        return speech

    def make_cli(self, speech=None, listen=False):
        with mock.patch("builtins.print") as printed:
            cli = jarvis.JarvisCLI(jarvis.AgentContext(FakeGroqClient(), self.config, self.registry, self.policy,
                                                       self.memory), self.voice, self.memory,
                                   speech=speech, listen=listen)
        return cli, printed

    @staticmethod
    def printed_text(printed) -> str:
        return "\n".join(" ".join(str(a) for a in call.args) for call in printed.call_args_list)

    def run_loop(self, cli, inputs):
        with mock.patch.object(cli, "handle_message") as handle, \
                mock.patch("builtins.input", side_effect=inputs), \
                mock.patch("builtins.print") as printed:
            code = cli.loop()
        return code, handle, printed

    def test_listen_without_speech_warns_and_stays_off(self):
        cli, printed = self.make_cli(speech=None, listen=True)
        self.assertFalse(cli.listen_mode)
        self.assertIn("Speech input unavailable", self.printed_text(printed))

    def test_listen_without_recorder_warns_and_stays_off(self):
        recorder = FakeMicRecorder()
        recorder.argv = None
        cli, printed = self.make_cli(speech=self.make_speech(recorder=recorder), listen=True)
        self.assertFalse(cli.listen_mode)
        self.assertIn("alsa-utils", self.printed_text(printed))

    def test_listen_with_available_speech_turns_on(self):
        cli, printed = self.make_cli(speech=self.make_speech(), listen=True)
        self.assertTrue(cli.listen_mode)
        self.assertNotIn("unavailable", self.printed_text(printed))
        cli_off, _ = self.make_cli(speech=self.make_speech(), listen=False)
        self.assertFalse(cli_off.listen_mode)

    def test_empty_line_in_listen_mode_records_and_sends(self):
        cli, _ = self.make_cli(speech=self.make_speech("what time is it"), listen=True)
        code, handle, printed = self.run_loop(cli, ["", "exit"])
        self.assertEqual(code, 0)
        handle.assert_called_once_with("what time is it")
        self.assertIn("You (voice): what time is it", self.printed_text(printed))

    def test_talk_command_records_even_when_listen_mode_off(self):
        cli, _ = self.make_cli(speech=self.make_speech("open youtube"), listen=False)
        self.assertFalse(cli.listen_mode)
        code, handle, _ = self.run_loop(cli, ["/talk", "exit"])
        self.assertEqual(code, 0)
        handle.assert_called_once_with("open youtube")
        self.assertFalse(cli.listen_mode)

    def test_empty_line_does_not_record_when_listen_mode_off(self):
        speech = self.make_speech()
        cli, _ = self.make_cli(speech=speech, listen=False)
        with mock.patch.object(speech, "listen") as listen:
            code, handle, _ = self.run_loop(cli, ["", "  ", "exit"])
        self.assertEqual(code, 0)
        listen.assert_not_called()
        handle.assert_not_called()

    def test_spoken_exit_leaves_the_loop(self):
        for spoken in ("Exit.", "Goodbye!", "quit"):
            with self.subTest(spoken=spoken):
                cli, _ = self.make_cli(speech=self.make_speech(spoken), listen=True)
                code, handle, _ = self.run_loop(cli, [""])  # a second input() would raise StopIteration
                self.assertEqual(code, 0)
                handle.assert_not_called()

    def test_voice_turn_stops_tts_before_recording(self):
        speech = self.make_speech()
        cli, _ = self.make_cli(speech=speech)
        order: list[str] = []
        with mock.patch.object(self.voice, "stop", side_effect=lambda: order.append("voice.stop")), \
                mock.patch.object(speech, "listen", side_effect=lambda: order.append("listen") or "hello"), \
                mock.patch("builtins.print"):
            self.assertEqual(cli.voice_turn(), "hello")
        self.assertEqual(order, ["voice.stop", "listen"])

    def test_privacy_disclosure_printed_once_per_session(self):
        cli, _ = self.make_cli(speech=self.make_speech("first"))
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cli.voice_turn(), "first")
            self.assertEqual(cli.voice_turn(), "first")
        disclosures = [call for call in printed.call_args_list if "Privacy" in " ".join(map(str, call.args))]
        self.assertEqual(len(disclosures), 1)
        self.assertIn("FakeCloud", " ".join(map(str, disclosures[0].args)))

    def test_listen_errors_are_printed_not_raised(self):
        speech = self.make_speech()
        cli, _ = self.make_cli(speech=speech)
        for error, expected in ((jarvis.SpeechInputError("I heard only silence."), "I heard only silence."),
                                (jarvis.JarvisAPIError("Cannot reach Groq.", "network", True), "Transcription failed"),
                                (RuntimeError("driver exploded"), "Speech input failed")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(speech, "listen", side_effect=error), \
                        mock.patch("builtins.print") as printed, \
                        mock.patch.object(jarvis.log, "exception"):
                    self.assertEqual(cli.voice_turn(), "")
                self.assertIn(expected, self.printed_text(printed))

    def test_cancelled_or_empty_transcript_sends_nothing(self):
        speech = self.make_speech()
        cli, _ = self.make_cli(speech=speech, listen=True)
        for result in (None, "", "   "):
            with self.subTest(result=result):
                with mock.patch.object(speech, "listen", return_value=result), mock.patch("builtins.print"):
                    self.assertEqual(cli.voice_turn(), "")
        with mock.patch.object(speech, "listen", side_effect=[None, "   ", jarvis.SpeechInputError("too short")]):
            code, handle, _ = self.run_loop(cli, ["", "", "", "exit"])
        self.assertEqual(code, 0)
        handle.assert_not_called()

    def test_voice_turn_without_speech_prints_reason(self):
        cli, _ = self.make_cli(speech=None)
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cli.voice_turn(), "")
        self.assertIn("not configured", self.printed_text(printed))

    def test_transcript_control_characters_are_removed(self):
        cli, _ = self.make_cli(speech=self.make_speech("hello\x1b[31m there\x07"))
        with mock.patch("builtins.print"):
            text = cli.voice_turn()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)

    def test_confirmation_still_requires_typed_input(self):
        request = jarvis.ConfirmationRequest("forget_fact", jarvis.Permission.SENSITIVE_ACTION, "delete memory 1", [])
        with mock.patch.object(jarvis.sys, "stdin") as stdin, mock.patch("builtins.input") as typed:
            stdin.isatty.return_value = False
            self.assertFalse(jarvis.terminal_confirm(request))
        typed.assert_not_called()

    def test_listen_command_toggles(self):
        cli, _ = self.make_cli(speech=self.make_speech())
        with mock.patch("builtins.print") as printed:
            cli.handle_command("/listen on")
            self.assertTrue(cli.listen_mode)
            cli.handle_command("/listen off")
            self.assertFalse(cli.listen_mode)
            cli.handle_command("/listen")
            self.assertFalse(cli.listen_mode)
        text = self.printed_text(printed)
        self.assertIn("Push-to-talk on", text)
        self.assertIn("Push-to-talk off", text)
        self.assertIn("Use /listen on or /listen off", text)

    def test_listen_on_refused_without_speech(self):
        cli, _ = self.make_cli(speech=None)
        with mock.patch("builtins.print") as printed:
            cli.handle_command("/listen on")
        self.assertFalse(cli.listen_mode)
        self.assertIn("Speech input unavailable", self.printed_text(printed))

    def test_status_shows_speech_settings(self):
        cli, _ = self.make_cli(speech=self.make_speech(), listen=True)
        with mock.patch("builtins.print") as printed:
            cli.handle_command("/status")
        self.assertIn("groq/whisper-large-v3-turbo, push-to-talk on", self.printed_text(printed))

    def test_help_mentions_talk_and_typed_confirmations(self):
        self.assertIn("/talk", jarvis.HELP_TEXT)
        self.assertIn("/listen", jarvis.HELP_TEXT)
        self.assertIn("Confirmation prompts must always be typed", jarvis.HELP_TEXT)


class SpeechChecksTests(TempDirTestCase):
    def run_checks(self, config, recorder):
        with mock.patch("jarvis.detect_recorder", return_value=recorder), \
                mock.patch("jarvis.initialize_client", side_effect=AssertionError("no network allowed")), \
                mock.patch("builtins.print") as printed:
            code = jarvis.run_checks(config)
        lines = [" ".join(str(a) for a in call.args) for call in printed.call_args_list]
        return code, lines

    def test_missing_recorder_is_a_warning(self):
        code, lines = self.run_checks(self.make_config(api_key=""), None)
        mic = [line for line in lines if "microphone recorder" in line]
        self.assertEqual(len(mic), 1)
        self.assertIn("warn", mic[0])
        self.assertIn("alsa-utils", mic[0])
        self.assertFalse(any("speech model" in line for line in lines))

    def test_present_recorder_is_ok(self):
        _, lines = self.run_checks(self.make_config(api_key="not-a-key"), ["arecord", "-q"])
        mic = [line for line in lines if "microphone recorder" in line]
        self.assertEqual(len(mic), 1)
        self.assertIn("ok", mic[0])
        self.assertIn("arecord", mic[0])

    def test_speech_model_line_uses_model_list(self):
        for models, expected in (((jarvis.DEFAULT_MODEL, jarvis.DEFAULT_STT_MODEL), "ok"),
                                 ((jarvis.DEFAULT_MODEL,), "warn")):
            with self.subTest(models=models):
                client = FakeGroqClient(models=models)
                with mock.patch("jarvis.detect_recorder", return_value=["arecord"]), \
                        mock.patch("jarvis.initialize_client", return_value=client), \
                        mock.patch("builtins.print") as printed:
                    jarvis.run_checks(self.config)
                lines = [" ".join(str(a) for a in call.args) for call in printed.call_args_list]
                speech_lines = [line for line in lines if "speech model" in line]
                self.assertEqual(len(speech_lines), 1)
                self.assertIn(expected, speech_lines[0])
                if expected == "warn":
                    self.assertIn("JARVIS_STT_MODEL", speech_lines[0])

    def test_list_model_ids_sorted_and_classified(self):
        client = FakeGroqClient(models=("whisper-large-v3-turbo", "llama-3.3-70b-versatile"))
        self.assertEqual(jarvis.list_model_ids(client), ["llama-3.3-70b-versatile", "whisper-large-v3-turbo"])
        failing = SimpleNamespace(models=SimpleNamespace(
            list=mock.Mock(side_effect=make_status_error(groq.AuthenticationError, 401, "bad key"))))
        with self.assertRaises(jarvis.JarvisAPIError) as ctx:
            jarvis.list_model_ids(failing)
        self.assertEqual(ctx.exception.kind, "auth")


class TranscribePcmTests(TempDirTestCase):
    def make(self, text="Hey Jarvis, what time is it?", error=None):
        self.transcriber = PromptFakeTranscriber(text, error)
        speech = jarvis.SpeechInput(self.transcriber, recorder=FakeMicRecorder(), notify=lambda message: None)
        self.addCleanup(speech.shutdown)
        return speech

    def test_passes_prompt_and_strips_wake_phrase(self):
        speech = self.make("Hey Jarvis, what time is it?")
        self.assertEqual(speech.transcribe_pcm(pcm_tone(1.0)), "what time is it?")
        self.assertEqual(self.transcriber.prompts, ["Hey JARVIS,"])
        self.assertTrue(self.transcriber.existed_during_upload)
        self.assertEqual(self.transcriber.paths[0].suffix, ".wav")

    def test_upload_is_a_valid_wav_of_the_pcm(self):
        speech = self.make()
        pcm = pcm_tone(0.8)
        seen = {}

        def capture(path, prompt=None):
            with wave.open(str(path), "rb") as wav:
                seen.update(rate=wav.getframerate(), channels=wav.getnchannels(),
                            frames=wav.readframes(wav.getnframes()))
            return "Hey JARVIS, hello"

        self.transcriber.transcribe = capture
        self.assertEqual(speech.transcribe_pcm(pcm), "hello")
        self.assertEqual((seen["rate"], seen["channels"], seen["frames"]), (16000, 1, pcm))

    def test_silent_or_short_audio_is_never_uploaded(self):
        for label, pcm in (("silent", bytes(32000)), ("short", pcm_tone(0.2)), ("empty", b"")):
            with self.subTest(audio=label):
                speech = self.make()
                with self.assertRaises(jarvis.SpeechInputError) as ctx:
                    speech.transcribe_pcm(pcm)
                self.assertIn("didn't hear a command", str(ctx.exception))
                self.assertEqual(self.transcriber.paths, [])
                self.assertEqual(list(speech._tmpdir.iterdir()), [])

    def test_phantom_filter_applies(self):
        amplitude = (jarvis.SILENCE_PEAK + jarvis.PHANTOM_PEAK) / 2
        speech = self.make("Hey JARVIS, thank you.")
        self.assertEqual(speech.transcribe_pcm(pcm_tone(1.0, amplitude)), "")
        speech = self.make("Hey JARVIS, thank you.")
        self.assertEqual(speech.transcribe_pcm(pcm_tone(1.0, 0.3)), "thank you.")

    def test_temp_wav_deleted_on_success_and_error(self):
        speech = self.make()
        speech.transcribe_pcm(pcm_tone(1.0))
        self.assertFalse(self.transcriber.paths[0].exists())
        self.assertEqual(list(speech._tmpdir.iterdir()), [])
        failing = self.make(error=jarvis.JarvisAPIError("Cannot reach Groq.", "network", True))
        with self.assertRaises(jarvis.JarvisAPIError):
            failing.transcribe_pcm(pcm_tone(1.0))
        self.assertEqual(list(failing._tmpdir.iterdir()), [])

    def test_audit_line_has_wake_mode_and_no_transcript(self):
        jarvis.setup_logging(self.config)
        self.addCleanup(close_logging_handlers)
        speech = self.make("Hey Jarvis, my PIN is four four seven one")
        self.assertEqual(speech.transcribe_pcm(pcm_tone(1.0)), "my PIN is four four seven one")
        with self.assertRaises(jarvis.SpeechInputError):
            speech.transcribe_pcm(bytes(32000))
        for handler in jarvis.audit_log.handlers:
            handler.flush()
        audit_text = (self.log_dir / "audit.log").read_text(encoding="utf-8")
        entries = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
        speech_entries = [e for e in entries if e.get("event") == "speech_input"]
        self.assertEqual([(e["mode"], e["outcome"]) for e in speech_entries],
                         [("wake", "transcribed"), ("wake", "silent")])
        self.assertEqual(speech_entries[0]["characters"], len("my PIN is four four seven one"))
        self.assertEqual(speech_entries[0]["backend"], "fake")
        self.assertNotIn("PIN", audit_text)


class GroqTranscriberPromptTests(TempDirTestCase):
    def test_prompt_passed_only_when_given(self):
        audio = write_wav(self.tmp / "clip.wav", 0.5, 0.3)
        client = FakeTranscriptionClient(["Hey JARVIS, hello", "hello"])
        transcriber = jarvis.GroqTranscriber(client, sleep=lambda seconds: None)
        self.assertEqual(transcriber.transcribe(audio, prompt="Hey JARVIS,"), "Hey JARVIS, hello")
        self.assertEqual(transcriber.transcribe(audio), "hello")
        self.assertEqual(client.calls[0]["prompt"], "Hey JARVIS,")
        self.assertNotIn("prompt", client.calls[1])


if __name__ == "__main__":
    unittest.main()
