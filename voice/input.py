"""Speech input: push-to-talk recording and Groq Whisper transcription."""

from __future__ import annotations

import array
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any
from typing import Callable

from config.settings import DEFAULT_STT_MODEL
from core.audit import audit
from intelligence.groq_client import JarvisAPIError, classify_api_error


# 16 kHz mono 16-bit PCM is what Whisper uses internally, and it keeps uploads small.
RECORDERS = (
    ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "wav"],
    ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"],
)


RECORDER_INSTALL_HINT = "Install a recorder with:  sudo apt install alsa-utils   (provides arecord)"


MIN_RECORDING_SECONDS = 0.4


# A quiet room measured ~0.015 on a laptop mic and speech is typically 0.1+, so anything
# below 0.03 is treated as silence and never uploaded.
SILENCE_PEAK = 0.03


PHANTOM_PEAK = 0.08


# Phrases Whisper tends to invent when given near-silent audio.
_WHISPER_PHANTOMS = frozenset({"thank you.", "thank you", "thanks for watching!", "thank you for watching.",
                               "you", "bye.", "."})


class SpeechInputError(Exception):
    """Recording or transcription failed; the message is safe to show the user."""


def detect_recorder() -> list[str] | None:
    for argv in RECORDERS:
        if shutil.which(argv[0]):
            return list(argv)
    return None


