"""Shared fixtures and fakes for the JARVIS MARK 50 test suite.

Everything external is faked: no network, no Groq key, no browser, no microphone,
no audio. The only subprocesses are harmless `sh` stand-ins for the microphone recorder.
All files live in temporary directories."""

from __future__ import annotations

import importlib
import array
import copy
import json
import math
import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import groq  # noqa: E402
import httpx  # noqa: E402

import jarvis  # noqa: E402,F401

try:
    # numpy's C core can only be loaded once per process. Import it before any test patches
    # sys.modules, or a patch that removes it on exit makes every later import fail.
    importlib.import_module("numpy")
except ImportError:
    pass


FAKE_KEY = "gsk_" + "a" * 40


class TempDirTestCase(unittest.TestCase):
    """Creates an isolated temp dir with workspace/notes/logs and a Config pointing at it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-test-")
        self.tmp = Path(self._tmp.name).resolve()
        self.workspace = self.tmp / "workspace"
        self.notes_dir = self.tmp / "notes"
        self.log_dir = self.tmp / "logs"
        self.memory_path = self.tmp / "memory" / "jarvis_memory.json"
        self.workspace.mkdir()
        self.config = self.make_config()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_config(self, **overrides) -> jarvis.Config:
        values = dict(
            api_key=FAKE_KEY,
            memory_path=self.memory_path,
            workspace=self.workspace,
            notes_dir=self.notes_dir,
            log_dir=self.log_dir,
        )
        values.update(overrides)
        return jarvis.Config(**values)

    def make_memory(self, **kwargs) -> jarvis.MemoryStore:
        store = jarvis.MemoryStore(self.memory_path, **kwargs)
        store.load()
        return store


class Recorder:
    """A confirm callable that records every ConfirmationRequest and returns a fixed answer."""

    def __init__(self, answer: bool):
        self.answer = answer
        self.calls: list[jarvis.ConfirmationRequest] = []

    def __call__(self, request: jarvis.ConfirmationRequest) -> bool:
        self.calls.append(request)
        return self.answer


class FakeBrowser:
    def __init__(self, result: bool = True):
        self.result = result
        self.urls: list[str] = []

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        return self.result


def make_ddgs_factory(results=None, errors=None, calls=None):
    """Return a DDGS stand-in. `errors` is a list of exceptions raised on successive calls."""
    errors = list(errors or [])
    calls = calls if calls is not None else []

    class FakeDDGS:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def text(self, query, max_results=None):
            calls.append({"query": query, "max_results": max_results})
            if errors:
                raise errors.pop(0)
            return list(results or [])

    return FakeDDGS


def tool_call(call_id: str, name: str, arguments) -> SimpleNamespace:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def response(content=None, tool_calls=None) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeGroqClient:
    """Scripted stand-in for groq.Groq: .chat.completions.create and .models.list."""

    def __init__(self, script=None, default=None, models=("llama-3.3-70b-versatile",)):
        self.script = list(script or [])
        self.default = default
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id=m) for m in models]))

    def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.script:
            item = self.script.pop(0)
        elif self.default is not None:
            item = self.default() if callable(self.default) else self.default
        else:
            raise AssertionError("FakeGroqClient ran out of scripted responses")
        if isinstance(item, BaseException):
            raise item
        return item


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def make_status_error(cls, status: int, message: str = "error", body=None, headers=None):
    resp = httpx.Response(status, request=_request(), headers=headers or {})
    return cls(message, response=resp, body=body)


def make_connection_error():
    return groq.APIConnectionError(message="Connection error.", request=_request())


class AgentTestCase(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.memory = self.make_memory()
        self.browser = FakeBrowser()
        self.tools = jarvis.JarvisTools(self.config, self.memory, browser_open=self.browser,
                                        ddgs_factory=make_ddgs_factory(results=[]))
        self.registry = jarvis.build_registry(self.tools)
        self.confirm = Recorder(True)
        self.policy = jarvis.PermissionPolicy("standard", confirm=self.confirm)

    def run_agent(self, client, text="hello", registry=None, config=None):
        ctx = jarvis.AgentContext(client, config or self.config, registry or self.registry, self.policy, self.memory)
        return jarvis.run_agent(ctx, text)

    @staticmethod
    def tool_messages(call_kwargs):
        return [m for m in call_kwargs["messages"] if m["role"] == "tool"]


def write_wav(path: Path, seconds: float, amplitude: float = 0.3, rate: int = 16000, freq: float = 440.0) -> Path:
    """Write a 16 kHz mono 16-bit WAV: a sine wave at `amplitude`, or silence when amplitude is 0."""
    count = int(seconds * rate)
    samples = array.array("h", (int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
                                for i in range(count)))
    if sys.byteorder == "big":
        samples.byteswap()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return path


class FakeTranscriptionClient:
    """Stand-in for groq.Groq exposing only .audio.transcriptions.create(**kwargs)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(text=item)


