"""The Mission Control event bus: one in-process place where real JARVIS events are published.

Producers (the main thread, the voice asyncio thread, the wake thread) call `emit`; the Mission
Control server subscribes and streams events to the browser. Publishing must therefore be
thread-safe, must never block on a slow consumer and must never raise into the code being observed,
or the UI could break the assistant it is meant to watch."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import math
import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from typing import Any
from typing import Callable
from typing import Iterator

from core.audit import clean_text
from core.audit import redact
from core.audit import safe_display


log = logging.getLogger("jarvis.events")


PROTOCOL_VERSION = 1


# Stands in for anything cut off by the depth cap, so the browser can tell "empty" from "elided".
TRUNCATED = "[...]"


# What publish allows in one payload. Looser than scrub's defaults because real events carry
# review lines (confirm.request: up to 400 lines of 1000 chars) and command output (up to 8000 chars).
PUBLISH_MAX_STR = 8000
PUBLISH_MAX_ITEMS = 500


# Longest string redacted whole; anything longer is cut to this first. redact() is linear, so this
# only bounds the worst case (about a second for 1 MiB). Cutting to the max_str that the caller keeps
# would be wrong: a quoted secret that starts before max_str but closes after the cut would lose its
# closing quote, and redact() would then stop at the first space and leak the rest. A secret would
# have to be over 1 MiB long to escape this limit.
SCRUB_INPUT_LIMIT = 1_048_576


# What redact() puts in place of a secret.
REDACTED = "[REDACTED]"


def _drop_partial_marker(text: str) -> str:
    """Remove a trailing non-empty, incomplete prefix of REDACTED (a cut landed inside the marker)."""
    for size in range(len(REDACTED) - 1, 0, -1):
        if text.endswith(REDACTED[:size]):
            return text[:-size]
    return text


def _settle(text: str) -> str:
    """Redact until nothing changes. One more pass is needed when removing control codes joined a
    credential name together ("pass\x01word=x"), or when a marker glued to the text after it
    ("secret: [REDACTED]more") is read as part of a value again; almost always one pass confirms it."""
    for _ in range(4):
        again = redact(text)
        if again == text:
            break
        text = again
    return text


def _cut(text: str, max_str: int) -> str:
    """Cut already redacted text to max_str so that redacting it again changes nothing.

    publish re-scrubs every field, so a cut that is not a fixed point of redact() would be rewritten
    there. Two cuts are not: one inside the marker after a credential name ("password=[RED", which
    redact() expands back to 19 characters, past the caller's limit; the partial marker is dropped),
    and one that turns the tail into a match of its own (a key prefix that is now exactly the right
    length; it is redacted now instead of later). A partial marker that redact() leaves alone
    ("xxxx[RED") is kept."""
    cut = text[:max_str]
    for _ in range(8):
        again = redact(cut)
        if again == cut:
            return cut
        trimmed = _drop_partial_marker(cut)
        if trimmed != cut:
            cut = trimmed
        elif len(again) > max_str:
            cut = _drop_partial_marker(again[:max_str])
        else:
            cut = again
    return cut


_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")

# What may stand before a zero-width joiner that is kept: an emoji, a skin-tone modifier, or the
# emoji presentation selector (U+FE0F) after an emoji, as in the sequences for a family or a job.
_EMOJI_BEFORE_JOINER = ("So", "Sk")


def _strip_format_run(match: re.Match[str]) -> str:
    run = match.group()
    out = []
    for index, char in enumerate(run):
        category = unicodedata.category(char)
        if category != "Cf":
            out.append(char)
        elif char == "\u200d" and 0 < index < len(run) - 1 and unicodedata.category(run[index + 1]) == "So" \
                and (unicodedata.category(run[index - 1]) in _EMOJI_BEFORE_JOINER or run[index - 1] == "\ufe0f"):
            out.append(char)
    return "".join(out)


def strip_format(text: str) -> str:
    """Remove format characters (zero-width spaces and joiners, soft hyphens, bidi controls, tags):
    they draw nothing, so "pass\\u200bword=x" would read as "password=x" in the page while redaction
    saw two words. A joiner inside an emoji sequence stays. Only non-ASCII runs are looked at, so
    ordinary text costs one scan."""
    if text.isascii():
        return text
    return _NON_ASCII_RUN.sub(_strip_format_run, text)


_current_request: contextvars.ContextVar[str | None] = contextvars.ContextVar("jarvis_request_id", default=None)


def scrub(value: Any, *, max_str: int = 2000, max_items: int = 200, depth: int = 4) -> Any:
    """Make any value safe to send to the browser: JSON types only, no secrets, no control codes, bounded.

    Redaction runs before the final truncation so a key cut in half can never slip past the patterns;
    the input is first cut to SCRUB_INPUT_LIMIT so a huge string cannot stall the caller.

    The result is a fixed point: scrubbing it again with the same (or a larger) max_str returns it
    unchanged, so publish re-scrubbing an already scrubbed field can never make it longer: redaction
    is repeated until it settles (see _settle) and the cut is one redact() leaves alone (see _cut)."""
    if isinstance(value, str):
        cleaned = clean_text(value[:SCRUB_INPUT_LIMIT])
        # Format characters go before redaction, so one cannot split a credential name in two.
        text = safe_display(redact(strip_format(cleaned)))
        if text != cleaned:  # unchanged text is already a fixed point: skip the extra pass
            text = _settle(text)
        return text if len(text) <= max_str else _cut(text, max_str)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth <= 0:
        return TRUNCATED
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:max_items]:
            name = scrub(str(key), max_str=100)
            if "[REDACTED]" in redact(f"{name}=x"):
                # A value stored under a secret-looking name ({"password": "hunter2"}) has no
                # pattern of its own to match, so the name decides, whatever the value's type
                # ({"password": ["hunter2"]}, {"pin": 1234}, {"api_key": {"v": ...}}).
                out[name] = "[REDACTED]"
            else:
                out[name] = scrub(item, max_str=max_str, max_items=max_items, depth=depth - 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset, deque)):
        return [scrub(item, max_str=max_str, max_items=max_items, depth=depth - 1)
                for item in list(value)[:max_items]]
    return scrub(str(value), max_str=max_str)


class EventBus:
    """Fan-out of JARVIS events with a bounded replay ring for clients that reconnect.

    One lock guards only the sequence number, the envelope and the ring; subscribers are called
    outside it so a slow or failing consumer can never stall a producer or another subscriber."""

    def __init__(self, history: int = 500, clock: Callable[[], float] = time.time) -> None:
        self.enabled = False
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self._ring: deque[dict[str, Any]] = deque(maxlen=max(1, history))
        # Highest seq pushed out of the ring. Transient events use seqs but never enter the ring,
        # so gaps in the ring cannot be used to tell whether history was lost; this can.
        self._evicted_seq = 0
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._last_emit: dict[str, float] = {}

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Register a consumer. Returns a function that unregisters it (safe to call twice)."""
        with self._lock:
            self._subscribers = [*self._subscribers, fn]

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers = [s for s in self._subscribers if s is not fn]

        return unsubscribe

    def publish(self, type: str, *, _transient: bool = False, **data: Any) -> dict[str, Any] | None:
        """Publish one event and return its envelope (None only if building it failed).

        Never raises. `_transient` events (meter levels and the like) reach subscribers but are not
        kept for replay. The payload is scrubbed with the PUBLISH_* limits, and works on a disabled
        bus too: only `emit` and `emit_throttled` check `enabled`. Subscribers run outside the lock,
        so delivery order across concurrent publishers may differ from seq order; clients rely on seq."""
        try:
            payload = scrub(data, max_str=PUBLISH_MAX_STR, max_items=PUBLISH_MAX_ITEMS, depth=4)
            request_id = _current_request.get()
            timestamp = int(self._clock() * 1000)
            with self._lock:
                self._seq += 1
                event: dict[str, Any] = {**payload, "v": PROTOCOL_VERSION, "seq": self._seq,
                                         "type": str(type), "timestamp": timestamp}
                if request_id:
                    event["request_id"] = request_id
                if not _transient:
                    if len(self._ring) == self._ring.maxlen:
                        self._evicted_seq = self._ring[0]["seq"]
                    self._ring.append(event)
                subscribers = self._subscribers
        except Exception:
            log.debug("could not build event %r", type, exc_info=True)
            return None
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:
                log.debug("event subscriber failed", exc_info=True)
        return event

    def recent(self, after_seq: int | None = None) -> list[dict[str, Any]] | None:
        """Events a reconnecting client missed (all retained events when `after_seq` is None).

        None means history was lost (the ring moved past `after_seq`, or the client is from a
        different run), so the caller should send a fresh snapshot instead."""
        with self._lock:
            if after_seq is None:
                return list(self._ring)
            if after_seq < self._evicted_seq or after_seq > self._seq:
                return None
            return [event for event in self._ring if event["seq"] > after_seq]

    @contextlib.contextmanager
    def request_scope(self, source: str) -> Iterator[str]:
        """Tag every event published in this block (and thread) with one new request id."""
        label = re.sub(r"[^a-z0-9]", "", str(source).lower())[:12] or "req"
        request_id = f"{label}-{uuid.uuid4().hex[:8]}"
        token = _current_request.set(request_id)
        try:
            yield request_id
        finally:
            _current_request.reset(token)

    def emit(self, type: str, **data: Any) -> None:
        """Publish only when Mission Control is on, so it costs nothing when off."""
        if self.enabled:
            self.publish(type, **data)

    def emit_throttled(self, key: str, interval: float, type: str, *, _transient: bool = False,
                       **data: Any) -> None:
        """`emit`, but at most one event per `key` every `interval` seconds (the rest are dropped).

        High-rate streams (the voice meter) pass `_transient=True`, or minutes of them would push
        every real event out of the replay ring."""
        if not self.enabled:
            return
        try:
            now = self._clock()
            with self._lock:
                last = self._last_emit.get(key)
                if last is not None and 0 <= now - last < interval:
                    return
                self._last_emit[key] = now
        except Exception:
            return
        self.publish(type, _transient=_transient, **data)


BUS = EventBus()


def emit(type: str, **data: Any) -> None:
    """Publish on the shared bus; a no-op unless Mission Control is enabled."""
    BUS.emit(type, **data)


def emit_throttled(key: str, interval: float, type: str, *, _transient: bool = False, **data: Any) -> None:
    """Rate-limited `emit` on the shared bus."""
    BUS.emit_throttled(key, interval, type, _transient=_transient, **data)
