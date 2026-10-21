"""The terminal interface: chat loop, commands, confirmations and voice turns."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import queue
import select
import signal
import sys
import threading
from typing import Callable
from typing import TYPE_CHECKING

from config.settings import VERSION
from core.audit import clean_text, log, safe_display
from core.orchestrator import run_agent
from core.policy import ConfirmationRequest, Permission
from core.session import Conversation
from intelligence.groq_client import JarvisAPIError
from memory.json_store import MemoryStoreError
from voice.input import SpeechInputError
from voice.wake import is_stop_phrase

if TYPE_CHECKING:
    from core.orchestrator import AgentContext
    from memory.json_store import MemoryStore
    from voice.input import SpeechInput
    from voice.output import VoiceEngine
    from voice.wake import WakeWordListener


def terminal_confirm(request: ConfirmationRequest) -> bool:
    """Ask the person at the keyboard to approve one specific operation."""
    if not sys.stdin.isatty():
        return False
    label = "SENSITIVE ACTION" if request.permission is Permission.SENSITIVE_ACTION else "ACTION"
    print(f"\n{style('!! Confirmation required', 'yellow')} [{label}: {request.tool_name}]")
    print(f"   Operation: {safe_display(request.operation)}")
    for path in request.affected_paths:
        print(f"   Affects:   {safe_display(path)}")
    if request.reason:
        print(f"   Why asked: {request.reason}")
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


EXIT_WORDS = {"exit", "quit", "/exit", "/quit", "goodbye", "goodbye jarvis", "bye jarvis"}


HELP_TEXT = """Talk to JARVIS in plain English, for example:
  search for the latest Python release      what time is it?
  remember that I prefer concise answers    what do you remember about me?
  open youtube and search for lo-fi music   how is the system doing?
  open vscode                               show git status for my-project

Commands:
  /help              this help             /tools          list tools and permissions
  /memory [query]    show stored memories  /forget <id>    delete a memory (asks first)
  /memory reset      back up a corrupted memory file and start fresh
  /voice on|off      toggle spoken replies /stop           stop speaking
  /clear             forget this session's conversation context
  /status            show configuration    exit            leave JARVIS
  /talk              speak one message     /listen on|off  push-to-talk: Enter on an empty line = speak
  /wake on|off       hands-free: say "Hey JARVIS", then your request
Saying "Hey JARVIS" while JARVIS is talking or working interrupts it; "Hey JARVIS, stop" just stops it.
Press Ctrl+C while JARVIS is working to cancel the current request.
Confirmation prompts must always be typed; a spoken "yes" cannot approve an action."""


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
        self.conversation = Conversation(max_turns=ctx.config.history_turns)
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

    def banner(self) -> None:
        print(style("\nJARVIS MARK 50", "bold") + style(f"  v{VERSION}", "dim"))
        print("Personal AI Assistant")
        voice = f"voice on ({self.voice.voice})" if self.voice.enabled else "text only"
        print(style(f"Model: {self.ctx.config.model} | {voice} | permissions: {self.ctx.policy.mode} | "
                    "/help for commands", "dim"))
        if self.voice.unavailable_reason:
            print(style(f"Voice unavailable: {self.voice.unavailable_reason}", "yellow"))
        if self.listen_mode:
            print(style("Push-to-talk on: press Enter on an empty line to speak, Enter again to stop.", "dim"))
        if self._want_wake:
            self._want_wake = False
            self.start_wake()
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
        elif command == "/voice":
            self._toggle_voice(rest)
        elif command == "/stop":
            self.voice.stop()
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
                                 ("Workspace", cfg.workspace), ("Notes", cfg.notes_dir), ("Logs", cfg.log_dir),
                                 ("Max tool rounds", cfg.max_tool_rounds),
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

    def _toggle_voice(self, arg: str) -> None:
        if arg not in ("on", "off"):
            print(f"  Voice is {'on' if self.voice.enabled else 'off'}. Use /voice on or /voice off.")
        elif self.voice.set_enabled(arg == "on"):
            print(f"  Voice on ({self.voice.voice}).")
        elif arg == "on":
            print(f"  Voice unavailable: {self.voice.unavailable_reason}")
        else:
            print("  Voice off; text only.")

    def handle_message(self, text: str) -> None:
        self.voice.stop()
        print(style("  ...", "dim"))
        try:
            result = run_agent(self.ctx, text, self.conversation.messages(), self.conversation.summary,
                               on_tool=lambda name: print(style(f"  > {name}", "dim")),
                               on_retry=lambda reason, delay: print(style(f"  {reason} Retrying in {delay:.0f}s...", "dim")))
        except KeyboardInterrupt:
            self._consume_interrupt()  # a wake-word interruption is fully handled here
            print(style("\n  Request cancelled. Any step that had already finished is not undone.", "yellow"))
            return
        except JarvisAPIError as exc:
            log.warning("API error (%s): %s", exc.kind, exc)
            self.say(f"I'm afraid I couldn't complete that. {exc}", speak=False)
            return
        reply = result.text
        if result.status == "incomplete":
            reply = f"{reply}\n\n(Stopped after {result.rounds} tool rounds; the task is incomplete.)"
        # Record the turn before speaking, so an interruption during say() cannot lose it.
        self.conversation.add(text, result.text)
        if self.ctx.config.save_conversations:
            self.memory.add_conversation(text, result.text)
        self.say(reply)

    def _run_message(self, line: str) -> None:
        """handle_message, marked busy so the wake word can interrupt it."""
        with self._busy_scope():
            self.handle_message(line)

    def loop(self) -> int:
        try:
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
