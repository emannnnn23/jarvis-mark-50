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
import os
import platform
import shlex
import shutil
import sys

from config.settings import Config, ConfigError, SCRIPT_DIR, VERSION, load_env_file, validate_api_key
from core.audit import audit, setup_logging
from core.orchestrator import AgentContext
from core.policy import PermissionPolicy
from intelligence.groq_client import JarvisAPIError, groq, initialize_client, list_model_ids, verify_model
from memory.json_store import MemoryStore, MemoryStoreError, load_memory
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
    "setup_logging",
    "shutil",
    "signal",
    "split_into_chunks",
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="JARVIS MARK 50 - personal AI assistant for Linux.")
    parser.add_argument("--text", action="store_true", help="text-only mode (no speech)")
    parser.add_argument("--model", help="override GROQ_MODEL for this run")
    parser.add_argument("--read-only", action="store_true", help="allow only read-only tools")
    parser.add_argument("--listen", action="store_true",
                        help="push-to-talk: press Enter on an empty line to speak (audio is sent to Groq)")
    parser.add_argument("--wake", action="store_true",
                        help='hands-free: say "Hey JARVIS" (detected locally), then your request')
    parser.add_argument("--check", action="store_true", help="check the environment and exit")
    parser.add_argument("--version", action="version", version=f"JARVIS MARK 50 {VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(SCRIPT_DIR / ".env")
    if args.model:
        os.environ["GROQ_MODEL"] = args.model
    try:
        config = Config.from_env(require_api_key=not args.check)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.read_only:
        config.permission_mode = "read_only"
    setup_logging(config)
    if args.check:
        return run_checks(config)

    memory = MemoryStore(config.memory_path)
    memory.load()
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
    registry = build_registry(JarvisTools(config, memory))
    speech = SpeechInput(GroqTranscriber(client, config.stt_model, config.stt_language),
                         max_seconds=config.stt_max_seconds)

    def make_wake_listener(owner: JarvisCLI) -> WakeWordListener:
        return WakeWordListener(
            OpenWakeWordDetector(config.wake_model), on_wake=owner._on_wake, on_command=owner._on_command,
            on_status=owner._on_status, is_speaking=lambda: voice.is_speaking,
            threshold=config.wake_threshold, speaking_threshold=config.wake_speaking_threshold,
            end_silence=config.wake_end_silence, max_seconds=min(config.stt_max_seconds, WAKE_MAX_SECONDS),
            ignore_after_wake=CHIME_IGNORE_SECONDS if config.wake_chime and voice.player else 0.0,
        )

    cli = JarvisCLI(AgentContext(client, config, registry, policy, memory), voice, memory,
                    speech=speech, listen=config.stt_enabled or args.listen,
                    wake_factory=make_wake_listener, wake=config.wake_enabled or args.wake)
    audit({"event": "session_start", "model": config.model, "permission_mode": config.permission_mode})
    try:
        return cli.loop()
    finally:
        voice.shutdown()
        speech.shutdown()
        audit({"event": "session_end"})


if __name__ == "__main__":
    sys.exit(main())