class FakeTranscriber:
    name = "fake"
    disclosure = "Your recording is sent to FakeCloud for transcription."

    def __init__(self, text="hello there", error: BaseException | None = None):
        self.text = text
        self.error = error
        self.paths: list[Path] = []

    def transcribe(self, path: Path) -> str:
        self.paths.append(path)
        self.existed_during_upload = path.exists()
        if self.error is not None:
            raise self.error
        return self.text


class FakeMicRecorder:
    """Pretends to record by writing a prepared WAV (or nothing) to the requested path."""

    def __init__(self, seconds: float = 1.0, amplitude: float = 0.3, write: bool = True,
                 start_error: BaseException | None = None):
        self.argv = ["fake-recorder"]
        self.seconds, self.amplitude, self.write = seconds, amplitude, write
        self.start_error = start_error
        self.started: list[Path] = []
        self.stop_calls = 0
        self.close_calls = 0
        self.stopped = threading.Event()

    def start(self, path: Path) -> None:
        self.started.append(path)
        if self.start_error is not None:
            raise self.start_error
        if self.write:
            write_wav(path, self.seconds, self.amplitude)

    def stop(self) -> None:
        self.stop_calls += 1
        self.stopped.set()

    def close(self) -> None:
        self.close_calls += 1


def close_logging_handlers() -> None:
    for logger in (jarvis.log, jarvis.audit_log):
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def const_frame(value: int) -> bytes:
    """One 80 ms frame in which every sample is `value`; distinct values make frames easy to find."""
    samples = array.array("h", [value] * jarvis.FRAME_SAMPLES)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def quiet(tag: int = 0) -> bytes:
    return const_frame(tag)  # rms below 0.001: background silence


def loud(tag: int = 0) -> bytes:
    return const_frame(10000 + tag)  # rms about 0.3: speech


def pcm_tone(seconds: float, amplitude: float = 0.3, freq: float = 440.0, rate: int = 16000) -> bytes:
    samples = array.array("h", (int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
                                for i in range(int(seconds * rate))))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


class FakeDetector:
    """Wake-word model stand-in. `scores` maps the index of each score() call to its score (default 0)."""

    name = "fake_model"

    def __init__(self, scores=None, error_at: int | None = None, error: BaseException | None = None):
        self.scores = dict(scores or {})
        self.error_at, self.error = error_at, error
        self.calls = 0
        self.resets = 0

    def score(self, frame: bytes) -> float:
        index, self.calls = self.calls, self.calls + 1
        if index == self.error_at:
            raise self.error
        return self.scores.get(index, 0.0)

    def reset(self) -> None:
        self.resets += 1


class PromptFakeTranscriber(FakeTranscriber):
    """FakeTranscriber that also accepts (and records) the Whisper prompt used by transcribe_pcm."""

    def __init__(self, text="hello there", error: BaseException | None = None):
        super().__init__(text, error)
        self.prompts: list[str | None] = []

    def transcribe(self, path: Path, prompt: str | None = None) -> str:
        self.prompts.append(prompt)
        return super().transcribe(path)


class FakeWakeListener:
    """What JarvisCLI needs from a WakeWordListener: running, start(), stop() and paused."""

    def __init__(self, start_error: BaseException | None = None):
        self.start_error = start_error
        self.paused = False
        self.is_running = False
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def running(self) -> bool:
        return self.is_running

    def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        self.is_running = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.is_running = False


def printed_text(printed) -> str:
    return "\n".join(" ".join(str(a) for a in call.args) for call in printed.call_args_list)


def fake_openwakeword_modules(models=None, download_error=None, model_error=None, onnx_dir=None):
    """sys.modules entries standing in for numpy and openwakeword (no real model is loaded)."""
    package = types.ModuleType("openwakeword")
    package.MODELS = models if models is not None else {
        name: {"model_path": str(Path(onnx_dir or "/nonexistent") / f"{name}_v0.1.tflite")}
        for name in ("alexa", "hey_jarvis", "timer")}
    utils = types.ModuleType("openwakeword.utils")
    utils.download_models = mock.Mock(side_effect=download_error)
    model_module = types.ModuleType("openwakeword.model")
    model_module.Model = mock.Mock(side_effect=model_error)
    package.utils, package.model = utils, model_module
    return {"numpy": types.ModuleType("numpy"), "openwakeword": package,
            "openwakeword.utils": utils, "openwakeword.model": model_module}
