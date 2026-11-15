"""The terminal interface: chat loop, commands, confirmations and voice turns."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import os
import queue
import re
import select
import signal
import sys
import threading
import time
from typing import Callable
from typing import TYPE_CHECKING

from config.settings import VERSION
from core.audit import clean_text, log, reveal_invisible, reveal_tracked, safe_display
from core.events import BUS, SCRUB_INPUT_LIMIT, emit, scrub
from core.orchestrator import run_agent
from core.policy import ConfirmationRequest, Permission
from core.session import Conversation
from intelligence.groq_client import JarvisAPIError
from memory.json_store import MemoryStoreError
from memory.task_store import MAX_TASKS, TaskStoreError, count_done_steps
from voice.input import SpeechInputError
from voice.wake import is_stop_phrase

if TYPE_CHECKING:
    from core.orchestrator import AgentContext
    from mission_control.bridge import MissionBridge
    from memory.json_store import MemoryStore
    from memory.task_store import TaskStore
    from voice.input import SpeechInput
    from voice.output import VoiceEngine
    from voice.wake import WakeWordListener


# Matches the review limit in tools/developer.py, so an approved change is always shown in full.
MAX_DETAIL_LINES = 400


# Line kinds of a review, and the colour each is printed in (context lines are printed as they are).
_KIND_COLOURS = {"add": "green", "del": "red", "hunk": "cyan"}


@dataclasses.dataclass(frozen=True)
class ConfirmationView:
    """Exactly what a confirmation shows, built once so the terminal and Mission Control show the same
    thing, and so an approval from the browser can be bound to it by `digest`.

    `operation`, `affected_paths` and `lines` are already revealed (invisible characters as \\u{...});
    `tool` and `reason` come from JARVIS itself, not the model, and are printed as they are, as before.

    A revealed character and the same six characters typed in the text read alike, so `revealed`
    records where each revealed one starts in `lines` (line index, offset in UTF-16 code units, as the
    browser indexes strings), and `literal_escapes` counts the "\\u{" typed in the details themselves."""

    label: str
    tool: str
    operation: str
    affected_paths: tuple[str, ...]
    reason: str
    hidden: int
    lines: tuple[tuple[str, str], ...]  # (kind, text): kind is add | del | hunk | ctx
    omitted: int
    revealed: tuple[tuple[int, int], ...] = ()
    literal_escapes: int = 0

    def canonical(self) -> bytes:
        """Canonical JSON of every field: any change to what is shown changes these bytes."""
        return json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")

    def digest(self, key: bytes) -> str:
        """HMAC-SHA256 of `canonical()` under a secret key. Keyed, because the digest is published next to
        a redacted copy of the view: a plain hash would let anyone holding that copy test guesses for
        the redacted text offline."""
        return hmac.new(key, self.canonical(), hashlib.sha256).hexdigest()

    def as_payload(self) -> dict:
        """The view as JSON types, in the shape of the confirm.request event (before scrubbing)."""
        return {"tool": self.tool, "label": self.label, "operation": self.operation,
                "affected_paths": list(self.affected_paths), "reason": self.reason, "hidden": self.hidden,
                "lines": [{"k": kind, "t": text} for kind, text in self.lines], "omitted": self.omitted}

    def reading_aids(self) -> dict:
        """The optional confirm.request fields that tell a revealed \\u{...} from typed text: where each
        revealed character starts (`revealed`, [line, UTF-16 offset] pairs) and how many "\\u{" the
        details contain (`literal_escapes`). Present only when there is something to mark or say."""
        aids: dict = {}
        if self.revealed:
            aids["revealed"] = [[line, offset] for line, offset in self.revealed]
        if self.literal_escapes:
            aids["literal_escapes"] = self.literal_escapes
        return aids


def _line_kind(line: str) -> str:
    if line.startswith("+") and not line.startswith("+++"):
        return "add"
    if line.startswith("-") and not line.startswith("---"):
        return "del"
    if line.startswith("@@"):
        return "hunk"
    return "ctx"


@dataclasses.dataclass(frozen=True)
class _Review:
    hidden: int
    lines: tuple[tuple[str, str], ...]
    omitted: int
    revealed: tuple[tuple[int, int], ...]
    literal_escapes: int


def _marks(shown: tuple[tuple[str, str], ...], starts: list[int]) -> tuple[tuple[int, int], ...]:
    """(line index, UTF-16 offset) of each revealed character in the lines shown. `starts` are code
    point offsets into the whole revealed text, in order. Each line is walked once (characters past
    U+FFFF count twice in UTF-16), so a line full of revealed characters stays linear."""
    marks: list[tuple[int, int]] = []
    pending = iter(starts)
    start = next(pending, None)
    line_start = 0
    for index, (_, line) in enumerate(shown):
        if start is None:
            break
        line_end = line_start + len(line)
        counted, units = 0, 0  # code points of this line walked so far, and their length in UTF-16
        while start is not None and start < line_end:
            offset = start - line_start
            units += offset - counted + sum(1 for char in line[counted:offset] if char > "\uffff")
            counted = offset
            marks.append((index, units))
            start = next(pending, None)
        line_start = line_end + 1  # past the "\n"
    return tuple(marks)


def _review_lines(details: str) -> _Review:
    """The revealed review: hidden characters, the first MAX_DETAIL_LINES lines with their kinds, lines
    left out, where each revealed character starts in the lines shown, and how many "\\u{" were typed."""
    # Reveal before anything is stripped, so control characters are shown rather than silently removed.
    cleaned = clean_text(details)
    revealed, hidden, starts = reveal_tracked(cleaned)
    # Every line break but "\n" is revealed, so splitlines() here splits on "\n" alone.
    lines = revealed.splitlines()
    shown = tuple((_line_kind(line), line) for line in lines[:MAX_DETAIL_LINES])
    return _Review(hidden, shown, max(0, len(lines) - MAX_DETAIL_LINES), _marks(shown, starts),
                   cleaned.count("\\u{"))


LITERAL_NOTE = "!! {count} literal \\u{{...}} sequence(s) in the text itself, not hidden characters"


def _print_review(hidden: int, lines: tuple[tuple[str, str], ...], omitted: int, literal_escapes: int = 0) -> None:
    if hidden:
        print("   " + style(f"!! {hidden} invisible or text-direction character(s), shown as \\u{{...}}", "yellow"))
    if literal_escapes:
        print("   " + style(LITERAL_NOTE.format(count=literal_escapes), "yellow"))
    for kind, line in lines:
        colour = _KIND_COLOURS.get(kind)
        print("   " + (style(line, colour) if colour else line))
    if omitted:
        print("   " + style(f"... {omitted} more lines not shown", "dim"))


def build_confirmation_view(request: ConfirmationRequest) -> ConfirmationView:
    """What `print_confirmation` shows for this request."""
    label = "SENSITIVE ACTION" if request.permission is Permission.SENSITIVE_ACTION else "ACTION"
    review = _review_lines(request.details) if request.details else _Review(0, (), 0, (), 0)
    return ConfirmationView(
        label=label,
        tool=request.tool_name,
        operation=reveal_invisible(clean_text(request.operation))[0],
        affected_paths=tuple(reveal_invisible(clean_text(path))[0] for path in request.affected_paths),
        reason=request.reason,
        hidden=review.hidden,
        lines=review.lines,
        omitted=review.omitted,
        revealed=review.revealed,
        literal_escapes=review.literal_escapes,
    )


def print_confirmation(view: ConfirmationView, prompt_hint: str = "") -> None:
    """Print a confirmation; `prompt_hint`, if given, is printed last without a newline, as a prompt."""
    print(f"\n{style('!! Confirmation required', 'yellow')} [{view.label}: {view.tool}]")
    print(f"   Operation: {view.operation}")
    for path in view.affected_paths:
        print(f"   Affects:   {path}")
    if view.reason:
        print(f"   Why asked: {view.reason}")
    _print_review(view.hidden, view.lines, view.omitted, view.literal_escapes)
    if prompt_hint:
        print(prompt_hint, end="", flush=True)


def print_details(details: str) -> None:
    """Show a confirmation's details (usually a unified diff), coloured and capped in length.
    Invisible and text-direction characters are shown as \\u{...} so nothing is hidden."""
    review = _review_lines(details)
    _print_review(review.hidden, review.lines, review.omitted, review.literal_escapes)


CONFIRM_PROMPT = "   Type 'yes' to proceed, anything else cancels: "


def terminal_confirm(request: ConfirmationRequest) -> bool:
    """Ask the person at the keyboard to approve one specific operation."""
    if not sys.stdin.isatty():
        return False
    print_confirmation(build_confirmation_view(request))
    try:
        answer = input(CONFIRM_PROMPT)
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in ("yes", "y")


_COLOURS = {"cyan": "36", "yellow": "33", "red": "31", "dim": "2", "bold": "1", "green": "32"}


def style(text: str, colour: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{_COLOURS[colour]}m{text}\033[0m"


# "continue", "resume" or "carry on", alone, with an optional "Jarvis" before or after.
_RESUME_PHRASE = re.compile(r"^\W*(?:jarvis\W+)?(?:continue|resume|carry\s+on)(?:\W+jarvis)?\W*$", re.IGNORECASE)


UNTRUSTED_PLAN_BANNER = "plan written after reading web or file content: check it before continuing"


def is_resume_phrase(text: str) -> bool:
    return bool(_RESUME_PHRASE.match(text))


def task_topic(task: dict) -> str:
    """What a task is about, in words: used to tell whether resuming it needs the developer tools."""
    return " ".join([task["goal"], *(s["text"] for s in task["steps"]), *task["files_changed"]])


def resume_prompt(task: dict) -> str:
    # The goal is quoted as a JSON string, so a quote inside it cannot end the quotation early.
    goal = json.dumps(task["goal"], ensure_ascii=False)
    return (f"Continue the unfinished task (saved goal, written earlier): {goal}. "
            "Check the current state first (files may have changed since), then do the remaining steps.")


EXIT_WORDS = {"exit", "quit", "/exit", "/quit", "goodbye", "goodbye jarvis", "bye jarvis"}

# Words that mean a request is about code; mentioning a workspace project's name counts too.
_DEVELOPER_WORDS = re.compile(
    r"\b(code|codebase|source|repo|repos|repository|git|diff|commit|commits|branch|test|tests|testing|lint|"
    r"linter|bug|bugs|fix|fixes|refactor|function|method|class|module|file|files|folder|project|projects|"
    r"build|compile|error|exception|stack ?trace|traceback|changed|changes|readme|architecture|implement|"
    r"rails|ruby|python|flutter|dart|npm|node|javascript|typescript|expo|react)\b", re.IGNORECASE)


def looks_like_developer_request(text: str, project_names: set[str] = frozenset()) -> bool:
    if _DEVELOPER_WORDS.search(text):
        return True
    # A sentence's closing full stop is not part of the project name.
    words = {word.rstrip(".") for word in re.findall(r"[a-z0-9_.-]+", text.lower())}
    return bool(words & project_names)


HELP_TEXT = """Talk to JARVIS in plain English, for example:
  search for the latest Python release      what time is it?
  remember that I prefer concise answers    what do you remember about me?
  open youtube and search for lo-fi music   how is the system doing?
  open vscode                               show git status for my-project