def wav_stats(path: Path) -> tuple[float, float]:
    """Return (duration in seconds, peak level 0-1) of a 16-bit PCM WAV recording."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate, width, channels = wav.getframerate(), wav.getsampwidth(), wav.getnchannels()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError, OSError):
        raise SpeechInputError("The recording could not be read.") from None
    if not frames and path.stat().st_size > 44:
        # A recorder stopped abruptly may leave a header claiming zero frames.
        frames, rate, width, channels = path.read_bytes()[44:], 16000, 2, 1
    if width != 2 or not frames or not rate:
        return 0.0, 0.0
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    peak = max(max(samples, default=0), -min(samples, default=0)) / 32768
    return len(samples) / max(channels, 1) / rate, peak


class MicrophoneRecorder:
    """Records the default microphone to a WAV file with arecord or pw-record."""

    def __init__(self, argv: list[str] | None = None, settle_seconds: float = 0.15):
        self.argv = argv if argv is not None else detect_recorder()
        self.settle_seconds = settle_seconds
        self._process: subprocess.Popen | None = None
        self._stderr = None
        self._lock = threading.Lock()

    def start(self, path: Path) -> None:
        if not self.argv:
            raise SpeechInputError(f"No microphone recorder found. {RECORDER_INSTALL_HINT}")
        executable = shutil.which(self.argv[0])
        if executable is None:
            raise SpeechInputError(f"'{self.argv[0]}' is not installed. {RECORDER_INSTALL_HINT}")
        if self._stderr is not None:
            self._stderr.close()
        self._stderr = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(
                [executable, *self.argv[1:], str(path)], shell=False, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self._stderr,
            )
        except OSError as exc:
            raise SpeechInputError(f"Could not start {self.argv[0]}: {exc.strerror or exc}") from None
        time.sleep(self.settle_seconds)
        if self._process.poll() is not None:
            raise SpeechInputError(self._failure_message())

    def _failure_message(self) -> str:
        detail = ""
        if self._stderr is not None:
            self._stderr.seek(0)
            detail = self._stderr.read().decode("utf-8", "replace").strip()[:200]
        lowered = detail.lower()
        if "busy" in lowered:
            hint = "Another program is using the microphone."
        elif "permission" in lowered or "denied" in lowered:
            hint = "Your user is not allowed to use the microphone (check sandbox or 'audio' group settings)."
        else:
            hint = "Check that a microphone is connected and enabled in Settings > Sound > Input."
        return f"The microphone could not be opened ({detail or 'the recorder exited'}). {hint}"

    def stop(self) -> None:
        """Stop recording. SIGINT lets the recorder finish writing the WAV header. Safe to call twice."""
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def close(self) -> None:
        self.stop()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None


class GroqTranscriber:
    """Speech-to-text through Groq's hosted Whisper models, reusing the chat API key."""

    name = "groq"

    def __init__(self, client, model: str = DEFAULT_STT_MODEL, language: str = "en",
                 sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.model = model
        self.language = language
        self.sleep = sleep

    @property
    def disclosure(self) -> str:
        return (f"Your recording is sent to Groq ({self.model}) for transcription. "
                "JARVIS deletes its local copy straight afterwards.")

    def transcribe(self, path: Path, prompt: str | None = None) -> str:
        kwargs: dict[str, Any] = {"file": (path.name, path.read_bytes()), "model": self.model,
                                  "response_format": "json", "temperature": 0}
        if self.language:
            kwargs["language"] = self.language
        if prompt:  # biases spelling, e.g. "JARVIS" rather than "Jervis"
            kwargs["prompt"] = prompt
        for attempt in range(2):  # one retry for transient failures
            try:
                result = self.client.audio.transcriptions.create(**kwargs)
                return (getattr(result, "text", "") or "").strip()
            except Exception as exc:  # noqa: BLE001
                error = classify_api_error(exc, self.model)
                if error.kind == "model":
                    error = JarvisAPIError(f"The speech model {self.model!r} is unavailable. "
                                           "Set JARVIS_STT_MODEL, e.g. whisper-large-v3.", "model")
                elif error.kind == "quota":
                    error = JarvisAPIError(f"The Groq speech quota for {self.model!r} is used up for now. Try again "
                                           "later, set JARVIS_STT_MODEL to another Whisper model, or type instead.",
                                           "quota")
                if not error.retryable or attempt == 1:
                    raise error from exc
                self.sleep(min(error.retry_after or 1.0, 5.0))
        return ""


class SpeechInput:
    """Push-to-talk: record until the user presses Enter (or the time limit), then transcribe.

    The microphone is only open between the two key presses; nothing records in the background.
    """

    def __init__(self, transcriber, recorder: MicrophoneRecorder | None = None, max_seconds: int = 60,
                 wait_for_stop: Callable[[], Any] | None = None, notify: Callable[[str], None] | None = None):
        self.transcriber = transcriber
        self.recorder = recorder or MicrophoneRecorder()
        self.max_seconds = max_seconds
        self.wait_for_stop = wait_for_stop or input
        self.notify = notify or print
        self._tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-stt-"))

    def availability(self) -> tuple[bool, str | None]:
        if not self.recorder.argv:
            return False, f"No microphone recorder found. {RECORDER_INSTALL_HINT}"
        return True, None

    def _time_up(self) -> None:
        self.recorder.stop()
        self.notify(f"\n  Time limit ({self.max_seconds}s) reached. Press Enter to continue.")

    def _record_outcome(self, mode: str, outcome: str, seconds: float = 0.0, characters: int = 0) -> None:
        audit({"event": "speech_input", "mode": mode, "backend": self.transcriber.name, "outcome": outcome,
               "seconds": round(seconds, 1), "characters": characters})

    def transcribe_pcm(self, pcm: bytes, rate: int = 16000) -> str:
        """Transcribe a hands-free command (raw 16-bit mono PCM). Same checks as push-to-talk;
        the wake phrase at the start is removed. Raises SpeechInputError for silence."""
        path = self._tmpdir / f"{uuid.uuid4().hex}.wav"
        try:
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(pcm)
            duration, peak = wav_stats(path)
            if duration < MIN_RECORDING_SECONDS or peak < SILENCE_PEAK:
                self._record_outcome("wake", "silent", duration)
                raise SpeechInputError("I didn't hear a command after the wake word.")
            text = self.transcriber.transcribe(path, prompt="Hey JARVIS,")
            text = strip_wake_phrase(text)
            if text.lower() in _WHISPER_PHANTOMS and peak < PHANTOM_PEAK:
                text = ""
            self._record_outcome("wake", "transcribed", duration, len(text))
            return text
        finally:
            path.unlink(missing_ok=True)

    def listen(self) -> str | None:
        """Returns the transcript, "" if nothing was heard, or None if the user cancelled."""
        path = self._tmpdir / f"{uuid.uuid4().hex}.wav"

        def record_outcome(outcome: str, seconds: float = 0.0, characters: int = 0) -> None:
            self._record_outcome("push_to_talk", outcome, seconds, characters)

        try:
            self.recorder.start(path)
            self.notify(f"  [mic] Listening... press Enter to stop (limit {self.max_seconds}s, Ctrl+C cancels).")
            timer = threading.Timer(self.max_seconds, self._time_up)
            timer.daemon = True
            timer.start()
            try:
                self.wait_for_stop()
            except (EOFError, KeyboardInterrupt):
                self.notify("\n  Recording cancelled; nothing was sent.")
                record_outcome("cancelled")
                return None
            finally:
                timer.cancel()
                self.recorder.stop()
            duration, peak = wav_stats(path)
            if duration < MIN_RECORDING_SECONDS:
                record_outcome("too_short", duration)
                raise SpeechInputError("That recording was too short. Press Enter, speak, then press Enter again.")
            if peak < SILENCE_PEAK:
                record_outcome("silent", duration)
                raise SpeechInputError("I heard only silence. Check that the microphone is unmuted and selected "
                                       "in Settings > Sound > Input.")
            self.notify("  Transcribing...")
            text = self.transcriber.transcribe(path)
            if text.lower() in _WHISPER_PHANTOMS and peak < PHANTOM_PEAK:
                text = ""
            record_outcome("transcribed", duration, len(text))
            return text
        finally:
            path.unlink(missing_ok=True)

    def shutdown(self) -> None:
        self.recorder.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)


_WAKE_PHRASE = re.compile(r"\b(?:(?:hey|hi|hello|okay|ok|a)[\s,.!-]+)?(?:jarvis|jervis|javis)\b[\s,.!?:;-]*",
                          re.IGNORECASE)


def strip_wake_phrase(text: str) -> str:
    """Drop everything up to and including the first "Hey JARVIS" (the pre-roll may also hold
    the tail of JARVIS's own speech when the user interrupted it)."""
    match = _WAKE_PHRASE.search(text)
    return text[match.end():].strip() if match else text.strip()
