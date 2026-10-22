"""Spoken output: edge-tts synthesis and local MP3 playback."""

from __future__ import annotations

import array
import asyncio
import concurrent.futures
import math
import re
import shutil
import sys
import tempfile
import threading
import uuid
import wave
from pathlib import Path
from typing import Any
from typing import Callable

from config.settings import DEFAULT_VOICE
from core.audit import log


AUDIO_PLAYERS = (
    ["mpv", "--no-video", "--really-quiet", "--no-terminal"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    ["mpg123", "-q"],
    ["gst-play-1.0", "--quiet", "--no-interactive"],
)


AUDIO_INSTALL_HINT = "Install an MP3 player with:  sudo apt install mpv   (or: sudo apt install mpg123)"


def write_chime(path: Path, rate: int = 16000) -> None:
    """Write a soft rising two-tone chime (about 0.2 s) as a 16-bit mono WAV."""
    samples = array.array("h")
    for frequency in (660.0, 880.0):
        count = int(rate * 0.09)
        for i in range(count):
            envelope = math.sin(math.pi * i / count)  # fade in and out, no clicks
            samples.append(int(0.25 * 32767 * envelope * math.sin(2 * math.pi * frequency * i / rate)))
    if sys.byteorder == "big":
        samples.byteswap()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())


def detect_audio_player() -> list[str] | None:
    for argv in AUDIO_PLAYERS:
        if shutil.which(argv[0]):
            return list(argv)
    return None


