"""One confirmation, answered at the terminal or in Mission Control: whichever answers first wins.

Used as the policy's confirm callable only while Mission Control is on (otherwise `terminal_confirm`
runs, unchanged). It keeps every guarantee of the terminal prompt and adds the browser as a second
channel under these rules:

- Nothing is asked without a real terminal (as before): the browser alone can never approve.
- The browser answers one specific prompt: a single-use id plus the sha256 digest of exactly what was
  shown. A wrong id, a used id or a different digest changes nothing.
- The browser sees the scrubbed view. When scrubbing changed anything (a secret was redacted, a line
  was too long for the wire), the browser could not see what it would approve, so it may only decline;
  approving then needs the terminal.
- Timeout, end of input and Ctrl+C decline. Ctrl+C is re-raised so the request can be cancelled.
- The audit line records who answered and how, never the details or the diff."""

from __future__ import annotations

import copy
import dataclasses
import hmac
import logging
import secrets
import select
import sys
import threading
import time
from collections import deque
from typing import Any
from typing import Callable

from core import events
from core.audit import audit, clean_text, redact
from core.events import PUBLISH_MAX_ITEMS, PUBLISH_MAX_STR, scrub
from core.policy import ConfirmationRequest
from ui.terminal import build_confirmation_view, print_confirmation, style


log = logging.getLogger("jarvis.mission_control")


MISSION_PROMPT = "   Type 'yes' to proceed (or answer in Mission Control), anything else cancels: "

# Longest single wait on the terminal; a browser answer is noticed within this.
POLL_INTERVAL = 0.1

# Recently resolved ids, so a late browser answer gets "already_answered" rather than "stale".
RESOLVED_KEPT = 32


class StdinAnswerReader:
    """Reads one typed line without blocking past `timeout`, so the broker can also watch the browser."""

    def __init__(self, stream=None) -> None:
        self._stream = stream

    def _current(self):
        return self._stream if self._stream is not None else sys.stdin

    def poll(self, timeout: float) -> str | None:
        """A typed line (with its newline), None if nothing was typed in time; EOFError at end of input."""
        stream = self._current()
        ready, _, _ = select.select([stream], [], [], max(0.0, timeout))
        if not ready:
            return None
        line = stream.readline()
        if line == "":
            raise EOFError
        return line

    def discard(self) -> None:
        """Drop anything half-typed after the browser answered, so it cannot become the next command."""
        try:
            import termios

            termios.tcflush(self._current().fileno(), termios.TCIFLUSH)
        except Exception:  # noqa: BLE001 - not a terminal (pipe, test double): nothing to drop
            pass


