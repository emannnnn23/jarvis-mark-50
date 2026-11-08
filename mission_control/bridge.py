"""Commands from Mission Control into the terminal's own loop.

The browser never runs anything itself. A command it sends is checked here, then put in a one-slot
inbox that the main thread reads in `JarvisCLI._read_line`, exactly where a typed line would be read.
From there it takes the same path as typed text (`_step`, `handle_command` or `handle_message`, then
`run_agent`), so the permission policy, the tools and the confirmations are the ones the terminal has.

What the browser may not do, because only the person at the keyboard should:

- leave JARVIS (every `EXIT_WORDS` entry);
- turn a microphone on (`/wake on`, `/listen on`, `/talk`): Mission Control must never be a way to
  start listening in the room;
- the destructive resets (`/forget`, `/memory reset`, `/tasks reset`), which ask with
  `terminal_confirm` directly and are meant to be typed.

The check runs on the same normalised line the loop acts on, and is repeated by the loop itself."""

from __future__ import annotations

import enum
import queue
import threading
import unicodedata
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING

from core.audit import clean_text
from ui.terminal import EXIT_WORDS

if TYPE_CHECKING:
    from mission_control.confirm import ConfirmationBroker


MAX_TEXT = 4000  # characters in one browser command, before normalising
MAX_COMMAND_ID = 128  # the browser's own id for a command, echoed back in acks and events


class SubmitResult(enum.Enum):
    """What happened to a submitted command; the values are the wire's ack status and reject reasons."""

    ACCEPTED = "accepted"
    BUSY = "busy"
    QUEUE_FULL = "queue_full"
    TERMINAL_ONLY = "terminal_only"
    INVALID = "invalid"
    TOO_LONG = "too_long"


# The slash commands the browser may send (the brief's allow-list). Unknown ones are refused as invalid
# rather than passed on, so a command added later is terminal-only until it is listed here.
BROWSER_COMMANDS = frozenset({
    "/help", "/tools", "/status", "/memory", "/tasks", "/resume", "/abandon", "/dev", "/voice", "/stop",
    "/clear", "/wake", "/listen",
})
TERMINAL_ONLY_COMMANDS = frozenset({"/forget", "/talk"})
# Allowed commands whose first argument makes them terminal-only.
TERMINAL_ONLY_ARGUMENTS = {"/memory": "reset", "/tasks": "reset", "/wake": "on", "/listen": "on"}


def normalise(text: Any) -> str:
    """The line exactly as the loop sees a typed one."""
    return clean_text(text).strip()


def _has_control_characters(line: str) -> bool:
    """A typed line never holds newlines or other control or separator characters; a browser line that
    does is refused, so the command parser can never read it differently from what the page showed.
    (str.split, used by the command parser, splits on \\x1c-\\x1f, \\x85 and \\u2028 too.)"""
    return any(unicodedata.category(char) in ("Cc", "Zl", "Zp") and char != "\t" for char in line)


def _is_exit_word(line: str) -> bool:
    # A superset of the loop's own test (`line.lower().rstrip(".!") in EXIT_WORDS`).
    lowered = line.lower()
    return (lowered.rstrip(".!") in EXIT_WORDS
            or " ".join(lowered.split()).rstrip(".! ") in EXIT_WORDS)


def browser_command_policy(line: str) -> tuple[bool, str]:
    """(allowed, reason) for one line from the browser; reason is "" | "terminal_only" | "invalid".

    Parsed the way `JarvisCLI.handle_command` parses it (first word lower-cased, the rest stripped),
    but compared case-insensitively, so it can only be stricter than the command it guards."""
    line = normalise(line)
    if not line or _has_control_characters(line):
        return False, "invalid"
    if _is_exit_word(line):
        return False, "terminal_only"
    if not line.startswith("/"):
        return True, ""  # a message for the model ("help" becomes /help, which is allowed)
    parts = line.split(maxsplit=1)
    command = parts[0].lower()
    rest = parts[1].strip().lower() if len(parts) > 1 else ""
    if command in TERMINAL_ONLY_COMMANDS:
        return False, "terminal_only"
    argument = TERMINAL_ONLY_ARGUMENTS.get(command)
    if argument is not None and rest.split()[:1] == [argument]:
        return False, "terminal_only"
    if command not in BROWSER_COMMANDS:
        return False, "invalid"
    return True, ""


class MissionBridge:
    """The browser's side of the inbox, cancel and approvals. Called on the server's loop thread.

    `busy` is set while the main thread is doing anything but waiting at the prompt; a command sent
    then is refused rather than queued behind work the person in the browser cannot see the end of.
    `take` (main thread) and `submit` share a lock, so a command is never accepted in the moment
    between the loop taking the previous one and marking itself busy."""

    def __init__(self, inbox: queue.Queue, cancel: Callable[[], bool], broker: ConfirmationBroker) -> None:
        self.inbox = inbox
        self._cancel = cancel
        self.broker = broker
        self.busy = threading.Event()
        self._lock = threading.Lock()

    def submit(self, command_id: str, text: str) -> SubmitResult:
        """Check one command and hand it to the loop. Never raises: both arguments come from the browser."""
        if not isinstance(command_id, str) or not 0 < len(command_id) <= MAX_COMMAND_ID:
            return SubmitResult.INVALID
        if not isinstance(text, str):
            return SubmitResult.INVALID
        if len(text) > MAX_TEXT:
            return SubmitResult.TOO_LONG
        line = normalise(text)
        allowed, reason = browser_command_policy(line)
        if not allowed:
            return SubmitResult.TERMINAL_ONLY if reason == "terminal_only" else SubmitResult.INVALID
        with self._lock:
            if self.busy.is_set():
                return SubmitResult.BUSY
            try:
                self.inbox.put_nowait((command_id, line))
            except queue.Full:
                return SubmitResult.QUEUE_FULL
        return SubmitResult.ACCEPTED

    def take(self) -> tuple[str, str] | None:
        """(command_id, line) for the loop, which is busy from then on; None when nothing is waiting."""
        with self._lock:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                return None
            self.busy.set()
            return item

    def cancel(self) -> bool:
        """Cancel the request in progress like Ctrl+C (and stop speaking). True if one was cancelled."""
        return bool(self._cancel())

    def answer(self, confirm_id: Any, digest: Any, approved: Any) -> str:
        """The browser's answer to the open confirmation, passed on exactly as received."""
        return self.broker.answer_from_browser(confirm_id, digest, approved)
