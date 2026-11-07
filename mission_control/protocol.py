"""The Mission Control wire protocol: which events exist, which fields each must carry, how to check one.

The browser and the backend are written separately, so the contract lives in one place. `EVENT_TYPES` is
the list of required payload fields per type; `validate_event` checks a full envelope against it and
against the exact enumeration values. `protocol_fixtures.json` holds one valid sample per type and is
validated by both this module's tests and the frontend's guards, so the two sides cannot drift apart
unnoticed. Optional fields may be absent; when present they are checked too."""

from __future__ import annotations

from typing import Any


PROTOCOL_VERSION = 1


# Required payload fields per event type (the envelope fields v, seq, type, timestamp are checked apart).
EVENT_TYPES: dict[str, frozenset[str]] = {
    "session.start": frozenset({
        "version", "provider", "model", "permission_mode", "voice_available", "voice_enabled",
        "mic_available", "wake_available", "memory_status", "tasks_status", "developer_mode",
    }),
    "session.end": frozenset(),
    "jarvis.state": frozenset({"state", "since"}),
    "request.start": frozenset({"source", "text"}),
    "request.end": frozenset({"status", "duration_ms"}),
    "assistant.message": frozenset({"text"}),
    "llm.call": frozenset({"phase", "model", "attempt"}),
    "llm.retry": frozenset({"reason", "delay"}),
    "llm.error": frozenset({"kind", "message", "retryable"}),
    "tool.execution": frozenset({"tool", "status"}),
    "memory.activity": frozenset({"op"}),
    "task.update": frozenset({"task"}),
    "task.progress": frozenset({"message"}),
    "voice.output": frozenset({"phase"}),
    "voice.input": frozenset({"mode", "phase"}),
    "voice.level": frozenset({"rms", "phase"}),
    "confirm.request": frozenset({
        "id", "digest", "tool", "label", "operation", "affected_paths", "reason", "hidden",
        "lines", "omitted", "browser_approvable", "expires_at",
    }),
    "confirm.resolved": frozenset({"id", "approved", "channel"}),
    "command.ack": frozenset({"command_id", "status"}),
    "command.rejected": frozenset({"command_id", "reason"}),
    "command.output": frozenset({"command_id", "command", "text"}),
    "system.metrics": frozenset({
        "cpu_percent", "memory_percent", "memory_total", "memory_available", "memory_used",
        "disk_percent", "disk_total", "disk_used", "uptime_s", "process_uptime_s", "load_avg", "os",
    }),
    "network.status": frozenset({"groq", "checked_at", "source"}),
    "notice": frozenset({"level", "text"}),
}


# Exact enumerations, keyed by (event type, field). Optional fields are listed too.
ENUMS: dict[tuple[str, str], frozenset[str]] = {
    ("session.start", "provider"): frozenset({"groq"}),
    ("session.start", "memory_status"): frozenset({"ok", "corrupted", "disabled"}),
    ("session.start", "tasks_status"): frozenset({"ok", "corrupted", "disabled"}),
    ("jarvis.state", "state"): frozenset({"idle", "listening", "processing", "speaking", "tool", "error"}),
    ("request.start", "source"): frozenset({"terminal", "voice", "browser"}),
    ("request.end", "status"): frozenset({"complete", "incomplete", "cancelled", "error"}),
    ("llm.call", "phase"): frozenset({"start", "end"}),
    ("tool.execution", "status"): frozenset({
        "requested", "awaiting_confirmation", "running", "success", "error", "denied", "partial", "unverified",
    }),
    ("memory.activity", "op"): frozenset({"retrieve", "search", "store"}),
    ("voice.output", "phase"): frozenset({"start", "chunk", "end", "error"}),
    ("voice.output", "timing"): frozenset({"exact", "none"}),
    ("voice.input", "mode"): frozenset({"off", "ptt", "wake"}),
    ("voice.input", "phase"): frozenset({
        "armed", "wake_detected", "collecting", "transcribing", "recording", "idle", "stopped",
    }),
    ("voice.level", "phase"): frozenset({"armed", "collecting"}),
    ("confirm.resolved", "channel"): frozenset({"terminal", "browser", "timeout", "cancelled"}),
    ("command.ack", "status"): frozenset({"accepted", "queued"}),
    ("command.rejected", "reason"): frozenset({"busy", "queue_full", "terminal_only", "invalid", "too_long"}),
    ("network.status", "groq"): frozenset({"up", "down", "unknown"}),
    ("network.status", "source"): frozenset({"probe", "passive"}),
    ("notice", "level"): frozenset({"info", "warn", "error"}),
}