Commands:
  /help              this help             /tools          list tools and permissions
  /memory [query]    show stored memories  /forget <id>    delete a memory (asks first)
  /memory reset      back up a corrupted memory file and start fresh
  /tasks             list unfinished and recent tasks   /resume [id]    carry on with a task ("continue" works too)
  /abandon [id]      give a task up        /tasks clear    forget finished and abandoned tasks
  /tasks reset       back up a corrupted task file and start fresh
  /voice on|off      toggle spoken replies /stop           stop speaking
  /clear             forget this session's conversation context
  /status            show configuration    exit            leave JARVIS
  /talk              speak one message     /listen on|off  push-to-talk: Enter on an empty line = speak
  /wake on|off       hands-free: say "Hey JARVIS", then your request
  /dev on|off|auto   developer tools (files, git, edits, tests); auto (the default) switches them on when you ask about code
Saying "Hey JARVIS" while JARVIS is talking or working interrupts it; "Hey JARVIS, stop" just stops it.
Press Ctrl+C while JARVIS is working to cancel the current request.
Confirmation prompts must always be typed in the terminal, or answered in Mission Control; a spoken yes never approves."""


def describe_time_since(iso: str, now: dt.datetime | None = None) -> str:
    """How long ago an ISO timestamp was, in words: "a moment ago", "3 hours ago", "yesterday"..."""
    try:
        then = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "a while ago"
    if then.tzinfo is None:
        then = then.astimezone()  # no offset recorded: it was local time
    now = now or dt.datetime.now().astimezone()
    seconds = (now - then).total_seconds()
    if seconds < 120:  # also a clock that was set back
        return "a moment ago"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    days = hours // 24
    return "yesterday" if days == 1 else f"{days} days ago"


def greeting() -> str:
    hour = dt.datetime.now().hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
    return f"Good {part}, Sir. Systems are ready."


# CSI sequences (colours, cursor moves) and two-character escapes, as style() and terminals write them.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


class _OutputTee:
    """stdout for a command sent from Mission Control: everything still reaches the terminal, and a
    copy (bounded, like scrub's own input) is kept for the browser. isatty and the rest come from the
    real stream, so style() colours the terminal exactly as before."""

    def __init__(self, target) -> None:
        self._target = target
        self._parts: list[str] = []
        self._size = 0

    def write(self, text: str) -> int:
        written = self._target.write(text)
        room = SCRUB_INPUT_LIMIT - self._size
        if room > 0 and isinstance(text, str):
            kept = text[:room]
            self._parts.append(kept)
            self._size += len(kept)
        return written if isinstance(written, int) else len(text)

    def flush(self) -> None:
        self._target.flush()

    def captured(self) -> str:
        return "".join(self._parts)

    def __getattr__(self, name: str):
        return getattr(self._target, name)


class JarvisCLI:
    def __init__(self, ctx: AgentContext, voice: VoiceEngine, memory: MemoryStore,
                 speech: SpeechInput | None = None, listen: bool = False,
                 wake_factory: Callable[["JarvisCLI"], WakeWordListener] | None = None, wake: bool = False):
        self.ctx = ctx
        self.voice = voice
        self.memory = memory
        self.speech = speech
        self.listen_mode = False
        self._speech_disclosed = False
        self._mic_hint_shown = False
        self.conversation = Conversation(max_turns=ctx.config.history_turns)
        self.last_seen = ""  # "<when> (<iso timestamp>)" of the last saved exchange, set by restore_conversation
        self._restored_when = ""
        # "continue" resumes a task only as the session's first message, or right after a request that
        # ended by pausing that task; otherwise it is an ordinary reply to the conversation.
        self._handled_message = False
        self._paused_by_last_request: str | None = None
        self.developer_mode = False  # offer the developer tools (files, git, edits, tests) to the model
        self._dev_auto = True  # switch developer mode on by itself when a message is about code
        self.wake_factory = wake_factory
        self.wake: WakeWordListener | None = None
        self._want_wake = wake
        self._wake_events: queue.Queue = queue.Queue()
        self._busy = False
        self._interrupt_pending = False
        self._interrupt_lock = threading.Lock()
        # Where the line being handled came from, for Mission Control (set by the voice paths, reset per step).
        self._request_source = "terminal"
        self._command_id: str | None = None  # Mission Control's id for the browser line being handled
        # Mission Control's bridge and its inbox (attach_mission); None while Mission Control is off.
        self._bridge: MissionBridge | None = None
        self._inbox: queue.Queue | None = None
        # A wake word heard at a confirmation prompt cancels the whole request, not just the prompt.
        ctx.policy.cancel_requested = lambda: self._interrupt_pending
        if listen:
            self._set_listen(True, quiet=True)

    @property
    def tasks(self) -> TaskStore | None:
        return self.ctx.tasks

    # -- hands-free ------------------------------------------------------------
    @property
    def hands_free(self) -> bool:
        return self.wake is not None and self.wake.running

    def start_wake(self) -> bool:
        if self.hands_free:
            return True
        self.stop_wake()  # reap a listener whose thread died, so its microphone is closed
        problem = self._speech_unavailable()
        if problem is None and self.wake_factory is None:
            problem = "Hands-free mode is not configured."
        if problem:
            print(style(f"Hands-free unavailable: {problem}", "yellow"))
            return False
        try:
            self.wake = self.wake_factory(self)
            self.wake.start()
        except (SpeechInputError, OSError) as exc:
            self.wake = None
            print(style(f"Hands-free unavailable: {exc}", "yellow"))
            return False
        print(style('Hands-free on: listening on this computer for "Hey JARVIS". Nothing leaves this machine '
                    "until you say it; then only your request (with about a second of audio from just "
                    "before it) is sent to Groq for transcription. "
                    "/wake off to stop.", "dim"))
        self._speech_disclosed = True
        emit("voice.input", mode="wake", phase="armed")
        return True

    def stop_wake(self) -> None:
        if self.wake is not None:
            self.wake.stop()
            self.wake = None
            emit("voice.input", mode="ptt" if self.listen_mode else "off", phase="stopped")
        # Drop anything the listener captured before it stopped: "the microphone is closed" must
        # also mean nothing recorded earlier is uploaded afterwards.
        while True:
            try:
                self._wake_events.get_nowait()
            except queue.Empty:
                break

    def attach_mission(self, bridge: MissionBridge | None) -> None:
        """Let Mission Control send lines to this loop through `bridge` (None detaches it again).

        Its inbox is a queue of its own: `stop_wake` empties `_wake_events`, and that must never drop
        (or deliver) a browser command."""
        self._bridge = bridge
        self._inbox = bridge.inbox if bridge is not None else None

    def request_cancel(self) -> bool:
        """Stop speaking, and cancel the request in progress exactly like Ctrl+C. True if one was cancelled.

        For other threads only (the wake-word listener, Mission Control): the SIGINT is delivered to the
        main thread, wherever the request is, and `_interrupt_pending` tells the loop it was not the user
        leaving. The main thread itself never calls this."""
        self.voice.stop()  # barge-in: stop talking at once
        with self._interrupt_lock:
            if not self._busy:
                return False
            self._interrupt_pending = True
            os.kill(os.getpid(), signal.SIGINT)
            return True

    def _on_wake(self) -> None:
        """Runs on the listener thread the moment the wake word is heard."""
        interrupted = self.voice.is_speaking
        if self.request_cancel():
            interrupted = True
        emit("voice.input", mode="wake", phase="wake_detected")
        if self.ctx.config.wake_chime:
            self.voice.chime()
        emit("voice.input", mode="wake", phase="collecting")  # the listener starts collecting as this returns
        self._wake_events.put(("status", "[wake] Interrupted. Listening..." if interrupted else "[wake] Listening..."))

    def _on_command(self, pcm: bytes) -> None:
        self._wake_events.put(("command", pcm))

    def _on_status(self, message: str) -> None:
        if BUS.enabled:
            # Either the listener gave up, or a heard-nothing timeout put it back to waiting for the wake word.
            ended = message.startswith("Hands-free listening stopped")
            emit("voice.input", mode="off" if ended else "wake", phase="stopped" if ended else "armed",
                 message=message)
        self._wake_events.put(("status", message))

    def _consume_interrupt(self) -> bool:
        with self._interrupt_lock:
            pending, self._interrupt_pending = self._interrupt_pending, False
            return pending

    def _voice_command(self, pcm: bytes) -> str:
        """Transcribe a hands-free command. Returns "" when there is nothing to send."""
        emit("voice.input", mode="wake", phase="transcribing")
        try:
            return self._transcribe_command(pcm)
        finally:
            if BUS.enabled and self.hands_free:  # back to waiting for the wake word
                emit("voice.input", mode="wake", phase="armed")

    def _transcribe_command(self, pcm: bytes) -> str:
        print(style("  Transcribing...", "dim"))
        try:
            text = safe_display(self.speech.transcribe_pcm(pcm)).strip()
        except SpeechInputError as exc:
            print(f"  {exc}")
            return ""
        except JarvisAPIError as exc:
            print(f"  Transcription failed: {exc}")
            return ""
        if not text:
            print("  I didn't catch a request after the wake word.")
            return ""
        if is_stop_phrase(text):
            print(f"{style('You (voice):', 'green')} {text}\n  Stopped.")
            return ""
        print(f"{style('You (voice):', 'green')} {text}")
        return text

    @contextlib.contextmanager
    def _busy_scope(self):
        """Marks work the wake word may interrupt (a request, or transcribing a spoken one)."""
        if self._bridge is not None:  # Mission Control: commands sent now are refused as busy
            self._bridge.busy.set()
        with self._interrupt_lock:
            self._busy = True
        try:
            yield
        finally:
            with self._interrupt_lock:
                self._busy = False
        # Reached only when nothing escaped: an interruption swallowed inside the work must not
        # linger and eat the user's next real Ctrl+C.
        with self._interrupt_lock:
            self._interrupt_pending = False

    def _read_line(self) -> str | None:
        """Typed input, or whichever comes first: a typed line, a spoken command (hands-free mode) or a
        line sent from Mission Control (while it is on).

        Returns None when a hands-free turn produced nothing to send, which is distinct from the
        empty line a user types to start push-to-talk.
        """
        prompt = style("You: ", "green")
        bridge = self._bridge
        if bridge is None and not self.hands_free and self._wake_events.empty():
            return clean_text(input(prompt)).strip()
        listening = self.hands_free  # only a listener that was running can stop while we wait
        if bridge is not None:
            bridge.busy.clear()  # back at the prompt: the browser may send a command now
        print(prompt, end="", flush=True)
        while True:
            try:
                kind, payload = self._wake_events.get_nowait()
            except queue.Empty:
                kind = payload = None
            if kind == "status":
                print(f"\n  {payload}")
                print(prompt, end="", flush=True)
                continue
            if kind == "command":  # (stop_wake drains the queue, so nothing captured before /wake off gets here)
                print()
                with self._busy_scope():
                    text = self._voice_command(payload)
                    if text:
                        self._request_source = "voice"
                    return text or None
            if bridge is not None:
                item = bridge.take()  # marks the bridge busy, so the next browser command is refused
                if item is not None:
                    self._command_id, line = item[0], clean_text(item[1]).strip()
                    self._request_source = "browser"
                    print(f"\n{style('You (browser):', 'green')} {safe_display(line)}")
                    return line
            if not self.hands_free and (bridge is None or listening):  # the listener stopped (error or /wake off)
                print()
                return None
            if select.select([sys.stdin], [], [], 0.1)[0]:
                raw = sys.stdin.readline()
                if not raw:
                    raise EOFError
                if bridge is not None:
                    bridge.busy.set()
                return clean_text(raw).strip()

    def _speech_unavailable(self) -> str | None:
        if self.speech is None:
            return "Speech input is not configured."
        ok, reason = self.speech.availability()
        return None if ok else reason

    def _set_listen(self, enabled: bool, quiet: bool = False) -> None:
        problem = self._speech_unavailable() if enabled else None
        if problem:
            self.listen_mode = False
            print(style(f"Speech input unavailable: {problem}", "yellow"))
            return
        self.listen_mode = enabled
        if not quiet:
            print("  Push-to-talk on: press Enter on an empty line to speak." if enabled
                  else "  Push-to-talk off. /talk still records a single message.")

    def voice_turn(self) -> str:
        """Record and transcribe one spoken message. Returns "" when there is nothing to send."""
        problem = self._speech_unavailable()
        if problem:
            print(f"  {problem}")
            return ""
        if not self._speech_disclosed:
            print(style(f"  Privacy: {self.speech.transcriber.disclosure}", "dim"))
            self._speech_disclosed = True
        self.voice.stop()  # never record JARVIS's own voice
        if self.wake is not None:
            self.wake.paused = True  # one microphone user at a time
        emit("voice.input", mode="ptt", phase="recording")
        try:
            text = self.speech.listen()
        except SpeechInputError as exc:
            print(f"  {exc}")
            return ""
        except JarvisAPIError as exc:
            print(f"  Transcription failed: {exc}")
            return ""
        except Exception as exc:  # noqa: BLE001 - speech problems must not end the session
            log.exception("Speech input failed")
            print(style(f"  Speech input failed ({type(exc).__name__}); details are in the log.", "red"))
            return ""
        finally:
            if self.wake is not None:
                self.wake.paused = False
            emit("voice.input", mode="ptt", phase="idle")
        if text is None:  # cancelled
            return ""
        text = safe_display(text).strip()
        if not text:
            print("  I didn't catch anything. Press Enter to try again, or type your message.")
            return ""
        print(f"{style('You (voice):', 'green')} {text}")
        return text

    def say(self, text: str, speak: bool = True) -> None:
        text = safe_display(text)
        print(f"\n{style('JARVIS:', 'cyan')} {text}\n")
        if BUS.enabled:  # scrubbed before the cut, so a secret split at 4000 characters cannot leak
            emit("assistant.message", text=scrub(text, max_str=4000))
        if speak:
            self.voice.say(text)

    def restore_conversation(self) -> int:
        """Reload the last `restore_turns` saved exchanges (already redacted) into this session.
        They are not saved again. Returns how many turns were restored."""
        entries = self.memory.recent_conversations(self.ctx.config.restore_turns)
        restored = self.conversation.restore(entries)
        if restored:
            stamp = entries[-1].get("timestamp")
            try:  # re-render a valid timestamp; anything else is dropped, never sent to the model
                stamp = dt.datetime.fromisoformat(stamp).isoformat()
            except (TypeError, ValueError):
                stamp = ""
            self._restored_when = describe_time_since(stamp)
            self.last_seen = f"{self._restored_when} ({stamp})" if stamp else self._restored_when
        return restored

    def pause_stale_tasks(self, reason: str = "JARVIS was closed before it finished", keep_time: bool = True) -> None:
        """A task still active outside a request is left over from a session that ended or crashed:
        pause it, so the model does not act on it until the user says to continue.

        `keep_time` keeps the task's last activity as its time, so the banner does not say "a moment ago"
        about a task that crashed days ago."""
        if self.tasks is None:
            return
        try:
            for task in self.tasks.unfinished():
                if task["status"] == "active":
                    self.tasks.pause(task["id"], reason, keep_time=keep_time)
        except TaskStoreError as exc:
            log.warning("Could not pause leftover tasks: %s", exc)

    def _describe_task(self, task: dict) -> str:
        steps = f"{count_done_steps(task)}/{len(task['steps'])} steps done, " if task["steps"] else ""
        reason = f": {task['paused_reason']}" if task.get("paused_reason") else ""
        if task["status"] == "active":
            state = "active"
        elif task["status"] == "paused":
            state = f"paused {describe_time_since(task['updated_at'])}{reason}"
        else:
            state = f"{task['status']} {describe_time_since(task['updated_at'])}"
        note = f" ({UNTRUSTED_PLAN_BANNER})" if task.get("untrusted_plan") else ""
        return safe_display(f"{task['goal']} ({steps}{state}){note}")

    def banner(self) -> None:
        print(style("\nJARVIS MARK 50", "bold") + style(f"  v{VERSION}", "dim"))
        print("Personal AI Assistant")
        voice = f"spoken replies on ({self.voice.voice})" if self.voice.enabled else "replies in text only"
        print(style(f"Model: {self.ctx.config.model} | {voice} | permissions: {self.ctx.policy.mode} | "
                    "/help for commands", "dim"))
        if self._restored_when:
            print(style(f"Picking up where we left off (last talked {self._restored_when}).", "dim"))
        if self.voice.unavailable_reason:
            print(style(f"Voice unavailable: {self.voice.unavailable_reason}", "yellow"))
        if self.listen_mode:
            print(style("Push-to-talk on: press Enter on an empty line to speak, Enter again to stop.", "dim"))
        if self._want_wake:
            self._want_wake = False
            self.start_wake()
        if not self.listen_mode and not self.hands_free:  # also when --wake was asked for but could not start
            print(style('Microphone off. To talk: /wake on (say "Hey JARVIS") or /listen on (push-to-talk).', "dim"))
        if self.tasks is not None:
            if self.tasks.corruption_error:
                print(style(f"Task warning: {self.tasks.corruption_error} Running without saved tasks; "
                            "type /tasks reset to back it up and start fresh.", "yellow"))
            unfinished = self.tasks.unfinished()
            if unfinished:
                print(style(f'Unfinished task: {self._describe_task(unfinished[0])}. Say "continue" or type /tasks.',
                            "yellow"))
        if self.memory.corruption_error:
            print(style(f"Memory warning: {self.memory.corruption_error} Running without saved memory; "
                        "type /memory reset to back it up and start fresh.", "yellow"))

    def handle_command(self, line: str) -> None:
        parts = line.split(maxsplit=1)
        command, rest = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        if command == "/help":
            print(HELP_TEXT)
        elif command == "/tools":
            for tool in self.ctx.registry.tools():
                level = tool.permission.value if isinstance(tool.permission, Permission) else "depends on arguments"
                print(f"  {tool.name:<22} {level}")
        elif command == "/memory" and rest == "reset":
            self._reset_memory()
        elif command == "/memory":
            self._show_memory(rest)
        elif command == "/forget":
            self._forget(rest)
        elif command == "/tasks" and rest == "reset":
            self._reset_tasks()
        elif command == "/tasks" and rest == "clear":
            self._clear_tasks()
        elif command == "/tasks":
            self._show_tasks()
        elif command == "/resume":
            self._resume_command(rest)
        elif command == "/abandon":
            self._abandon(rest)
        elif command == "/voice":
            self._toggle_voice(rest)
        elif command == "/stop":
            self.voice.stop()
        elif command == "/dev":
            if rest in ("on", "off", "auto"):
                self.developer_mode = rest == "on"
                self._dev_auto = rest == "auto"  # /dev off stays off; /dev auto switches on when you mention code
            state = "on" if self.developer_mode else ("off, switching on when you ask about code" if self._dev_auto
                                                      else "off until you type /dev on or /dev auto")
            print(f"  Developer tools are {state}." + ("" if rest in ("on", "off", "auto") else
                                                         " Use /dev on, /dev off or /dev auto."))
        elif command == "/listen":
            if rest in ("on", "off"):
                self._set_listen(rest == "on")
            else:
                print(f"  Push-to-talk is {'on' if self.listen_mode else 'off'}. Use /listen on or /listen off.")
        elif command == "/wake":
            if rest == "on":
                self.start_wake()
            elif rest == "off":
                self.stop_wake()
                print("  Hands-free off; the microphone is closed.")
            else:
                print(f"  Hands-free is {'on' if self.hands_free else 'off'}. Use /wake on or /wake off.")
        elif command == "/clear":
            self.conversation.clear()
            print("Session context cleared. Saved memories are unaffected.")
        elif command == "/status":
            cfg = self.ctx.config
            for label, value in (("Model", cfg.model), ("Voice", f"{cfg.voice} ({'on' if self.voice.enabled else 'off'})"),
                                 ("Permission mode", self.ctx.policy.mode), ("Memory file", cfg.memory_path),
                                 ("Task file", f"{cfg.tasks_path} ({len(self.tasks.unfinished()) if self.tasks else 0} "
                                               "unfinished)"),
                                 ("Workspace", cfg.workspace), ("Notes", cfg.notes_dir), ("Logs", cfg.log_dir),
                                 ("Max tool rounds", f"{cfg.max_tool_rounds} ({cfg.max_dev_tool_rounds} with developer tools)"),
                                 ("Developer tools", "on" if self.developer_mode else
                                  ("off (on when you ask about code)" if self._dev_auto else "off (until /dev on or /dev auto)")),
                                 ("Speech input", f"{cfg.stt_backend}/{cfg.stt_model}, push-to-talk "
                                                  f"{'on' if self.listen_mode else 'off'}"),
                                 ("Hands-free", f"{'on' if self.hands_free else 'off'} ({cfg.wake_model}, "
                                                f"threshold {cfg.wake_threshold})")):
                print(f"  {label:<16} {value}")
        else:
            print(f"Unknown command {command}. Type /help.")

    def _show_memory(self, query: str) -> None:
        profile = self.memory.profile()
        if profile["user_name"] or profile["location"]:
            print(f"  Profile: name={profile['user_name'] or '-'}, location={profile['location'] or '-'}")
        facts = self.memory.search_memory(query, limit=50)
        if not facts:
            print("  No matching memories." if query else "  No memories stored yet.")
        for fact in facts:
            print(f"  [{fact['id']}] {fact['text']}  {style(str(fact.get('created_at') or ''), 'dim')}")

    def _forget(self, fact_id: str) -> None:
        fact = self.memory.find_fact(fact_id)
        if not fact:
            print("  Usage: /forget <id>  (see /memory for ids)")
            return
        request = ConfirmationRequest("forget_fact", Permission.SENSITIVE_ACTION,
                                      f"Permanently delete memory {fact_id}: {fact['text']!r}", [str(self.memory.path)])
        if not terminal_confirm(request):
            print("  Cancelled; nothing was deleted.")
            return
        try:
            self.memory.forget_fact(fact_id)
            print("  Deleted.")
        except MemoryStoreError as exc:
            print(f"  {exc}")

    def _reset_memory(self) -> None:
        if self.memory.writable:
            print("  Memory is healthy; nothing to reset. Use /forget <id> to delete individual memories.")
            return
        request = ConfirmationRequest("memory_reset", Permission.SENSITIVE_ACTION,
                                      "Move the corrupted memory file aside and start an empty memory",
                                      [str(self.memory.path)])
        if not terminal_confirm(request):
            print("  Cancelled.")
            return
        try:
            backup = self.memory.reset_corrupted()
            print(f"  Done. The old file was kept at {backup}")
        except (OSError, MemoryStoreError) as exc:
            print(f"  Could not reset memory: {exc}")

    # -- tasks -----------------------------------------------------------------
    def _tasks_available(self) -> bool:
        if self.tasks is None:
            print("  Task tracking is not available in this session.")
        return self.tasks is not None

    def _show_tasks(self) -> None:
        if not self._tasks_available():
            return
        if self.tasks.corruption_error:
            print(style(f"  {self.tasks.corruption_error} Type /tasks reset to back it up and start fresh.", "yellow"))
        unfinished = self.tasks.unfinished()
        finished = [t for t in self.tasks.recent(10) if t["status"] not in ("active", "paused")]
        if not unfinished and not finished:
            print("  No tasks yet.")
            return
        if unfinished:
            print("  Unfinished (say \"continue\", or /resume <id>):")
            for task in unfinished:
                print(f"    [{task['id']}] {self._describe_task(task)}")
        if finished:
            print("  Recent:")
            for task in finished:
                print(f"    [{task['id']}] {self._describe_task(task)}")

    def _default_task(self) -> dict | None:
        """The task meant when none is named: the active one, else the most recently updated paused one."""
        return self.tasks.active() or next(iter(self.tasks.unfinished()), None)

    def _pick_task(self, task_id: str, verb: str) -> dict | None:
        """The task named by `task_id`, or the default task when no id was given."""
        if task_id:
            task = self.tasks.get(task_id)
            if task is None:
                print(f"  No task with id {safe_display(task_id)!r}. /tasks lists them.")
            return task
        task = self._default_task()
        if task is None:
            print(f"  There is no unfinished task to {verb}.")
        return task

    def _resume_command(self, task_id: str) -> None:
        if not self._tasks_available():
            return
        task = self._pick_task(task_id, "resume")
        if task is None:
            return
        prompt = self._resume_task(task, reopen_abandoned=bool(task_id))  # only a task named by id comes back
        if prompt:
            self._run_message(prompt, topic=task_topic(task), said=f"/resume {task_id}".strip())

    def _resume_task(self, task: dict, reopen_abandoned: bool = False) -> str | None:
        """Make the task active again. Returns the message to send the model, or None if it cannot resume."""
        try:
            task = self.tasks.resume(task["id"], reopen_abandoned=reopen_abandoned)
        except TaskStoreError as exc:
            print(f"  {exc}")
            return None
        print(style(f"  Resuming: {safe_display(task['goal'])}", "dim"))
        return resume_prompt(task)

    def _abandon(self, task_id: str) -> None:
        if not self._tasks_available():
            return
        task = self._pick_task(task_id, "abandon")
        if task is None:
            return
        try:
            task = self.tasks.abandon(task["id"])
            print(f"  Abandoned: {safe_display(task['goal'])}")
        except TaskStoreError as exc:
            print(f"  {exc}")

    def _clear_tasks(self) -> None:
        if not self._tasks_available():
            return
        try:
            finished = [t for t in self.tasks.recent(MAX_TASKS) if t["status"] in ("done", "abandoned")]
            for task in finished:
                self.tasks.forget(task["id"])
        except TaskStoreError as exc:
            print(f"  {exc}")
            return
        print(f"  Forgot {len(finished)} finished or abandoned task(s)." if finished
              else "  No finished or abandoned tasks to forget.")

    def _reset_tasks(self) -> None:
        if not self._tasks_available():
            return
        if self.tasks.writable:
            print("  The task file is healthy; nothing to reset. Use /tasks clear to forget finished tasks.")
            return
        request = ConfirmationRequest("tasks_reset", Permission.SENSITIVE_ACTION,
                                      "Move the corrupted task file aside and start an empty task list",
                                      [str(self.tasks.path)])
        if not terminal_confirm(request):
            print("  Cancelled.")
            return
        try:
            backup = self.tasks.reset_corrupted()
            print(f"  Done. The old file was kept at {backup}")
        except (OSError, TaskStoreError) as exc:
            print(f"  Could not reset tasks: {exc}")

    def _toggle_voice(self, arg: str) -> None:
        if arg not in ("on", "off"):
            print(f"  Voice is {'on' if self.voice.enabled else 'off'}. Use /voice on or /voice off.")
        elif self.voice.set_enabled(arg == "on"):
            print(f"  Voice on ({self.voice.voice}).")
        elif arg == "on":
            print(f"  Voice unavailable: {self.voice.unavailable_reason}")
        else:
            print("  Voice off; text only.")

    def voice_state(self) -> str:
        """What JARVIS can truthfully say about its own voice input and output right now."""
        speaking = (f"Your replies are spoken aloud ({self.voice.voice})." if self.voice.enabled
                    else "Your replies are shown as text only (not spoken).")
        if self.hands_free:
            listening = ('Hands-free is on: the user wakes you by saying "Hey JARVIS" out loud; this '
                         "message may have been spoken. Typing also works.")
        elif self.listen_mode:
            listening = "Push-to-talk is on: the user presses Enter on an empty line to speak."
        else:
            listening = ('The microphone is off; the user is typing. To talk by voice they type /wake on '
                         '(then say "Hey JARVIS") or /listen on (push-to-talk), or start with --wake.')
        return f"{speaking} {listening}"

    def _maybe_enter_developer_mode(self, text: str) -> None:
        if not self.developer_mode and self._dev_auto and looks_like_developer_request(text, self._project_names()):
            self.developer_mode = True
            print(style("  (developer tools on for this session; /dev off to hide them)", "dim"))
            emit("notice", level="info", text="Developer tools switched on for this session.")

    def _project_names(self) -> set[str]:
        try:
            return {p.name.lower() for p in self.ctx.config.workspace.expanduser().iterdir()
                    if p.is_dir() and not p.name.startswith(".")}
        except OSError:
            return set()

    def _shortcut_task(self) -> dict | None:
        """The task "continue" picks up, or None when the word belongs to the conversation instead:
        only the session's first message, or a reply to a request that ended by pausing a task, resumes."""
        if self._paused_by_last_request is not None:
            task = self.tasks.get(self._paused_by_last_request)
            return task if task is not None and task["status"] in ("active", "paused") else None
        if self._handled_message:
            return None
        return self._default_task()

    def handle_message(self, text: str, topic: str | None = None, said: str | None = None) -> None:
        """`topic` is what the message is about when `text` does not say so (a resume message), and
        `said` is what the user actually typed or spoke when `text` is a message written for them."""
        self.voice.stop()
        said = text if said is None else said
        source, self._request_source = self._request_source, "terminal"
        command_id, self._command_id = self._command_id, None
        if self.tasks is not None and is_resume_phrase(text):
            task = self._shortcut_task()  # otherwise "continue" is an ordinary message
            if task is not None:
                prompt = self._resume_task(task)
                if prompt is None:  # the reason was shown
                    return
                text, topic = prompt, task_topic(task)
        self._handled_message = True
        self._paused_by_last_request = None
        self._maybe_enter_developer_mode(topic if topic is not None else text)
        print(style("  ...", "dim"))
        with BUS.request_scope(source), self._request_events(source, said, command_id) as outcome:
            try:
                result = run_agent(self.ctx, text, self.conversation.messages(), self.conversation.summary,
                                   on_tool=lambda name: print(style(f"  > {name}", "dim")),
                                   on_retry=lambda reason, delay: print(style(f"  {reason} Retrying in {delay:.0f}s...", "dim")),
                                   tool_groups={"core", "developer"} if self.developer_mode else {"core"},
                                   voice_state=self.voice_state(), last_seen=self.last_seen,
                                   on_progress=self._show_progress)
            except KeyboardInterrupt:
                outcome["status"] = "cancelled"
                self._consume_interrupt()  # a wake-word interruption is fully handled here
                print(style("\n  Request cancelled. Any step that had already finished is not undone.", "yellow"))
                self._paused_by_last_request = self._pause_active_task("interrupted by the user")
                return
            except JarvisAPIError as exc:
                outcome.update(status="error", error_kind=exc.kind)
                log.warning("API error (%s): %s", exc.kind, exc)
                self.say(f"I'm afraid I couldn't complete that. {exc}", speak=False)
                # No tool ran, but a resumed task would otherwise stay active with no word of why it stopped.
                self._paused_by_last_request = self._pause_active_task(f"the model could not be reached: {exc}",
                                                                       record_error=True)
                return
            outcome.update(status=result.status, rounds=result.rounds)
            self._paused_by_last_request = result.paused_task
            reply = result.text
            if result.status == "incomplete":
                reply = f"{reply}\n\n(The task is incomplete.)"
            # Record the turn before speaking, so an interruption during say() cannot lose it.
            self.conversation.add(text, result.text)
            if self.ctx.config.save_conversations:  # the log keeps the user's own words, not a resume message
                self.memory.add_conversation(said, result.text)
            self.say(reply)

    def _show_progress(self, message: str) -> None:
        """A task step changed: show it in the terminal and send it to Mission Control."""
        print(style(f"  {safe_display(message)}", "dim"))
        emit("task.progress", message=message)

    @contextlib.contextmanager
    def _request_events(self, source: str, said: str, command_id: str | None = None):
        """Publish request.start, then request.end on every way out of the request (Mission Control only).

        The body records how it ended in the yielded dict; anything that escapes still ends the request,
        so the browser never shows JARVIS "processing" forever. `command_id` ties a browser request to
        the command that started it."""
        started = time.monotonic()
        outcome: dict = {}
        if BUS.enabled:  # scrub before the cut: a key split at 500 characters must not leak its head
            extra = {"command_id": command_id} if command_id else {}
            emit("request.start", source=source, text=scrub(said, max_str=500), **extra)
        try:
            yield outcome
        except KeyboardInterrupt:
            outcome.setdefault("status", "cancelled")
            raise
        except BaseException:
            outcome.setdefault("status", "error")
            outcome.setdefault("error_kind", "internal")
            raise
        finally:
            if BUS.enabled:
                fields = {"status": outcome.get("status", "error"),
                          "duration_ms": max(0, int((time.monotonic() - started) * 1000))}
                if isinstance(outcome.get("rounds"), int):
                    fields["rounds"] = outcome["rounds"]
                if outcome.get("error_kind"):
                    fields["error_kind"] = str(outcome["error_kind"])
                emit("request.end", **fields)

    def _pause_active_task(self, reason: str, record_error: bool = False) -> str | None:
        """Pause the active task, if any, and say so. Returns the id of the task paused."""
        if self.tasks is None:
            return None
        try:
            task = self.tasks.active()
            if task is not None:
                if record_error:
                    self.tasks.record_error(task["id"], reason)
                self.tasks.pause(task["id"], reason)
                print(style(f"  Task paused: {safe_display(task['goal'])}. Say \"continue\" to pick it up again.",
                            "dim"))
                emit("notice", level="info", text=f"Task paused: {task['goal']}")
                return task["id"]
        except TaskStoreError as exc:
            log.warning("Could not pause the task: %s", exc)
        return None

    def _run_message(self, line: str, topic: str | None = None, said: str | None = None) -> None:
        """handle_message, marked busy so the wake word can interrupt it."""
        extra = {key: value for key, value in (("topic", topic), ("said", said)) if value is not None}
        with self._busy_scope():
            self.handle_message(line, **extra)

    def _browser_may_run(self, line: str) -> bool:
        """The bridge already refused what the browser may not do; checked again here, on the line the
        loop is about to act on, so nothing that reaches the inbox some other way can slip through."""
        from mission_control.bridge import browser_command_policy  # only reached with Mission Control on

        allowed, reason = browser_command_policy(line)
        if allowed:
            return True
        print(style("  Not run: that can only be typed in the terminal." if reason == "terminal_only"
                    else "  Not run: that is not a command Mission Control can send.", "yellow"))
        emit("command.rejected", command_id=self._command_id or "", reason=reason)
        self._request_source, self._command_id = "terminal", None
        return False

    @contextlib.contextmanager
    def _browser_output(self, command_id: str | None, command: str):
        """Run a slash command sent from Mission Control: the terminal shows its output as usual, and the
        browser gets a copy as command.output (escape codes stripped, scrubbed, at most 8000 characters)."""
        tee = _OutputTee(sys.stdout)
        try:
            with contextlib.redirect_stdout(tee):
                yield
        finally:
            if BUS.enabled:  # scrubbed before the cut, like every other text that leaves for the browser
                emit("command.output", command_id=command_id or "", command=scrub(command, max_str=500),
                     text=scrub(_ANSI_ESCAPE.sub("", tee.captured()), max_str=8000))

    def loop(self) -> int:
        try:
            self.restore_conversation()
            self.pause_stale_tasks()
            self.banner()
            self.say(greeting())
            while True:
                try:
                    if self._step():
                        return 0
                except KeyboardInterrupt:
                    # A wake-word interruption arrives as SIGINT; it may land anywhere in the step.
                    if not self._consume_interrupt():
                        print(style("\n  Cancelled.", "yellow"))
        finally:
            self.stop_wake()
            self.pause_stale_tasks("the session ended before it finished", keep_time=False)

    def _step(self) -> bool:
        """Read and handle one input. Returns True when the user wants to leave."""
        self._request_source = "terminal"  # the voice and browser paths below say otherwise
        self._command_id = None
        try:
            line = self._read_line()
        except KeyboardInterrupt:
            if self._consume_interrupt():
                return False
            print()
            line = "exit"
        except EOFError:
            print()
            line = "exit"
        except UnicodeDecodeError:
            print("  That input could not be decoded as UTF-8; please try again.")
            return False
        if line is None:  # a hands-free turn with nothing to send; never starts push-to-talk
            return False
        from_browser = self._request_source == "browser"
        if from_browser and not self._browser_may_run(line):
            return False
        if (not line and self.listen_mode) or line.lower() == "/talk":
            line = self.voice_turn()
            if line:
                self._request_source = "voice"
        elif not line and not self.hands_free and not self._mic_hint_shown:
            # An empty Enter with no voice mode on does nothing; say how to talk, once per session.
            self._mic_hint_shown = True
            print(style('  The microphone is off. Type a message, or turn on voice: /wake on (say "Hey JARVIS"), '
                        "/listen on (press Enter to talk), or /talk for one spoken message.", "dim"))
        if not line:
            return False
        if line.lower().rstrip(".!") in EXIT_WORDS:
            self.say("Until next time, Sir.")
            self.voice.wait(timeout=8)
            return True
        if line.lower() == "help":
            line = "/help"
        try:
            if line.startswith("/") and from_browser:
                with self._browser_output(self._command_id, line):
                    self.handle_command(line)
            elif line.startswith("/"):
                self.handle_command(line)
            else:
                self._run_message(line)
        except KeyboardInterrupt:
            if not self._consume_interrupt():
                print(style("\n  Cancelled.", "yellow"))
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the session
            log.exception("Unhandled error in the chat loop")
            print(style(f"  Something went wrong ({type(exc).__name__}); details are in the log.", "red"))
        return False