def clean_text_for_speech(text: str) -> str:
    """Turn a Markdown-ish chat reply into something pleasant to hear."""
    text = re.sub(r"```.*?(```|\Z)", " I've put the code on screen. ", text, flags=re.DOTALL)
    text = re.sub(r"^\s*[\[{].*[\]}]\s*$", " ", text, flags=re.MULTILINE)  # raw JSON lines
    text = re.sub(r"^\s*(Traceback \(most recent call last\):|File \".*\", line \d+).*$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"<[^>]{1,80}>", " ", text)  # stray markup
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"https?://\S+", "the link on screen", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", " ", text, flags=re.MULTILINE)  # table rules
    text = text.replace("|", ", ")
    text = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", text)
    text = re.sub(r"[*#>~]+", " ", text)
    text = re.sub(r"\s*\n+\s*", ". ", text)
    text = re.sub(r"\.\s*\.(\s*\.)*", ".", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = text.strip(" .,")
    return text + "." if text and text[-1] not in ".!?" else text


def split_into_chunks(text: str, max_chars: int = 350) -> list[str]:
    """Split on sentence boundaries so long replies synthesize and play progressively."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > max_chars:  # very long sentence: break on a space
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


async def _cancel_pending_tasks() -> None:
    tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class VoiceEngine:
    """edge-tts synthesis plus a local MP3 player, on a background event loop.

    One asyncio loop runs in a daemon thread for the whole session, so asyncio.run() is never
    called repeatedly. Starting a new utterance cancels the previous one, and an asyncio.Lock
    guarantees two utterances never play at the same time.
    """

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, voice: str = DEFAULT_VOICE, enabled: bool = True, player: list[str] | None = None,
                 synthesize: Callable[[str, str, Path], Any] | None = None,
                 notify: Callable[[str], None] | None = None):
        self.voice = voice
        self.player = player if player is not None else detect_audio_player()
        self._synthesize_override = synthesize
        self.notify = notify or (lambda message: print(f"\n[voice] {message}", file=sys.stderr))
        self.unavailable_reason: str | None = None
        self.enabled = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock: asyncio.Lock | None = None
        self._current: concurrent.futures.Future | None = None
        self._current_lock = threading.RLock()
        self._chime_path: Path | None = None
        self._failures = 0
        self._tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-tts-"))
        if enabled:
            self.set_enabled(True)

    def availability(self) -> tuple[bool, str | None]:
        if self._synthesize_override is None:
            try:
                import edge_tts  # noqa: F401
            except ImportError:
                return False, "edge-tts is not installed (pip install edge-tts)."
        if not self.player:
            return False, f"No audio player found. {AUDIO_INSTALL_HINT}"
        return True, None

    def set_enabled(self, enabled: bool) -> bool:
        if not enabled:
            self.stop()
            self.enabled = False
            return False
        ok, reason = self.availability()
        self.unavailable_reason = reason
        self.enabled = ok
        if ok:
            self._failures = 0
        return ok

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, name="jarvis-voice", daemon=True)
            self._thread.start()
        return self._loop

    async def _synthesize(self, text: str, path: Path) -> None:
        if self._synthesize_override is not None:
            result = self._synthesize_override(text, self.voice, path)
            if asyncio.iscoroutine(result):
                await result
            return
        import edge_tts
        communicate = edge_tts.Communicate(text, self.voice)
        await asyncio.wait_for(communicate.save(str(path)), timeout=30)

    async def _play(self, path: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            *self.player, str(path),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=180)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
            raise
        if process.returncode != 0:
            raise RuntimeError(f"{self.player[0]} exited with code {process.returncode}")

    async def speak(self, text: str) -> None:
        """Synthesize and play `text`, preparing the next chunk while the current one plays."""
        chunks = split_into_chunks(clean_text_for_speech(text))
        if not chunks:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            paths = [self._tmpdir / f"{uuid.uuid4().hex}.mp3" for _ in chunks]
            pending = asyncio.ensure_future(self._synthesize(chunks[0], paths[0]))
            try:
                for index in range(len(chunks)):
                    await pending
                    if index + 1 < len(chunks):
                        pending = asyncio.ensure_future(self._synthesize(chunks[index + 1], paths[index + 1]))
                    await self._play(paths[index])
                    paths[index].unlink(missing_ok=True)
            finally:
                if not pending.done():
                    pending.cancel()
                for path in paths:
                    path.unlink(missing_ok=True)

    async def _speak_safely(self, text: str) -> None:
        try:
            await self.speak(text)
            self._failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - voice must never take the assistant down
            self._failures += 1
            log.warning("Voice output failed: %s", exc)
            if self._failures >= self.MAX_CONSECUTIVE_FAILURES:
                self.enabled = False
                self.notify("Voice output keeps failing; switching to text-only mode. Use /voice on to retry.")
            elif self._failures == 1:
                reason = "no network connection?" if isinstance(exc, (OSError, asyncio.TimeoutError)) else type(exc).__name__
                self.notify(f"Voice output failed ({reason}); the reply is shown as text.")

    def say(self, text: str) -> concurrent.futures.Future | None:
        """Speak in the background. Interrupts anything currently being spoken."""
        if not self.enabled or not text.strip():
            return None
        with self._current_lock:
            self._stop_locked()
            future = asyncio.run_coroutine_threadsafe(self._speak_safely(text), self._ensure_loop())
            self._current = future
        return future

    @property
    def is_speaking(self) -> bool:
        current = self._current
        return current is not None and not current.done()

    def _stop_locked(self) -> None:
        if self._current is not None and not self._current.done():
            self._current.cancel()
        self._current = None

    def stop(self) -> None:
        """Stop speaking now. Safe to call from any thread (the wake-word listener uses it)."""
        with self._current_lock:
            self._stop_locked()

    def chime(self) -> None:
        """A short two-tone cue that JARVIS is listening. Plays even in text-only mode."""
        if not self.player:
            return
        try:
            if self._chime_path is None:
                self._chime_path = self._tmpdir / "listening-chime.wav"
                write_chime(self._chime_path)
            future = asyncio.run_coroutine_threadsafe(self._play(self._chime_path), self._ensure_loop())
            future.add_done_callback(lambda f: f.cancelled() or f.exception())  # never raise
        except Exception as exc:  # noqa: BLE001 - a missing chime must never matter
            log.debug("Chime failed: %s", exc)

    def wait(self, timeout: float = 15) -> None:
        if self._current is not None:
            try:
                self._current.result(timeout=timeout)
            except (concurrent.futures.CancelledError, concurrent.futures.TimeoutError):
                pass

    def shutdown(self) -> None:
        current = self._current
        self.stop()
        if current is not None:  # let the cancelled task terminate its player process first
            concurrent.futures.wait([current], timeout=3)
        if self._loop is not None:
            # Cancel anything still pending (e.g. a chime scheduled a moment ago) and let it unwind,
            # so no player is left running and no coroutine is dropped without being awaited.
            try:
                asyncio.run_coroutine_threadsafe(_cancel_pending_tasks(), self._loop).result(timeout=3)
            except (concurrent.futures.TimeoutError, RuntimeError):
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=3)
            if self._thread is None or not self._thread.is_alive():
                self._loop.close()
            self._loop = self._thread = None
        shutil.rmtree(self._tmpdir, ignore_errors=True)
