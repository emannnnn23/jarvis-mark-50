"""Hands-free mode: wake word and voice interruption.

Detection runs locally with openWakeWord. Audio never leaves the machine until the wake word is
heard; then only the command that follows is transcribed. Saying the wake word while JARVIS is
speaking or working interrupts it. A wake word is used for interruption (rather than any loud
sound) because a laptop microphone hears JARVIS's own voice from the speakers."""

from __future__ import annotations

import array
import collections
import math
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from typing import Callable

from config.settings import DEFAULT_WAKE_MODEL
from core.audit import audit, log
from voice.input import SpeechInputError


FRAME_SAMPLES = 1280  # 80 ms at 16 kHz, the frame size openWakeWord expects


FRAME_SECONDS = FRAME_SAMPLES / 16000


FRAME_BYTES = FRAME_SAMPLES * 2


STREAM_RECORDERS = (
    ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"],
)


MIN_SPEECH_RMS = 0.01


MAX_NOISE_RMS = 0.02  # cap on the tracked background level, so a rising noise cannot make JARVIS deaf


WAKE_MAX_SECONDS = 20  # a hands-free request; long dictation belongs to push-to-talk


SPEECH_PEAK_FRACTION = 0.25  # after speech starts, quieter than this share of its peak counts as a pause


WAKE_INSTALL_HINT = ("Install the wake-word engine with:  .venv/bin/pip install --no-deps openwakeword==0.6.0 && "
                     ".venv/bin/pip install -r requirements-wake.txt")


WAKE_RECORDER_HINT = "Hands-free mode streams audio with arecord. Install it with:  sudo apt install alsa-utils"


CHIME_IGNORE_SECONDS = 0.8  # player start-up latency plus the chime itself


STOP_PHRASES = frozenset({"stop", "stop it", "stop talking", "cancel", "never mind", "nevermind", "quiet",
                          "be quiet", "shush", "that's enough", "thats enough", "enough", "nothing", "forget it"})


def is_stop_phrase(text: str) -> bool:
    return re.sub(r"[^a-z' ]", "", text.lower()).strip() in STOP_PHRASES


