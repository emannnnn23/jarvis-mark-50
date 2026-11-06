"""The terminal interface: chat loop, commands, confirmations and voice turns."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import queue
import re
import select
import signal
import sys
import threading
from typing import Callable
from typing import TYPE_CHECKING

from config.settings import VERSION
from core.audit import clean_text, log, reveal_invisible, safe_display
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
    from memory.json_store import MemoryStore
    from memory.task_store import TaskStore
    from voice.input import SpeechInput
    from voice.output import VoiceEngine
    from voice.wake import WakeWordListener


# Matches the review limit in tools/developer.py, so an approved change is always shown in full.
MAX_DETAIL_LINES = 400


def print_details(details: str) -> None:
    """Show a confirmation's details (usually a unified diff), coloured and capped in length.
    Invisible and text-direction characters are shown as \\u{...} so nothing is hidden."""
    # Reveal before anything is stripped, so control characters are shown rather than silently removed.
    revealed, hidden = reveal_invisible(clean_text(details))
    if hidden:
        print("   " + style(f"!! {hidden} invisible or text-direction character(s), shown as \\u{{...}}", "yellow"))
    lines = revealed.splitlines()
    for line in lines[:MAX_DETAIL_LINES]:
        if line.startswith("+") and not line.startswith("+++"):
            print("   " + style(line, "green"))
        elif line.startswith("-") and not line.startswith("---"):
            print("   " + style(line, "red"))
        elif line.startswith("@@"):
            print("   " + style(line, "cyan"))
        else:
            print("   " + line)
    if len(lines) > MAX_DETAIL_LINES:
        print("   " + style(f"... {len(lines) - MAX_DETAIL_LINES} more lines not shown", "dim"))


def terminal_confirm(request: ConfirmationRequest) -> bool:
    """Ask the person at the keyboard to approve one specific operation."""
    if not sys.stdin.isatty():
        return False
    label = "SENSITIVE ACTION" if request.permission is Permission.SENSITIVE_ACTION else "ACTION"
    print(f"\n{style('!! Confirmation required', 'yellow')} [{label}: {request.tool_name}]")
    print(f"   Operation: {reveal_invisible(clean_text(request.operation))[0]}")
    for path in request.affected_paths:
        print(f"   Affects:   {reveal_invisible(clean_text(path))[0]}")
    if request.reason:
        print(f"   Why asked: {request.reason}")
    if request.details:
        print_details(request.details)
    try:
        answer = input("   Type 'yes' to proceed, anything else cancels: ")
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
Confirmation prompts must always be typed; a spoken "yes" cannot approve an action."""


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
        return True

    def stop_wake(self) -> None:
        if self.wake is not None:
            self.wake.stop()
            self.wake = None
        # Drop anything the listener captured before it stopped: "the microphone is closed" must
        # also mean nothing recorded earlier is uploaded afterwards.
        while True:
            try:
                self._wake_events.get_nowait()
            except queue.Empty:
                break

    def _on_wake(self) -> None:
        """Runs on the listener thread the moment the wake word is heard."""
        interrupted = self.voice.is_speaking
        self.voice.stop()  # barge-in: stop talking at once
        with self._interrupt_lock:
            if self._busy:  # cancel the request in progress, exactly like Ctrl+C
                self._interrupt_pending = True
                interrupted = True
                os.kill(os.getpid(), signal.SIGINT)
        if self.ctx.config.wake_chime:
            self.voice.chime()
        self._wake_events.put(("status", "[wake] Interrupted. Listening..." if interrupted else "[wake] Listening..."))

    def _on_command(self, pcm: bytes) -> None:
        self._wake_events.put(("command", pcm))

    def _on_status(self, message: str) -> None:
        self._wake_events.put(("status", message))

    def _consume_interrupt(self) -> bool:
        with self._interrupt_lock:
            pending, self._interrupt_pending = self._interrupt_pending, False
            return pending

    def _voice_command(self, pcm: bytes) -> str:
        """Transcribe a hands-free command. Returns "" when there is nothing to send."""
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
        """Typed input, or (in hands-free mode) whichever comes first: a typed line or a spoken command.

        Returns None when a hands-free turn produced nothing to send, which is distinct from the
        empty line a user types to start push-to-talk.
        """
        prompt = style("You: ", "green")
        if not self.hands_free and self._wake_events.empty():
            return clean_text(input(prompt)).strip()
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
                    return self._voice_command(payload) or None
            if not self.hands_free:  # the listener stopped (error or /wake off)
                print()
                return None
            if select.select([sys.stdin], [], [], 0.1)[0]:
                raw = sys.stdin.readline()
                if not raw:
                    raise EOFError
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
        try:
            result = run_agent(self.ctx, text, self.conversation.messages(), self.conversation.summary,
                               on_tool=lambda name: print(style(f"  > {name}", "dim")),
                               on_retry=lambda reason, delay: print(style(f"  {reason} Retrying in {delay:.0f}s...", "dim")),
                               tool_groups={"core", "developer"} if self.developer_mode else {"core"},
                               voice_state=self.voice_state(), last_seen=self.last_seen,
                               on_progress=lambda message: print(style(f"  {safe_display(message)}", "dim")))
        except KeyboardInterrupt:
            self._consume_interrupt()  # a wake-word interruption is fully handled here
            print(style("\n  Request cancelled. Any step that had already finished is not undone.", "yellow"))
            self._paused_by_last_request = self._pause_active_task("interrupted by the user")
            return
        except JarvisAPIError as exc:
            log.warning("API error (%s): %s", exc.kind, exc)
            self.say(f"I'm afraid I couldn't complete that. {exc}", speak=False)
            # No tool ran, but a resumed task would otherwise stay active with no word of why it stopped.
            self._paused_by_last_request = self._pause_active_task(f"the model could not be reached: {exc}",
                                                                   record_error=True)
            return
        self._paused_by_last_request = result.paused_task
        reply = result.text
        if result.status == "incomplete":
            reply = f"{reply}\n\n(The task is incomplete.)"
        # Record the turn before speaking, so an interruption during say() cannot lose it.
        self.conversation.add(text, result.text)
        if self.ctx.config.save_conversations:  # the log keeps the user's own words, not a resume message
            self.memory.add_conversation(said, result.text)
        self.say(reply)

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
                return task["id"]
        except TaskStoreError as exc:
            log.warning("Could not pause the task: %s", exc)
        return None

    def _run_message(self, line: str, topic: str | None = None, said: str | None = None) -> None:
        """handle_message, marked busy so the wake word can interrupt it."""
        extra = {key: value for key, value in (("topic", topic), ("said", said)) if value is not None}
        with self._busy_scope():
            self.handle_message(line, **extra)

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
        if (not line and self.listen_mode) or line.lower() == "/talk":
            line = self.voice_turn()
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
            if line.startswith("/"):
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