# Longest text the protocol allows in these fields (the backend scrubs and truncates to these).
MAX_TEXT: dict[tuple[str, str], int] = {
    ("request.start", "text"): 500,
    ("assistant.message", "text"): 4000,
    ("command.output", "text"): 8000,
}


# Fields that must be true booleans.
BOOL_FIELDS: dict[str, tuple[str, ...]] = {
    "session.start": ("voice_available", "voice_enabled", "mic_available", "wake_available", "developer_mode"),
    "llm.call": ("ok",),
    "llm.error": ("retryable",),
    "confirm.request": ("browser_approvable",),
    "confirm.resolved": ("approved",),
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_envelope(event: dict[str, Any], problems: list[str]) -> None:
    if event.get("v") != PROTOCOL_VERSION or not _is_int(event.get("v")):
        problems.append(f"v must be {PROTOCOL_VERSION}")
    if not _is_int(event.get("seq")) or event["seq"] < 0:
        problems.append("seq must be a non-negative integer")
    if not isinstance(event.get("type"), str):
        problems.append("type must be a string")
    if not _is_int(event.get("timestamp")) or event["timestamp"] < 0:
        problems.append("timestamp must be an integer (epoch ms)")
    if "request_id" in event and not isinstance(event["request_id"], str):
        problems.append("request_id must be a string")


def _check_shapes(type_: str, event: dict[str, Any], problems: list[str]) -> None:
    """Type-specific shape checks for the fields where a wrong type would break a consumer."""
    for field in BOOL_FIELDS.get(type_, ()):
        if field in event and not isinstance(event[field], bool):
            problems.append(f"{field} must be a boolean")
    if type_ == "voice.level" and "rms" in event:
        rms = event["rms"]
        if not _is_number(rms) or not 0 <= rms <= 1:
            problems.append("rms must be a number from 0 to 1")
    if type_ == "task.update" and "task" in event:
        if event["task"] is not None and not isinstance(event["task"], dict):
            problems.append("task must be an object or null")
    if type_ == "voice.output" and "words" in event:
        words = event["words"]
        ok = isinstance(words, list) and all(
            isinstance(pair, list) and len(pair) == 2 and all(_is_number(n) for n in pair) for pair in words
        )
        if not ok:
            problems.append("words must be a list of [offset_ms, dur_ms] pairs")
    if type_ == "confirm.request":
        if "lines" in event:
            lines = event["lines"]
            ok = isinstance(lines, list) and all(
                isinstance(line, dict) and isinstance(line.get("k"), str) and isinstance(line.get("t"), str)
                for line in lines
            )
            if not ok:
                problems.append("lines must be a list of {k, t} objects")
        if "affected_paths" in event:
            paths = event["affected_paths"]
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                problems.append("affected_paths must be a list of strings")
        for field in ("hidden", "omitted"):
            if field in event and not _is_int(event[field]):
                problems.append(f"{field} must be an integer")


def validate_event(ev: dict[str, Any]) -> list[str]:
    """Return what is wrong with an event envelope (empty list = valid). Never raises, never mutates."""
    if not isinstance(ev, dict):
        return ["event must be an object"]
    problems: list[str] = []
    _check_envelope(ev, problems)
    type_ = ev.get("type")
    if not isinstance(type_, str):
        return problems
    required = EVENT_TYPES.get(type_)
    if required is None:
        problems.append(f"unknown event type: {type_[:60]!r}")
        return problems
    for field in sorted(required):
        if field not in ev:
            problems.append(f"{type_}: missing field {field}")
    for (enum_type, field), allowed in ENUMS.items():
        if enum_type == type_ and field in ev:
            value = ev[field]
            if not isinstance(value, str) or value not in allowed:
                problems.append(f"{type_}: {field} must be one of {', '.join(sorted(allowed))}")
    for (text_type, field), limit in MAX_TEXT.items():
        if text_type == type_ and field in ev:
            value = ev[field]
            if not isinstance(value, str):
                problems.append(f"{type_}: {field} must be a string")
            elif len(value) > limit:
                problems.append(f"{type_}: {field} is longer than {limit} characters")
    _check_shapes(type_, ev, problems)
    return problems