def frame_rms(frame: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    if not samples:
        return 0.0
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768


class UtteranceCollector:
    """Collects the command after the wake word and decides when the speaker has finished:
    `end_silence` seconds of quiet after speech, `start_timeout` with no speech, or `max_seconds`."""

    def __init__(self, noise_rms: float, pre_roll: bytes = b"", end_silence: float = 1.0,
                 start_timeout: float = 5.0, max_seconds: float = 20.0, ignore_seconds: float = 0.0):
        self.threshold = max(MIN_SPEECH_RMS, noise_rms * 3.0)
        self.ignore_seconds = ignore_seconds  # e.g. while the listening chime plays
        self.end_silence = end_silence
        self.start_timeout = start_timeout
        self.max_seconds = max_seconds
        self.frames = [pre_roll] if pre_roll else []
        self.heard_speech = False
        self.peak_rms = 0.0
        self.elapsed = 0.0
        self.silence = 0.0

    def feed(self, frame: bytes) -> str | None:
        """Returns None to keep listening, "done" when a command is complete, or "no_speech"."""
        self.frames.append(frame)
        self.elapsed += len(frame) / 2 / 16000
        rms = frame_rms(frame)
        # Relative to the speaker's own level too, so a steady background (a fan, other voices)
        # between the floor and normal speech still counts as the pause that ends the request.
        speaking = rms >= max(self.threshold, SPEECH_PEAK_FRACTION * self.peak_rms)
        if self.elapsed > self.ignore_seconds and speaking:
            self.heard_speech, self.silence = True, 0.0
            self.peak_rms = max(self.peak_rms, rms)
        elif self.heard_speech:
            self.silence += len(frame) / 2 / 16000
        if self.heard_speech and self.silence >= self.end_silence:
            return "done"
        if self.elapsed >= self.max_seconds:
            return "done" if self.heard_speech else "no_speech"
        if not self.heard_speech and self.elapsed >= self.start_timeout:
            return "no_speech"
        return None

    def audio(self) -> bytes:
        return b"".join(self.frames)


class OpenWakeWordDetector:
    """Wraps an openWakeWord model. score() takes one 80 ms frame of 16-bit PCM."""

    def __init__(self, model: str = DEFAULT_WAKE_MODEL):
        try:
            import numpy
            import openwakeword
            import openwakeword.utils
            from openwakeword.model import Model
        except ImportError:
            raise SpeechInputError(f"The wake-word engine is not installed. {WAKE_INSTALL_HINT}") from None
        self._numpy = numpy
        self.name = Path(model).stem if model.endswith(".onnx") else model
        if not model.endswith(".onnx"):
            if model not in openwakeword.MODELS:
                raise SpeechInputError(f"Unknown wake word model {model!r}. Built-in models: "
                                       f"{', '.join(sorted(openwakeword.MODELS))}, or give a path to an .onnx file.")
            onnx_path = Path(openwakeword.MODELS[model]["model_path"].replace(".tflite", ".onnx"))
            if not onnx_path.exists():
                print(f"Downloading the {model!r} wake-word model (about 7 MB, one time only)...")
                try:
                    openwakeword.utils.download_models(model_names=[model])
                except Exception as exc:  # noqa: BLE001
                    raise SpeechInputError(f"Could not download the wake-word model: {exc}") from None
        elif not Path(model).expanduser().is_file():
            raise SpeechInputError(f"Wake-word model file {model!r} not found.")
        try:
            self._model = Model(wakeword_models=[str(Path(model).expanduser()) if model.endswith(".onnx") else model],
                                inference_framework="onnx")
        except Exception as exc:  # noqa: BLE001
            raise SpeechInputError(f"Could not load the wake-word model: {exc}") from None

    def score(self, frame: bytes) -> float:
        prediction = self._model.predict(self._numpy.frombuffer(frame, dtype=self._numpy.int16))
        return float(max(prediction.values(), default=0.0))

    def reset(self) -> None:
        self._model.reset()


def wake_engine_available() -> tuple[bool, str]:
    """Check every runtime dependency without importing them (openwakeword alone takes ~1.5 s)."""
    import importlib.util
    for module in ("openwakeword", "onnxruntime", "numpy", "scipy", "sklearn", "tqdm", "requests"):
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            return False, f"{module} is not installed. {WAKE_INSTALL_HINT}"
    return True, "openWakeWord"


class WakeWordListener:
    """Background thread: reads the microphone continuously, runs the wake-word model on each
    frame, and after a detection collects the spoken command until the speaker pauses.

    Callbacks run on the listener thread: on_wake() right after a detection (used to stop
    speech and cancel work), on_command(pcm) with the finished command, on_status(message).
    """

    def __init__(self, detector, on_wake: Callable[[], None], on_command: Callable[[bytes], None],
                 on_status: Callable[[str], None] | None = None, is_speaking: Callable[[], bool] = lambda: False,
                 threshold: float = 0.6, speaking_threshold: float = 0.75, end_silence: float = 1.0,
                 start_timeout: float = 5.0, max_seconds: float = 20.0, pre_roll_seconds: float = 1.0,
                 cooldown_seconds: float = 1.5, recorder_argv: list[str] | None = None,
                 stream_factory: Callable[[], Any] | None = None, ignore_after_wake: float = 0.0):
        self.detector = detector
        self.ignore_after_wake = ignore_after_wake
        self.on_wake = on_wake
        self.on_command = on_command
        self.on_status = on_status or (lambda message: None)
        self.is_speaking = is_speaking
        self.threshold = threshold
        self.speaking_threshold = speaking_threshold
        self.end_silence = end_silence
        self.start_timeout = start_timeout
        self.max_seconds = max_seconds
        self.pre_roll_frames = max(1, round(pre_roll_seconds / FRAME_SECONDS))
        self.cooldown_frames = round(cooldown_seconds / FRAME_SECONDS)
        if recorder_argv is None:
            recorder_argv = next((list(a) for a in STREAM_RECORDERS if shutil.which(a[0])), None)
        self.recorder_argv = recorder_argv
        self.stream_factory = stream_factory
        self.paused = False
        self.noise_rms = 0.003
        self._process: subprocess.Popen | None = None
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _open_stream(self):
        if self.stream_factory is not None:
            return self.stream_factory()
        if not self.recorder_argv:
            raise SpeechInputError(WAKE_RECORDER_HINT)
        executable = shutil.which(self.recorder_argv[0])
        if executable is None:
            raise SpeechInputError(f"'{self.recorder_argv[0]}' is not installed. {WAKE_RECORDER_HINT}")
        with tempfile.TemporaryFile() as errors:
            try:
                self._process = subprocess.Popen(
                    [executable, *self.recorder_argv[1:]], shell=False, start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                )
            except OSError as exc:
                raise SpeechInputError(f"Could not start {self.recorder_argv[0]}: {exc.strerror or exc}") from None
            time.sleep(0.2)
            code = self._process.poll()
            if code is not None:  # e.g. no capture device, or it is busy
                errors.seek(0)
                detail = errors.read().decode("utf-8", "replace").strip()[:200] or f"exit code {code}"
                self._process = None
                raise SpeechInputError(f"The microphone could not be opened ({detail}). Check Settings > Sound > Input.")
        return self._process.stdout

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._stream = self._open_stream()
        self._thread = threading.Thread(target=self._run, name="jarvis-wake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        if self._stream is not None and hasattr(self._stream, "close"):
            try:
                self._stream.close()
            except OSError:
                pass
        self._process = self._stream = self._thread = None

    def _read_frame(self) -> bytes | None:
        data = b""
        while len(data) < FRAME_BYTES:
            chunk = self._stream.read(FRAME_BYTES - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _run(self) -> None:
        try:
            self._listen()
        except Exception as exc:  # noqa: BLE001 - report, never crash the session
            if not self._stopping.is_set():
                log.exception("Wake-word listener failed")
                self.on_status(f"Hands-free listening stopped: {exc}")
        finally:
            process = self._process  # whatever ended the thread, close the microphone
            if process is not None and process.poll() is None:
                process.terminate()

    def _listen(self) -> None:
        ring: collections.deque[bytes] = collections.deque(maxlen=self.pre_roll_frames)
        collector: UtteranceCollector | None = None
        cooldown = 0
        while not self._stopping.is_set():
            frame = self._read_frame()
            if frame is None:
                if not self._stopping.is_set():
                    self.on_status("Hands-free listening stopped: the microphone stream ended. "
                                   "Type /wake on to try again.")
                return
            if self.paused:
                ring.clear()
                collector = None
                continue
            if collector is not None:
                outcome = collector.feed(frame)
                if outcome == "done":
                    self.on_command(collector.audio())
                elif outcome == "no_speech":
                    self.on_status("I didn't hear a command. Say \"Hey JARVIS\" to try again.")
                if outcome:
                    collector, cooldown = None, self.cooldown_frames
                    self.detector.reset()
                continue
            score = self.detector.score(frame)
            ring.append(frame)
            rms = frame_rms(frame)
            if score < 0.1 and rms < max(self.noise_rms * 3, MIN_SPEECH_RMS) and not self.is_speaking():
                # Track the room's background level (never JARVIS's own voice), within a cap.
                self.noise_rms = min(MAX_NOISE_RMS, 0.95 * self.noise_rms + 0.05 * rms)
            if cooldown:
                cooldown -= 1
                continue
            if score >= (self.speaking_threshold if self.is_speaking() else self.threshold):
                audit({"event": "wake_word", "model": getattr(self.detector, "name", "custom"),
                       "score": round(score, 3)})
                self.detector.reset()
                collector = UtteranceCollector(self.noise_rms, b"".join(ring), self.end_silence,
                                               self.start_timeout, self.max_seconds, self.ignore_after_wake)
                ring.clear()
                self.on_wake()