@dataclasses.dataclass
class _Pending:
    id: str
    digest: str
    payload: dict[str, Any]
    approvable: bool
    outcome: tuple[str, bool] | None = None  # (channel, approved), set once by whoever answers first


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class ConfirmationBroker:
    """The policy's confirm callable while Mission Control is on. At most one confirmation is open."""

    def __init__(self, reader=None, clock: Callable[[], float] = time.monotonic, timeout: float = 600.0,
                 isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
                 publish: Callable[..., Any] = events.emit,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.reader = reader if reader is not None else StdinAnswerReader()
        self._clock = clock
        self._wall_clock = wall_clock  # only for expires_at, which the browser compares with its own clock
        self.timeout = timeout
        self._isatty = isatty
        self._publish = publish
        self._lock = threading.Lock()
        self._pending: _Pending | None = None
        self._resolved: deque[str] = deque(maxlen=RESOLVED_KEPT)

    # --- the policy side --------------------------------------------------------------------------

    def confirm(self, request: ConfirmationRequest) -> bool:
        """Show the request, then wait for the first answer from the terminal or the browser."""
        if not self._isatty():
            return False
        view = build_confirmation_view(request)
        shown, approvable = self._browser_view(view)
        confirm_id = secrets.token_hex(16)
        payload = {"id": confirm_id, "digest": view.digest(), **shown, "browser_approvable": approvable,
                   "expires_at": self._expires_at()}
        pending = _Pending(confirm_id, payload["digest"], payload, approvable)
        with self._lock:
            if self._pending is not None:
                # Only the main thread asks; a second prompt at once is a bug, and declining is safe.
                log.warning("a confirmation was requested while another was open; declined")
                return False
            self._pending = pending
        outcome: tuple[str, bool] = ("cancelled", False)
        try:
            self._publish("confirm.request", _transient=True, **copy.deepcopy(payload))
            print_confirmation(view, prompt_hint=MISSION_PROMPT)
            outcome = self._wait(pending)
            return outcome[1]
        except KeyboardInterrupt:
            outcome = self._settle(pending, ("cancelled", False), force=True)
            print()
            raise
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None
                self._resolved.append(confirm_id)
            self._resolve(confirm_id, view.tool, outcome)

    def _wait(self, pending: _Pending) -> tuple[str, bool]:
        deadline = self._clock() + self.timeout if self.timeout > 0 else None
        while True:
            with self._lock:
                outcome = pending.outcome
            if outcome is not None:  # the browser answered
                self._after_browser(outcome[1])
                return outcome
            wait = POLL_INTERVAL
            if deadline is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    outcome = self._settle(pending, ("timeout", False))
                    if outcome[0] == "timeout":
                        print()
                        print("   " + style(f"No answer within {self.timeout:g} s: cancelled.", "yellow"))
                    else:
                        self._after_browser(outcome[1])
                    return outcome
                wait = min(wait, remaining)
            try:
                line = self.reader.poll(wait)
            except EOFError:
                print()
                return self._settle(pending, ("terminal", False))
            except Exception:  # noqa: BLE001 - an unreadable terminal declines; it never approves
                log.debug("confirmation reader failed", exc_info=True)
                print()
                return self._settle(pending, ("cancelled", False))
            if line is not None:
                outcome = self._settle(pending, ("terminal", clean_text(line).strip().lower() in ("yes", "y")))
                if outcome[0] == "browser":  # it answered while the line was being read: it was first
                    self._after_browser(outcome[1])
                return outcome

    def _settle(self, pending: _Pending, outcome: tuple[str, bool], force: bool = False) -> tuple[str, bool]:
        """Record `outcome` unless someone answered first (`force`: Ctrl+C overrides, since it declines)."""
        with self._lock:
            if pending.outcome is None or force:
                pending.outcome = outcome
            return pending.outcome

    def _after_browser(self, approved: bool) -> None:
        discard = getattr(self.reader, "discard", None)
        if discard is not None:
            discard()
        print()
        if approved:
            print("   " + style("Approved in Mission Control.", "green"))
        else:
            print("   " + style("Declined in Mission Control.", "yellow"))

    def _resolve(self, confirm_id: str, tool: str, outcome: tuple[str, bool]) -> None:
        channel, approved = outcome
        try:
            self._publish("confirm.resolved", id=confirm_id, approved=approved, channel=channel)
        except Exception:  # noqa: BLE001 - publishing never decides an approval
            log.debug("could not publish confirm.resolved", exc_info=True)
        audit({"event": "confirmation", "tool": tool, "approved": approved, "channel": channel})

    def _browser_view(self, view) -> tuple[dict[str, Any], bool]:
        """What the browser is sent, and whether it is exactly the view (so the browser may approve)."""
        exact = view.as_payload()
        shown = scrub(exact, max_str=PUBLISH_MAX_STR, max_items=PUBLISH_MAX_ITEMS, depth=4)
        redacted = any(redact(text) != text for text in _strings(exact))
        return shown, shown == exact and not redacted

    def _expires_at(self) -> int | None:
        if self.timeout <= 0:
            return None
        return int((self._wall_clock() + self.timeout) * 1000)

    # --- the browser side -------------------------------------------------------------------------

    def answer_from_browser(self, confirm_id: str, digest: str, approved: bool) -> str:
        """Answer the open confirmation: "ok" | "stale" | "already_answered" | "digest_mismatch" |
        "terminal_only". Only `approved is True` approves; anything else declines."""
        confirm_id, digest = str(confirm_id), str(digest)
        with self._lock:
            pending = self._pending
            if pending is None or not hmac.compare_digest(pending.id.encode(), confirm_id.encode()):
                return "already_answered" if confirm_id in self._resolved else "stale"
            if pending.outcome is not None:
                return "already_answered"
            if not hmac.compare_digest(pending.digest.encode(), digest.encode()):
                return "digest_mismatch"
            approve = approved is True
            if approve and not pending.approvable:
                return "terminal_only"
            pending.outcome = ("browser", approve)
            return "ok"

    def pending_payload(self) -> dict[str, Any] | None:
        """The open confirmation in the confirm.request shape (for a snapshot), or None."""
        with self._lock:
            pending = self._pending
            if pending is None or pending.outcome is not None:
                return None
            return copy.deepcopy(pending.payload)
