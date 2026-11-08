#!/usr/bin/env python3
"""JARVIS MARK 50 - a personal, tool-using AI assistant for Zorin OS / Linux.

Run it with `python jarvis.py` (see --help). This file is the entry point: startup checks,
command-line options and main(). The application lives in packages, one per layer:

    config/        settings from the environment and .env
    core/          request lifecycle (orchestrator), persona and context, session, policy, audit
    intelligence/  the Groq provider
    tools/         tool registry and the tools the model may call
    memory/        persistent JSON memory
    voice/         speech output, push-to-talk input, hands-free wake word
    ui/            the terminal interface

`import jarvis` also re-exports the main public names for scripts and tests. When patching in
tests, patch a function in the module that defines it (e.g. "ui.terminal.run_agent"), not here.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import os
import platform
import queue
import shlex
import shutil
import sys

from config.settings import Config, ConfigError, SCRIPT_DIR, VERSION, load_env_file, validate_api_key
from core.audit import audit, setup_logging
from core.events import BUS, emit, emit_throttled
from core.orchestrator import AgentContext
from core.policy import PermissionPolicy
from intelligence.groq_client import JarvisAPIError, groq, initialize_client, list_model_ids, verify_model
from memory.json_store import MemoryStore, MemoryStoreError, load_memory
from memory.task_store import TaskStore
from mission_control import BUILD_COMMAND as MISSION_BUILD_COMMAND, DIST_DIR as MISSION_DIST_DIR
from mission_control.launch import open_browser_securely
from tools.catalog import JarvisTools, build_registry
from ui.terminal import JarvisCLI, style, terminal_confirm
from voice.input import GroqTranscriber, RECORDER_INSTALL_HINT, SpeechInput, detect_recorder
from voice.output import AUDIO_INSTALL_HINT, VoiceEngine, detect_audio_player
from voice.wake import CHIME_IGNORE_SECONDS, OpenWakeWordDetector, WAKE_MAX_SECONDS, WakeWordListener, wake_engine_available
import asyncio  # noqa: F401 - re-exported for tests that patch it
import select  # noqa: F401 - re-exported for tests that patch it
import signal  # noqa: F401 - re-exported for tests that patch it
import subprocess  # noqa: F401 - re-exported for tests that patch it
import time  # noqa: F401 - re-exported for tests that patch it

# Public API re-exports.
from config.settings import (  # noqa: F401 - re-exported
    DEFAULT_MODEL,
    DEFAULT_STT_MODEL,
    Permissions,
    ProjectCommand,
    ProjectRule,
    _env_float,
    load_permissions,
    parse_app_overrides,
)
from core.audit import (  # noqa: F401 - re-exported
    audit_log,
    log,
    redact,
    safe_display,
)
from core.orchestrator import (  # noqa: F401 - re-exported
    AgentResult,
    format_final_response,
    run_agent,
)
from core.policy import (  # noqa: F401 - re-exported
    ConfirmationRequest,
    Permission,
)
from core.session import (  # noqa: F401 - re-exported
    Conversation,
)
from intelligence.groq_client import (  # noqa: F401 - re-exported
    call_model,
    classify_api_error,
)
from memory.json_store import (  # noqa: F401 - re-exported
    empty_memory,
)
from tools.catalog import (  # noqa: F401 - re-exported
    _object,
)
from tools.filesystem import (  # noqa: F401 - re-exported
    resolve_safe_path,
)
from tools.developer import (  # noqa: F401 - re-exported
    command_fits,
    resolve_argv,
)
from tools.linux_system import (  # noqa: F401 - re-exported
    BoundedResult,
    battery_status,
    cancel_running_commands,
    launch_detached,
    project_env,
    run_bounded,
    run_command,
)
from tools.registry import (  # noqa: F401 - re-exported
    STATUS_ERROR,
    Tool,
    ToolError,
    ToolRegistry,
    ToolValidationError,
    UNTRUSTED_NOTICE,
    execute_tool,
    parse_arguments,
    serialize_tool_result,
)
from ui.terminal import (  # noqa: F401 - re-exported
    HELP_TEXT,
)
from voice.input import (  # noqa: F401 - re-exported
    MicrophoneRecorder,
    PHANTOM_PEAK,
    SILENCE_PEAK,
    SpeechInputError,
    strip_wake_phrase,
    wav_stats,
)
from voice.output import (  # noqa: F401 - re-exported
    clean_text_for_speech,
    split_into_chunks,
    write_chime,
)
from voice.wake import (  # noqa: F401 - re-exported
    FRAME_BYTES,
    FRAME_SAMPLES,
    FRAME_SECONDS,
    MAX_NOISE_RMS,
    MIN_SPEECH_RMS,
    STOP_PHRASES,
    UtteranceCollector,
    WAKE_INSTALL_HINT,
    frame_rms,
    is_stop_phrase,
)

__all__ = [
    "AUDIO_INSTALL_HINT",
    "AgentContext",
    "AgentResult",
    "BoundedResult",
    "CHIME_IGNORE_SECONDS",
    "Config",
    "ConfigError",
    "ConfirmationRequest",
    "Conversation",
    "DEFAULT_MODEL",
    "DEFAULT_STT_MODEL",
    "FRAME_BYTES",
    "FRAME_SAMPLES",
    "FRAME_SECONDS",
    "GroqTranscriber",
    "HELP_TEXT",
    "JarvisAPIError",
    "JarvisCLI",
    "JarvisTools",
    "MAX_NOISE_RMS",
    "MIN_SPEECH_RMS",
    "MemoryStore",
    "MemoryStoreError",
    "MicrophoneRecorder",
    "OpenWakeWordDetector",
    "PHANTOM_PEAK",
    "Permission",
    "PermissionPolicy",
    "Permissions",
    "ProjectCommand",
    "ProjectRule",
    "RECORDER_INSTALL_HINT",
    "SCRIPT_DIR",
    "SILENCE_PEAK",
    "STATUS_ERROR",
    "STOP_PHRASES",
    "SpeechInput",
    "SpeechInputError",
    "TaskStore",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolValidationError",
    "UNTRUSTED_NOTICE",
    "UtteranceCollector",
    "VERSION",
    "VoiceEngine",
    "WAKE_INSTALL_HINT",
    "WAKE_MAX_SECONDS",
    "WakeWordListener",
    "_env_float",
    "_object",
    "annotations",
    "asyncio",
    "audit",
    "audit_log",
    "battery_status",
    "build_registry",
    "call_model",
    "cancel_running_commands",
    "classify_api_error",
    "clean_text_for_speech",
    "command_fits",
    "detect_audio_player",
    "detect_recorder",
    "empty_memory",
    "execute_tool",
    "format_final_response",
    "frame_rms",
    "groq",
    "initialize_client",
    "is_stop_phrase",
    "launch_detached",
    "list_model_ids",
    "load_env_file",
    "load_memory",
    "load_permissions",
    "log",
    "main",
    "os",
    "parse_app_overrides",
    "parse_args",
    "parse_arguments",
    "project_env",
    "redact",
    "resolve_argv",
    "resolve_safe_path",
    "run_agent",
    "run_bounded",
    "run_checks",
    "run_command",
    "safe_display",
    "select",
    "serialize_tool_result",
    "session_start_payload",
    "setup_logging",
    "shutil",
    "signal",
    "split_into_chunks",
    "start_mission_control",
    "strip_wake_phrase",
    "style",
    "subprocess",
    "sys",
    "terminal_confirm",
    "time",
    "validate_api_key",
    "verify_model",
    "wake_engine_available",
    "wav_stats",
    "write_chime",
]


def run_checks(config: Config) -> int:
    """Print a diagnostic report for the local setup. Never prints the API key."""
    problems = 0

    def report(ok: bool | None, label: str, detail: str = "") -> None:
        nonlocal problems
        mark = style("ok  ", "green") if ok else style("warn", "yellow") if ok is None else style("FAIL", "red")
        problems += ok is False
        print(f"  [{mark}] {label}{': ' + detail if detail else ''}")

    print("JARVIS MARK 50 environment check")
    report(sys.version_info >= (3, 10), "Python", platform.python_version())
    for modules, required in ((("groq",), True), (("ddgs", "duckduckgo_search"), True),
                              (("edge_tts",), False), (("psutil",), False)):
        for module in modules:
            try:
                __import__(module)
                report(True, f"module {module}")
                break
            except ImportError:
                continue
        else:
            report(False if required else None, f"module {modules[0]}", "not installed (pip install -r requirements.txt)")
    player = detect_audio_player()
    report(True if player else None, "audio player", player[0] if player else AUDIO_INSTALL_HINT)
    try:
        validate_api_key(config.api_key)
        report(True, "GROQ_API_KEY", "set (value hidden)")
        key_ok = True
    except ConfigError as exc:
        report(False, "GROQ_API_KEY", str(exc).splitlines()[0])
        key_ok = False
    if key_ok and groq is not None:
        try:
            client = initialize_client(config)
            verify_model(client, config.model)
            report(True, "Groq model", f"{config.model} is available")
        except JarvisAPIError as exc:
            report(False, "Groq model", str(exc))
        else:
            try:
                stt_ok = config.stt_model in list_model_ids(client)
                report(True if stt_ok else None, "speech model",
                       config.stt_model + ("" if stt_ok else " is not available (set JARVIS_STT_MODEL)"))
            except JarvisAPIError as exc:
                report(None, "speech model", f"could not be checked: {exc}")
    recorder = detect_recorder()
    report(True if recorder else None, "microphone recorder", recorder[0] if recorder else RECORDER_INSTALL_HINT)
    wake_ok, wake_detail = wake_engine_available()
    report(True if wake_ok else None, "wake-word engine", wake_detail + (f" ({config.wake_model})" if wake_ok else ""))
    # Found without importing it: aiohttp is only ever imported by mission_control/server.py.
    if importlib.util.find_spec("aiohttp") is not None:
        try:
            aiohttp_version = importlib.metadata.version("aiohttp")
        except importlib.metadata.PackageNotFoundError:
            aiohttp_version = "version unknown"
        report(True, "Mission Control server", f"aiohttp {aiohttp_version}")
    else:
        report(None, "Mission Control server", "aiohttp is not installed (pip install -r requirements.txt)")
    ui_built = (MISSION_DIST_DIR / "index.html").is_file()
    report(True if ui_built else None, "Mission Control UI", "built" if ui_built else f"not built: {MISSION_BUILD_COMMAND}")
    if config.permissions_file.exists():
        kinds = [spec.kind for spec in config.commands.values()]
        report(True, "permissions file", f"{config.permissions_file} ({kinds.count('tests')} test and "
                                         f"{kinds.count('lint')} lint commands, {len(config.projects)} project rules)")
    else:
        report(None, "permissions file", f"{config.permissions_file} not found; no project commands are allowed")
    try:
        load_memory(config.memory_path)
        report(True, "memory file", str(config.memory_path))
    except MemoryStoreError as exc:
        report(False, "memory file", str(exc))
    report(True if config.workspace.is_dir() else None, "workspace", str(config.workspace)
           + ("" if config.workspace.is_dir() else " (does not exist; set JARVIS_WORKSPACE)"))
    editor = shlex.split(config.editor)[0] if config.editor else "code"
    report(True if shutil.which(editor) else None, "editor", editor)
    print("All required checks passed." if not problems else f"{problems} problem(s) found.")
    return 1 if problems else 0


def run_wake_test(config: Config, seconds: float = 20, detector=None, stream_factory=None,
                  tick: float = 0.25) -> int:
    """Listen locally for the wake word and show the live microphone level and wake score, then
    say what to change. Nothing is recorded to disk or sent anywhere."""
    if detector is None:
        ok, detail = wake_engine_available()
        if not ok:
            print(detail)
            return 1
        try:
            detector = OpenWakeWordDetector(config.wake_model)
        except SpeechInputError as exc:
            print(exc)
            return 1
    stats = {"frames": 0, "score": 0.0, "best": 0.0, "level": 0.0, "peak": 0.0}
    heard: list[float] = []

    class Probe:  # watches every frame the listener scores
        name = getattr(detector, "name", "custom")

        def score(self, frame: bytes) -> float:
            value = detector.score(frame)
            level = frame_rms(frame)
            stats.update(frames=stats["frames"] + 1, score=value, level=level,
                         best=max(stats["best"], value), peak=max(stats["peak"], level))
            return value

        def reset(self) -> None:
            detector.reset()

    listener = WakeWordListener(Probe(), on_wake=lambda: heard.append(stats["score"]), on_command=lambda pcm: None,
                                on_status=lambda message: None, threshold=config.wake_threshold,
                                speaking_threshold=config.wake_speaking_threshold, end_silence=0.5,
                                start_timeout=1.0, max_seconds=2, stream_factory=stream_factory)
    print(f'Say "Hey JARVIS" a few times in the next {seconds:.0f} seconds (Ctrl+C stops early).')
    print("This runs only on this computer; nothing is recorded or sent anywhere.\n")
    try:
        listener.start()
    except SpeechInputError as exc:
        print(f"The microphone could not be used: {exc}")
        return 1
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline and listener.running:
            time.sleep(tick)
            bar = "#" * min(20, int(stats["level"] / 0.01)) if stats["level"] else ""
            print(f"\r  level [{bar:<20}] {stats['level']:.3f}   wake score {stats['score']:.2f}   "
                  f"best {stats['best']:.2f}   heard: {len(heard)}  ", end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
    print("\n")
    threshold = config.wake_threshold
    if stats["frames"] == 0:
        print("No sound reached JARVIS from the microphone. Check that it is connected and selected in "
              "Settings > Sound > Input, then try again.")
        return 1
    if heard:
        print(f'Heard "Hey JARVIS" {len(heard)} time(s); best score {stats["best"]:.2f} (needed {threshold}). '
              "Hands-free works: start JARVIS with  python jarvis.py --wake")
        return 0
    if stats["peak"] < 0.05:
        print(f"Your microphone is very quiet (loudest sound {stats['peak']:.3f}). Raise its input volume in "
              "Settings > Sound > Input, or run:  wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 80%")
    elif stats["peak"] >= 0.9:
        # Distorted (clipped) speech no longer sounds like the wake word; seen on 2026-10-23 with mic boost on.
        print(f"Your microphone is too loud: sound reached {stats['peak']:.2f} of the maximum, so speech is "
              f"distorted (best score {stats['best']:.2f}, needed {threshold}). Turn the gain down, e.g.\n"
              "  amixer -c 0 sset 'Mic Boost' 0\n  amixer -c 0 sset 'Capture' 60%\n"
              "  wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 70%\n"
              "then run this test again; speech should peak around 0.2-0.6.")
    elif stats["best"] >= 0.3:
        lower = round(max(0.3, threshold - 0.1), 2)
        tip = (f", or set JARVIS_WAKE_THRESHOLD={lower} in .env" if lower < threshold else "")
        print(f"It nearly heard you (best score {stats['best']:.2f}, needed {threshold}). Speak a little closer "
              f"and say it as two clear words{tip}.")
    else:
        print(f'It did not recognise "Hey JARVIS" (best score {stats["best"]:.2f}). Say "Hey" and "JARVIS" '
              "clearly, about arm's length from the laptop, and try again.")
    return 1


def session_start_payload(config: Config, cli: JarvisCLI, voice: VoiceEngine) -> dict:
    """The facts Mission Control shows about this session (no secrets: the key is never included)."""
    def store_status(store) -> str:
        if store is None:
            return "disabled"
        return "corrupted" if getattr(store, "corruption_error", None) else "ok"

    def available(component) -> bool:
        try:
            return component is not None and bool(component.availability()[0])
        except Exception:  # noqa: BLE001 - a probe failure means "not available", never a crash
            return False

    return {
        "version": VERSION,
        "provider": "groq",
        "model": config.model,
        "permission_mode": cli.ctx.policy.mode,
        "voice_available": available(voice),
        "voice_enabled": bool(voice.enabled),
        "mic_available": available(cli.speech),
        "wake_available": cli.wake_factory is not None and bool(wake_engine_available()[0]),
        "memory_status": store_status(cli.memory),
        "tasks_status": store_status(cli.tasks),
        "developer_mode": bool(cli.developer_mode),
    }


# Seconds between voice.level events per phase: 5 Hz while armed, 10 Hz while collecting a command.
VOICE_LEVEL_INTERVAL = {"armed": 0.2, "collecting": 0.1}


def publish_voice_level(phase: str, rms: float) -> None:
    """Wake-listener level callback: a throttled voice.level for the live meter (a no-op when Mission Control is off)."""
    emit_throttled(f"voice.level.{phase}", VOICE_LEVEL_INTERVAL.get(phase, 0.2), "voice.level",
                   rms=min(1.0, max(0.0, float(rms))), phase=phase)


def start_mission_control(config: Config, cli: JarvisCLI, voice: VoiceEngine):
    """Start the Mission Control server and turn the event bus on. Returns the server, or None.

    While it runs, confirmations go through a ConfirmationBroker (typed in the terminal, or answered in
    Mission Control) and the browser can send lines to the loop through a MissionBridge. Any failure
    (aiohttp missing, port in use) is one yellow line, and the terminal works exactly as before: the
    previous confirm callable (terminal_confirm) is put back and the bridge detached. The tokenised
    link goes to the terminal only, never to the log or the audit trail, and the browser is opened
    through a private redirect file so the token is not on any command line."""
    try:
        from mission_control.bridge import MissionBridge
        from mission_control.confirm import ConfirmationBroker
        from mission_control.server import MissionControlError, MissionControlServer
    except ImportError as exc:
        print(style(f"Mission Control is unavailable ({exc}); pip install -r requirements.txt. "
                    "The terminal works as usual.", "yellow"))
        return None
    policy = cli.ctx.policy
    previous_confirm = policy.confirm
    broker = ConfirmationBroker(timeout=config.confirm_timeout)
    # One slot: a browser command waits for the loop, never a queue of them.
    bridge = MissionBridge(queue.Queue(maxsize=1), cli.request_cancel, broker)
    server = MissionControlServer(BUS, port=config.ui_port, dist_dir=MISSION_DIST_DIR,
                                  dev_origin=config.ui_dev_origin, bridge=bridge, broker=broker)
    # In place before the server listens, so no browser can ever meet the terminal-only prompt.
    policy.confirm = broker.confirm
    cli.attach_mission(bridge)
    try:
        url = server.start()
    except BaseException as exc:
        policy.confirm = previous_confirm
        cli.attach_mission(None)
        if not isinstance(exc, Exception):
            raise  # Ctrl+C during startup still leaves; the terminal state is already restored
        if isinstance(exc, MissionControlError):
            print(style(f"Mission Control could not start: {exc} The terminal works as usual.", "yellow"))
        else:
            log.warning("Mission Control failed to start", exc_info=True)
            server.stop()
            print(style(f"Mission Control could not start ({type(exc).__name__}). The terminal works as usual.",
                        "yellow"))
        return None
    BUS.enabled = True
    emit("session.start", **session_start_payload(config, cli, voice))
    if cli.tasks is not None:  # a task left unfinished from last time is shown from the first frame
        cli.tasks.publish_current()
    link = f"{url}/#token={server.token}"
    print(f"Mission Control: {link}")
    audit({"event": "mission_control_start", "port": server.port})
    if config.ui_open_browser and not open_browser_securely(link):
        print(style("  Could not open a browser; open the link above yourself.", "dim"))
    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="JARVIS MARK 50 - personal AI assistant for Linux.")
    parser.add_argument("--text", action="store_true", help="text-only mode (no speech)")
    parser.add_argument("--model", help="override GROQ_MODEL for this run")
    parser.add_argument("--read-only", action="store_true", help="allow only read-only tools")
    parser.add_argument("--listen", action="store_true",
                        help="push-to-talk: press Enter on an empty line to speak (audio is sent to Groq)")
    parser.add_argument("--wake", action="store_true",
                        help='hands-free: say "Hey JARVIS" (detected locally), then your request')
    parser.add_argument("--mission-control", action="store_true",
                        help="open Mission Control, the live 3D view of JARVIS in your browser (local only)")
    parser.add_argument("--no-browser", action="store_true",
                        help="with Mission Control: print its link instead of opening a browser")
    parser.add_argument("--check", action="store_true", help="check the environment and exit")
    parser.add_argument("--wake-test", action="store_true",
                        help='test "Hey JARVIS" detection with your microphone (local only) and exit')
    parser.add_argument("--version", action="version", version=f"JARVIS MARK 50 {VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(SCRIPT_DIR / ".env")
    if args.model:
        os.environ["GROQ_MODEL"] = args.model
    try:
        config = Config.from_env(require_api_key=not (args.check or args.wake_test))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.read_only:
        config.permission_mode = "read_only"
    if args.mission_control:
        config.ui_enabled = True
    if args.no_browser:
        config.ui_open_browser = False
    setup_logging(config)
    if args.check:
        return run_checks(config)
    if args.wake_test:
        return run_wake_test(config)

    memory = MemoryStore(config.memory_path)
    memory.load()
    tasks = TaskStore(config.tasks_path)
    tasks.load()  # a corrupted task file is reported by the banner, like the memory file
    try:
        client = initialize_client(config)
        verify_model(client, config.model)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except JarvisAPIError as exc:
        if exc.kind in ("auth", "permission", "model"):
            print(f"Startup error: {exc}", file=sys.stderr)
            return 2
        print(style(f"Warning: could not verify the Groq model ({exc}). Continuing anyway.", "yellow"))

    voice = VoiceEngine(config.voice, enabled=config.tts_enabled and not args.text)
    policy = PermissionPolicy(config.permission_mode, confirm=terminal_confirm)
    registry = build_registry(JarvisTools(config, memory, tasks=tasks))
    speech = SpeechInput(GroqTranscriber(client, config.stt_model, config.stt_language),
                         max_seconds=config.stt_max_seconds)

    def make_wake_listener(owner: JarvisCLI) -> WakeWordListener:
        return WakeWordListener(
            OpenWakeWordDetector(config.wake_model), on_wake=owner._on_wake, on_command=owner._on_command,
            on_status=owner._on_status, is_speaking=lambda: voice.is_speaking,
            threshold=config.wake_threshold, speaking_threshold=config.wake_speaking_threshold,
            end_silence=config.wake_end_silence, max_seconds=min(config.stt_max_seconds, WAKE_MAX_SECONDS),
            ignore_after_wake=CHIME_IGNORE_SECONDS if config.wake_chime and voice.player else 0.0,
            on_level=publish_voice_level,
        )

    cli = JarvisCLI(AgentContext(client, config, registry, policy, memory, tasks), voice, memory,
                    speech=speech, listen=config.stt_enabled or args.listen,
                    wake_factory=make_wake_listener, wake=config.wake_enabled or args.wake)
    audit({"event": "session_start", "model": config.model, "permission_mode": config.permission_mode})
    mission = None
    try:
        if config.ui_enabled:
            mission = start_mission_control(config, cli, voice)
        return cli.loop()
    finally:
        if mission is not None:  # before voice.shutdown, so the browser sees the session end
            emit("session.end")
            BUS.enabled = False
            mission.stop()
        voice.shutdown()
        speech.shutdown()
        audit({"event": "session_end"})


if __name__ == "__main__":
    sys.exit(main())
